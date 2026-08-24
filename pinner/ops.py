"""Operator tools (WP7): kill switch, dead-letter requeue, status summary.

Importable logic (tested) + thin CLIs in scripts/ops.py. Everything here is
for HUMANS operating the system — the autonomous runner never calls these.
"""

from __future__ import annotations

from pinner.repo.pins import PinsRepo
from pinner.repo.products import ProductsRepo

PIN_STATUSES = [
    "QUEUED", "ENRICHING", "ENRICHED", "BRIDGING", "BRIDGED",
    "PINNING", "PINNED", "VERIFYING", "VERIFIED", "PAUSED", "DEAD",
]
PRODUCT_STATUSES = [
    "PENDING_FETCH", "FETCHING", "FETCHED", "MODERATING",
    "APPROVED", "REJECTED", "DEAD_FETCH", "DEAD_MODERATE",
]


def find_account(db, name: str) -> dict | None:
    return db.accounts.find_one({"name": name})


def _require_account(db, name: str) -> dict:
    account = find_account(db, name)
    if account is None:
        raise KeyError(f"account not found: {name!r}")
    return account


def pause_account(db, name: str, *, run_id: str = "ops") -> int:
    """Kill switch: pause every active pin AND flag the account itself."""
    account = _require_account(db, name)
    paused_pins = PinsRepo(db).pause_account(str(account["_id"]), run_id=run_id)
    db.accounts.update_one({"_id": account["_id"]}, {"$set": {"status": "PAUSED"}})
    return paused_pins


def resume_account(db, name: str, *, run_id: str = "ops") -> int:
    """Re-activate a paused account and resume its pins."""
    account = _require_account(db, name)
    resumed = PinsRepo(db).resume_account(str(account["_id"]), run_id=run_id)
    db.accounts.update_one({"_id": account["_id"]}, {"$set": {"status": "ACTIVE"}})
    return resumed


def requeue(db, collection: str, doc_id, *, run_id: str = "ops") -> dict:
    """Requeue a DEAD document (pins or products) with a fresh attempt budget."""
    if collection == "pins":
        return PinsRepo(db).requeue_dead(doc_id, run_id=run_id)
    if collection == "products":
        return ProductsRepo(db).requeue_dead(doc_id, run_id=run_id)
    raise ValueError(f"unknown collection: {collection!r} (use 'pins' or 'products')")


def status_summary(db) -> dict:
    """One-glance health snapshot for the dashboard/Telegram digest."""
    accounts = [
        {
            "name": a.get("name"),
            "status": a.get("status"),
            "pins_today": (a.get("stats") or {}).get("pins_today", 0),
            "last_pin_at": (a.get("stats") or {}).get("last_pin_at"),
        }
        for a in db.accounts.find().sort("name")
    ]
    pins = {s: db.pins.count_documents({"status": s}) for s in PIN_STATUSES}
    products = {s: db.products.count_documents({"status": s}) for s in PRODUCT_STATUSES}
    return {
        "accounts": accounts,
        "pins": pins,
        "products": products,
        "last_run": db.runs.find_one(
            sort=[("started_at", -1)], projection={"started_at": 1, "stats": 1}
        ),
    }
