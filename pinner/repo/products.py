"""Product repository — WP4 contract (implemented next).

Contract to implement on top of pinner.repo.mongo:

    claim_next_fetch(db, run_id, lease_ttl) -> dict | None
        Atomic findOneAndUpdate on {status: PENDING_FETCH,
        attempt.next_attempt_at <= now, lease expired-or-null} setting
        status=FETCHING, lease={owner: run_id, expires_at}.

    claim_next_moderation(db, run_id, lease_ttl) -> dict | None
        Same pattern for {status: FETCHED} -> MODERATING.

    transition(db, doc_id, event, patch) -> dict
        Optimistic: updateOne({_id, status: expected_source}) using the
        registry-resolved source; IllegalTransitionError otherwise.

    record_failure / requeue_dead / sweep_expired_leases — see spec §3.3.

All writes also append to audit_log (pinner.repo.audit, WP4).
"""
