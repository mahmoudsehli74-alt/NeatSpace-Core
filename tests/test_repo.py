"""WP4 repo-layer tests: the Phase-1 exit-criteria suite.

Covers, against a REAL MongoDB:
  * claims: exclusivity, account scoping, FIFO + finisher-first ordering,
    backoff gating, lease handling
  * transitions: full happy-path walks of both machines, optimistic-concurrency
    guard (ConcurrentStateError), illegal events, patch safety
  * failures: retry-with-backoff, poison after max attempts, immediate
    PERMANENT poison, per-stage attempt reset
  * crash-injection matrix: worker dies at EVERY working state -> sweep
    recovers to the correct predecessor, consumes an attempt, clears the lease
  * races: 4 parallel workers claim 100 docs exactly once; two workers race
    one transition with a barrier rendezvous -> exactly one winner
  * pause/resume (kill switch), dead-letter requeue, audit fire-and-forget
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta

import pytest

from pinner.repo import audit, engine
from pinner.repo.pins import PinsRepo
from pinner.repo.products import ProductsRepo
from pinner.statemachine import IllegalTransitionError, Machine

T0 = datetime(2026, 1, 1, 12, 0, 0)
DETERMINISTIC_BACKOFF = lambda count: 60 * count  # noqa: E731


@pytest.fixture()
def mdb(db):
    from pinner.repo.mongo import migrate

    migrate(db)
    return db


def seed_pin(mdb, *, account_id="acc1", product_id=None, status="QUEUED", **extra):
    now = engine.utcnow()
    doc = {
        "account_id": account_id,
        "product_id": product_id or f"prod-{uuid.uuid4().hex[:8]}",
        "status": status,
        "attempt": engine.fresh_attempt(),
        "created_at": now,
        "updated_at": now,
        **extra,
    }
    mdb.pins.insert_one(doc)
    return doc


def seed_product(mdb, *, source="aliexpress", status="PENDING_FETCH", **extra):
    now = engine.utcnow()
    doc = {
        "source": source,
        "source_product_id": f"sid-{uuid.uuid4().hex[:10]}",
        "status": status,
        "attempt": engine.fresh_attempt(),
        "created_at": now,
        "updated_at": now,
        "first_seen_at": now,
        **extra,
    }
    mdb.products.insert_one(doc)
    return doc


def expire_lease(mdb, collection, doc_id, *, before):
    """Expire a lease RELATIVE to the test's clock (before - 1s), never wall time."""
    mdb[collection].update_one(
        {"_id": doc_id}, {"$set": {"lease.expires_at": before - timedelta(seconds=1)}}
    )


# --- Claims & leases ---------------------------------------------------------------


def test_claim_is_exclusive_and_sets_lease(mdb):
    pins = PinsRepo(mdb)
    seed_pin(mdb, product_id="p1")
    seed_pin(mdb, product_id="p2")
    a = pins.claim_next_for_account("acc1", "run-1", now=T0)
    b = pins.claim_next_for_account("acc1", "run-2", now=T0)
    assert a is not None and b is not None
    assert a[0]["_id"] != b[0]["_id"]
    assert a[1] == "CLAIM_ENRICH" and a[0]["status"] == "ENRICHING"
    assert a[0]["lease"]["owner"] == "run-1"
    assert a[0]["lease"]["expires_at"] == T0 + timedelta(seconds=engine.DEFAULT_LEASE_TTL_SECONDS)
    assert b[0]["lease"]["owner"] == "run-2"


def test_claim_scoped_to_account(mdb):
    pins = PinsRepo(mdb)
    seed_pin(mdb, account_id="acc1")
    seed_pin(mdb, account_id="acc2")
    claimed = pins.claim_next_for_account("acc2", "run-1", now=T0)
    assert claimed[0]["account_id"] == "acc2"
    assert pins.claim_next_for_account("acc2", "run-2", now=T0) is None


