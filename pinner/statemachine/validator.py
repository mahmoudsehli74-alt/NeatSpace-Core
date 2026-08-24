"""Pure validator over the transition registry.

The orchestrator NEVER decides state changes ad hoc: it emits events and calls
``transition_for``. If a (machine, state, event) pair is not in the registry,
the transition is illegal and this module raises — before any external side
effect can happen.
"""

from __future__ import annotations

from pinner.statemachine.registry import (
    _LOOKUP,
    STATE_ROLES,
    TRANSITIONS,
    Kind,
    Machine,
    Role,
    Transition,
)


class IllegalTransitionError(Exception):
    """Raised when an event is not legal from the current state."""

    def __init__(self, machine: Machine | str, source: str, event: str) -> None:
        self.machine = str(machine)
        self.source = source
        self.event = event
        super().__init__(f"illegal transition: {machine} state={source!r} event={event!r}")


class AmbiguousTransitionError(IllegalTransitionError):
    """Raised when an event has multiple targets and no explicit target was given."""

    def __init__(self, machine: Machine | str, source: str, event: str) -> None:
        super().__init__(machine, source, event)
        self.message = (
            f"ambiguous transition: {machine} state={source!r} event={event!r}; pass target="
        )


def _machine(machine: Machine | str) -> Machine:
    try:
        return Machine(machine)
    except ValueError as exc:
        raise ValueError(f"unknown machine: {machine!r}") from exc


def transitions_from(machine: Machine | str, source: str, event: str) -> tuple[Transition, ...]:
    """All registered transitions for (machine, state, event). Empty tuple if none."""
    return _LOOKUP.get((_machine(machine), source, event), ())


def can_transition(
    machine: Machine | str, source: str, event: str, *, target: str | None = None
) -> bool:
    """True iff the event is legal from the state (optionally to a specific target)."""
    rows = transitions_from(machine, source, event)
    if not rows:
        return False
    if target is None:
        return True
    return any(row.target == target for row in rows)


def transition_for(
    machine: Machine | str, source: str, event: str, *, target: str | None = None
) -> Transition:
    """Resolve exactly one transition or raise IllegalTransitionError.

    Pass ``target`` for multi-target events (RESUME); omit it elsewhere.
    """
    rows = transitions_from(machine, source, event)
    if not rows:
        raise IllegalTransitionError(machine, source, event)
    if target is not None:
        for row in rows:
            if row.target == target:
                return row
        raise IllegalTransitionError(machine, source, event)
    if len(rows) > 1:
        raise AmbiguousTransitionError(machine, source, event)
    return rows[0]


def legal_events(machine: Machine | str, source: str) -> tuple[str, ...]:
    """Sorted, de-duplicated events legal from the given state."""
    m = _machine(machine)
    return tuple(
        sorted({row.event for row in TRANSITIONS if row.machine is m and row.source == source})
    )


def state_role(machine: Machine | str, state: str) -> Role:
    """Role (IDLE/WORKING/PAUSED/TERMINAL) of a state in a machine."""
    m = _machine(machine)
    try:
        return STATE_ROLES[m][state]
    except KeyError as exc:
        raise ValueError(f"unknown state {state!r} in machine {m.value!r}") from exc


def states_with_role(machine: Machine | str, role: Role) -> frozenset[str]:
    m = _machine(machine)
    return frozenset(s for s, r in STATE_ROLES[m].items() if r is role)


def claimable_states(machine: Machine | str) -> frozenset[str]:
    return states_with_role(machine, Role.IDLE)


def working_states(machine: Machine | str) -> frozenset[str]:
    return states_with_role(machine, Role.WORKING)


def terminal_states(machine: Machine | str) -> frozenset[str]:
    return states_with_role(machine, Role.TERMINAL)


def transition_of_kind(machine: Machine | str, source: str, kind: Kind) -> Transition:
    """The unique transition of a given KIND out of a state (e.g. the RETRY or
    POISON path from a WORKING state). Registry invariants guarantee exactly
    one exists for WORKING states; raises IllegalTransitionError if none and
    ValueError if ambiguous."""
    m = _machine(machine)
    rows = [
        t
        for t in TRANSITIONS
        if t.machine is m and t.source == source and t.kind is Kind(kind)
    ]
    if not rows:
        raise IllegalTransitionError(m, source, f"<kind:{kind}>")
    if len(rows) > 1:
        raise ValueError(f"ambiguous {kind} transitions from {source!r} in {m}")
    return rows[0]


def sweep_target(machine: Machine | str, working_state: str) -> str:
    """Where an expired lease returns a WORKING state to (its predecessor IDLE state)."""
    rows = transitions_from(machine, working_state, "SWEEP_EXPIRED")
    sweeps = [row for row in rows if row.kind is Kind.SWEEP]
    if len(sweeps) != 1:
        raise ValueError(
            f"expected exactly one SWEEP_EXPIRED transition from {working_state!r}, "
            f"found {len(sweeps)}"
        )
    return sweeps[0].target


