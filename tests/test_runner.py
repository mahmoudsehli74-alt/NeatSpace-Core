"""Runner integration tests: the WHOLE system on fakes against a real Mongo.

This is the Phase 2 dress rehearsal: adapter + agents + bridge + Pinterest +
token store + telegram are all injected fakes, but the state machine, repos,
governor, crypto envelopes, audit trail, and run bookkeeping are REAL.
"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from pinner.adapters.base import (
    CandidateProduct,
    PermanentAdapterError,
    StoreAdapter,
)
from pinner.agents import GeminiJsonClient, Moderator, Strategist
from pinner.agents.client import AgentSchemaError
from pinner.crypto import tokens as crypto
from pinner.runner.main import Runner, RunnerConfig, RunnerDeps
from pinner.runner.tokens import TokenStore
from pinner.seeds import seed_accounts
from pinner.tools.bridge import BridgeTool
from pinner.tools.http import HttpReply
from pinner.tools.pinterest import PinterestTool
from tests.test_agents import CLEAN_RAW, approve_verdict, good_strategy
from tests.test_tools_bridge import FakeTransport

T0 = datetime(2026, 1, 15, 12, 0, 0)
KEY32 = bytes(range(32))


# --- fakes ----------------------------------------------------------------------------


class FakeAdapter(StoreAdapter):
    name = "stub-store"

    def __init__(self, fail_fetch: Exception | None = None):
        self.fail_fetch = fail_fetch

    def search_products(self, niche_query: str, *, max_results: int = 10):
        pid = niche_query.replace(" ", "-").lower()
        return [
            CandidateProduct(
                source=self.name,
                source_product_id=f"{pid}-1",
                title="Stainless Sink Caddy",
                image_url="https://cdn/H1.jpg",
                product_url=f"https://www.aliexpress.com/item/{pid}1.html",
            )
        ]

    def get_product_details(self, candidate):
        if self.fail_fetch:
            raise self.fail_fetch
        return dict(CLEAN_RAW, source_url=candidate.product_url)

    def build_affiliate_url(self, product_url: str, *, product_id: str | None = None) -> str:
        return "https://s.click.aliexpress.com/e/_TEST"


class ScriptedGemini:
    """Fake genai client executing a script (exception or parsed model)."""

    def __init__(self, *behaviors):
        self.behaviors = list(behaviors)
        self.calls = []

    @property
    def models(self):
        outer = self

        class _Models:
            def generate_content(self, *, model, contents, config):
                outer.calls.append((model, contents, config))
                behavior = outer.behaviors.pop(0) if outer.behaviors else None
                if isinstance(behavior, Exception):
                    raise behavior
                return SimpleNamespace(parsed=behavior, text=None)

        return _Models()


def reply(body: dict, status: int = 200) -> HttpReply:
    return HttpReply(status, json.dumps(body).encode(), "application/json")


def commit_reply() -> HttpReply:
    return reply({"commit": {"sha": "sha-x", "html_url": "https://github.com/c"}}, status=201)


def pinterest_full_script(pin_id: str = "pin-77") -> list[HttpReply]:
    return [
        reply({"items": [{"id": "b-1", "name": "Kitchen Organization"}]}),  # boards
        reply({"items": []}),                                               # find_by_link miss
        reply({"id": pin_id}, status=201),                                  # create pin
        reply({"id": pin_id, "board_id": "b-1"}),                           # verify get_pin
    ]


def make_deps(
    *,
    adapter=None,
    gemini_script=None,
    bridge_replies=None,
    pinterest_replies=None,
    refresh_replies=None,
    strategist_content=None,
) -> tuple[RunnerDeps, dict]:
    gemini = ScriptedGemini(
        *(gemini_script or [approve_verdict(), strategist_content or good_strategy()])
    )
    client = GeminiJsonClient("k", model="test-model", raw=gemini)
    moderator = Moderator(client, image_fetcher=lambda url: None)
    strategist = Strategist(client)

    default_bridge = [
        HttpReply(404, b"{}"),  # GET file: not found
        commit_reply(),  # PUT: committed
        HttpReply(200, b"{}", "application/json"),  # deploy verify
    ]
    bridge_transport = FakeTransport(*(bridge_replies or default_bridge))
    bridge = BridgeTool("pat", transport=bridge_transport)

    pinterest_transport = FakeTransport(*(pinterest_replies or []))
    refresh_transport = FakeTransport(
        *(refresh_replies or [reply({"access_token": "new-at", "refresh_token": "new-rt",
                                     "expires_in": 2592000})])
    )

    telegram_messages: list[str] = []

    def telegram(text: str) -> None:
        telegram_messages.append(text)

    deps = RunnerDeps(
        adapter=adapter or FakeAdapter(),
        moderator=moderator,
        strategist=strategist,
        bridge=bridge,
        token_store=None,  # replaced per-test via closure below
        pinterest_factory=lambda token: PinterestTool(token, transport=pinterest_transport),
        telegram=telegram,
        image_fetcher=lambda url: b"JPEGDATA",
    )

    def token_store_for(db) -> TokenStore:
        store = TokenStore(db, KEY32, app_id="app", app_secret="secret",
                           transport=refresh_transport)
        for name in ("NeatSpace Kitchen",):
            account = db.accounts.find_one({"name": name})
            if account and not db.oauth_tokens.find_one({"account_id": str(account["_id"])}):
                db.oauth_tokens.insert_one({
                    "account_id": str(account["_id"]),
                    "refresh_blob": crypto.encrypt_token(KEY32, "refresh-token-1"),
                })
        return store

    return deps, {
        "gemini": gemini,
        "bridge_transport": bridge_transport,
        "pinterest_transport": pinterest_transport,
        "refresh_transport": refresh_transport,
        "telegram": telegram_messages,
        "token_store_for": token_store_for,
    }


@pytest.fixture()
def mdb(db):
    from pinner.repo.mongo import migrate

    migrate(db)
    seed_accounts(db, github_user="builder", now=T0)
    # focus on ONE niche/account to keep transport scripts linear
    db.accounts.delete_many({"name": {"$ne": "NeatSpace Kitchen"}})
    db.niches.delete_many({"name": {"$ne": "kitchen"}})
    return db


def assemble(mdb, *, now=None, **kwargs) -> tuple[Runner, dict]:
    """Build a Runner from fakes. fakes['telegram'] stays the recorded MESSAGE
    LIST for assertions; the callable rides inside deps."""
    import uuid

    deps, fakes = make_deps(**kwargs)
    deps.token_store = fakes["token_store_for"](mdb)
    fakes["adapter"] = deps.adapter
    fakes["moderator"] = deps.moderator
    fakes["strategist"] = deps.strategist
    fakes["bridge"] = deps.bridge
    fakes["pinterest_factory"] = deps.pinterest_factory
    runner = Runner(
        mdb, deps, config=RunnerConfig(),
        run_id=f"test-run-{uuid.uuid4().hex[:6]}", now=now or T0,
    )
    return runner, fakes


# --- tests ------------------------------------------------------------------------------


def test_full_run_end_to_end_verified(mdb):
    runner, fakes = assemble(mdb, pinterest_replies=pinterest_full_script())
    stats = runner.execute()

    assert stats["discovered"] == 1 and stats["new_products"] == 1
    assert stats["fetched"] == 1 and stats["approved"] == 1
    assert stats["pins_queued"] == 1 and stats["enriched"] == 1
    assert stats["bridged"] == 1 and stats["pinned"] == 1 and stats["verified"] == 1

    product = mdb.products.find_one()
    assert product["status"] == "APPROVED"
    assert product["affiliate_url"] == "https://s.click.aliexpress.com/e/_TEST"

    pin = mdb.pins.find_one()
    assert pin["status"] == "VERIFIED"
    assert pin["pin"]["pin_id"] == "pin-77"
    assert pin["bridge"]["url"].endswith("/?id=stub-store-kitchen-organization-1")
    assert pin["bridge"]["commit_sha"] == "sha-x"

    account = mdb.accounts.find_one({"name": "NeatSpace Kitchen"})
    assert account["stats"]["pins_today"] == 1
    assert account["boards_cache"][0]["id"] == "b-1"  # refreshed during pinning

    run_doc = mdb.runs.find_one({"run_id": runner.run_id})
    assert run_doc["finished_at"] is not None and run_doc["stats"]["verified"] == 1

    assert any("verified=1" in m for m in fakes["telegram"])
    # bridge verified the deployed JSON before BRIDGE_OK
    verify_urls = [c["url"] for c in fakes["bridge_transport"].calls if c["method"] == "GET"]
    assert any("/products/stub-store-kitchen-organization-1.json" in u for u in verify_urls)


def test_dry_run_never_touches_pinterest(mdb):
    runner, fakes = assemble(mdb)
    runner.cfg.dry_run = True
    stats = runner.execute()

    assert stats["bridged"] == 1 and "pinned" not in stats
    assert fakes["pinterest_transport"].calls == []  # zero Pinterest API calls
    pin = mdb.pins.find_one()
    assert pin["status"] == "BRIDGED"
    assert pin["bridge"]["commit_sha"]  # bridge work was REAL
    assert any("DRY RUN" in m for m in fakes["telegram"])


def test_reconcile_adopts_existing_pin(mdb):
    bridge_url_suffix = "?id=stub-store-kitchen-organization-1"
    pinterest = [
        reply({"items": [{"id": "b-1", "name": "Kitchen Organization"}]}),
        reply({"items": [{"id": "pin-existing", "link": f"https://neatspace-kitchen.github.io/{bridge_url_suffix}"}]}),
        reply({"id": "pin-existing", "board_id": "b-1"}),  # verify
    ]
    runner, fakes = assemble(mdb, pinterest_replies=pinterest)
    stats = runner.execute()

    assert stats["pinned"] == 1 and stats["verified"] == 1
    pin = mdb.pins.find_one()
    assert pin["pin"]["pin_id"] == "pin-existing"  # adopted, not duplicated
    methods = [c["method"] for c in fakes["pinterest_transport"].calls]
    assert "POST" not in methods  # create_pin never called


def test_transient_agent_failure_retries_on_a_later_run(mdb):
    """Production semantics: a transient failure reverts the stage with a
    backoff, so the retry happens on the NEXT cron run, not the same one."""
    first, _ = assemble(
        mdb,
        gemini_script=[approve_verdict(), AgentSchemaError("flake")],
    )
    first_stats = first.execute()
    assert first_stats.get("pin_failed") == 1
    pin = mdb.pins.find_one()
    assert pin["status"] == "QUEUED"  # reverted to predecessor
    assert pin["attempt"]["count"] == 1
    assert pin["attempt"]["next_attempt_at"] > T0  # backoff-gated

    # two hours later the backoff has elapsed and the retry succeeds
    second, _ = assemble(
        mdb,
        now=T0.replace(hour=14),
        pinterest_replies=pinterest_full_script(),
        gemini_script=[good_strategy()],  # moderation already done; enrich retries
    )
    second_stats = second.execute()

    assert second_stats["verified"] == 1
    pin = mdb.pins.find_one()
    assert pin["status"] == "VERIFIED"
    assert pin["attempt"]["count"] == 0  # budget reset on the successful advance


def test_permanent_fetch_failure_poisons_and_alerts(mdb):
    runner, fakes = assemble(
        mdb, adapter=FakeAdapter(fail_fetch=PermanentAdapterError("aliexpress", "gone"))
    )
    stats = runner.execute()

    assert stats["fetch_failed"] == 1
    product = mdb.products.find_one()
    assert product["status"] == "DEAD_FETCH"
    assert any("DEAD" in m for m in fakes["telegram"])
    assert mdb.pins.count_documents({}) == 0  # nothing queued from a dead product


def test_token_expiry_refreshes_and_retries(mdb):
    pinterest = [
        reply({"items": [{"id": "b-1", "name": "Kitchen Organization"}]}),  # boards
        HttpReply(401, b"{}", "application/json"),                          # 401!
        reply({"items": []}),                                               # retry: find miss
        reply({"id": "pin-99"}, status=201),                                # create
        reply({"id": "pin-99", "board_id": "b-1"}),                         # verify
    ]
    runner, fakes = assemble(mdb, pinterest_replies=pinterest)
    stats = runner.execute()

    assert stats["verified"] == 1 and stats["token_refreshes"] == 1
    assert fakes["refresh_transport"].calls[0]["url"].endswith("/v5/oauth/token")
    account = mdb.accounts.find_one({"name": "NeatSpace Kitchen"})
    # rotated refresh token persisted (still decryptable with the master key)
    tokens_doc = mdb.oauth_tokens.find_one({"account_id": str(account["_id"])})
    assert crypto.decrypt_token(KEY32, tokens_doc["refresh_blob"]) == "new-rt"
    assert crypto.decrypt_token(KEY32, tokens_doc["access_blob"]) == "new-at"


def test_governor_blocks_when_warmup_cap_saturated(mdb):
    mdb.accounts.update_one(
        {"name": "NeatSpace Kitchen"},
        {"$set": {"stats": {"pins_today": 2, "pins_today_date": T0.strftime("%Y-%m-%d"),
                            "last_pin_at": None}}},
    )
    runner, fakes = assemble(mdb)
    stats = runner.execute()

    assert stats["governor_blocks"] >= 1
    assert "pinned" not in stats
    pin = mdb.pins.find_one()
    assert pin["status"] == "QUEUED"  # never claimed
    assert fakes["pinterest_transport"].calls == []
