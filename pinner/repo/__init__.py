"""Repo layer: Mongo-backed state repositories.

WP2: mongo.py — connectivity + idempotent index migrations.
WP4: engine.py — shared concurrency machinery (atomic claims + leases, guarded
transitions, failure handling, sweeps, pause/resume); pins.py / products.py —
thin typed wrappers; audit.py — append-only, fire-and-forget audit trail.
"""
