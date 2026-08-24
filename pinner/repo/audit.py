"""Append-only audit log.

Every state transition, claim, sweep, and failure lands here, which makes crash
forensics a query instead of a mystery and feeds the Telegram digest (Phase 3).
Fire-and-forget by contract: a broken audit write must NEVER kill a pin —
``log`` swallows storage errors and returns False.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pymongo.errors import PyMongoError


def _utcnow() -> datetime:
    # Local twin of engine.utcnow — keeps audit import-free of engine
    # (engine imports audit; the reverse edge would be circular).
    return datetime.now(timezone.utc).replace(tzinfo=None)


def log(
    db,
    *,
    run_id: str | None,
    entity: str,
    entity_id: Any,
    event: str,
    from_state: str | None = None,
    to_state: str | None = None,
    detail: dict | None = None,
    now: datetime | None = None,
) -> bool:
    """Insert one audit entry. Returns False (never raises) on storage failure."""
    try:
        db["audit_log"].insert_one(
            {
                "ts": now if now is not None else _utcnow(),
                "run_id": run_id,
                "entity": entity,
                "entity_id": entity_id,
                "event": event,
                "from_state": from_state,
                "to_state": to_state,
                "detail": detail,
            }
        )
        return True
    except PyMongoError:
        return False


def recent(db, *, limit: int = 50, entity_id: Any | None = None) -> list[dict]:
    """Newest-first audit entries, optionally filtered by entity."""
    query: dict = {"entity_id": entity_id} if entity_id is not None else {}
    return list(db["audit_log"].find(query).sort("ts", -1).limit(limit))
