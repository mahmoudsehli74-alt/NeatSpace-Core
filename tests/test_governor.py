"""Governor tests (WP5) — pure logic, no Mongo needed."""

from __future__ import annotations

from datetime import datetime, timedelta

from pinner.governor.quotas import (
    DEFAULT_MIN_INTERVAL_MIN,
    WARMUP_CAP_EARLY,
    WARMUP_CAP_LATE,
    bump_pin_stats,
    daily_cap,
    decide,
    effective_pins_today,
    should_graduate,
    warmup_day,
)

T0 = datetime(2026, 1, 15, 12, 0, 0)  # day 15 of a warmup started 2026-01-01
WARMUP_STARTED = datetime(2026, 1, 1, 9, 0, 0)


def warmup_account(**over):
    doc = {
        "name": "NeatSpace Kitchen",
        "status": "WARMUP",
        "warmup": {"started_at": WARMUP_STARTED},
        "quotas": {"pins_daily_cap": 10, "min_pin_interval_min": 20},
        "stats": {"pins_today": 0, "pins_today_date": "2026-01-15", "last_pin_at": None},
    }
    doc.update(over)
    return doc


def active_account(**over):
    doc = {
        "name": "NeatSpace Kitchen",
        "status": "ACTIVE",
        "quotas": {"pins_daily_cap": 10, "min_pin_interval_min": 20},
        "stats": {"pins_today": 0, "pins_today_date": "2026-01-15", "last_pin_at": None},
    }
    doc.update(over)
    return doc


# --- warm-up curve -------------------------------------------------------------------


def test_warmup_day_is_one_based():
    assert warmup_day(warmup_account(), WARMUP_STARTED) == 1
    assert warmup_day(warmup_account(), WARMUP_STARTED + timedelta(days=13)) == 14


def test_daily_cap_warmup_curve():
    acc = warmup_account()
    assert daily_cap(acc, WARMUP_STARTED + timedelta(days=13)) == WARMUP_CAP_EARLY  # day 14 -> 2
    assert daily_cap(acc, T0) == WARMUP_CAP_LATE  # day 15 -> 5
    assert daily_cap(acc, WARMUP_STARTED + timedelta(days=29)) == WARMUP_CAP_LATE  # day 30 -> 5
    # day 31+: graduated, falls back to the account's own cap
    assert daily_cap(acc, WARMUP_STARTED + timedelta(days=30)) == 10


def test_daily_cap_active_uses_quotas():
    assert daily_cap(active_account(), T0) == 10
    bare = active_account(quotas={})  # tolerant of missing config
    assert daily_cap(bare, T0) == 10


def test_should_graduate_after_30_days():
    acc = warmup_account()
    assert not should_graduate(acc, WARMUP_STARTED + timedelta(days=29))
    assert should_graduate(acc, WARMUP_STARTED + timedelta(days=30))
    assert not should_graduate(active_account(), T0)  # already graduated


# --- decide() ------------------------------------------------------------------------


def test_decide_allows_normal_warmup_pin():
    decision = decide(warmup_account(), now=T0)
    assert decision.allowed and decision.reason == "ok"
    assert decision.detail["cap"] == WARMUP_CAP_LATE


def test_decide_blocks_at_daily_cap():
    acc = warmup_account()
    assert decide(acc, now=T0, pins_today=4).allowed
    assert not decide(acc, now=T0, pins_today=5).allowed
    assert "daily cap reached (5/5)" in decide(acc, now=T0, pins_today=5).reason


def test_decide_rolls_over_utc_day():
    acc = warmup_account(stats={"pins_today": 5, "pins_today_date": "2026-01-14",
                                "last_pin_at": T0 - timedelta(days=1)})
    assert effective_pins_today(acc, T0) == 0
    assert decide(acc, now=T0).allowed


def test_decide_enforces_min_spacing():
    recent = {"pins_today": 1, "pins_today_date": "2026-01-15",
              "last_pin_at": T0 - timedelta(minutes=10)}
    blocked = decide(active_account(stats=recent), now=T0)
    assert not blocked.allowed and "min spacing" in blocked.reason
    ok = decide(active_account(stats=dict(recent, last_pin_at=T0 - timedelta(minutes=25))), now=T0)
    assert ok.allowed


def test_decide_default_interval_when_quotas_missing():
    acc = active_account(quotas={})
    just_under = T0 - timedelta(minutes=DEFAULT_MIN_INTERVAL_MIN - 1)
    just_over = T0 - timedelta(minutes=DEFAULT_MIN_INTERVAL_MIN + 1)
    assert decide(acc, now=T0, last_pin_at=just_under).allowed is False
    assert decide(acc, now=T0, last_pin_at=just_over).allowed


def test_decide_blocks_paused_and_killed():
    for status in ("PAUSED", "KILLED"):
        decision = decide(active_account(status=status), now=T0)
        assert not decision.allowed and status in decision.reason


def test_decide_gemini_rpd_budget_guard():
    acc = active_account()
    assert decide(acc, now=T0, gemini_calls_today=199).allowed
    blocked = decide(acc, now=T0, gemini_calls_today=200)
    assert not blocked.allowed and "rpd budget" in blocked.reason


# --- stats bump ----------------------------------------------------------------------


def test_bump_pin_stats_increments_and_dates():
    stats = {"pins_today": 2, "pins_today_date": "2026-01-15", "last_pin_at": None}
    bumped = bump_pin_stats(stats, T0)
    assert bumped == {"pins_today": 3, "pins_today_date": "2026-01-15", "last_pin_at": T0}


def test_bump_pin_stats_resets_on_rollover():
    stats = {"pins_today": 5, "pins_today_date": "2026-01-14",
             "last_pin_at": T0 - timedelta(days=1)}
    bumped = bump_pin_stats(stats, T0)
    assert bumped["pins_today"] == 1 and bumped["pins_today_date"] == "2026-01-15"