def test_claim_skips_backoff_gated_documents(mdb):
    pins = PinsRepo(mdb)
    doc = seed_pin(mdb)
    mdb.pins.update_one(
        {"_id": doc["_id"]},
        {"$set": {"attempt.next_attempt_at": T0 + timedelta(hours=1)}},
    )
    assert pins.claim_next_for_account("acc1", "run-1", now=T0) is None
    later = T0 + timedelta(hours=2)
    assert pins.claim_next_for_account("acc1", "run-2", now=later) is not None


def test_claim_never_takes_working_documents_even_with_expired_lease(mdb):
    """A crashed WORKING doc (expired lease) is NOT claimable — only the sweep
    may revert it. This keeps claim and recovery strictly separated."""
    pins = PinsRepo(mdb)
    doc = seed_pin(mdb)
    mdb.pins.update_one(
        {"_id": doc["_id"]},
        {
            "$set": {
                "status": "ENRICHING",
                "lease": {"owner": "ghost", "expires_at": T0 + timedelta(minutes=5)},
            }
        },
    )
    assert pins.claim_next_for_account("acc1", "run-1", now=T0) is None
    expire_lease(mdb, "pins", doc["_id"], before=T0 + timedelta(hours=1))
    assert pins.claim_next_for_account("acc1", "run-1", now=T0 + timedelta(hours=1)) is None


def test_claim_order_is_fifo_and_finisher_first(mdb):
    pins = PinsRepo(mdb)
    old_queued = seed_pin(mdb, product_id="old-q")
    mdb.pins.update_one({"_id": old_queued["_id"]}, {"$set": {"created_at": T0}})
    inflight = seed_pin(mdb, product_id="inflight")
    mdb.pins.update_one(
        {"_id": inflight["_id"]},
        {"$set": {"created_at": T0 + timedelta(seconds=1), "status": "ENRICHED"}},
    )
    doc, event = pins.claim_next_for_account("acc1", "run-1", now=T0)
    # finisher-first: the ENRICHED pin advances before the QUEUED one starts
    assert doc["_id"] == inflight["_id"]
    assert event == "CLAIM_BRIDGE"


# --- Transitions: full walks & guards ------------------------------------------------


def test_publication_full_happy_path_walk(mdb):
    pins = PinsRepo(mdb)
    doc = seed_pin(mdb, product_id="p-final")
    pid = doc["_id"]

    claimed, event = pins.claim_next_for_account("acc1", "run-1", now=T0)
    assert event == "CLAIM_ENRICH" and claimed["_id"] == pid
    doc = pins.transition(
        pid, "ENRICH_OK", patch={"content": {"title": "10 Kitchen Finds"}}, run_id="run-1"
    )
    assert doc["status"] == "ENRICHED"
    assert doc["attempt"]["count"] == 0  # per-stage budget reset on ADVANCE
    assert doc.get("lease") is None and doc["content"]["title"] == "10 Kitchen Finds"

    _, event = pins.claim_next_for_account("acc1", "run-1", now=T0)
    assert event == "CLAIM_BRIDGE"
    doc = pins.transition(
        pid,
        "BRIDGE_OK",
        patch={"bridge": {"url": "https://x.shop/p/1", "commit_sha": "aa"}},
        run_id="run-1",
    )
    assert doc["status"] == "BRIDGED"

    _, event = pins.claim_next_for_account("acc1", "run-1", now=T0)
    assert event == "CLAIM_PIN"
    doc = pins.transition(pid, "PIN_OK", patch={"pin": {"pin_id": "pin-xyz"}}, run_id="run-1")
    assert doc["status"] == "PINNED" and doc["pin"]["pin_id"] == "pin-xyz"

    _, event = pins.claim_next_for_account("acc1", "run-1", now=T0)
    assert event == "CLAIM_VERIFY"
    doc = pins.transition(pid, "VERIFY_OK", run_id="run-1")
    assert doc["status"] == "VERIFIED"

    assert pins.claim_next_for_account("acc1", "run-1", now=T0) is None  # terminal
    assert mdb.audit_log.count_documents({"event": "ENRICH_OK", "entity_id": pid}) == 1


