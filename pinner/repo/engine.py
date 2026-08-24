"""Generic Mongo state-machine engine: atomic claims with leases, guarded
transitions, failure handling, and crash-recovery sweeps — the machinery both
repositories (pins, products) share.

Concurrency model — MongoDB server-side atomicity is the single source of truth:

* claim      ``find_one_and_update`` on (source status + attempt-ready +
             lease-free) atomically sets the WORKING status and a lease.
             Exactly one worker can win any given document.
* transition Resolves the event against the document's CURRENT status via the
             registry, then ``update_one({_id, status: expected_source})``.
             A stale worker physically cannot double-transition: its guard
             matches zero documents and ConcurrentStateError is raised.
* fail       Retry-with-backoff or poison-to-DEAD, decided from the registry's
             RETRY/POISON transitions for the current state and the attempt
             count. Attempts are per-stage: a successful ADVANCE resets them.
* sweep      Reverts WORKING documents whose lease expired (worker crash) to
             their predecessor IDLE state, consuming an attempt.

The ``fault`` parameters are TEST-ONLY hooks invoked after resolution but
immediately before the write — the injection point for crash/race simulation.
They must never be used in production code.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument

from pinner.repo import audit
from pinner.statemachine import (
    STATE_ROLES,
    TRANSITIONS,
    IllegalTransitionError,
    Kind,
    Machine,
    Role,
    can_transition,
    state_role,
    sweep_target,
    transition_for,
    transition_of_kind,
    working_states,
)

DEFAULT_LEASE_TTL_SECONDS = 30 * 60
DEFAULT_MAX_ATTEMPTS = 3
BACKOFF_SCHEDULE_SECONDS = (300, 3600, 21600)  # 5 min -> 1 h -> 6 h

ErrorClass = str  # "TRANSIENT" | "PERMANENT"
BackoffFn = Callable[[int], int]  # attempt count -> seconds until next try


class ConcurrentStateError(Exception):
    """Another worker changed the document between read and guarded write."""


def utcnow() -> datetime:
    """Naive UTC now — BSON datetimes are tz-less; this keeps all comparisons
    (Python-side and Mongo-side) consistent."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def fresh_attempt() -> dict:
    return {
        "count": 0,
        "last_error": None,
        "last_error_at": None,
        "last_error_class": None,
        "next_attempt_at": None,
    }


def backoff_seconds(count: int, *, jitter: float = 0.0) -> int:
    """Backoff for the Nth attempt with optional ±jitter fraction (0.2 = ±20%)."""
    base = BACKOFF_SCHEDULE_SECONDS[min(count - 1, len(BACKOFF_SCHEDULE_SECONDS) - 1)]
    if jitter > 0:
        base = int(base * random.uniform(1 - jitter, 1 + jitter))
    return base


def default_backoff(count: int) -> int:
    return backoff_seconds(count, jitter=0.2)


def _now(now: datetime | None) -> datetime:
    return now if now is not None else utcnow()


def _resolve_claim(machine: Machine, event: str) -> tuple[str, str]:
    rows = [
        t for t in TRANSITIONS if t.machine is machine and t.event == event and t.kind is Kind.CLAIM
    ]
    if len(rows) != 1:
        raise ValueError(f"expected exactly one CLAIM transition for event {event!r}")
    return rows[0].source, rows[0].target


def claim_one(
    db,
    collection: str,
    machine: Machine,
    *,
    event: str,
    run_id: str,
    extra_filter: dict | None = None,
    lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    now: datetime | None = None,
) -> dict | None:
    """Atomically claim one document for a CLAIM event, acquiring a lease.

    Claims documents whose backoff has elapsed and whose lease is free (or
    expired), oldest first. Returns the updated document or None.
    """
    now = _now(now)
    source, target = _resolve_claim(machine, event)
    ready = {
        "$or": [
            {"attempt": None},
            {"attempt.next_attempt_at": None},
            {"attempt.next_attempt_at": {"$lte": now}},
        ]
    }
    lease_free = {"$or": [{"lease": None}, {"lease.expires_at": {"$lte": now}}]}
    clauses: list[dict] = [{"status": source}, ready, lease_free]
    if extra_filter:
        clauses.append(extra_filter)
    update = {
        "$set": {
            "status": target,
            "updated_at": now,
            "lease": {
                "owner": run_id,
                "claimed_at": now,
                "expires_at": now + timedelta(seconds=lease_ttl_seconds),
            },
        },
        "$unset": {"paused_from": ""},
    }
    doc = db[collection].find_one_and_update(
        {"$and": clauses},
        update,
        sort=[("created_at", 1), ("_id", 1)],
        return_document=ReturnDocument.AFTER,
    )
    if doc is not None:
        audit.log(
            db,
            run_id=run_id,
            entity=collection,
            entity_id=doc["_id"],
            event=event,
            from_state=source,
            to_state=target,
        )
    return doc


