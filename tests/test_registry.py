"""Exhaustive state-machine tests (WP3).

The expected matrices below are written INDEPENDENTLY from the registry
implementation, straight from the architecture spec (§3 transition tables).
The parametrized walk visits every (state, event) pair of both machines and
asserts the registry agrees with the spec — legal pairs resolve to the exact
target, and every other pair is rejected. A typo anywhere on either side
fails these tests.

Counts locked by the spec:
    publication: 33 transitions (4 CLAIM, 4 ADVANCE, 4 RETRY, 4 POISON,
                4 SWEEP, 8 PAUSE, 4 RESUME, 1 OPS)
    ingest:      13 transitions (2 CLAIM, 3 ADVANCE, 2 RETRY, 2 POISON,
                2 SWEEP, 0 PAUSE, 0 RESUME, 2 OPS)
"""

from __future__ import annotations

import pytest

from pinner.statemachine import (
    EVENTS,
    INGEST_TRANSITIONS,
    PUBLICATION_TRANSITIONS,
    AmbiguousTransitionError,
    IllegalTransitionError,
    Machine,
    PinState,
    ProductState,
    Role,
    can_transition,
    check_invariants,
    claimable_states,
    legal_events,
    state_role,
    sweep_target,
    terminal_states,
    transition_for,
    working_states,
)

# --- Spec matrices: {state: {event: target | frozenset(targets)}} ----------------

EXPECTED_PUB: dict[str, dict[str, object]] = {
    "QUEUED": {"CLAIM_ENRICH": "ENRICHING", "PAUSE": "PAUSED"},
    "ENRICHING": {
        "ENRICH_OK": "ENRICHED",
        "ENRICH_FAIL": "QUEUED",
        "ENRICH_POISON": "DEAD",
        "SWEEP_EXPIRED": "QUEUED",
        "PAUSE": "PAUSED",
    },
    "ENRICHED": {"CLAIM_BRIDGE": "BRIDGING", "PAUSE": "PAUSED"},
    "BRIDGING": {
        "BRIDGE_OK": "BRIDGED",
        "BRIDGE_FAIL": "ENRICHED",
        "BRIDGE_POISON": "DEAD",
        "SWEEP_EXPIRED": "ENRICHED",
        "PAUSE": "PAUSED",
    },
    "BRIDGED": {"CLAIM_PIN": "PINNING", "PAUSE": "PAUSED"},
    "PINNING": {
        "PIN_OK": "PINNED",
        "PIN_FAIL": "BRIDGED",
        "PIN_POISON": "DEAD",
        "SWEEP_EXPIRED": "BRIDGED",
        "PAUSE": "PAUSED",
    },
    "PINNED": {"CLAIM_VERIFY": "VERIFYING", "PAUSE": "PAUSED"},
    "VERIFYING": {
        "VERIFY_OK": "VERIFIED",
        "VERIFY_FAIL": "PINNED",
        "VERIFY_POISON": "DEAD",
        "SWEEP_EXPIRED": "PINNED",
        "PAUSE": "PAUSED",
    },
    "VERIFIED": {},
    "PAUSED": {"RESUME": frozenset({"QUEUED", "ENRICHED", "BRIDGED", "PINNED"})},
    "DEAD": {"REQUEUE_DEAD": "QUEUED"},
}

EXPECTED_INGEST: dict[str, dict[str, object]] = {
    "PENDING_FETCH": {"CLAIM_FETCH": "FETCHING"},
    "FETCHING": {
        "FETCH_OK": "FETCHED",
        "FETCH_FAIL": "PENDING_FETCH",
        "FETCH_POISON": "DEAD_FETCH",
        "SWEEP_EXPIRED": "PENDING_FETCH",
    },
    "FETCHED": {"CLAIM_MODERATE": "MODERATING"},
    "MODERATING": {
        "MODERATE_APPROVE": "APPROVED",
        "MODERATE_REJECT": "REJECTED",
        "MODERATE_FAIL": "FETCHED",
        "MODERATE_POISON": "DEAD_MODERATE",
        "SWEEP_EXPIRED": "FETCHED",
    },
    "APPROVED": {},
    "REJECTED": {},
    "DEAD_FETCH": {"REQUEUE_DEAD": "PENDING_FETCH"},
    "DEAD_MODERATE": {"REQUEUE_DEAD": "FETCHED"},
}

PUB_EVENTS = EVENTS[Machine.PUBLICATION]
INGEST_EVENTS = EVENTS[Machine.INGEST]

# --- Structural invariants --------------------------------------------------------


def test_registry_self_invariants():
    """The registry must satisfy every structural safety rule."""
    assert check_invariants() == []


def test_transition_counts_match_spec():
    assert len(PUBLICATION_TRANSITIONS) == 33
    assert len(INGEST_TRANSITIONS) == 13


def test_no_duplicate_exact_rows():
    rows = PUBLICATION_TRANSITIONS + INGEST_TRANSITIONS
    keys = [(t.machine, t.event, t.source, t.target) for t in rows]
    assert len(keys) == len(set(keys))