def test_transition_illegal_event_raises(mdb):
    pins = PinsRepo(mdb)
    doc = seed_pin(mdb)
    with pytest.raises(IllegalTransitionError):
        pins.transition(doc["_id"], "ENRICH_OK", run_id="r")


def test_transition_stale_worker_cannot_double_advance(mdb):
    pins = PinsRepo(mdb)
    seed_pin(mdb)
    claimed, _ = pins.claim_next_for_account("acc1", "run-1", now=T0)
    pins.transition(claimed["_id"], "ENRICH_OK", run_id="run-1")
    with pytest.raises(IllegalTransitionError):
        pins.transition(claimed["_id"], "ENRICH_OK", run_id="run-1-stale")


def test_transition_optimistic_guard_concurrent_state_error(mdb):
    pins = PinsRepo(mdb)
    doc = seed_pin(mdb)
    claimed, _ = pins.claim_next_for_account("acc1", "run-1", now=T0)

    def concurrent_sweep():
        # A concurrent sweeper reverts the doc behind our read (fault hook =
        # the rendezvous between our read and our guarded write).
        mdb.pins.update_one(
            {"_id": doc["_id"]},
            {"$set": {"status": "QUEUED"}, "$unset": {"lease": ""}},
        )

    with pytest.raises(engine.ConcurrentStateError):
        pins.transition(doc["_id"], "ENRICH_OK", run_id="run-1", fault=concurrent_sweep)


def test_patch_cannot_override_reserved_fields(mdb):
    pins = PinsRepo(mdb)
    doc = seed_pin(mdb)
    claimed, _ = pins.claim_next_for_account("acc1", "run-1", now=T0)
    with pytest.raises(ValueError):
        pins.transition(doc["_id"], "ENRICH_OK", patch={"status": "VERIFIED"})
    with pytest.raises(ValueError):
        pins.transition(doc["_id"], "ENRICH_OK", patch={"lease": {}})


# --- Failure handling -----------------------------------------------------------------


def test_fail_transient_retries_with_backoff_then_poisons(mdb):
    pins = PinsRepo(mdb)
    doc = seed_pin(mdb)
    claim_times = [T0, T0 + timedelta(hours=1), T0 + timedelta(hours=2)]
    for i, t in enumerate(claim_times, start=1):
        claimed, _ = pins.claim_next_for_account("acc1", f"run-{i}", now=t)
        result = pins.fail(
            claimed["_id"], error="gemini 429", backoff=DETERMINISTIC_BACKOFF, now=t
        )
        if i < 3:
            assert result["status"] == "QUEUED"  # reverted to predecessor
            assert result["attempt"]["count"] == i
            assert result["attempt"]["next_attempt_at"] == t + timedelta(seconds=60 * i)
        else:
            assert result["status"] == "DEAD"  # attempts exhausted -> poison
            assert result["attempt"]["next_attempt_at"] is None
    assert mdb.pins.find_one({"_id": doc["_id"]})["status"] == "DEAD"


def test_fail_permanent_poisons_immediately(mdb):
    pins = PinsRepo(mdb)
    seed_pin(mdb)
    claimed, _ = pins.claim_next_for_account("acc1", "run-1", now=T0)
    result = pins.fail(claimed["_id"], error="schema invalid", error_class="PERMANENT", now=T0)
    assert result["status"] == "DEAD"
    assert result["attempt"]["last_error_class"] == "PERMANENT"


def test_fail_on_idle_document_raises(mdb):
    pins = PinsRepo(mdb)
    doc = seed_pin(mdb)  # QUEUED, not working
    with pytest.raises(ValueError):
        pins.fail(doc["_id"], error="boom", now=T0)