def check_invariants() -> list[str]:
    """Structural self-check of the registry. Must return [] — asserted in tests.

    These rules encode the safety thesis of the whole design:
      * forward-only flow (only OPS may exit TERMINAL states),
      * WORKING states are entered only via CLAIM and always have exactly one
        sweep path, at least one retry path, and a poison path,
      * pause/resume are the only ways in/out of PAUSED.
    """
    problems: list[str] = []
    seen: set[tuple] = set()

    for tr in TRANSITIONS:
        roles = STATE_ROLES.get(tr.machine, {})
        if tr.source not in roles:
            problems.append(f"{tr.machine}: unknown source state {tr.source!r}")
            continue
        if tr.target not in roles:
            problems.append(f"{tr.machine}: unknown target state {tr.target!r}")
            continue
        key = (tr.machine, tr.event, tr.source, tr.target)
        if key in seen:
            problems.append(f"duplicate transition row: {key}")
        seen.add(key)

        src_role = roles[tr.source]
        dst_role = roles[tr.target]

        if tr.kind is Kind.CLAIM and not (src_role is Role.IDLE and dst_role is Role.WORKING):
            problems.append(f"CLAIM must be IDLE->WORKING: {tr}")
        if tr.kind is Kind.ADVANCE and not (
            src_role is Role.WORKING and dst_role in (Role.IDLE, Role.TERMINAL)
        ):
            problems.append(f"ADVANCE must be WORKING->IDLE|TERMINAL: {tr}")
        if tr.kind in (Kind.RETRY, Kind.SWEEP) and not (
            src_role is Role.WORKING and dst_role is Role.IDLE
        ):
            problems.append(f"RETRY/SWEEP must be WORKING->IDLE: {tr}")
        if tr.kind is Kind.POISON and not (
            src_role is Role.WORKING and dst_role is Role.TERMINAL
        ):
            problems.append(f"POISON must be WORKING->TERMINAL: {tr}")
        if tr.kind is Kind.PAUSE and not (
            src_role in (Role.IDLE, Role.WORKING) and dst_role is Role.PAUSED
        ):
            problems.append(f"PAUSE must be active->PAUSED: {tr}")
        if tr.kind is Kind.RESUME and not (
            src_role is Role.PAUSED and dst_role is Role.IDLE
        ):
            problems.append(f"RESUME must be PAUSED->IDLE: {tr}")
        if src_role is Role.TERMINAL and tr.kind is not Kind.OPS:
            problems.append(f"non-OPS exit from TERMINAL state: {tr}")
        if dst_role is Role.WORKING and tr.kind is not Kind.CLAIM:
            problems.append(f"WORKING state entered by non-CLAIM event: {tr}")

    for machine, roles in STATE_ROLES.items():
        for state, role in roles.items():
            outgoing = [tr for tr in TRANSITIONS if tr.machine is machine and tr.source == state]
            if not outgoing and role not in (Role.TERMINAL,):
                problems.append(f"{machine}: state {state!r} ({role}) has no outgoing transitions")
            if role is Role.IDLE:
                claims = [tr for tr in outgoing if tr.kind is Kind.CLAIM]
                if not claims:
                    problems.append(f"{machine}: IDLE state {state!r} has no CLAIM transition")
            if role is Role.WORKING:
                sweeps = [
                    tr for tr in outgoing if tr.event == "SWEEP_EXPIRED" and tr.kind is Kind.SWEEP
                ]
                retries = [tr for tr in outgoing if tr.kind is Kind.RETRY]
                poisons = [tr for tr in outgoing if tr.kind is Kind.POISON]
                if len(sweeps) != 1:
                    problems.append(
                        f"{machine}: WORKING state {state!r} needs exactly 1 SWEEP, "
                        f"has {len(sweeps)}"
                    )
                if not retries:
                    problems.append(f"{machine}: WORKING state {state!r} has no RETRY path")
                if not poisons:
                    problems.append(f"{machine}: WORKING state {state!r} has no POISON path")
            if role is Role.PAUSED:
                entries = [tr for tr in TRANSITIONS if tr.machine is machine and tr.target == state]
                if not all(tr.kind is Kind.PAUSE for tr in entries):
                    problems.append(f"{machine}: PAUSED entered by non-PAUSE event")

    for machine in Machine:
        declared = set(STATE_ROLES[machine])
        referenced = {
            tr.source for tr in TRANSITIONS if tr.machine is machine
        } | {tr.target for tr in TRANSITIONS if tr.machine is machine}
        orphaned = declared - referenced
        if orphaned:
            problems.append(f"{machine}: states declared but never referenced: {sorted(orphaned)}")

    return problems
