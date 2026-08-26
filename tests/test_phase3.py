"""Phase 3 tests: metrics collection, aggregation, archiving, Analyst agent."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from pinner.agents.analyst import Analyst
from pinner.agents.client import GeminiJsonClient
from pinner.agents.schemas import PerformanceProposal, ProposalItem
from pinner.metrics import aggregate, archive_terminal_products, collect_account_metrics
from pinner.tools.http import HttpReply
from tests.test_tools_bridge import FakeTransport

T0 = datetime(2026, 1, 15, 12, 0, 0)


def verified_pin(mdb, *, pin_id="pin-1", account_id="a1", age_days=10,
                 angle="budget-luxury", product_id=None, **extra):
    created = T0 - timedelta(days=age_days)
    mdb.pins.insert_one(
        {
            "account_id": account_id,
            "product_id": product_id or f"prod-{pin_id}",
            "status": "VERIFIED",
            "content": {"landing_angle": angle, "title": "t"},
            "pin": {"pin_id": pin_id},
            "created_at": created,
            "updated_at": created,
            **extra,
        }
    )


def analytics_reply(impressions=1000, clicks=75) -> HttpReply:
    body = {
        "all": {
            "daily_metrics": [
                {"date": "2026-01-14", "metrics": {"IMPRESSION": impressions // 2,
                                                   "OUTBOUND_CLICK": clicks // 2}},
                {"date": "2026-01-15", "metrics": {"IMPRESSION": impressions - impressions // 2,
                                                   "OUTBOUND_CLICK": clicks - clicks // 2}},
            ]
        }
    }
    return HttpReply(200, json.dumps(body).encode(), "application/json")


@pytest.fixture()
def mdb(db):
    from pinner.repo.mongo import migrate

    migrate(db)
    return db


# --- collector ------------------------------------------------------------------------


def test_collector_snapshots_verified_pins_once_per_day(mdb):
    verified_pin(mdb, pin_id="pin-1", age_days=10)
    verified_pin(mdb, pin_id="pin-fresh", age_days=1)   # too young -> skipped
    fake = FakeTransport(analytics_reply())
    from pinner.tools.pinterest import PinterestTool

    report = collect_account_metrics(
        mdb, PinterestTool("tok", transport=fake), {"_id": "a1", "name": "Acc"},
        now=T0,
    )
    assert report == {"account": "Acc", "collected": 1, "skipped": 1}
    snap = mdb.pin_metrics.find_one({"pin_id": "pin-1"})
    assert snap["impressions"] == 1000 and snap["outbound_clicks"] == 75

    # re-run the same day: upsert overwrites, still exactly ONE snapshot doc
    fake2 = FakeTransport(analytics_reply(2000, 100))
    collect_account_metrics(mdb, PinterestTool("tok", transport=fake2),
                            {"_id": "a1", "name": "Acc"}, now=T0)
    assert mdb.pin_metrics.count_documents({"pin_id": "pin-1"}) == 1
    assert mdb.pin_metrics.find_one({"pin_id": "pin-1"})["impressions"] == 2000


def test_collector_survives_dead_pins(mdb):
    verified_pin(mdb, pin_id="pin-dead", age_days=10)
    replies = [HttpReply(404, b"{}", "application/json")]
    from pinner.tools.pinterest import PinterestTool

    report = collect_account_metrics(
        mdb, PinterestTool("tok", transport=FakeTransport(*replies)),
        {"_id": "a1", "name": "Acc"}, now=T0,
    )
    assert report["collected"] == 0 and report["skipped"] == 1
    assert mdb.pin_metrics.count_documents({}) == 0


# --- aggregation -------------------------------------------------------------------------


def test_aggregate_joins_angles_and_orders_by_ctr(mdb):
    # angle A: high CTR; angle B: low CTR
    for pid, imp, clk, angle in (
        ("p-a", 1000, 150, "gift-guide"), ("p-b", 1000, 10, "problem-solver")
    ):
        verified_pin(mdb, pin_id=pid, angle=angle)
        mdb.pin_metrics.insert_one({
            "pin_id": pid, "account_id": "a1", "captured_day": "2026-01-15",
            "captured_at": T0, "impressions": imp, "outbound_clicks": clk,
            "pin_clicks": 0, "saves": 0,
        })
    result = aggregate(mdb)
    assert result["pins_measured"] == 2
    assert result["impressions"] == 2000 and result["outbound_clicks"] == 160
    assert result["ctr"] == 0.08
    assert [a["landing_angle"] for a in result["angles"]][0] == "gift-guide"


def test_aggregate_empty_is_safe(mdb):
    assert aggregate(mdb)["pins_measured"] == 0 and aggregate(mdb)["ctr"] is None


# --- archiving ------------------------------------------------------------------------------


def test_archive_strips_raw_of_fully_terminal_products_only(mdb):
    now = T0
    old = T0 - timedelta(days=90)
    for status in ("REJECTED", "DEAD_FETCH"):
        mdb.products.insert_one({"source": "s", "source_product_id": f"{status}-1",
                                 "status": status, "raw": {"title": "x"},
                                 "last_updated_at": old})
    # approved + all pins VERIFIED and old -> strippable
    mdb.products.insert_one({"source": "s", "source_product_id": "ok-1", "status": "APPROVED",
                             "raw": {"title": "keep-me-nothing"}, "last_updated_at": old})
    mdb.pins.insert_one({"account_id": "a", "product_id": _id_of(mdb, "ok-1"),
                         "status": "VERIFIED", "created_at": old})
    # approved with a LIVE pipeline -> protected
    mdb.products.insert_one({"source": "s", "source_product_id": "live-1", "status": "APPROVED",
                             "raw": {"title": "live"}, "last_updated_at": old})
    mdb.pins.insert_one({"account_id": "a", "product_id": _id_of(mdb, "live-1"),
                         "status": "PINNED", "created_at": old})

    stripped = archive_terminal_products(mdb, now=now)
    assert stripped == 3
    assert mdb.products.find_one({"source_product_id": "REJECTED-1"})["raw_stripped"] is True
    assert "raw" not in mdb.products.find_one({"source_product_id": "ok-1"})
    assert mdb.products.find_one({"source_product_id": "live-1"})["raw"]["title"] == "live"
    # identity survives stripping
    assert mdb.products.find_one({"source_product_id": "ok-1"})["source_product_id"] == "ok-1"


def _id_of(db, sid):
    return db.products.find_one({"source_product_id": sid})["_id"]


# --- analyst --------------------------------------------------------------------------------


def test_analyst_produces_proposal_with_data_citations():
    from types import SimpleNamespace

    proposal = PerformanceProposal(
        summary="Gift-guide angles dominate CTR.",
        proposals=[ProposalItem(target="landing_angle",
                                change="Shift titles toward gift framing",
                                rationale="gift-guide ctr 15% vs 1% baseline")],
        keep_doing="Budget-luxury imagery on gift-guide pins.",
    )
    fake = SimpleNamespace(parsed=proposal)

    class M:
        models = property(lambda self: self)

        def generate_content(self, **kw):
            return fake

    analyst = Analyst(GeminiJsonClient("k", model="m", raw=M()))
    result = analyst.review({"ctr": 0.08, "angles": []})
    assert result.proposals[0].target == "landing_angle"


def test_analyst_prompt_carries_numbers_not_payloads():

    from pinner.agents.analyst import ANALYST_SYSTEM, ANALYST_USER_TEMPLATE

    blob = ANALYST_SYSTEM + ANALYST_USER_TEMPLATE
    assert "OUTBOUND CTR" in blob and "<untrusted_product_data>" not in blob
