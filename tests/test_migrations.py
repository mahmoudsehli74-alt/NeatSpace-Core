"""Migration & schema-integrity tests (WP2).

Proves the three safety-critical unique indexes physically reject duplicates:
  * products(source, source_product_id)
  * pins(account_id, product_id)
  * pins(pin_id) partial
and that the audit TTL index is exactly 90 days. These indexes are the
double-publish backstop — if these tests fail, nothing else matters.
"""

from __future__ import annotations

import pytest
from pymongo.errors import DuplicateKeyError

from pinner.repo.mongo import AUDIT_TTL_SECONDS, migrate

EXPECTED_INDEXES = {
    "products": {"_id_", "ux_source_product", "ix_claim", "ix_dedup", "ix_archive"},
    "pins": {"_id_", "ux_account_product", "ux_pin_id", "ix_claim", "ix_account_status_recent"},
    "accounts": {"_id_", "ux_name", "ix_status", "ix_niche"},
    "niches": {"_id_", "ux_name"},
    "oauth_tokens": {"_id_", "ux_account"},
    "runs": {"_id_", "ix_started"},
    "pin_metrics": {"_id_", "ix_pin_ts", "ix_account_ts"},
    "audit_log": {"_id_", "ttl_ts", "ix_entity_ts"},
}


def _pin(account_id: str, product_id: str, **extra) -> dict:
    return {"account_id": account_id, "product_id": product_id, "status": "QUEUED", **extra}


def test_migrate_creates_all_expected_indexes(db):
    migrate(db)
    for collection, expected in EXPECTED_INDEXES.items():
        actual = set(db[collection].index_information())
        assert actual == expected, f"{collection}: {actual ^ expected}"


def test_migrate_is_idempotent(db):
    first = migrate(db)
    second = migrate(db)
    assert first == second


def test_products_unique_source_and_product_id(db):
    migrate(db)
    doc = {"source": "aliexpress", "source_product_id": "123", "status": "PENDING_FETCH"}
    db.products.insert_one(doc)
    with pytest.raises(DuplicateKeyError):
        db.products.insert_one(doc)
    # Same product_id under a different source is a different product.
    db.products.insert_one({"source": "temu", "source_product_id": "123",
                            "status": "PENDING_FETCH"})


def test_pins_unique_account_and_product(db):
    migrate(db)
    db.pins.insert_one(_pin("acc1", "prod1"))
    with pytest.raises(DuplicateKeyError):
        db.pins.insert_one(_pin("acc1", "prod1"))
    # Another account pinning the same product is legal.
    db.pins.insert_one(_pin("acc2", "prod1"))


def test_pins_unique_pin_id_partial_index(db):
    migrate(db)
    db.pins.insert_one(_pin("acc1", "prod1", pin_id="pin-abc"))
    # Duplicate pin_id on a DIFFERENT (account, product) doc still collides.
    with pytest.raises(DuplicateKeyError):
        db.pins.insert_one(_pin("acc2", "prod2", pin_id="pin-abc"))
    # Docs without pin_id are exempt from the partial index (no collision).
    db.pins.insert_one(_pin("acc3", "prod3"))
    db.pins.insert_one(_pin("acc4", "prod4"))


def test_oauth_tokens_unique_per_account(db):
    migrate(db)
    db.oauth_tokens.insert_one({"account_id": "acc1", "encrypted_blob": {"ciphertext": "x"}})
    with pytest.raises(DuplicateKeyError):
        db.oauth_tokens.insert_one({"account_id": "acc1", "encrypted_blob": {"ciphertext": "y"}})


def test_audit_log_ttl_is_90_days(db):
    migrate(db)
    info = db.audit_log.index_information()["ttl_ts"]
    assert info["expireAfterSeconds"] == AUDIT_TTL_SECONDS == 90 * 24 * 3600
