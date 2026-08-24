"""Quota governor (WP5): the ONLY component allowed to say "not now".

PURE logic — no I/O, no Mongo, no clock side effects. Everything here is
exhaustively unit-testable and injectable (callers pass ``now``).

Rules (from the architecture spec §5):
  * WARMUP accounts ramp gently: day 1-14 -> 2 pins/day, day 15-30 -> 5/day,
    then graduate to the account's own cap (runner flips status to ACTIVE
    via ``should_graduate``; ``decide`` applies the right cap either way).
  * ACTIVE accounts: ``quotas.pins_daily_cap``.
  * Minimum spacing between pins per account (default 20 minutes).
  * Global Gemini free-tier RPD budget guard — deny BEFORE burning the call.
  * PAUSED / KILLED accounts: nothing flows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

WARMUP_CAP_EARLY = 2  # day 1-14
WARMUP_CAP_LATE = 5  # day 15-30
WARMUP_EARLY_DAYS = 14
WARMUP_TOTAL_DAYS = 30
DEFAULT_ACTIVE_CAP = 10
DEFAULT_MIN_INTERVAL_MIN = 20
# Conservative shared budget for the free tier (leave headroom below the
# documented RPD; moderation is global-once-per-product so this covers it).
DEFAULT_GEMINI_RPD_BUDGET = 200


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    detail: dict = field(default_factory=dict)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _today(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def warmup_day(account: dict, now: datetime) -> int:
    """1-based age in days of a WARMUP account (never below 1)."""
    started = (account.get("warmup") or {}).get("started_at")
    if started is None:
        return 1
    if started.tzinfo is not None:
        started = started.replace(tzinfo=None)
    return max(1, (now - started).days + 1)


def daily_cap(account: dict, now: datetime) -> int:
    """Today's pin cap for the account, warm-up curve included."""
    if account.get("status") == "WARMUP":
        day = warmup_day(account, now)
        if day <= WARMUP_EARLY_DAYS:
            return WARMUP_CAP_EARLY
        if day <= WARMUP_TOTAL_DAYS:
            return WARMUP_CAP_LATE
    quotas = account.get("quotas") or {}
    return int(quotas.get("pins_daily_cap", DEFAULT_ACTIVE_CAP))


def should_graduate(account: dict, now: datetime) -> bool:
    """True when a WARMUP account has completed its 30-day ramp."""
    return account.get("status") == "WARMUP" and warmup_day(account, now) > WARMUP_TOTAL_DAYS


def effective_pins_today(account: dict, now: datetime) -> int:
    """pins_today, treated as 0 when the stored counter belongs to a previous
    UTC day (the runner persists the reset via ``bump_pin_stats``)."""
    stats = account.get("stats") or {}
    if stats.get("pins_today_date") != _today(now):
        return 0
    return int(stats.get("pins_today", 0))


def min_interval_minutes(account: dict) -> int:
    return int((account.get("quotas") or {}).get("min_pin_interval_min", DEFAULT_MIN_INTERVAL_MIN))


def decide(
    account: dict,
    *,
    now: datetime | None = None,
    pins_today: int | None = None,
    last_pin_at: datetime | None = None,
    gemini_calls_today: int | None = None,
    gemini_rpd_budget: int = DEFAULT_GEMINI_RPD_BUDGET,
) -> Decision:
    """Allow or deny the next pin for this account, with the reason why."""
    now = now if now is not None else _utcnow()
    status = account.get("status", "ACTIVE")
    if status in ("PAUSED", "KILLED"):
        return Decision(False, f"account {status}", {"status": status})

    stats = account.get("stats") or {}
    if pins_today is None:
        pins_today = effective_pins_today(account, now)
    if last_pin_at is None:
        last_pin_at = stats.get("last_pin_at")

    cap = daily_cap(account, now)
    detail = {
        "status": status,
        "warmup_day": warmup_day(account, now) if status == "WARMUP" else None,
        "cap": cap,
        "pins_today": pins_today,
    }

    if pins_today >= cap:
        return Decision(False, f"daily cap reached ({pins_today}/{cap})", detail)

    interval_min = min_interval_minutes(account)
    if last_pin_at is not None:
        if last_pin_at.tzinfo is not None:
            last_pin_at = last_pin_at.replace(tzinfo=None)
        elapsed_min = (now - last_pin_at).total_seconds() / 60
        detail["minutes_since_last_pin"] = round(elapsed_min, 1)
        if elapsed_min < interval_min:
            return Decision(
                False,
                f"min spacing not elapsed ({elapsed_min:.0f}/{interval_min} min)",
                detail,
            )

    if gemini_calls_today is not None and gemini_calls_today >= gemini_rpd_budget:
        return Decision(
            False, f"gemini rpd budget reached ({gemini_calls_today}/{gemini_rpd_budget})", detail
        )

    return Decision(True, "ok", detail)


def bump_pin_stats(stats: dict, now: datetime) -> dict:
    """Pure stats update after a successful pin: increments the daily counter
    (resetting it on UTC-day rollover) and records last_pin_at."""
    today = _today(now)
    count = int(stats.get("pins_today", 0)) if stats.get("pins_today_date") == today else 0
    return {
        "pins_today": count + 1,
        "pins_today_date": today,
        "last_pin_at": now,
    }