def _check_patch(patch: dict | None) -> dict:
    if not patch:
        return {}
    reserved = {"status", "lease"} & set(patch)
    if reserved:
        raise ValueError(f"patch must not override reserved fields: {sorted(reserved)}")
    return patch


def transition_doc(
    db,
    collection: str,
    machine: Machine,
    doc_id,
    *,
    event: str,
    target: str | None = None,
    patch: dict | None = None,
    run_id: str | None = None,
    now: datetime | None = None,
    fault: Callable[[], None] | None = None,
) -> dict:
    """Emit a state-machine event for a document, guarded on its current status.

    Raises IllegalTransitionError if the event is illegal for the document's
    state, ConcurrentStateError if another worker won the write, KeyError if
    the document does not exist.
    """
    now = _now(now)
    coll = db[collection]
    doc = coll.find_one({"_id": doc_id})
    if doc is None:
        raise KeyError(f"document not found: {doc_id}")
    tr = transition_for(machine, doc["status"], event, target=target)
    if fault is not None:
        fault()
    sets: dict = {"status": tr.target, "updated_at": now}
    if tr.kind is Kind.ADVANCE:
        sets["attempt"] = fresh_attempt()  # stage completed -> retry budget resets
    if tr.kind is Kind.PAUSE:
        # Resume must land on an IDLE state: a WORKING source rewinds to its sweep target.
        source = tr.source
        sets["paused_from"] = (
            sweep_target(machine, source)
            if state_role(machine, source) is Role.WORKING
            else source
        )
    sets.update(_check_patch(patch))
    result = coll.update_one(
        {"_id": doc_id, "status": tr.source},
        {"$set": sets, "$unset": {"lease": "", "paused_from": ""}},
    )
    if result.matched_count == 0:
        raise ConcurrentStateError(f"document {doc_id} changed state concurrently")
    audit.log(
        db,
        run_id=run_id,
        entity=collection,
        entity_id=doc_id,
        event=tr.event,
        from_state=tr.source,
        to_state=tr.target,
    )
    return coll.find_one({"_id": doc_id})


def fail_doc(
    db,
    collection: str,
    machine: Machine,
    doc_id,
    *,
    error: str,
    error_class: ErrorClass = "TRANSIENT",
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    run_id: str | None = None,
    now: datetime | None = None,
    backoff: BackoffFn | None = None,
    fault: Callable[[], None] | None = None,
) -> dict:
    """Record a stage failure on a WORKING document: retry with backoff, or
    poison to the terminal DEAD state when attempts are exhausted or the
    caller classifies the error PERMANENT."""
    now = _now(now)
    coll = db[collection]
    doc = coll.find_one({"_id": doc_id})
    if doc is None:
        raise KeyError(f"document not found: {doc_id}")
    state = doc["status"]
    if state_role(machine, state) is not Role.WORKING:
        raise ValueError(f"cannot fail a non-WORKING document (status={state!r})")
    count = (doc.get("attempt") or {}).get("count", 0) + 1
    poison = error_class == "PERMANENT" or count >= max_attempts
    kind = Kind.POISON if poison else Kind.RETRY
    tr = transition_of_kind(machine, state, kind)
    if fault is not None:
        fault()
    next_at = None if poison else now + timedelta(seconds=(backoff or default_backoff)(count))
    attempt = {
        "count": count,
        "last_error": str(error)[:500],
        "last_error_at": now,
        "last_error_class": error_class,
        "next_attempt_at": next_at,
    }
    result = coll.update_one(
        {"_id": doc_id, "status": state},
        {
            "$set": {"status": tr.target, "updated_at": now, "attempt": attempt},
            "$unset": {"lease": ""},
        },
    )
    if result.matched_count == 0:
        raise ConcurrentStateError(f"document {doc_id} changed state concurrently")
    audit.log(
        db,
        run_id=run_id,
        entity=collection,
        entity_id=doc_id,
        event=tr.event,
        from_state=tr.source,
        to_state=tr.target,
        detail={"error_class": error_class, "attempt": count},
    )
    return coll.find_one({"_id": doc_id})