def test_requeue_dead_resets_attempt_budget(mdb):
    pins = PinsRepo(mdb)
    seed_pin(mdb)
    claimed, _ = pins.claim_next_for_account("acc1", "run-1", now=T0)
    pins.fail(claimed["_id"], error="permanent", error_class="PERMANENT", now=T0)
    result = pins.requeue_dead(claimed["_id"], run_id="ops", now=T0)
    assert result["status"] == "QUEUED"
    assert result["attempt"]["count"] == 0


# --- Crash-injection matrix: worker dies at every working state --------------------------


@pytest.mark.parametrize(
    ("working", "predecessor", "claim_event"),
    [
        ("ENRICHING", "QUEUED", "CLAIM_ENRICH"),
        ("BRIDGING", "ENRICHED", "CLAIM_BRIDGE"),
        ("PINNING", "BRIDGED", "CLAIM_PIN"),
        ("VERIFYING", "PINNED", "CLAIM_VERIFY"),
    ],
)
def test_sweep_crash_matrix_publication(mdb, working, predecessor, claim_event):
    pins = PinsRepo(mdb)
    seed_pin(mdb, status=predecessor)
    claimed, _ = pins.claim_next_for_account("acc1", "run-crashed", now=T0)
    assert claimed["status"] == working

    # ... worker dies here. Next run starts much later:
    later = T0 + timedelta(hours=2)
    expire_lease(mdb, "pins", claimed["_id"], before=later)
    reverted = pins.sweep(run_id="run-next", now=later, backoff=DETERMINISTIC_BACKOFF)

    assert len(reverted) == 1
    assert reverted[0] == {"_id": claimed["_id"], "from": working, "to": predecessor}
    doc = mdb.pins.find_one({"_id": claimed["_id"]})
    assert doc["status"] == predecessor
    assert doc.get("lease") is None  # lease released
    assert doc["attempt"]["count"] == 1  # the crash consumed an attempt
    assert doc["attempt"]["next_attempt_at"] == later + timedelta(seconds=60)
    assert mdb.audit_log.count_documents(
        {"entity": "pins", "entity_id": claimed["_id"], "event": "SWEEP_EXPIRED"}
    ) == 1

    # and the recovered doc is claimable again once backoff elapses
    reclaimed = pins.claim_next_for_account("acc1", "run-next2", now=later + timedelta(minutes=5))
    assert reclaimed is not None and reclaimed[0]["_id"] == claimed["_id"]


@pytest.mark.parametrize(
    ("working", "predecessor"),
    [("FETCHING", "PENDING_FETCH"), ("MODERATING", "FETCHED")],
)
def test_sweep_crash_matrix_ingest(mdb, working, predecessor):
    products = ProductsRepo(mdb)
    seed_product(mdb, status=predecessor)
    claim = (
        products.claim_next_fetch if working == "FETCHING" else products.claim_next_moderation
    )
    claimed = claim("run-crashed", now=T0)
    assert claimed["status"] == working

    later = T0 + timedelta(hours=2)
    expire_lease(mdb, "products", claimed["_id"], before=later)
    reverted = products.sweep(run_id="run-next", now=later, backoff=DETERMINISTIC_BACKOFF)

    assert len(reverted) == 1 and reverted[0]["to"] == predecessor
    doc = mdb.products.find_one({"_id": claimed["_id"]})
    assert doc["status"] == predecessor and doc.get("lease") is None
    assert doc["attempt"]["count"] == 1


def test_sweep_ignores_live_leases_and_empty_queues(mdb):
    pins = PinsRepo(mdb)
    seed_pin(mdb)
    claimed, _ = pins.claim_next_for_account("acc1", "run-1", now=T0)
    assert pins.sweep(run_id="run-2", now=T0 + timedelta(minutes=5)) == []
    assert mdb.pins.find_one({"_id": claimed["_id"]})["status"] == "ENRICHING"


