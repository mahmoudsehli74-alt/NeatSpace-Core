"""Seeds + ops tests (WP7) — Mongo-backed."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from pinner import ops, seeds
from pinner.repo.mongo import migrate

T0 = datetime(2026, 1, 15, 12, 0, 0)


@pytest.fixture()
def mdb(db):
    migrate(db)
    return db


def test_seed_accounts_creates_niches_and_accounts(mdb):
    report = seeds.seed_accounts(mdb, github_user="builder", now=T0)
    assert len(report["niches"]) == 3 and len(report["accounts"]) == 3
    assert mdb.niches.count_documents({}) == 3
    assert mdb.accounts.count_documents({}) == 3

    kitchen = mdb.accounts.find_one({"name": "NeatSpace Kitchen"})
    assert kitchen["status"] == "WARMUP"
    assert kitchen["warmup"]["started_at"] == T0
    assert kitchen["site"]["repo_full_name"] == "builder/neatspace-kitchen"
    niche = mdb.niches.find_one({"_id": kitchen["niche_id"]})
    assert niche["name"] == "kitchen"


def test_seed_is_idempotent_and_preserves_lifecycle(mdb):
    seeds.seed_accounts(mdb, github_user="builder", now=T0)
    # the account lives a little...
    mdb.accounts.update_one(
        {"name": "NeatSpace Kitchen"},
        {"$set": {"status": "ACTIVE", "stats": {"pins_today": 3,
                                                "pins_today_date": "2026-01-20",
                                                "last_pin_at": T0 + timedelta(days=5)}}},
    )
    later = T0 + timedelta(days=6)
    seeds.seed_accounts(mdb, github_user="newuser", now=later)
    assert mdb.accounts.count_documents({}) == 3  # no duplicates
    kitchen = mdb.accounts.find_one({"name": "NeatSpace Kitchen"})
    assert kitchen["status"] == "ACTIVE"  # not reset
    assert kitchen["stats"]["pins_today"] == 3  # not reset
    assert kitchen["warmup"]["started_at"] == T0  # warm-up clock untouched
    assert kitchen["site"]["repo_full_name"] == "newuser/neatspace-kitchen"  # config refreshed


def test_unique_name_indexes_reject_duplicates(mdb):
    seeds.seed_accounts(mdb, github_user="builder", now=T0)
    from pymongo.errors import DuplicateKeyError

    with pytest.raises(DuplicateKeyError):
        mdb.accounts.insert_one({"name": "NeatSpace Kitchen"})
    with pytest.raises(DuplicateKeyError):
        mdb.niches.insert_one({"name": "kitchen"})


def test_seed_one_account_rejects_unknown_niche(mdb):
    seeds.seed_niches(mdb, now=T0)
    with pytest.raises(ValueError):
        seeds.seed_one_account(mdb, name="X", niche="does-not-exist",
                               repo_full_name="u/r", now=T0)


# --- ops -----------------------------------------------------------------------------


@pytest.fixture()
def seeded(mdb):
    seeds.seed_accounts(mdb, github_user="builder", now=T0)
    return mdb


def test_ops_find_and_require_account(seeded):
    assert ops.find_account(seeded, "NeatSpace Kitchen") is not None
    assert ops.find_account(seeded, "ghost") is None
    with pytest.raises(KeyError):
        ops.pause_account(seeded, "ghost")


def test_ops_pause_resume_account_flips_status_and_pins(seeded):
    from tests.test_repo import seed_pin

    account = ops.find_account(seeded, "NeatSpace Kitchen")
    seed_pin(seeded, account_id=str(account["_id"]))
    seed_pin(seeded, account_id=str(account["_id"]), status="VERIFIED")

    paused = ops.pause_account(seeded, "NeatSpace Kitchen")
    assert paused == 1  # only the QUEUED pin; VERIFIED untouched
    assert ops.find_account(seeded, "NeatSpace Kitchen")["status"] == "PAUSED"
    assert seeded.pins.count_documents({"status": "PAUSED"}) == 1

    resumed = ops.resume_account(seeded, "NeatSpace Kitchen")
    assert resumed == 1
    assert ops.find_account(seeded, "NeatSpace Kitchen")["status"] == "ACTIVE"
    assert seeded.pins.count_documents({"status": "QUEUED"}) == 1


def test_ops_requeue_dead_pin(seeded):
    from tests.test_repo import seed_pin

    seed_pin(seeded, status="DEAD")
    dead = seeded.pins.find_one({"status": "DEAD"})
    result = ops.requeue(seeded, "pins", dead["_id"])
    assert result["status"] == "QUEUED" and result["attempt"]["count"] == 0
    with pytest.raises(ValueError):
        ops.requeue(seeded, "bogus", dead["_id"])


def test_ops_status_summary(seeded):
    from tests.test_repo import seed_pin

    account = ops.find_account(seeded, "NeatSpace Kitchen")
    seed_pin(seeded, account_id=str(account["_id"]))
    seed_pin(seeded, account_id=str(account["_id"]), status="VERIFIED")
    seed_pin(seeded, account_id=str(account["_id"]), status="DEAD")

    summary = ops.status_summary(seeded)
    assert {a["name"] for a in summary["accounts"]} == {
        "NeatSpace Kitchen", "NeatSpace Aesthetics", "NeatSpace Selfcare"
    }
    assert summary["pins"]["QUEUED"] == 1
    assert summary["pins"]["VERIFIED"] == 1
    assert summary["pins"]["DEAD"] == 1
    assert summary["last_run"] is None  # no runs yet
