"""Pin repository — WP4 contract (implemented next).

Contract to implement on top of pinner.repo.mongo:

    claim(db, account_id, run_id, lease_ttl) -> dict | None
        Claim one QUEUED/ENRICHED/BRIDGED/PINNED pin for this account
        (atomic findOneAndUpdate + lease).

    transition(db, doc_id, event, patch) -> dict
        Optimistic-concurrency transition via the registry.

    record_failure / requeue_dead / pause_account_pins / resume_account_pins
    sweep_expired_leases(run_id) — reverts WORKING states with expired leases
    to their sweep_target, with Reconciler hooks before re-executing any
    side-effecting stage (Phase 2).

Every transition writes audit_log. Unique indexes ux_account_product and
ux_pin_id (WP2) are the double-publish backstop beneath all of this.
"""
