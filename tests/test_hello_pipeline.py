"""Hello pipeline — the Phase 1 exit gate.

Proves the system works AS A SYSTEM against a real MongoDB:
  * happy path: every state of both machines traversed in order, audit chain
    matches the spec exactly, governor + stats integrated
  * crash matrix: the worker is killed at each of 12 checkpoints (including
    the two dangerous windows: after the bridge commit and after the pin
    create, before their status writes) — resume two "hours" later still
    reaches VERIFIED with exactly ONE pin document
  * moderation rejection never creates a pin
  * the governor blocks at cap before any pin work happens
  * re-running a finished pipeline is a pure no-op (no double stats/audit)
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from pinner.pipeline import (
    CLEAN_TITLE,
    CRASH_CHECKPOINTS,
    DIRTY_TITLE,
    HELLO_ACCOUNT,
    SimulatedCrash,
    run_hello_pipeline,
)
from pinner.repo.mongo import migrate

T0 = datetime(2026, 1, 15, 12, 0, 0)

EXPECTED_PRODUCT_CHAIN = ["FETCHING", "FETCHED", "MODERATING", "APPROVED"]
EXPECTED_PIN_CHAIN = [
    "ENRICHING", "ENRICHED", "BRIDGING", "BRIDGED",
    "PINNING", "PINNED", "VERIFYING", "VERIFIED",
]


@pytest.fixture()
def mdb(db):
    migrate(db)
    return db


def test_hello_pipeline_happy_path(mdb):
    result = run_hello_pipeline(mdb, now=T0)

    assert result.outcome == "verified"
    assert result.product_states == EXPECTED_PRODUCT_CHAIN
    assert result.pin_states == EXPECTED_PIN_CHAIN

    product = mdb.products.find_one({"_id": result.product_id})
    assert product["status"] == "APPROVED"
    assert product["raw"]["title"] == CLEAN_TITLE
    assert product["moderation"]["verdict"] == "APPROVE"

    pin = mdb.pins.find_one({"_id": result.pin_id})
    assert pin["status"] == "VERIFIED"
    assert pin["content"]["disclosure_included"] is True
    assert pin["bridge"]["url"].startswith("https://neatspace-kitchen.github.io/p/")
    assert pin["bridge"]["commit_sha"]
    assert pin["pin"]["pin_id"].startswith("pin-")
    assert pin.get("lease") is None

    # governor integration: the successful pin was counted once
    account = mdb.accounts.find_one({"name": HELLO_ACCOUNT})
    assert account["stats"] == {
        "pins_today": 1,
        "pins_today_date": T0.strftime("%Y-%m-%d"),
        "last_pin_at": T0,
    }


@pytest.mark.parametrize("checkpoint", CRASH_CHECKPOINTS)
def test_hello_pipeline_survives_crash_at_every_checkpoint(mdb, checkpoint):
    """Kill the worker at the checkpoint; resume two hours later; the walk
    still completes with exactly one product and one pin, VERIFIED."""
    with pytest.raises(SimulatedCrash) as crash:
        run_hello_pipeline(mdb, now=T0, crash_at=checkpoint)
    assert crash.value.checkpoint == checkpoint

    resumed = run_hello_pipeline(mdb, now=T0 + timedelta(hours=2))

    assert resumed.outcome == "verified"
    assert mdb.products.count_documents({}) == 1
    assert mdb.pins.count_documents({}) == 1  # exactly-once, no duplicates
    pin = mdb.pins.find_one()
    assert pin["status"] == "VERIFIED"
    assert pin["pin"]["pin_id"].startswith("pin-")
    assert pin.get("lease") is None
    # the crash consumed at most one swept attempt per interrupted stage
    assert pin["attempt"]["count"] <= 1
    # recovery is explained in the audit trail
    events = [e["event"] for e in mdb.audit_log.find({"entity": "pins"})]
    if checkpoint.startswith(("after_enrich", "after_bridge", "after_pin", "after_verify")):
        assert "SWEEP_EXPIRED" in events


def test_hello_pipeline_rejected_product_never_pins(mdb):
    result = run_hello_pipeline(mdb, now=T0, title=DIRTY_TITLE)

    assert result.outcome == "rejected"
    product = mdb.products.find_one({"_id": result.product_id})
    assert product["status"] == "REJECTED"
    assert product["moderation"]["reasons"] == ["policy: adult"]
    assert mdb.pins.count_documents({}) == 0  # no pin was ever created


def test_hello_pipeline_governor_blocks_at_cap(mdb):
    """One pin per (account, product) is guaranteed by the unique index, so
    the cap is exercised by saturating the warmup day-1 budget (2/day)."""
    first = run_hello_pipeline(mdb, now=T0)
    assert first.outcome == "verified"

    mdb.accounts.update_one({"name": HELLO_ACCOUNT}, {"$set": {"stats.pins_today": 2}})

    blocked = run_hello_pipeline(mdb, now=T0 + timedelta(minutes=30))
    assert blocked.outcome == "blocked"
    assert "daily cap reached (2/2)" in blocked.blocked_reason
    assert blocked.pin_id is None            # no pin work was started
    assert mdb.pins.count_documents({}) == 1  # only the successful pin exists
    assert mdb.pins.count_documents({"lease": {"$ne": None}}) == 0


def test_hello_pipeline_rerun_is_a_noop(mdb):
    first = run_hello_pipeline(mdb, now=T0)
    audit_before = mdb.audit_log.count_documents({})
    stats_before = mdb.accounts.find_one({"name": HELLO_ACCOUNT})["stats"]

    # +25 min: past the 20-min spacing rule, so the governor lets us through —
    # and the walk must STILL be a pure no-op because everything is terminal.
    second = run_hello_pipeline(mdb, now=T0 + timedelta(minutes=25))

    assert second.outcome == "verified"
    assert second.pin_id == first.pin_id
    assert mdb.pins.count_documents({}) == 1
    assert mdb.audit_log.count_documents({}) == audit_before  # zero new writes
    stats_after = mdb.accounts.find_one({"name": HELLO_ACCOUNT})["stats"]
    assert stats_after == stats_before  # no double-count


def test_hello_pipeline_deterministic_pin_id_survives_crash(mdb):
    """The after_pin_create crash is THE dangerous window: the side effect
    happened but the status write didn't. The deterministic stub pin id means
    the resumed run adopts the same pin instead of creating a second one."""
    with pytest.raises(SimulatedCrash):
        run_hello_pipeline(mdb, now=T0, crash_at="after_pin_create")
    resumed = run_hello_pipeline(mdb, now=T0 + timedelta(hours=2))
    assert resumed.outcome == "verified"
    assert mdb.pins.count_documents({}) == 1
    ids = mdb.pins.distinct("pin.pin_id")
    assert len(ids) == 1 and ids[0].startswith("pin-")
