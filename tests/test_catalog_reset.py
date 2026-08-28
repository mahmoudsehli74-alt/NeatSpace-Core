"""Catalog reset (ops) + dead-state autonomy invariant + domain destinations."""

from __future__ import annotations

import pytest

from pinner.ops import reset_dead_products
from tests.test_qa_audit import (
    T0,
    BearerRouter,
    MultiNicheAdapter,
    default_bridge_replies,
    make_runner,
)


def dead_product(mdb, *, pid: str, error: str):
    kitchen = mdb.accounts.find_one({"name": "NeatSpace Kitchen"})
    doc = {
        "source": "stub-store", "source_product_id": pid, "status": "DEAD_FETCH",
        "attempt": {"count": 3, "last_error": error, "last_error_at": T0,
                    "last_error_class": "PERMANENT", "next_attempt_at": None},
        "first_seen_at": T0, "last_updated_at": T0, "created_at": T0,
        "discovered_niche_id": kitchen["niche_id"],
    }
    mdb.products.insert_one(doc)
    return doc


@pytest.fixture()
def mdb(db):
    import tests.test_qa_audit as qa
    from pinner.repo.mongo import migrate

    migrate(db)
    qa.seed_all_accounts(db)
    return db


def test_reset_is_scoped_to_error_signature(mdb):
    dead_product(mdb, pid="111",
                 error="[aliexpress] iop error MissingParameter: promotion_link_type")
    dead_product(mdb, pid="222", error="[aliexpress] product not found")  # genuinely bad
    report = reset_dead_products(mdb, now=T0)              # default: MissingParameter
    assert report["reset"] == 1
    revived = mdb.products.find_one({"source_product_id": "111"})
    assert revived["status"] == "PENDING_FETCH"
    assert revived["attempt"]["count"] == 0                # fresh budget
    untouched = mdb.products.find_one({"source_product_id": "222"})
    assert untouched["status"] == "DEAD_FETCH"             # scoped, not swept
    assert mdb.audit_log.count_documents({"event": "CATALOG_RESET"}) == 1


def test_reset_all_sweeps_everything_explicitly(mdb):
    dead_product(mdb, pid="111", error="[aliexpress] iop error MissingParameter: x")
    dead_product(mdb, pid="222", error="[aliexpress] product not found")
    report = reset_dead_products(mdb, error_substrings=(), now=T0)
    assert report["reset"] == 2


def test_runner_never_touches_dead_products_on_its_own(mdb):
    """AUTONOMY INVARIANT: DEAD is terminal for the runner — only ops reset
    can revive. A catalog of dead products yields a no-op run."""
    dead_product(mdb, pid="111", error="[aliexpress] iop error MissingParameter: x")
    runner, _ = make_runner(mdb, dry_run=True,
                            adapter=MultiNicheAdapter(active_niches=()),
                            gemini_script=[], run_id_suffix="deadnoop")
    stats = runner.execute()
    assert stats.get("fetched") is None and stats.get("new_products") is None
    assert mdb.products.find_one({"source_product_id": "111"})["status"] == "DEAD_FETCH"


def test_reset_then_run_advances_full_pipeline(mdb):
    """The end-to-end healing proof: reset the incident casualties, then a
    single dry-run completes fetch -> approve -> enrich -> bridge."""
    doc = dead_product(mdb, pid="100500111",   # kitchen fixture pid: resolvable
                       error="[aliexpress] iop error MissingParameter: promotion_link_type")
    report = reset_dead_products(mdb, now=T0)
    assert report["reset"] == 1

    runner, _ = make_runner(mdb, dry_run=True,
                            adapter=MultiNicheAdapter(active_niches=()),
                            gemini_script=[], run_id_suffix="healed")
    stats = runner.execute()
    assert stats.get("fetched") == 1
    assert stats.get("bridged") == 1                       # full advance, unblocked
    assert mdb.products.find_one({"_id": doc["_id"]})["status"] != "DEAD_FETCH"


def test_pins_created_use_custom_domain_destination(mdb):
    """Launch evidence: with domains wired, destinations are the custom
    storefronts, not github.io."""
    from pinner.seeds import seed_accounts as _sa  # noqa: F401

    kitchen = mdb.accounts.find_one({"name": "NeatSpace Kitchen"})
    mdb.accounts.update_one({"_id": kitchen["_id"]},
                            {"$set": {"site.custom_domain": "neatspacekitchen.store"}})
    product = {
        "source": "stub-store", "source_product_id": "100500777",
        "status": "APPROVED", "raw": {"title": "T", "description": "d",
        "images": ["https://cdn/x.jpg"], "price": {"current": 1, "currency": "USD"},
        "source_url": "https://x"}, "affiliate_url": "https://s.click/x",
        "discovered_niche_id": kitchen["niche_id"],
        "attempt": {"count": 0, "last_error": None, "last_error_at": None,
                    "last_error_class": None, "next_attempt_at": None},
        "first_seen_at": T0, "last_updated_at": T0, "created_at": T0,
    }
    mdb.products.insert_one(product)
    mdb.pins.insert_one({
        "account_id": str(kitchen["_id"]), "product_id": product["_id"],
        "status": "QUEUED",
        "attempt": {"count": 0, "last_error": None, "last_error_at": None,
                    "last_error_class": None, "next_attempt_at": None},
        "created_at": T0, "updated_at": T0,
    })
    runner, _ = make_runner(mdb, dry_run=False,
                            adapter=_Noop(), gemini_script=[],
                            pinterest_router=BearerRouter(),
                            bridge_replies=default_bridge_replies(1),
                            pins_per_account=1, daily_cap_override=4,
                            run_id_suffix="domain-dest")
    runner.execute()
    created = mdb.runs.find_one({"run_id": runner.run_id})["pins_created"]
    assert len(created) == 1
    assert created[0]["destination"].startswith("https://neatspacekitchen.store/?id=")


class _Noop:
    name = "noop"

    def __getattr__(self, item):
        raise AssertionError(f"_Noop.{item} called")
