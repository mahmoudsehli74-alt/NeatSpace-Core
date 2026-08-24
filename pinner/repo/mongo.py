"""Mongo connectivity + idempotent index migrations (WP2).

Safety-critical unique indexes — the double-publish backstop. Even buggy code
cannot create two pins for the same (account, product) pair or two products
with the same (source, source_product_id); the database physically rejects it.
Treat any change here with the same gravity as an auth change:

    ux_source_product   products(source, source_product_id)          UNIQUE
    ux_account_product  pins(account_id, product_id)                 UNIQUE
    ux_pin_id           pins(pin_id)  partial(pin_id exists)         UNIQUE

``migrate`` is idempotent: createIndexes is a no-op for already-existing
identical indexes, so it runs safely on every deploy.
"""

from __future__ import annotations

from pymongo import IndexModel, MongoClient
from pymongo.database import Database

DEFAULT_DB_NAME = "affiliate-pinner"

AUDIT_TTL_SECONDS = 90 * 24 * 3600  # 90 days


def _claim_index() -> IndexModel:
    return IndexModel([("status", 1), ("attempt.next_attempt_at", 1)], name="ix_claim")


COLLECTION_INDEXES: dict[str, tuple[IndexModel, ...]] = {
    "products": (
        IndexModel(
            [("source", 1), ("source_product_id", 1)],
            name="ux_source_product",
            unique=True,
        ),
        _claim_index(),
        IndexModel([("dedup_hash", 1)], name="ix_dedup"),
        IndexModel([("status", 1), ("last_updated_at", 1)], name="ix_archive"),
    ),
    "pins": (
        IndexModel(
            [("account_id", 1), ("product_id", 1)],
            name="ux_account_product",
            unique=True,
        ),
        IndexModel(
            [("pin_id", 1)],
            name="ux_pin_id",
            unique=True,
            partialFilterExpression={"pin_id": {"$exists": True}},
        ),
        _claim_index(),
        IndexModel(
            [("account_id", 1), ("status", 1), ("updated_at", -1)],
            name="ix_account_status_recent",
        ),
    ),
    "accounts": (
        IndexModel([("status", 1)], name="ix_status"),
        IndexModel([("niche_id", 1)], name="ix_niche"),
    ),
    "oauth_tokens": (
        IndexModel([("account_id", 1)], name="ux_account", unique=True),
    ),
    "runs": (
        IndexModel([("started_at", -1)], name="ix_started"),
    ),
    "pin_metrics": (
        IndexModel([("pin_id", 1), ("captured_at", 1)], name="ix_pin_ts"),
        IndexModel([("account_id", 1), ("captured_at", -1)], name="ix_account_ts"),
    ),
    "audit_log": (
        IndexModel(
            [("ts", 1)],
            name="ttl_ts",
            expireAfterSeconds=AUDIT_TTL_SECONDS,
        ),
        IndexModel([("entity", 1), ("entity_id", 1), ("ts", -1)], name="ix_entity_ts"),
    ),
}


def get_client(uri: str) -> MongoClient:
    """Client with sane timeouts for a short-lived cron runner."""
    return MongoClient(uri, serverSelectionTimeoutMS=10_000)


def migrate(db: Database) -> dict[str, list[str]]:
    """Create all collections' indexes (idempotent). Returns created index names."""
    report: dict[str, list[str]] = {}
    for collection, models in COLLECTION_INDEXES.items():
        report[collection] = db[collection].create_indexes(models)
    return report