def sweep_expired_leases(
    db,
    collection: str,
    machine: Machine,
    *,
    run_id: str | None = None,
    now: datetime | None = None,
    backoff: BackoffFn | None = None,
    limit: int = 500,
) -> list[dict]:
    """Crash recovery: revert WORKING documents with expired leases to their
    predecessor IDLE state, consuming an attempt. Race-safe — the guarded
    update means a concurrent sweeper can never revert a document twice."""
    now = _now(now)
    coll = db[collection]
    query = {"status": {"$in": sorted(working_states(machine))}, "lease.expires_at": {"$lt": now}}
    reverted: list[dict] = []
    for doc in coll.find(query).limit(limit):
        state = doc["status"]
        target = sweep_target(machine, state)
        count = (doc.get("attempt") or {}).get("count", 0) + 1
        attempt = {
            "count": count,
            "last_error": "lease expired before completion (worker crash?)",
            "last_error_at": now,
            "last_error_class": "SWEEP",
            "next_attempt_at": now + timedelta(seconds=(backoff or default_backoff)(count)),
        }
        result = coll.update_one(
            {"_id": doc["_id"], "status": state, "lease.expires_at": {"$lt": now}},
            {
                "$set": {"status": target, "updated_at": now, "attempt": attempt},
                "$unset": {"lease": ""},
            },
        )
        if result.matched_count:
            reverted.append({"_id": doc["_id"], "from": state, "to": target})
            audit.log(
                db,
                run_id=run_id,
                entity=collection,
                entity_id=doc["_id"],
                event="SWEEP_EXPIRED",
                from_state=state,
                to_state=target,
                detail={"attempt": count},
            )
    return reverted


def pause_all(
    db,
    collection: str,
    machine: Machine,
    *,
    extra_filter: dict | None = None,
    run_id: str | None = None,
    now: datetime | None = None,
    limit: int = 1000,
) -> int:
    """Kill switch: move every active document to PAUSED (recording paused_from).
    Machines without a PAUSED role are a no-op. Returns the number paused."""
    if Role.PAUSED not in STATE_ROLES.get(machine, {}).values():
        return 0
    now = _now(now)
    coll = db[collection]
    active = [s for s, r in STATE_ROLES[machine].items() if r in (Role.IDLE, Role.WORKING)]
    query: dict = {"status": {"$in": sorted(active)}}
    if extra_filter:
        query = {"$and": [query, extra_filter]}
    paused = 0
    for doc in coll.find(query).limit(limit):
        state = doc["status"]
        if not can_transition(machine, state, "PAUSE"):
            continue
        paused_from = (
            sweep_target(machine, state)
            if state_role(machine, state) is Role.WORKING
            else state
        )
        result = coll.update_one(
            {"_id": doc["_id"], "status": state},
            {
                "$set": {"status": "PAUSED", "paused_from": paused_from, "updated_at": now},
                "$unset": {"lease": ""},
            },
        )
        if result.matched_count:
            paused += 1
            audit.log(
                db,
                run_id=run_id,
                entity=collection,
                entity_id=doc["_id"],
                event="PAUSE",
                from_state=state,
                to_state="PAUSED",
            )
    return paused


def resume_all(
    db,
    collection: str,
    machine: Machine,
    *,
    extra_filter: dict | None = None,
    run_id: str | None = None,
    now: datetime | None = None,
    limit: int = 1000,
) -> int:
    """Resume PAUSED documents back to the IDLE state they came from."""
    now = _now(now)
    coll = db[collection]
    query: dict = {"status": "PAUSED"}
    if extra_filter:
        query = {"$and": [query, extra_filter]}
    resumed = 0
    for doc in coll.find(query).limit(limit):
        back_to = doc.get("paused_from")
        if back_to is None:
            continue
        try:
            tr = transition_for(machine, "PAUSED", "RESUME", target=back_to)
        except IllegalTransitionError:
            continue  # corrupted paused_from: leave for manual inspection
        result = coll.update_one(
            {"_id": doc["_id"], "status": "PAUSED"},
            {"$set": {"status": tr.target, "updated_at": now}, "$unset": {"paused_from": ""}},
        )
        if result.matched_count:
            resumed += 1
            audit.log(
                db,
                run_id=run_id,
                entity=collection,
                entity_id=doc["_id"],
                event="RESUME",
                from_state="PAUSED",
                to_state=tr.target,
            )
    return resumed


def requeue_dead(
    db,
    collection: str,
    machine: Machine,
    doc_id,
    *,
    run_id: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Operator tool: move a DEAD document back to the start of the pipeline
    with a fresh attempt budget."""
    return transition_doc(
        db,
        collection,
        machine,
        doc_id,
        event="REQUEUE_DEAD",
        patch={"attempt": fresh_attempt()},
        run_id=run_id,
        now=now,
    )
