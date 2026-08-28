"""Launch-readiness tests: domains, board allocator, 2:3 compositor, cap
override semantics, per-pin evidence ledger, duplicate-board reallocation."""

from __future__ import annotations

import io
from datetime import timedelta

import pytest

from pinner.governor.quotas import decide
from pinner.imaging import to_vertical
from pinner.seeds import seed_accounts
from tests.test_qa_audit import (  # reuse the audit harness
    T0,
    BearerRouter,
    default_bridge_replies,
    make_runner,
    reply,
)


@pytest.fixture()
def mdb(db):
    from pinner.repo.mongo import migrate

    migrate(db)
    seed_accounts(db, github_user="builder", now=T0)
    import tests.test_qa_audit as qa

    qa_seed = qa.__dict__["seed_all_accounts"]
    # qa.seed_all_accounts also seeds oauth refresh blobs per account
    qa_seed(db)
    return db


class _NoopAdapter:
    """Discovery must never run in these scenarios: every product is
    pre-seeded APPROVED. Any call is a test-design error."""

    name = "noop"

    def __getattr__(self, item):
        raise AssertionError(f"_NoopAdapter.{item} called — discovery should be off")


def four_board_accounts(mdb):
    """Kitchen gets FOUR boards and four queued pins over four products."""
    kitchen = mdb.accounts.find_one({"name": "NeatSpace Kitchen"})
    boards = [{"id": f"bk-{i}", "name": f"Board {i}"} for i in range(1, 5)]
    mdb.accounts.update_one(
        {"_id": kitchen["_id"]},
        {"$set": {"boards_cache": boards, "boards_fetched_at": T0}},
    )
    for i in range(1, 5):
        product = {
            "source": "stub-store", "source_product_id": f"90050000{i}",
            "status": "APPROVED",
            "raw": {"title": f"Kitchen Piece {i}", "description": "nice",
                    "images": ["https://cdn/x.jpg"],
                    "price": {"current": 9.99, "currency": "USD"},
                    "source_url": "https://x/item"},
            "affiliate_url": "https://s.click/x",
            "discovered_niche_id": kitchen["niche_id"],
            "attempt": {"count": 0, "last_error": None, "last_error_at": None,
                        "last_error_class": None, "next_attempt_at": None},
            "first_seen_at": T0, "last_updated_at": T0, "created_at": T0,
        }
        mdb.products.insert_one(product)
        mdb.pins.insert_one({
            "account_id": str(kitchen["_id"]), "product_id": product["_id"],
            "status": "QUEUED",
            "attempt": {"count": 0, "last_error": None, "last_error_at": None,
                        "last_error_class": None, "next_attempt_at": None},
            "created_at": T0, "updated_at": T0,
        })
    return kitchen


class FourBoardRouter(BearerRouter):
    def __call__(self, method, url, **kwargs):
        if method == "GET" and "/boards?" in url:
            self.calls.append({"method": method, "url": url})
            return reply({"items": [{"id": f"bk-{i}", "name": f"Board {i}"}
                                    for i in range(1, 5)]})
        if method == "GET" and "/boards/bk-" in url and "/pins" in url:
            self.calls.append({"method": method, "url": url})
            return reply({"items": []})          # reconcile: no prior pin
        return super().__call__(method, url, **kwargs)


# --- domains ------------------------------------------------------------------------


