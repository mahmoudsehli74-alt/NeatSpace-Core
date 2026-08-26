"""The learning loop (Phase 3): collect Pinterest metrics per pin, aggregate
per account, and produce the numbers the Performance Analyst reasons over.

Collector contract:
  * One snapshot per (pin, UTC day) — idempotent re-runs via upsert.
  * VERIFIED pins only, older than ``min_age_days`` (let data accrue) and
    within the lookback window.
  * A dead/unfindable pin is logged and skipped, never fatal to the sweep.

Aggregation joins pin_metrics -> pins.content.landing_angle so the analyst
learns WHICH ANGLES convert, per account and overall. CTR = outbound_clicks /
impressions (the locked success metric).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

MIN_AGE_DAYS_DEFAULT = 7
LOOKBACK_DAYS_DEFAULT = 30
CAPTURE_WINDOW_DAYS = 7


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def collect_account_metrics(
    db,
    tool,  # PinterestTool bound to the account's token
    account: dict,
    *,
    min_age_days: int = MIN_AGE_DAYS_DEFAULT,
    lookback_days: int = LOOKBACK_DAYS_DEFAULT,
    now: datetime | None = None,
) -> dict:
    """Snapshot recent verified pins of one account into pin_metrics."""
    now = now or _utcnow()
    collected, skipped = 0, 0
    lookback_floor = now - timedelta(days=lookback_days)
    pins = list(
        db.pins.find(
            {
                "account_id": str(account["_id"]),
                "status": "VERIFIED",
                "updated_at": {"$gte": lookback_floor},
            }
        )
    )
    for pin in pins:
        created = pin.get("created_at") or now
        if now - created < timedelta(days=min_age_days):
            skipped += 1
            continue
        pin_id = (pin.get("pin") or {}).get("pin_id")
        if not pin_id:
            skipped += 1
            continue
        start = (now - timedelta(days=CAPTURE_WINDOW_DAYS)).strftime("%Y-%m-%d")
        end = now.strftime("%Y-%m-%d")
        try:
            totals = tool.get_pin_analytics(pin_id, start_date=start, end_date=end)
        except Exception as exc:
            logger.warning("metrics unavailable for %s: %s", pin_id, exc)
            skipped += 1
            continue
        db.pin_metrics.update_one(
            {"pin_id": pin_id, "captured_day": end},
            {
                "$set": {
                    "pin_id": pin_id,
                    "account_id": str(account["_id"]),
                    "captured_day": end,
                    "captured_at": now,
                    "impressions": totals.get("IMPRESSION", 0),
                    "outbound_clicks": totals.get("OUTBOUND_CLICK", 0),
                    "pin_clicks": totals.get("PIN_CLICK", 0),
                    "saves": totals.get("SAVE", 0),
                }
            },
            upsert=True,
        )
        collected += 1
    return {"account": account.get("name"), "collected": collected, "skipped": skipped}


def aggregate(db, *, account_id: str | None = None) -> dict:
    """Learning-loop view: totals + CTR + best/worst landing angles."""
    match: dict = {}
    if account_id:
        match["account_id"] = account_id
    pipeline = [
        {"$match": match},
        {
            "$group": {
                "_id": "$pin_id",
                "impressions": {"$max": "$impressions"},
                "outbound_clicks": {"$max": "$outbound_clicks"},
                "saves": {"$max": "$saves"},
            }
        },
    ]
    rows = list(db.pin_metrics.aggregate(pipeline))
    impressions = sum(r["impressions"] for r in rows)
    clicks = sum(r["outbound_clicks"] for r in rows)
    ctr = round(clicks / impressions, 4) if impressions else None

    angle_totals: dict[str, list[int]] = {}
    latest_pin_per_metric: dict[str, dict] = {r["_id"]: r for r in rows}
    angle_rows = []
    for metric_row in latest_pin_per_metric.values():
        pin_doc = db.pins.find_one({"pin.pin_id": metric_row["_id"]}, {"content.landing_angle": 1})
        if not pin_doc or not pin_doc.get("content"):
            continue
        angle_rows.append((metric_row["_id"], (pin_doc["content"] or {}).get("landing_angle")))
    for pin_id, angle in angle_rows:
        r = latest_pin_per_metric[pin_id]
        bucket = angle_totals.setdefault(angle or "unknown", [0, 0])
        bucket[0] += r["impressions"]
        bucket[1] += r["outbound_clicks"]
    angles = []
    for angle, (imp, clk) in angle_totals.items():
        angles.append(
            {
                "landing_angle": angle,
                "pins": db.pins.count_documents({"content.landing_angle": angle}),
                "impressions": imp,
                "outbound_clicks": clk,
                "ctr": round(clk / imp, 4) if imp else None,
            }
        )
    angles.sort(key=lambda a: a["ctr"] or 0, reverse=True)
    return {
        "pins_measured": len(rows),
        "impressions": impressions,
        "outbound_clicks": clicks,
        "ctr": ctr,
        "angles": angles[:8],
    }


def archive_terminal_products(db, *, min_age_days: int = 60, now: datetime | None = None) -> int:
    """M0 hygiene: strip bulky raw payloads from products whose lifecycle is
    fully terminal (REJECTED/DEAD) or whose every pin is VERIFIED and old.
    Identity survives (source_product_id/dedup_hash) — dedup still works."""
    now = now or _utcnow()
    floor = now - timedelta(days=min_age_days)
    stripped = 0
    query = {
        "status": {"$in": ["APPROVED", "REJECTED", "DEAD_FETCH", "DEAD_MODERATE"]},
        "last_updated_at": {"$lte": floor},
        "raw.title": {"$exists": True},
    }
    for product in db.products.find(query):
        pid = product["_id"]
        pins_exist = db.pins.count_documents({"product_id": pid}) > 0
        if pins_exist:
            unfinished = db.pins.count_documents(
                {"product_id": pid, "status": {"$nin": ["VERIFIED", "DEAD"]}}
            )
            if unfinished:
                continue  # a live pipeline may still need raw payloads
        db.products.update_one(
            {"_id": pid},
            {"$set": {"status": product["status"], "raw_stripped_at": now, "raw_stripped": True},
             "$unset": {"raw": ""}},
        )
        # status stays; a marker records why raw vanished
        stripped += 1
    return stripped
