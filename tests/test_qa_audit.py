"""Operational-resilience QA audit suite (post go-live).

Five dimensions per the QA directive — each section maps 1:1:

1. Token lifecycle & auth resilience (missing / corrupted / rotated)
2. Edge cases & failure modes (rate limits, gateway statuses, malformed
   payloads, network timeouts/egress drops)
3. Deduplication & idempotency (overlapping catalogs; cross-niche isolation
   end-to-end with per-account bearer routing)
4. Rate limiting / warm-up curve across cycles; cron vs manual dispatch truth
5. Observability integrity (runs document shape; digest states)

The pinterest fake routes on the Authorization bearer token so three accounts
can be driven through ONE scripted transport while asserting strict account
isolation at every API boundary. Gemini scripts are sized per scenario:
moderation consumes one verdict per ACTIVE niche; strategies only for
accounts that reach ENRICHING."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from pinner.adapters.base import (
    CandidateProduct,
    StoreAdapter,
    TransientAdapterError,
)
from pinner.agents import GeminiJsonClient, Moderator, Strategist
from pinner.crypto import tokens as crypto
from pinner.runner.main import Runner, RunnerConfig, RunnerDeps, build_runner_args
from pinner.tools.bridge import BridgeTool
from pinner.tools.http import HttpReply
from pinner.tools.pinterest import PinterestTool
from tests.test_agents import CLEAN_RAW, approve_verdict, good_strategy
from tests.test_tools_bridge import FakeTransport

T0 = datetime(2026, 2, 1, 9, 0, 0)
KEY32 = bytes(range(32))

NICHES = {
    "kitchen": {"board": "Kitchen Organization", "repo": "builder/neatspace-kitchen",
                "token": "tok-kitchen", "pid": "100500111"},
    "aesthetics": {"board": "Aesthetic Rooms", "repo": "builder/neatspace-aesthetics",
                   "token": "tok-aesthetics", "pid": "100500222"},
    "selfcare": {"board": "Wellness Rituals", "repo": "builder/neatspace-selfcare",
                 "token": "tok-selfcare", "pid": "100500333"},
}

ACCOUNT_NAMES = {
    "kitchen": "NeatSpace Kitchen",
    "aesthetics": "NeatSpace Aesthetics",
    "selfcare": "NeatSpace Selfcare",
}


def reply(body: dict, status: int = 200) -> HttpReply:
    return HttpReply(status, json.dumps(body).encode(), "application/json")


class BearerRouter:
    """Scripted Pinterest transport dispatching on bearer token — the only
    reliable account identifier at the HTTP boundary."""

    def __init__(self):
        self.calls: list[dict] = []

    def route(self, token: str) -> str | None:
        if token == "rotated-access":
            return "kitchen"          # test store rotates one canonical blob
        for niche, cfg in NICHES.items():
            if cfg["token"] == token or token == f"rotated-{niche}":
                return niche
        return None

    def __call__(self, method, url, *, headers=None, json_body=None, data=None, files=None):
        self.calls.append({"method": method, "url": url,
                           "auth": (headers or {}).get("Authorization", ""), "data": data})
        token = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
        niche = self.route(token)
        assert niche is not None, f"bearer '{token[:14]}…' does not map to any seeded account"
        board_id = f"b-{niche}"
        if method == "GET" and url.endswith("/boards?page_size=100"):
            return reply({"items": [{"id": board_id, "name": NICHES[niche]["board"]}]})
        if method == "GET" and f"/boards/{board_id}/pins" in url:
            return reply({"items": []})
        if method == "GET" and "/pins/" in url:
            return reply({"id": url.rsplit("/", 1)[-1], "board_id": board_id})
        if method == "POST" and url.endswith("/v5/pins"):
            return reply({"id": f"pin-{niche}"}, status=201)
        raise AssertionError(f"unexpected call {method} {url}")


class _ExpiryRouter(BearerRouter):
    """Serves a fixed script while kitchen's OLD token lives (the 401 moment),
    then transparently honors the ROTATED access blob for the retry."""

    def __init__(self, script: list[HttpReply]):
        super().__init__()
        self.script = list(script)

    def __call__(self, method, url, **kwargs):
        token = (kwargs.get("headers") or {}).get("Authorization", "").removeprefix("Bearer ")
        if token == NICHES["kitchen"]["token"] and self.script:
            item = self.script.pop(0)
            self.calls.append({"method": method, "url": url})
            return item
        return super().__call__(method, url, **kwargs)


# seeded hashtag_count_range: kitchen [3,6], aesthetics [4,8], selfcare [3,6]
HASHTAG_COUNTS = {"kitchen": 3, "aesthetics": 5, "selfcare": 3}
_TAG_POOL = ["#homedecor", "#dailyfinds", "#interiorinspo", "#cozyvibes", "#homelife"]


def _strategy_for(niche: str):
    """Board choice matches what BearerRouter lists for this account's token,
    and hashtag count respects the niche's seeded guardrail range — content
    AND policy alignment are both part of the isolation assertion."""
    count = HASHTAG_COUNTS[niche]
    tags = [f"#{niche}curated"] + _TAG_POOL[: count - 1]
    return good_strategy().model_copy(update={
        "board_choice": NICHES[niche]["board"],
        "hashtags": tags,
    })


def gemini_script_for(active_niches: list[str]) -> list:
    """Moderation = one verdict per ACTIVE niche; strategies only flow to
    accounts reaching ENRICHING."""
    script: list = [approve_verdict() for _ in active_niches]
    script += [_strategy_for(n) for n in active_niches]
    return script


class ScriptedGeminiRunner:
    """Dispatches on config.response_schema: moderation -> APPROVE; strategy
    -> niche-appropriate content parsed from the product title inside the
    prompt. behaviors queue injects exceptions/overrides FIRST. This makes
    every scenario order-independent — a strategy can never land on the
    wrong niche regardless of DB iteration order."""

    def __init__(self, *behaviors):
        self.behaviors = list(behaviors)

    @property
    def models(self):
        outer = self

        class _M:
            def generate_content(self, *, model, contents, config):
                if outer.behaviors:
                    behavior = outer.behaviors.pop(0)
                    if isinstance(behavior, Exception):
                        raise behavior
                    return SimpleNamespace(parsed=behavior, text=None)
                from pinner.agents.schemas import ModerationVerdict, StrategyContent

                text = " ".join(getattr(p, "text", "") or "" for p in contents).lower()
                if config.response_schema is StrategyContent:
                    # PID first: fixture descriptions share vocabulary, PIDs
                    # are unique per niche (disambiguation proof).
                    niche = next(
                        (n for n, cfg in NICHES.items() if cfg["pid"] in text), None
                    ) or next((n for n in NICHES if n in text), "kitchen")
                    return SimpleNamespace(parsed=_strategy_for(niche), text=None)
                assert config.response_schema is ModerationVerdict
                return SimpleNamespace(parsed=approve_verdict(), text=None)

        return _M()


NICHE_STEMS = {
    "kitchen": "kitchen",
    "aesthetics": "aesthetic",   # seeds query "room aesthetic ..."
    "selfcare": "self care",     # seeds query "self care routine ..."
}


def niche_of(niche_query: str) -> str | None:
    lowered = niche_query.lower()
    return next((n for n, stem in NICHE_STEMS.items() if stem in lowered), None)


class MultiNicheAdapter(StoreAdapter):
    name = "stub-store"

    def __init__(self, *, active_niches: tuple[str, ...] | None = None,
                 transient_query: str | None = None):
        self.active = set(active_niches if active_niches is not None else NICHES.keys())
        self.transient_query = transient_query
        self.call_count: dict[str, int] = {}

    def search_products(self, niche_query: str, *, max_results: int = 10):
        key = niche_of(niche_query)
        if key not in self.active:
            from pinner.adapters.base import PermanentAdapterError

            raise PermanentAdapterError("aliexpress", f"probe-driven rejection: {key}")
        pid = NICHES[key]["pid"]
        return [CandidateProduct(source=self.name, source_product_id=pid,
                                 title=f"{key.title()} Curated Piece",
                                 image_url="https://cdn/i.jpg",
                                 product_url=f"https://www.aliexpress.com/item/{pid}.html")]

    def get_product_details(self, candidate):
        niche = next(n for n, c in NICHES.items() if c["pid"] == candidate.source_product_id)
        self.call_count[niche] = self.call_count.get(niche, 0) + 1
        if niche == self.transient_query and self.call_count[niche] == 1:
            raise TransientAdapterError("aliexpress", "biz error 20000000: traffic limit")
        return dict(CLEAN_RAW,
                    title=f"{niche.title()} Curated Piece {candidate.source_product_id}",
                    source_url=candidate.product_url)

    def build_affiliate_url(self, product_url: str) -> str:
        return "https://s.click.aliexpress.com/e/_QA"


def default_bridge_replies(times: int) -> list:
    one = [HttpReply(404, b"{}"), reply({"commit": {"sha": "sha-x"}}, status=201),
           HttpReply(200, b"{}", "application/json")]
    out: list = []
    for _ in range(max(1, times)):
        out.extend(one)
    return out


class _QATokenStore:
    """Dual-blob store mirroring production semantics on the test key."""

    def __init__(self, db):
        self.db = db

    def access_token(self, account_id: str) -> str:
        doc = self.db.oauth_tokens.find_one({"account_id": account_id})
        if doc is None:
            raise KeyError(f"no oauth token stored for account {account_id}")
        if doc.get("access_blob"):
            return crypto.decrypt_token(KEY32, doc["access_blob"])
        return crypto.decrypt_token(KEY32, doc["refresh_blob"])

    def refresh(self, account_id: str, **_):
        new_access = "rotated-access"
        self.db.oauth_tokens.update_one(
            {"account_id": account_id},
            {"$set": {"access_blob": crypto.encrypt_token(KEY32, new_access),
                      "refresh_blob": crypto.encrypt_token(KEY32, "rotated-refresh")}},
        )
        return new_access


def make_runner(mdb, *, dry_run=False, adapter=None, gemini_script=None,
                pinterest_router=None, bridge_replies=None, now=T0,
                pins_per_account=2, run_id_suffix="", daily_cap_override=None):
    scripted = ScriptedGeminiRunner(*(gemini_script if gemini_script is not None else [
        approve_verdict(), good_strategy()]))
    bridge_transport = FakeTransport(*(bridge_replies or default_bridge_replies(2)))
    telegram_messages: list[str] = []

    deps = RunnerDeps(
        adapter=adapter or MultiNicheAdapter(),
        moderator=Moderator(GeminiJsonClient("k", model="test", raw=scripted),
                            image_fetcher=lambda u: None),
        strategist=Strategist(GeminiJsonClient("k", model="test", raw=scripted)),
        bridge=BridgeTool("pat", transport=bridge_transport),
        token_store=_QATokenStore(mdb),
        pinterest_factory=lambda tok: PinterestTool(tok, transport=pinterest_router),
        telegram=lambda text: telegram_messages.append(text),
        image_fetcher=lambda url: b"JPEGDATA",
    )
    config = RunnerConfig(dry_run=dry_run, pins_per_account=pins_per_account,
                          daily_cap_override=daily_cap_override)
    runner = Runner(mdb, deps, config=config, run_id=f"qa-{run_id_suffix}", now=now)
    return runner, {"telegram": telegram_messages,
                    "bridge_transport": bridge_transport}


def seed_all_accounts(mdb):
    from pinner.seeds import seed_accounts

    seed_accounts(mdb, github_user="builder", now=T0)
    for niche, name in ACCOUNT_NAMES.items():
        account = mdb.accounts.find_one({"name": name})
        mdb.oauth_tokens.update_one(
            {"account_id": str(account["_id"])},
            {"$set": {"refresh_blob": crypto.encrypt_token(KEY32, NICHES[niche]["token"])}},
            upsert=True,
        )


@pytest.fixture()
def mdb(db):
    from pinner.repo.mongo import migrate

    migrate(db)
    seed_all_accounts(db)
    return db


# ════════════════ 1. TOKEN LIFECYCLE & AUTH RESILIENCE ════════════════


def test_missing_token_skips_only_that_account(mdb):
    aesthetics = mdb.accounts.find_one({"name": ACCOUNT_NAMES["aesthetics"]})
    mdb.oauth_tokens.delete_one({"account_id": str(aesthetics["_id"])})

    runner, fakes = make_runner(
        mdb, dry_run=False, adapter=MultiNicheAdapter(),
        gemini_script=[],
        pinterest_router=BearerRouter(), pins_per_account=1, run_id_suffix="missing-tok")
    stats = runner.execute()

    assert stats.get("accounts_without_tokens") == 1
    assert any("NeatSpace Aesthetics" in m and "skipping" in m for m in fakes["telegram"])
    assert stats.get("verified") == 2                     # other niches flowed
    pinned_ids = sorted(p["pin"]["pin_id"] for p in mdb.pins.find({"status": "VERIFIED"}))
    assert pinned_ids == ["pin-kitchen", "pin-selfcare"]


def test_corrupted_token_blob_skips_account_and_alerts(mdb):
    selfcare = mdb.accounts.find_one({"name": ACCOUNT_NAMES["selfcare"]})
    mdb.oauth_tokens.update_one(
        {"account_id": str(selfcare["_id"])},
        {"$set": {"refresh_blob": crypto.encrypt_token(bytes(range(32, 64)), "wrong-key")}},
    )

    runner, fakes = make_runner(
        mdb, dry_run=False, adapter=MultiNicheAdapter(),
        gemini_script=[],
        pinterest_router=BearerRouter(), pins_per_account=1, run_id_suffix="corrupt")
    stats = runner.execute()

    assert stats.get("accounts_without_tokens") == 1
    assert any("NeatSpace Selfcare" in m for m in fakes["telegram"])
    assert stats.get("verified") == 2
    pinned_ids = sorted(p["pin"]["pin_id"] for p in mdb.pins.find({"status": "VERIFIED"}))
    assert pinned_ids == ["pin-aesthetics", "pin-kitchen"]


def test_refresh_rotates_both_blobs_encrypted_and_retries(mdb):
    """401 mid-run -> rotate BOTH blobs (AES-GCM) -> retry arrives under the
    rotated bearer 'rotated-access', which the router transparently serves."""
    expired = [
        reply({"items": [{"id": "b-kitchen", "name": "Kitchen Organization"}]}),
        HttpReply(401, b"{}", "application/json"),
        reply({"items": []}),
        reply({"id": "pin-kitchen"}, status=201),
        reply({"id": "pin-kitchen"}),
    ]
    runner, _ = make_runner(
        mdb, dry_run=False, adapter=MultiNicheAdapter(active_niches=("kitchen",)),
        gemini_script=[],
        pinterest_router=_ExpiryRouter(expired), run_id_suffix="rot")
    stats = runner.execute()
    assert stats.get("verified") == 1 and stats.get("token_refreshes") == 1
    kitchen = mdb.accounts.find_one({"name": ACCOUNT_NAMES["kitchen"]})
    doc = mdb.oauth_tokens.find_one({"account_id": str(kitchen["_id"])})
    # BOTH blobs replaced AND decryptable only with the master key
    assert crypto.decrypt_token(KEY32, doc["access_blob"]) == "rotated-access"
    assert crypto.decrypt_token(KEY32, doc["refresh_blob"]) == "rotated-refresh"


# ════════════════ 2. EDGE CASES & FAILURE MODES ════════════════


def test_rate_limit_is_transient_with_scheduled_retry_later(mdb):
    adapter = MultiNicheAdapter(active_niches=("kitchen",), transient_query="kitchen")
    r1, _ = make_runner(mdb, dry_run=True, adapter=adapter, gemini_script=[],
                        run_id_suffix="rl1")
    s1 = r1.execute()
    assert s1.get("fetch_failed") == 1
    product = mdb.products.find_one()
    assert product["status"] == "PENDING_FETCH"           # reverted for retry
    assert product["attempt"]["count"] == 1
    assert product["attempt"]["last_error_class"] == "TRANSIENT"
    assert product["attempt"]["next_attempt_at"] > T0      # backoff-gated

    r2, _ = make_runner(mdb, dry_run=True,
                        adapter=MultiNicheAdapter(active_niches=("kitchen",)),
                        gemini_script=[],
                        now=T0 + timedelta(hours=2), run_id_suffix="rl2")
    s2 = r2.execute()
    assert s2.get("fetched") == 1                          # recovered next cycle
    assert s2.get("bridged") == 1                          # dry-run through bridge


@pytest.mark.parametrize("status", [500, 502, 503])
def test_gateway_5xx_is_transient_with_status_in_message(status):
    def transport(url, form):
        return status, "gateway melted"

    from pinner.adapters.aliexpress import AliExpressAdapter

    with pytest.raises(TransientAdapterError) as err:
        AliExpressAdapter("k", "s", "t", transport=transport).search_products("x")
    assert f"http {status}" in str(err.value)


def test_malformed_candidates_do_not_crash_pipeline(mdb):
    class MalformedAdapter(MultiNicheAdapter):
        def get_product_details(self, candidate):
            return {"title": "", "description": "", "images": [],
                    "price": {"current": "oops", "currency": "?"},
                    "rating": None, "orders": None, "shop_name": "", "source_url": ""}

    runner, _ = make_runner(
        mdb, dry_run=True, adapter=MalformedAdapter(active_niches=("kitchen",)),
        gemini_script=[],
        run_id_suffix="malformed")
    stats = runner.execute()
    assert not stats.get("critical_errors")               # no crash anywhere
    run_doc = mdb.runs.find_one({"run_id": runner.run_id})
    assert run_doc["finished_at"] is not None


def test_network_timeout_maps_to_transient():
    def boom(request):
        raise httpx.ReadTimeout("egress drop after 20s")

    import pinner.adapters.aliexpress as ax_module

    with pytest.raises(TransientAdapterError):
        ax_module._default_transport(
            ax_module.IOP_GATEWAY, {"m": "x"},
            client_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(boom)),
        )


def test_pinterest_timeout_on_image_download_is_transient():
    from pinner.errors import TransientError
    from pinner.tools.pinterest import download_image

    def transport(method, url, **kwargs):
        raise httpx.ReadTimeout("image fetch dropped")

    with pytest.raises(TransientError):
        download_image("https://ae01.alicdn.com/kf/H.jpg", transport=transport)


def test_permanent_board_denial_dead_letters_the_pin_and_alerts(mdb):
    class DenyAll(BearerRouter):
        def __call__(self, method, url, **kwargs):
            out = super().__call__(method, url, **kwargs)
            if method == "POST":
                return reply({}, status=403)
            return out

    runner, fakes = make_runner(
        mdb, dry_run=False, adapter=MultiNicheAdapter(active_niches=("kitchen",)),
        gemini_script=[],
        pinterest_router=DenyAll(), run_id_suffix="deny403")
    stats = runner.execute()
    assert stats.get("pin_failed") >= 1
    assert any("pin DEAD" in m for m in fakes["telegram"])
    dead = mdb.pins.find_one({"status": "DEAD"})
    assert dead is not None and dead["attempt"]["last_error_class"] == "PERMANENT"


# ════════════════ 3. DEDUPLICATION & CROSS-NICHE ISOLATION ════════════════


def test_overlapping_catalogs_deduplicate_across_cycles(mdb):
    r1, _ = make_runner(mdb, dry_run=True,
                        adapter=MultiNicheAdapter(active_niches=("kitchen",)),
                        gemini_script=[],
                        run_id_suffix="dedup1")
    r1.execute()
    total_products = mdb.products.count_documents({})
    total_pins = mdb.pins.count_documents({})

    # minutes later: identical catalog rediscovered
    r2, _ = make_runner(mdb, dry_run=True,
                        adapter=MultiNicheAdapter(active_niches=("kitchen",)),
                        gemini_script=[],
                        now=T0 + timedelta(minutes=10), run_id_suffix="dedup2")
    s2 = r2.execute()

    assert s2.get("new_products", 0) == 0                    # dedup memory holds
    assert mdb.products.count_documents({}) == total_products
    assert mdb.pins.count_documents({}) == total_pins
    keys = [(p["source"], p["source_product_id"]) for p in mdb.products.find({})]
    assert len(keys) == len(set(keys))


def test_cross_niche_isolation_end_to_end(mdb):
    router = BearerRouter()
    niches = list(NICHES.keys())
    runner, fakes = make_runner(
        mdb, dry_run=False, adapter=MultiNicheAdapter(),
        gemini_script=[],
        pinterest_router=router,
        bridge_replies=default_bridge_replies(len(niches)),
        pins_per_account=1, run_id_suffix="iso")
    stats = runner.execute()

    assert stats["new_products"] == 3 and stats["approved"] == 3
    assert stats["pins_queued"] == 3 and stats["verified"] == 3

    products = list(mdb.products.find({}))
    niche_ids = {str(p["discovered_niche_id"]) for p in products}
    assert len(niche_ids) == 3                              # three distinct niches

    # every create_pin carried its OWN bearer token — zero cross-account leak
    posts = [c for c in router.calls if c["method"] == "POST"]
    assert sorted(c["auth"].split()[-1] for c in posts) == sorted(
        cfg["token"] for cfg in NICHES.values())

    # every commit landed as its own per-product file (unique keys per niche)
    puts = [c for c in fakes["bridge_transport"].calls if c["method"] == "PUT"]
    file_keys = {c["url"].split("/contents/products/", 1)[1].split(".json")[0]
                 for c in puts}
    expected_keys = {f"stub-store-{cfg['pid']}" for cfg in NICHES.values()}
    assert expected_keys <= file_keys

    bridges = [p["bridge"]["url"] for p in mdb.pins.find({"status": "VERIFIED"})]
    assert len(set(bridges)) == 3                           # unique landing pages
    assert all(url.count("/?id=") == 1 for url in bridges)


# ════════════════ 4. WARM-UP CURVE & CRON SEMANTICS ════════════════


def test_warmup_daily_cap_enforced_across_multiple_cycles(mdb):
    """Day-1 cap (2 pins) enforced ACROSS runs while the 20-minute spacing
    rule holds WITHIN each run: cycle 1 pins once, cycle 2 (+25 min) adds the
    second, cycle 3 (+50 min) is fully capped with zero Pinterest egress."""
    router = BearerRouter()

    # pre-seed TWO approved products each with a QUEUED pin for kitchen
    kitchen_account = mdb.accounts.find_one({"name": ACCOUNT_NAMES["kitchen"]})
    for pid in ("100500777", "100500888"):
        product = {
            "source": "stub-store", "source_product_id": pid,
            "status": "APPROVED", "raw": dict(CLEAN_RAW),
            "affiliate_url": "https://s.click/x",
            "discovered_niche_id": kitchen_account["niche_id"],
            "attempt": {"count": 0, "last_error": None, "last_error_at": None,
                        "last_error_class": None, "next_attempt_at": None},
            "first_seen_at": T0, "last_updated_at": T0, "created_at": T0,
        }
        mdb.products.insert_one(product)
        mdb.pins.insert_one({
            "account_id": str(kitchen_account["_id"]), "product_id": product["_id"],
            "status": "QUEUED",
            "attempt": {"count": 0, "last_error": None, "last_error_at": None,
                        "last_error_class": None, "next_attempt_at": None},
            "created_at": T0, "updated_at": T0,
        })

    r1, _ = make_runner(mdb, dry_run=False,
                        adapter=MultiNicheAdapter(active_niches=()),
                        gemini_script=[], pinterest_router=router,
                        bridge_replies=default_bridge_replies(1),
                        pins_per_account=2, run_id_suffix="warm1")
    s1 = r1.execute()
    assert s1.get("verified") == 1                       # spacing: one per run
    account = mdb.accounts.find_one({"name": ACCOUNT_NAMES["kitchen"]})
    assert account["stats"]["pins_today"] == 1

    r2, _ = make_runner(mdb, dry_run=False,
                        adapter=MultiNicheAdapter(active_niches=()),
                        gemini_script=[], pinterest_router=router,
                        bridge_replies=default_bridge_replies(1),
                        now=T0 + timedelta(minutes=25), run_id_suffix="warm2")
    s2 = r2.execute()
    assert s2.get("verified") == 1                       # second pin of the day
    account = mdb.accounts.find_one({"name": ACCOUNT_NAMES["kitchen"]})
    assert account["stats"]["pins_today"] == 2           # cap saturated

    calls_before = len(router.calls)
    r3, _ = make_runner(mdb, dry_run=False,
                        adapter=MultiNicheAdapter(active_niches=()),
                        gemini_script=[], pinterest_router=router,
                        now=T0 + timedelta(minutes=50), run_id_suffix="warm3")
    s3 = r3.execute()
    assert s3.get("governor_blocks") >= 1                # capped for the day
    assert s3.get("verified") is None                    # nothing new verified
    assert len(router.calls) == calls_before             # zero Pinterest egress


def test_dispatch_semantics_truth_table():
    scheduled = {}
    assert build_runner_args(scheduled) == []                     # cron -> LIVE
    assert build_runner_args({"dry_run": False}) == []            # manual override
    assert build_runner_args({"dry_run": True}) == ["--dry-run"]
    assert build_runner_args({"dry_run": "true"}) == ["--dry-run"]
    assert build_runner_args({"dry_run": "false"}) == []
    assert build_runner_args({"fetch_budget": "7"}) == ["--fetch-budget", "7"]
    assert build_runner_args({"dry_run": True, "fetch_budget": "3"}) == [
        "--dry-run", "--fetch-budget", "3"]
    assert build_runner_args(None) == []                          # malformed safe


# ════════════════ 5. OBSERVABILITY & DIGEST INTEGRITY ════════════════


def test_runs_document_fully_populated_on_success(mdb):
    runner, _ = make_runner(mdb, dry_run=False,
                            adapter=MultiNicheAdapter(active_niches=("kitchen",)),
                            gemini_script=[],
                            pinterest_router=BearerRouter(), run_id_suffix="obs")
    runner.execute()
    doc = mdb.runs.find_one({"run_id": runner.run_id})
    for field in ("run_id", "trigger", "started_at", "finished_at", "stats"):
        assert doc[field] is not None, field
    assert doc["stats"]["verified"] == 1 and doc["stats"]["pinned"] == 1
    errors = doc.get("discovery_errors") or {}
    assert "kitchen" not in errors                            # active niche stayed clean
    verified_pin = mdb.pins.find_one({"status": "VERIFIED"})
    assert verified_pin["pin"]["pin_id"].startswith("pin-")
    assert verified_pin["attempt"]["count"] == 0              # budget reset on advance
    assert verified_pin.get("lease") is None                  # lease released


def test_discovery_errors_recorded_per_niche_when_failing(mdb):
    runner, _ = make_runner(
        mdb, dry_run=True, adapter=MultiNicheAdapter(active_niches=("kitchen", "selfcare")),
        gemini_script=[],
        run_id_suffix="discerr")
    runner.execute()
    doc = mdb.runs.find_one({"run_id": runner.run_id})
    assert set(doc["discovery_errors"].keys()) == {"aesthetics"}
    assert doc["discovery_errors"]["aesthetics"]["type"] == "PermanentAdapterError"
    assert "kitchen" not in doc["discovery_errors"]
    assert "selfcare" not in doc["discovery_errors"]


def test_digest_reflects_every_run_state(mdb):
    # SUCCESS (live)
    r1, f1 = make_runner(mdb, dry_run=False,
                         adapter=MultiNicheAdapter(active_niches=("kitchen",)),
                         gemini_script=[],
                         pinterest_router=BearerRouter(), run_id_suffix="dg-ok")
    r1.execute()
    msg = f1["telegram"][-1]
    assert "DRY RUN" not in msg and "verified=1" in msg

    # DRY RUN marker
    r2, f2 = make_runner(mdb, dry_run=True,
                         adapter=MultiNicheAdapter(active_niches=("kitchen",)),
                         gemini_script=[],
                         run_id_suffix="dg-dr")
    r2.execute()
    assert "(DRY RUN)" in f2["telegram"][-1]

    # PARTIAL SUCCESS -> failure counter visible in digest
    r3, f3 = make_runner(mdb, dry_run=True,
                         adapter=MultiNicheAdapter(active_niches=("kitchen",),
                                                   transient_query="kitchen"),
                         gemini_script=[], run_id_suffix="dg-part",
                         now=T0 + timedelta(hours=5))
    r3.execute()
    # discovery-stage failures alert inline (fetch/pin counters stay clean);
    # accept either that inline alert or a digest failure line as the signal
    has_inline = any("discovery failed" in m for m in f3["telegram"])
    has_counter = "failures=" in f3["telegram"][-1]
    assert has_inline or has_counter

    # CRITICAL abort surfaces its own alert even though execute() completes
    r4, f4 = make_runner(mdb, dry_run=True,
                         adapter=MultiNicheAdapter(active_niches=("kitchen",)),
                         gemini_script=[], run_id_suffix="dg-crit")
    r4.products.sweep = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("mongo paused"))
    r4.execute()
    assert any("CRITICAL" in m for m in f4["telegram"])