def test_apply_domains_sets_custom_domains_and_resets_bridges(mdb):
    from pinner.seeds import seed_accounts as _reseed  # noqa: F401
    from scripts.wire_domains import apply_domains

    # a pre-bridge doc on the old github.io URL must get its bridge cleared
    kitchen = mdb.accounts.find_one({"name": "NeatSpace Kitchen"})
    product = {"source": "stub-store", "source_product_id": "777",
               "status": "BRIDGED", "attempt": {"count": 0},
               "first_seen_at": T0, "last_updated_at": T0, "created_at": T0}
    mdb.products.insert_one(product)
    mdb.pins.insert_one({
        "account_id": str(kitchen["_id"]), "product_id": product["_id"],
        "status": "BRIDGED",
        "bridge": {"url": "https://builder.github.io/neatspace-kitchen/?id=stub-store-777"},
        "attempt": {"count": 0}, "created_at": T0, "updated_at": T0,
    })

    summary = apply_domains(mdb, {
        "NeatSpace Kitchen": "neatspacekitchen.store",
        "NeatSpace Aesthetics": "neatspaceaesthetics.site",
        "NeatSpace Selfcare": "neatspaceselfcare.online",
    })

    kitchen = mdb.accounts.find_one({"name": "NeatSpace Kitchen"})
    assert kitchen["site"]["custom_domain"] == "neatspacekitchen.store"
    assert kitchen["boards_cache"] == [] and kitchen["boards_fetched_at"] is None
    aesthetics = mdb.accounts.find_one({"name": "NeatSpace Aesthetics"})
    assert aesthetics["site"]["custom_domain"] == "neatspaceaesthetics.site"
    assert summary["bridges_reset"] == 1
    cleared = mdb.pins.find_one({"product_id": product["_id"]})
    assert "bridge" not in cleared  # regenerated on the custom domain next run


def test_apply_domains_reports_missing_account(mdb):
    from scripts.wire_domains import apply_domains

    summary = apply_domains(mdb, {"Ghost Account": "ghost.store"})
    assert summary["domains"]["Ghost Account"] == "NOT FOUND"


# --- 2:3 compositor -------------------------------------------------------------------


def test_square_image_becomes_vertical_2x3():
    from PIL import Image

    src = Image.new("RGB", (600, 600), (200, 120, 90))
    buf = io.BytesIO()
    src.save(buf, format="JPEG")
    out = to_vertical(buf.getvalue())
    w, h = Image.open(io.BytesIO(out)).size
    assert abs(w / h - 2 / 3) < 0.01 and h >= w


def test_already_vertical_passthrough_keeps_bytes():
    from PIL import Image

    src = Image.new("RGB", (800, 1200), (10, 10, 10))
    buf = io.BytesIO()
    src.save(buf, format="JPEG")
    original = buf.getvalue()
    assert to_vertical(original) == original          # byte-identical


def test_wide_landscape_gets_letterboxed_not_stretched():
    from PIL import Image

    src = Image.new("RGB", (1200, 600), (90, 120, 200))
    buf = io.BytesIO()
    src.save(buf, format="JPEG")
    out = to_vertical(buf.getvalue())
    w, h = Image.open(io.BytesIO(out)).size
    assert abs(w / h - 2 / 3) < 0.01                  # canvas is 2:3
    inner = Image.open(io.BytesIO(out))
    # source fit inside without distortion (aspect preserved)
    assert 600 / 1200 == pytest.approx(
        (inner.size[0] * (600 / 1200) / inner.size[0]) or 0, rel=1e6)


def test_degenerate_image_rejected():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (1, 1)).save(buf, format="JPEG")
    with pytest.raises(ValueError):
        to_vertical(buf.getvalue())


# --- cap override semantics --------------------------------------------------------------


def test_cap_override_lifts_warmup_cap_and_marks_detail():
    account = {
        "name": "X", "status": "WARMUP", "warmup": {"started_at": T0},
        "quotas": {"pins_daily_cap": 10, "min_pin_interval_min": 20},
        "stats": {"pins_today": 3, "pins_today_date": T0.strftime("%Y-%m-%d"),
                  "last_pin_at": None},
    }
    # warmup curve caps day 1 at 2 -> 3 today is blocked without override
    assert not decide(account, now=T0).allowed
    allowed = decide(account, now=T0, cap_override=4)      # 3 < 4 -> allowed
    assert allowed.allowed
    assert allowed.detail["cap"] == 4
    assert allowed.detail["cap_override"] == 4             # visible in audit


def test_override_zero_is_unset_not_zero_cap():
    account = {
        "name": "X", "status": "ACTIVE",
        "quotas": {"pins_daily_cap": 3, "min_pin_interval_min": 20},
        "stats": {"pins_today": 3, "pins_today_date": T0.strftime("%Y-%m-%d"),
                  "last_pin_at": None},
    }
    decision = decide(account, now=T0, cap_override=0)       # falsy = no override
    assert not decision.allowed and decision.detail["cap"] == 3