def test_event_universe_matches_spec():
    assert set(PUB_EVENTS) == {
        "CLAIM_ENRICH", "ENRICH_OK", "ENRICH_FAIL", "ENRICH_POISON",
        "CLAIM_BRIDGE", "BRIDGE_OK", "BRIDGE_FAIL", "BRIDGE_POISON",
        "CLAIM_PIN", "PIN_OK", "PIN_FAIL", "PIN_POISON",
        "CLAIM_VERIFY", "VERIFY_OK", "VERIFY_FAIL", "VERIFY_POISON",
        "SWEEP_EXPIRED", "PAUSE", "RESUME", "REQUEUE_DEAD",
    }
    assert set(INGEST_EVENTS) == {
        "CLAIM_FETCH", "FETCH_OK", "FETCH_FAIL", "FETCH_POISON",
        "CLAIM_MODERATE", "MODERATE_APPROVE", "MODERATE_REJECT",
        "MODERATE_FAIL", "MODERATE_POISON", "SWEEP_EXPIRED", "REQUEUE_DEAD",
    }


# --- Exhaustive (state x event) matrices -------------------------------------------


@pytest.mark.parametrize("state", [s.value for s in PinState])
def test_publication_matrix_exhaustive(state):
    expected = EXPECTED_PUB.get(state, {})
    for event in PUB_EVENTS:
        if event not in expected:
            assert not can_transition(Machine.PUBLICATION, state, event)
            with pytest.raises(IllegalTransitionError):
                transition_for(Machine.PUBLICATION, state, event)
            continue
        target = expected[event]
        if isinstance(target, frozenset):
            assert can_transition(Machine.PUBLICATION, state, event)
            for tgt in target:
                assert transition_for(
                    Machine.PUBLICATION, state, event, target=tgt
                ).target == tgt
            with pytest.raises(AmbiguousTransitionError):
                transition_for(Machine.PUBLICATION, state, event)
        else:
            assert transition_for(Machine.PUBLICATION, state, event).target == target


@pytest.mark.parametrize("state", [s.value for s in ProductState])
def test_ingest_matrix_exhaustive(state):
    expected = EXPECTED_INGEST.get(state, {})
    for event in INGEST_EVENTS:
        if event not in expected:
            assert not can_transition(Machine.INGEST, state, event)
            with pytest.raises(IllegalTransitionError):
                transition_for(Machine.INGEST, state, event)
        else:
            assert transition_for(Machine.INGEST, state, event).target == expected[event]


def test_matrix_covers_every_spec_row():
    """Every row in the spec dicts exists in the registry (guards against
    the registry silently omitting a spec transition)."""
    for machine, expected in (
        (Machine.PUBLICATION, EXPECTED_PUB),
        (Machine.INGEST, EXPECTED_INGEST),
    ):
        for state, events in expected.items():
            for event, target in events.items():
                if isinstance(target, frozenset):
                    for tgt in target:
                        assert can_transition(machine, state, event, target=tgt)
                else:
                    assert can_transition(machine, state, event)


# --- Roles, claims, sweeps ------------------------------------------------------------


def test_publication_roles():
    assert claimable_states(Machine.PUBLICATION) == frozenset(
        {"QUEUED", "ENRICHED", "BRIDGED", "PINNED"}
    )
    assert working_states(Machine.PUBLICATION) == frozenset(
        {"ENRICHING", "BRIDGING", "PINNING", "VERIFYING"}
    )
    assert terminal_states(Machine.PUBLICATION) == frozenset({"VERIFIED", "DEAD"})
    assert state_role(Machine.PUBLICATION, "PAUSED") is Role.PAUSED


def test_ingest_roles():
    assert claimable_states(Machine.INGEST) == frozenset({"PENDING_FETCH", "FETCHED"})
    assert working_states(Machine.INGEST) == frozenset({"FETCHING", "MODERATING"})
    assert terminal_states(Machine.INGEST) == frozenset(
        {"APPROVED", "REJECTED", "DEAD_FETCH", "DEAD_MODERATE"}
    )


def test_sweep_targets_return_to_predecessor_idle_state():
    assert sweep_target(Machine.PUBLICATION, "ENRICHING") == "QUEUED"
    assert sweep_target(Machine.PUBLICATION, "BRIDGING") == "ENRICHED"
    assert sweep_target(Machine.PUBLICATION, "PINNING") == "BRIDGED"
    assert sweep_target(Machine.PUBLICATION, "VERIFYING") == "PINNED"
    assert sweep_target(Machine.INGEST, "FETCHING") == "PENDING_FETCH"
    assert sweep_target(Machine.INGEST, "MODERATING") == "FETCHED"


def test_sweep_rejects_non_working_state():
    with pytest.raises(ValueError):
        sweep_target(Machine.PUBLICATION, "QUEUED")


def test_legal_events_examples():
    assert legal_events(Machine.PUBLICATION, "QUEUED") == ("CLAIM_ENRICH", "PAUSE")
    assert legal_events(Machine.PUBLICATION, "VERIFIED") == ()
    assert set(legal_events(Machine.PUBLICATION, "PAUSED")) == {"RESUME"}


def test_unknown_inputs_fail_loudly():
    with pytest.raises(ValueError):
        transition_for("does-not-exist", "QUEUED", "CLAIM_ENRICH")
    with pytest.raises(IllegalTransitionError):
        transition_for(Machine.PUBLICATION, "NOT_A_STATE", "CLAIM_ENRICH")
    with pytest.raises(ValueError):
        state_role(Machine.PUBLICATION, "NOT_A_STATE")