def test_sweep_is_idempotent_under_double_sweep(mdb):
    pins = PinsRepo(mdb)
    seed_pin(mdb)
    claimed, _ = pins.claim_next_for_account("acc1", "run-1", now=T0)
    later = T0 + timedelta(hours=2)
    expire_lease(mdb, "pins", claimed["_id"], before=later)
    first = pins.sweep(run_id="a", now=later, backoff=DETERMINISTIC_BACKOFF)
    second = pins.sweep(run_id="b", now=later, backoff=DETERMINISTIC_BACKOFF)
    assert len(first) == 1 and second == []
    doc = mdb.pins.find_one({"_id": claimed["_id"]})
    assert doc["attempt"]["count"] == 1  # not double-charged


# --- Pause / resume (kill switch) -----------------------------------------------------


def test_pause_and_resume_account_roundtrip(mdb):
    pins = PinsRepo(mdb)
    queued = seed_pin(mdb, product_id="pq")                     # IDLE (QUEUED)
    inflight = seed_pin(mdb, product_id="pe", status="ENRICHED")  # finisher-first target
    done = seed_pin(mdb, product_id="pv", status="VERIFIED")    # terminal

    claimed, event = pins.claim_next_for_account("acc1", "run-1", now=T0)
    assert claimed["_id"] == inflight["_id"] and event == "CLAIM_BRIDGE"
    # inflight is now BRIDGING (WORKING) with a live lease

    paused = pins.pause_account("acc1", run_id="ops", now=T0)
    assert paused == 2  # QUEUED + BRIDING; VERIFIED is terminal and untouched
    states = {d["_id"]: d for d in mdb.pins.find({})}
    assert states[queued["_id"]]["status"] == "PAUSED"
    assert states[queued["_id"]]["paused_from"] == "QUEUED"
    assert states[inflight["_id"]]["status"] == "PAUSED"
    # WORKING source rewinds to its sweep target on pause
    assert states[inflight["_id"]]["paused_from"] == "ENRICHED"
    assert states[done["_id"]]["status"] == "VERIFIED"

    assert pins.claim_next_for_account("acc1", "run-1", now=T0) is None  # nothing claimable

    resumed = pins.resume_account("acc1", run_id="ops", now=T0)
    assert resumed == 2
    states = {d["_id"]: d for d in mdb.pins.find({})}
    assert states[queued["_id"]]["status"] == "QUEUED"
    assert states[inflight["_id"]]["status"] == "ENRICHED"  # back where it came from
    assert "paused_from" not in states[queued["_id"]]
    # and work continues: the recovered ENRICHED pin is claimable again
    reclaimed, event = pins.claim_next_for_account("acc1", "run-2", now=T0)
    assert reclaimed["_id"] == inflight["_id"] and event == "CLAIM_BRIDGE"


def test_pause_on_machine_without_paused_role_is_noop(mdb):
    seed_product(mdb)
    assert (
        engine.pause_all(mdb, "products", Machine.INGEST, run_id="ops", now=T0) == 0
    )
    assert mdb.products.count_documents({"status": "PENDING_FETCH"}) == 1


# --- Products lifecycle -----------------------------------------------------------------


def test_products_ingest_lifecycle_approve(mdb):
    products = ProductsRepo(mdb)
    status, doc = products.upsert_candidate("aliexpress", "1005006", dedup_hash="h1")
    assert status == "created" and doc["status"] == "PENDING_FETCH"
    again_status, again_doc = products.upsert_candidate("aliexpress", "1005006")
    assert again_status == "exists" and again_doc["_id"] == doc["_id"]
    assert mdb.products.count_documents({}) == 1

    claimed = products.claim_next_fetch("run-1", now=T0)
    doc = products.transition(
        claimed["_id"], "FETCH_OK", patch={"raw": {"title": "Gadget"}}, run_id="run-1"
    )
    assert doc["status"] == "FETCHED" and doc["raw"]["title"] == "Gadget"

    claimed = products.claim_next_moderation("run-1", now=T0)
    assert claimed["_id"] == doc["_id"]
    doc = products.transition(
        claimed["_id"],
        "MODERATE_APPROVE",
        patch={"moderation": {"verdict": "APPROVE", "confidence": 0.93}},
        run_id="run-1",
    )
    assert doc["status"] == "APPROVED"
    assert products.claim_next_moderation("run-2", now=T0) is None


