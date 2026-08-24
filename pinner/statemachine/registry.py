"""Declarative state machine registry — the single source of truth for control flow.

Two machines are defined here as pure data:

Publication (drives the ``pins`` collection)::

    QUEUED ──CLAIM_ENRICH──▶ ENRICHING ──ENRICH_OK──▶ ENRICHED
       ◀──ENRICH_FAIL/SWEEP─┘
    ENRICHED ──CLAIM_BRIDGE──▶ BRIDGING ──BRIDGE_OK──▶ BRIDGED
    BRIDGED ──CLAIM_PIN──▶ PINNING ──PIN_OK──▶ PINNED
    PINNED ──CLAIM_VERIFY──▶ VERIFYING ──VERIFY_OK──▶ VERIFIED (terminal success)

    working ──*_POISON──▶ DEAD (terminal failure, ops-requeueable)
    any active state ──PAUSE──▶ PAUSED ──RESUME──▶ predecessor idle state
    working ──SWEEP_EXPIRED──▶ predecessor idle state (lease-expiry recovery)

Ingest (drives the ``products`` collection)::

    PENDING_FETCH ──CLAIM_FETCH──▶ FETCHING ──FETCH_OK──▶ FETCHED
    FETCHED ──CLAIM_MODERATE──▶ MODERATING ──▶ APPROVED | REJECTED (terminals)
    failures: FETCH_FAIL/MODERATE_FAIL (retry), *_POISON (dead), SWEEP_EXPIRED (lease)

Semantics:
    * CLAIM transitions are the only way INTO a WORKING state. Claiming is an
      atomic findOneAndUpdate in the repo layer that also acquires the lease.
    * RETRY/SWEEP transitions return a WORKING state to its predecessor IDLE
      state and bump attempts / next_attempt_at (repo layer).
    * POISON means permanent failure (e.g., LLM output failed schema validation
      three times) — retrying would only burn quota.
    * OPS transitions (REQUEUE_DEAD) are manual operator actions, never taken
      by the autonomous runner.

This module is deliberately PURE (no I/O): every rule of control flow lives
here as data, so the entire control surface is exhaustively unit-testable
(tests/test_registry.py) and machine-checkable (validator.check_invariants).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Machine(StrEnum):
    PUBLICATION = "publication"
    INGEST = "ingest"


class Role(StrEnum):
    IDLE = "IDLE"  # claimable; no side effect in flight
    WORKING = "WORKING"  # lease held; an external side effect may be in flight
    PAUSED = "PAUSED"  # kill-switched; idle but not claimable until resumed
    TERMINAL = "TERMINAL"  # done (success or permanent failure)


class Kind(StrEnum):
    CLAIM = "CLAIM"  # IDLE -> WORKING (atomic claim + lease in repo layer)
    ADVANCE = "ADVANCE"  # WORKING -> next stage (or terminal success)
    RETRY = "RETRY"  # WORKING -> predecessor IDLE (transient failure, backoff)
    POISON = "POISON"  # WORKING -> TERMINAL failure (permanent)
    SWEEP = "SWEEP"  # WORKING -> predecessor IDLE (expired lease recovery)
    PAUSE = "PAUSE"  # active -> PAUSED (kill switch)
    RESUME = "RESUME"  # PAUSED -> predecessor IDLE (uses doc.paused_from)
    OPS = "OPS"  # manual operator action (e.g., requeue a DEAD doc)


class PinState(StrEnum):
    QUEUED = "QUEUED"
    ENRICHING = "ENRICHING"
    ENRICHED = "ENRICHED"
    BRIDGING = "BRIDGING"
    BRIDGED = "BRIDGED"
    PINNING = "PINNING"
    PINNED = "PINNED"
    VERIFYING = "VERIFYING"
    VERIFIED = "VERIFIED"
    PAUSED = "PAUSED"
    DEAD = "DEAD"


class ProductState(StrEnum):
    PENDING_FETCH = "PENDING_FETCH"
    FETCHING = "FETCHING"
    FETCHED = "FETCHED"
    MODERATING = "MODERATING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    DEAD_FETCH = "DEAD_FETCH"
    DEAD_MODERATE = "DEAD_MODERATE"


@dataclass(frozen=True)
class Transition:
    machine: Machine
    event: str
    source: str
    target: str
    kind: Kind


def _t(machine: Machine, event: str, source: str, target: str, kind: Kind) -> Transition:
    return Transition(machine=machine, event=event, source=source, target=target, kind=kind)


_M = Machine.PUBLICATION
_P = PinState
_K = Kind

_ACTIVE_PIN_STATES = (
    _P.QUEUED,
    _P.ENRICHING,
    _P.ENRICHED,
    _P.BRIDGING,
    _P.BRIDGED,
    _P.PINNING,
    _P.PINNED,
    _P.VERIFYING,
)

# fmt: off
PUBLICATION_TRANSITIONS: tuple[Transition, ...] = (
    # --- enrichment stage ---
    _t(_M, "CLAIM_ENRICH", _P.QUEUED, _P.ENRICHING, _K.CLAIM),
    _t(_M, "ENRICH_OK", _P.ENRICHING, _P.ENRICHED, _K.ADVANCE),
    _t(_M, "ENRICH_FAIL", _P.ENRICHING, _P.QUEUED, _K.RETRY),
    _t(_M, "ENRICH_POISON", _P.ENRICHING, _P.DEAD, _K.POISON),
    _t(_M, "SWEEP_EXPIRED", _P.ENRICHING, _P.QUEUED, _K.SWEEP),
    # --- bridge stage (GitHub/Pages commit) ---
    _t(_M, "CLAIM_BRIDGE", _P.ENRICHED, _P.BRIDGING, _K.CLAIM),
    _t(_M, "BRIDGE_OK", _P.BRIDGING, _P.BRIDGED, _K.ADVANCE),
    _t(_M, "BRIDGE_FAIL", _P.BRIDGING, _P.ENRICHED, _K.RETRY),
    _t(_M, "BRIDGE_POISON", _P.BRIDGING, _P.DEAD, _K.POISON),
    _t(_M, "SWEEP_EXPIRED", _P.BRIDGING, _P.ENRICHED, _K.SWEEP),
    # --- pin stage (deploy check + Pinterest create; reconciler runs on recovery) ---
    _t(_M, "CLAIM_PIN", _P.BRIDGED, _P.PINNING, _K.CLAIM),
    _t(_M, "PIN_OK", _P.PINNING, _P.PINNED, _K.ADVANCE),
    _t(_M, "PIN_FAIL", _P.PINNING, _P.BRIDGED, _K.RETRY),
    _t(_M, "PIN_POISON", _P.PINNING, _P.DEAD, _K.POISON),
    _t(_M, "SWEEP_EXPIRED", _P.PINNING, _P.BRIDGED, _K.SWEEP),
    # --- verification stage ---
    _t(_M, "CLAIM_VERIFY", _P.PINNED, _P.VERIFYING, _K.CLAIM),
    _t(_M, "VERIFY_OK", _P.VERIFYING, _P.VERIFIED, _K.ADVANCE),
    _t(_M, "VERIFY_FAIL", _P.VERIFYING, _P.PINNED, _K.RETRY),
    _t(_M, "VERIFY_POISON", _P.VERIFYING, _P.DEAD, _K.POISON),
    _t(_M, "SWEEP_EXPIRED", _P.VERIFYING, _P.PINNED, _K.SWEEP),
    # --- kill switch ---
    *(_t(_M, "PAUSE", state, _P.PAUSED, _K.PAUSE) for state in _ACTIVE_PIN_STATES),
    # --- resume: repo layer picks the target from doc.paused_from ---
    _t(_M, "RESUME", _P.PAUSED, _P.QUEUED, _K.RESUME),
    _t(_M, "RESUME", _P.PAUSED, _P.ENRICHED, _K.RESUME),
    _t(_M, "RESUME", _P.PAUSED, _P.BRIDGED, _K.RESUME),
    _t(_M, "RESUME", _P.PAUSED, _P.PINNED, _K.RESUME),
    # --- operator tools ---
    _t(_M, "REQUEUE_DEAD", _P.DEAD, _P.QUEUED, _K.OPS),
)
# fmt: on

_M = Machine.INGEST
_S = ProductState

# fmt: off
INGEST_TRANSITIONS: tuple[Transition, ...] = (
    # --- fetch stage (adapter) ---
    _t(_M, "CLAIM_FETCH", _S.PENDING_FETCH, _S.FETCHING, _K.CLAIM),
    _t(_M, "FETCH_OK", _S.FETCHING, _S.FETCHED, _K.ADVANCE),
    _t(_M, "FETCH_FAIL", _S.FETCHING, _S.PENDING_FETCH, _K.RETRY),
    _t(_M, "FETCH_POISON", _S.FETCHING, _S.DEAD_FETCH, _K.POISON),
    _t(_M, "SWEEP_EXPIRED", _S.FETCHING, _S.PENDING_FETCH, _K.SWEEP),
    # --- moderation stage (global, once per product) ---
    _t(_M, "CLAIM_MODERATE", _S.FETCHED, _S.MODERATING, _K.CLAIM),
    _t(_M, "MODERATE_APPROVE", _S.MODERATING, _S.APPROVED, _K.ADVANCE),
    _t(_M, "MODERATE_REJECT", _S.MODERATING, _S.REJECTED, _K.ADVANCE),
    _t(_M, "MODERATE_FAIL", _S.MODERATING, _S.FETCHED, _K.RETRY),
    _t(_M, "MODERATE_POISON", _S.MODERATING, _S.DEAD_MODERATE, _K.POISON),
    _t(_M, "SWEEP_EXPIRED", _S.MODERATING, _S.FETCHED, _K.SWEEP),
    # --- operator tools ---
    _t(_M, "REQUEUE_DEAD", _S.DEAD_FETCH, _S.PENDING_FETCH, _K.OPS),
    _t(_M, "REQUEUE_DEAD", _S.DEAD_MODERATE, _S.FETCHED, _K.OPS),
)
# fmt: on

TRANSITIONS: tuple[Transition, ...] = PUBLICATION_TRANSITIONS + INGEST_TRANSITIONS

STATE_ROLES: dict[Machine, dict[str, Role]] = {
    Machine.PUBLICATION: {
        PinState.QUEUED: Role.IDLE,
        PinState.ENRICHING: Role.WORKING,
        PinState.ENRICHED: Role.IDLE,
        PinState.BRIDGING: Role.WORKING,
        PinState.BRIDGED: Role.IDLE,
        PinState.PINNING: Role.WORKING,
        PinState.PINNED: Role.IDLE,
        PinState.VERIFYING: Role.WORKING,
        PinState.VERIFIED: Role.TERMINAL,
        PinState.PAUSED: Role.PAUSED,
        PinState.DEAD: Role.TERMINAL,
    },
    Machine.INGEST: {
        ProductState.PENDING_FETCH: Role.IDLE,
        ProductState.FETCHING: Role.WORKING,
        ProductState.FETCHED: Role.IDLE,
        ProductState.MODERATING: Role.WORKING,
        ProductState.APPROVED: Role.TERMINAL,
        ProductState.REJECTED: Role.TERMINAL,
        ProductState.DEAD_FETCH: Role.TERMINAL,
        ProductState.DEAD_MODERATE: Role.TERMINAL,
    },
}


def _index() -> dict[tuple[Machine, str, str], tuple[Transition, ...]]:
    """Group transitions by (machine, source, event). One event may map to several
    targets (only RESUME does); every other (machine, source, event) key is unique."""
    grouped: dict[tuple[Machine, str, str], list[Transition]] = {}
    for tr in TRANSITIONS:
        grouped.setdefault((tr.machine, tr.source, tr.event), []).append(tr)
    return {key: tuple(rows) for key, rows in grouped.items()}


_LOOKUP = _index()

EVENTS: dict[Machine, tuple[str, ...]] = {
    machine: tuple(sorted({tr.event for tr in TRANSITIONS if tr.machine is machine}))
    for machine in Machine
}
