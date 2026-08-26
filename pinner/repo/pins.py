"""Pin repository: the publication state machine (pins collection).

One document per (account, product) — the unique index ux_account_product is
the double-publish backstop. Claims are account-scoped and "finisher-first":
in-flight pins (ENRICHED/BRIDGED/PINNED) advance before brand-new work
(QUEUED) starts, so interrupted pins complete quickly.
"""

from __future__ import annotations

from pinner.repo import engine
from pinner.statemachine import Machine

COLLECTION = "pins"
MACHINE = Machine.PUBLICATION

# Finisher-first claim order: advance in-flight pins before starting new ones.
_CLAIM_ORDER = ("CLAIM_BRIDGE", "CLAIM_PIN", "CLAIM_VERIFY", "CLAIM_ENRICH")


class PinsRepo:
    def __init__(self, db) -> None:
        self.db = db

    def claim_next_for_account(
        self, account_id: str, run_id: str, *, events: tuple[str, ...] | None = None, **kwargs
    ) -> tuple[dict, str] | None:
        """Claim the most urgent pin for an account. Returns (doc, event) so
        the caller knows which stage it just entered, or None. ``events``
        overrides the claim order (dry-run passes an order without CLAIM_PIN)."""
        for event in events or _CLAIM_ORDER:
            doc = engine.claim_one(
                self.db,
                COLLECTION,
                MACHINE,
                event=event,
                run_id=run_id,
                extra_filter={"account_id": account_id},
                **kwargs,
            )
            if doc is not None:
                return doc, event
        return None

    def transition(self, doc_id, event: str, **kwargs) -> dict:
        return engine.transition_doc(
            self.db, COLLECTION, MACHINE, doc_id, event=event, **kwargs
        )

    def fail(self, doc_id, *, error: str, **kwargs) -> dict:
        return engine.fail_doc(self.db, COLLECTION, MACHINE, doc_id, error=error, **kwargs)

    def sweep(self, **kwargs) -> list[dict]:
        return engine.sweep_expired_leases(self.db, COLLECTION, MACHINE, **kwargs)

    def pause_account(self, account_id: str, **kwargs) -> int:
        """Kill switch for one account: every active pin goes to PAUSED."""
        return engine.pause_all(
            self.db, COLLECTION, MACHINE, extra_filter={"account_id": account_id}, **kwargs
        )

    def resume_account(self, account_id: str, **kwargs) -> int:
        return engine.resume_all(
            self.db, COLLECTION, MACHINE, extra_filter={"account_id": account_id}, **kwargs
        )

    def requeue_dead(self, doc_id, **kwargs) -> dict:
        return engine.requeue_dead(self.db, COLLECTION, MACHINE, doc_id, **kwargs)