def test_products_ingest_lifecycle_reject(mdb):
    products = ProductsRepo(mdb)
    products.upsert_candidate("aliexpress", "2001")
    claimed = products.claim_next_fetch("run-1", now=T0)
    products.transition(claimed["_id"], "FETCH_OK", run_id="run-1")
    claimed = products.claim_next_moderation("run-1", now=T0)
    result = products.transition(
        claimed["_id"],
        "MODERATE_REJECT",
        patch={"moderation": {"verdict": "REJECT"}},
        run_id="run-1",
    )
    assert result["status"] == "REJECTED"


# --- Concurrency: the race gatekeepers ---------------------------------------------------


def test_concurrent_claims_exactly_once(mdb):
    """4 workers, 100 documents: every doc claimed exactly once, no overlap."""
    for i in range(100):
        seed_pin(mdb, account_id="acc1", product_id=f"p-{i}")
    results: list[list] = [[], [], [], []]

    def worker(idx):
        pins = PinsRepo(mdb)
        while True:
            claimed = pins.claim_next_for_account("acc1", f"run-{idx}", now=engine.utcnow())
            if claimed is None:
                return
            results[idx].append(str(claimed[0]["_id"]))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    all_ids = [i for chunk in results for i in chunk]
    assert len(all_ids) == 100
    assert len(set(all_ids)) == 100  # exactly-once across workers
    assert mdb.pins.count_documents({"status": "ENRICHING"}) == 100


def test_concurrent_transition_race_exactly_one_winner(mdb):
    """Two workers race to advance the same document; a barrier rendezvous
    guarantees both have read before either writes. One wins, one gets
    ConcurrentStateError — never two writes."""
    pins = PinsRepo(mdb)
    seed_pin(mdb)
    claimed, _ = pins.claim_next_for_account("acc1", "run-1", now=T0)
    doc_id = claimed["_id"]

    barrier = threading.Barrier(2)
    outcomes: list = []

    def racer(idx):
        def rendezvous():
            barrier.wait(timeout=5)

        try:
            pins.transition(doc_id, "ENRICH_OK", run_id=f"race-{idx}", fault=rendezvous)
            outcomes.append("ok")
        except engine.ConcurrentStateError:
            outcomes.append("conflict")

    threads = [threading.Thread(target=racer, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert sorted(outcomes) == ["conflict", "ok"]
    assert mdb.pins.find_one({"_id": doc_id})["status"] == "ENRICHED"
    assert mdb.audit_log.count_documents({"event": "ENRICH_OK", "entity_id": doc_id}) == 1


# --- Audit --------------------------------------------------------------------------------


def test_audit_never_raises_on_storage_failure():
    class BoomCollection:
        def insert_one(self, *args, **kwargs):
            from pymongo.errors import PyMongoError

            raise PyMongoError("storage down")

    class BoomDb:
        def __getitem__(self, key):
            return BoomCollection()

    assert audit.log(BoomDb(), run_id="r", entity="pins", entity_id=1, event="X") is False


def test_audit_recent_reader(mdb):
    seed_pin(mdb)
    PinsRepo(mdb).claim_next_for_account("acc1", "run-1", now=T0)
    entries = audit.recent(mdb, limit=10)
    assert len(entries) >= 1
    assert entries[0]["event"] == "CLAIM_ENRICH"
