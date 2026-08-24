"""Runner entrypoint — Phase 3 contract.

Loop skeleton (thin consumer of WP4-WP7, deterministic end to end):
    1. open run record; sweep expired leases (reconciler-aware)
    2. per ACTIVE/WARMUP account, while governor.allows(): claim next pin/product
    3. walk stages via the state machine registry; every external side effect
       behind intent-write + reconcile
    4. write run stats; send Telegram digest; exit (run-to-completion, < 25 min)
"""
