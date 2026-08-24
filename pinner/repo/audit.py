"""Append-only audit log — WP4 contract (implemented next).

Contract: log(db, run_id, entity, entity_id, event, from_state, to_state,
detail, latency_ms) — fire-and-forget insert into audit_log; never blocks
and never raises into the caller (a broken audit write must not kill a pin).
The 90-day TTL index (WP2) keeps the M0 cluster lean.
"""