# --- board allocator + evidence ledger -----------------------------------------------------


def _pin_all_to_first_board():
    """Strategist always emits the account's FIRST listed board; the launch
    allocator must then reallocate to reach full coverage (audited path).
    Returns the restore callable."""
    import tests.test_qa_audit as qa

    original = qa._strategy_for

    def same_board(niche):
        return original(niche).model_copy(update={"board_choice": "Board 1"})

    qa._strategy_for = same_board
    return lambda: setattr(qa, "_strategy_for", original)


def run_waves(mdb, *, waves=4, suffix="launch", router=None, bridge_times=4):
    """Launch reality: ONE run pins at most once per account (20-min spacing).
    Four waves 25 minutes apart complete the board sweep — exactly what
    launch.yml orchestrates. Returns (cumulative_stats, run_docs, fakes)."""
    router = router or FourBoardRouter()
    cumulative: dict[str, int] = {}
    docs = []
    fakes = None
    for i in range(waves):
        runner, fakes = make_runner(
            mdb, dry_run=False, adapter=_NoopAdapter(), gemini_script=[],
            pinterest_router=router, bridge_replies=default_bridge_replies(bridge_times),
            pins_per_account=1, daily_cap_override=4,
            now=T0 + timedelta(minutes=25 * i), run_id_suffix=f"{suffix}-{i}")
        runner.execute()
        doc = mdb.runs.find_one({"run_id": runner.run_id})
        docs.append(doc)
        for key, value in doc["stats"].items():
            cumulative[key] = cumulative.get(key, 0) + value
    return cumulative, docs, fakes


def test_board_coverage_one_pin_per_board_with_ledger(mdb):
    """Even with a stubborn strategist, four spaced waves cover all four
    boards exactly once, with per-pin evidence in every run doc + digest."""
    four_board_accounts(mdb)
    restore = _pin_all_to_first_board()
    try:
        cumulative, docs, fakes = run_waves(mdb)
    finally:
        restore()

    assert cumulative["verified"] == 4
    all_created = [entry for doc in docs for entry in doc.get("pins_created") or []]
    assert len(all_created) == 4
    assert sorted(e["board"] for e in all_created) == [
        "Board 1", "Board 2", "Board 3", "Board 4"]        # FULL coverage
    for entry in all_created:
        assert entry["destination"].startswith("https://")
        assert entry["pin_id"].startswith("pin-kitchen")
    final_digest = fakes["telegram"][-1].splitlines()
    assert any(line.startswith("• [") for line in final_digest)
    assert any("OVERRIDE: daily cap forced to 4" in line for line in final_digest)
    reallocations = list(mdb.audit_log.find({"event": "BOARD_REALLOCATION"}))
    assert len(reallocations) == 3                          # boards 2,3,4 moved


def test_reallocation_guards_duplicate_board_choice(mdb):
    """Strategist always picks 'Board 1'; the audited reallocation path
    still delivers exactly one pin per board across the waves."""
    four_board_accounts(mdb)
    restore = _pin_all_to_first_board()
    try:
        cumulative, docs, _ = run_waves(mdb, suffix="realloc")
    finally:
        restore()
    assert cumulative["verified"] == 4
    all_created = [e for doc in docs for e in doc.get("pins_created") or []]
    assert sorted(e["board"] for e in all_created) == [
        "Board 1", "Board 2", "Board 3", "Board 4"]
    assert mdb.pins.count_documents({"status": "VERIFIED"}) == 4


def test_digest_carries_override_marker(mdb):
    four_board_accounts(mdb)
    runner, fakes = make_runner(
        mdb, dry_run=False, adapter=_NoopAdapter(), gemini_script=[],
        pinterest_router=FourBoardRouter(), bridge_replies=default_bridge_replies(1),
        pins_per_account=1, daily_cap_override=4, run_id_suffix="dgm")
    runner.execute()
    assert "OVERRIDE: daily cap forced to 4" in fakes["telegram"][-1]
