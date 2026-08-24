"""Product repository: the ingest state machine (products collection).

Thin, typed wrappers over the shared engine. The ingest machine is global per
product — moderation runs ONCE per product regardless of how many accounts
eventually pin it (free-tier RPD discipline).
"""

from __future__ import annotations

from datetime import datetime

from pymongo.errors import DuplicateKeyError

from pinner.repo import engine
from pinner.statemachine import Machine

COLLECTION = "products"
MACHINE = Machine.INGEST


class ProductsRepo:
    def __init__(self, db) -> None:
        self.db = db

    def upsert_candidate(
        self,
        source: str,
        source_product_id: str,
        *,
        dedup_hash: str | None = None,
        now: datetime | None = None,
    ) -> tuple[str, dict]:
        """Register a discovered product. Returns ("created"|"exists", doc).
        The unique index (source, source_product_id) makes this idempotent —
        re-discovering a known product is a no-op, never a duplicate."""
        now = engine._now(now)
        doc = {
            "source": source,
            "source_product_id": source_product_id,
            "status": "PENDING_FETCH",
            "dedup_hash": dedup_hash,
            "attempt": engine.fresh_attempt(),
            "first_seen_at": now,
            "last_updated_at": now,
            "created_at": now,
        }
        try:
            self.db[COLLECTION].insert_one(doc)
            return "created", doc
        except DuplicateKeyError:
            return "exists", self.db[COLLECTION].find_one(
                {"source": source, "source_product_id": source_product_id}
            )

    def claim_next_fetch(self, run_id: str, **kwargs) -> dict | None:
        return engine.claim_one(
            self.db, COLLECTION, MACHINE, event="CLAIM_FETCH", run_id=run_id, **kwargs
        )

    def claim_next_moderation(self, run_id: str, **kwargs) -> dict | None:
        return engine.claim_one(
            self.db, COLLECTION, MACHINE, event="CLAIM_MODERATE", run_id=run_id, **kwargs
        )

    def transition(self, doc_id, event: str, **kwargs) -> dict:
        return engine.transition_doc(
            self.db, COLLECTION, MACHINE, doc_id, event=event, **kwargs
        )

    def fail(self, doc_id, *, error: str, **kwargs) -> dict:
        return engine.fail_doc(self.db, COLLECTION, MACHINE, doc_id, error=error, **kwargs)

    def sweep(self, **kwargs) -> list[dict]:
        return engine.sweep_expired_leases(self.db, COLLECTION, MACHINE, **kwargs)

    def requeue_dead(self, doc_id, **kwargs) -> dict:
        return engine.requeue_dead(self.db, COLLECTION, MACHINE, doc_id, **kwargs)
