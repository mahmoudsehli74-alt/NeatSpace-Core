"""The cron runner (Phase 2 finale): wires every validated layer together.

Shaped EXACTLY like the hello pipeline (sweep -> governor -> claim -> stage
executor -> guarded write) but with real integrations injected via
``RunnerDeps`` — which is also how the integration test runs the whole system
on fakes against a real MongoDB.

Stage executors never decide control flow; they raise, and the claim loops
map exceptions onto the state machine's failure classes via the shared
taxonomy (pinner.errors): PermanentError/GuardrailError -> PERMANENT, else
TRANSIENT. PinterestTokenExpired triggers decrypt -> refresh -> rotate ->
retry-once. Gemini usage is counted per UTC day so the governor's RPD guard
spans runs.

--dry-run: discovery -> fetch -> moderation -> enrich -> bridge -> deploy
verify complete for real, but Pinterest is NEVER called (no token needed) —
pins stop at BRIDGED for a supervised first cycle.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from pymongo.errors import DuplicateKeyError

from pinner import notify
from pinner.adapters.base import (
    CandidateProduct,
    PermanentError,
    StoreAdapter,
)
from pinner.agents import DEFAULT_MODEL, Moderator, Strategist
from pinner.agents.guardrails import GuardrailError
from pinner.agents.schemas import ModerationVerdict
from pinner.crypto.tokens import TokenDecryptionError
from pinner.errors import PermanentError as ToolPermanentError
from pinner.errors import TransientError as ToolTransientError
from pinner.governor.quotas import bump_pin_stats, decide, should_graduate
from pinner.repo.engine import utcnow
from pinner.repo.pins import PinsRepo
from pinner.repo.products import ProductsRepo
from pinner.runner.tokens import TokenStore
from pinner.tools.bridge import BridgeTool
from pinner.tools.http import Transport
from pinner.tools.pinterest import PinterestTokenExpired, PinterestTool

DISCLOSURE = "As an affiliate, we may earn from qualifying purchases."
BOARDS_MAX_AGE_DAYS = 7
DRY_RUN_EVENTS = ("CLAIM_BRIDGE", "CLAIM_VERIFY", "CLAIM_ENRICH")  # no CLAIM_PIN


@dataclass
class RunnerDeps:
    adapter: StoreAdapter
    moderator: Moderator
    strategist: Strategist
    bridge: BridgeTool
    token_store: TokenStore
    pinterest_factory: Callable[[str], PinterestTool]
    telegram: Callable[[str], None]
    image_fetcher: Callable[[str], bytes]  # raises Transient/Permanent on failure
    pinterest_transport: Transport | None = None  # observability for tests


@dataclass
class RunnerConfig:
    dry_run: bool = False
    fetch_budget: int = 10
    moderation_budget: int = 10
    pins_per_account: int = 2


def _landing_payload(product: dict, content: dict, product_key: str) -> dict:
    raw = product.get("raw") or {}
    return {
        "key": product_key,
        "title": content["title"],
        "description": content["description"],
        "hashtags": content["hashtags"],
        "landing_angle": content["landing_angle"],
        "board_choice": content["board_choice"],
        "product": {
            "title": raw.get("title"),
            "price": raw.get("price"),
            "image": (raw.get("images") or [None])[0],
            "images": (raw.get("images") or [])[:5],
            "source_url": raw.get("source_url"),
        },
        "affiliate_url": product.get("affiliate_url"),
        "disclosure": DISCLOSURE,
    }


class Runner:
    def __init__(
        self,
        db,
        deps: RunnerDeps,
        *,
        config: RunnerConfig | None = None,
        run_id: str | None = None,
        now: datetime | None = None,
    ) -> None:
        self.db = db
        self.deps = deps
        self.cfg = config or RunnerConfig()
        self.run_id = run_id or f"run-{uuid.uuid4().hex[:10]}"
        self._now_value = now
        self.products = ProductsRepo(db)
        self.pins = PinsRepo(db)
        self.stats: dict[str, int] = defaultdict(int)

    def now(self) -> datetime:
        return self._now_value or utcnow()

    # ------------------------------------------------------------------ helpers

    def _alert(self, text: str) -> None:
        self.deps.telegram(text)

    def _gemini_calls_today(self) -> int:
        doc = self.db.counters.find_one({"_id": "gemini_calls"})
        if doc and doc.get("date") == self.now().strftime("%Y-%m-%d"):
            return int(doc.get("count", 0))
        return 0

    def _count_gemini(self, uses: int = 1) -> None:
        today = self.now().strftime("%Y-%m-%d")
        result = self.db.counters.update_one(
            {"_id": "gemini_calls", "date": today}, {"$inc": {"count": uses}}
        )
        if result.matched_count == 0:
            self.db.counters.update_one(
                {"_id": "gemini_calls"},
                {"$set": {"date": today, "count": uses}},
                upsert=True,
            )

    @staticmethod
    def _error_class(exc: Exception) -> str:
        if isinstance(exc, (PermanentError, ToolPermanentError, GuardrailError)):
            return "PERMANENT"
        return "TRANSIENT"

    # ------------------------------------------------------------------ execute

    def execute(self) -> dict:
        run_doc = {
            "run_id": self.run_id,
            "trigger": "manual" if self.cfg.dry_run else "cron",
            "dry_run": self.cfg.dry_run,
            "started_at": self.now(),
            "stats": {},
        }
        self.db.runs.insert_one(run_doc)
        try:
            self.products.sweep(run_id=self.run_id, now=self.now())
            self.pins.sweep(run_id=self.run_id, now=self.now())
            self._discover()
            self._fetch_stage()
            self._moderation_stage()
            self._publication_stage()
        except Exception as exc:  # critical: keep the run record, alert, exit clean
            self.stats["critical_errors"] = self.stats.get("critical_errors", 0) + 1
            self._alert(f"CRITICAL run {self.run_id} aborted: {type(exc).__name__}: {exc}")
        finally:
            self.db.runs.update_one(
                {"run_id": self.run_id},
                {"$set": {"finished_at": utcnow(), "stats": dict(self.stats)}},
            )
        self._send_digest()
        return dict(self.stats)

    # ------------------------------------------------------------------ stages

    def _discover(self) -> None:
        active = list(self.db.accounts.find({"status": {"$in": ["ACTIVE", "WARMUP"]}}))
        niches = {n["_id"]: n for n in self.db.niches.find()}
        seen_niches: set = set()
        for account in active:
            niche = niches.get(account.get("niche_id"))
            if niche is None or niche["_id"] in seen_niches:
                continue
            seen_niches.add(niche["_id"])
            keywords = (niche.get("board_keywords") or ["home"])[0]
            try:
                candidates = self.deps.adapter.search_products(keywords, max_results=8)
            except Exception as exc:
                self.stats["discovery_errors"] += 1
                detail = str(exc)[:400]  # adapter diagnostics now embedded in msg
                self._alert(
                    f"[{self.run_id}] discovery failed for {niche['name']}: "
                    f"{type(exc).__name__}: {detail}"
                )
                self.db.runs.update_one(
                    {"run_id": self.run_id},
                    {"$set": {f"discovery_errors.{niche['name']}": {
                        "type": type(exc).__name__, "detail": detail}}},
                )
                continue
            for candidate in candidates:
                status, _ = self.products.upsert_candidate(
                    candidate.source,
                    candidate.source_product_id,
                    extra={"discovered_niche_id": niche["_id"]},
                    now=self.now(),
                )
                self.stats["discovered"] += 1
                if status == "created":
                    self.stats["new_products"] += 1

    def _fetch_stage(self) -> None:
        for _ in range(self.cfg.fetch_budget):
            claimed = self.products.claim_next_fetch(self.run_id, now=self.now())
            if claimed is None:
                return
            candidate = CandidateProduct(
                source=claimed["source"],
                source_product_id=claimed["source_product_id"],
                title=(claimed.get("raw") or {}).get("title", ""),
                image_url=None,
                product_url=(claimed.get("raw") or {}).get("source_url", ""),
            )
            try:
                raw = self.deps.adapter.get_product_details(candidate)
                affiliate_url = self.deps.adapter.build_affiliate_url(
                    raw.get("source_url") or candidate.product_url
                )
                self.products.transition(
                    claimed["_id"],
                    "FETCH_OK",
                    patch={"raw": raw, "affiliate_url": affiliate_url},
                    run_id=self.run_id,
                    now=self.now(),
                )
                self.stats["fetched"] += 1
            except Exception as exc:
                self.products.fail(
                    claimed["_id"],
                    error=str(exc),
                    error_class=self._error_class(exc),
                    run_id=self.run_id,
                    now=self.now(),
                )
                self.stats["fetch_failed"] += 1
                if self._error_class(exc) == "PERMANENT":
                    self._alert(
                        f"[{self.run_id}] product DEAD (fetch): {str(exc)[:200]}"
                    )

    def _moderation_stage(self) -> None:
        for _ in range(self.cfg.moderation_budget):
            claimed = self.products.claim_next_moderation(self.run_id, now=self.now())
            if claimed is None:
                return
            try:
                self._count_gemini()
                verdict: ModerationVerdict = self.deps.moderator.review(claimed.get("raw") or {})
                event = "MODERATE_APPROVE" if verdict.verdict == "APPROVE" else "MODERATE_REJECT"
                self.products.transition(
                    claimed["_id"],
                    event,
                    patch={"moderation": verdict.model_dump()},
                    run_id=self.run_id,
                    now=self.now(),
                )
                self.stats["moderated"] += 1
                if verdict.verdict == "APPROVE":
                    self.stats["approved"] += 1
                    self._enqueue_pins(claimed)
                else:
                    self.stats["rejected"] += 1
            except Exception as exc:
                self.products.fail(
                    claimed["_id"],
                    error=str(exc),
                    error_class=self._error_class(exc),
                    run_id=self.run_id,
                    now=self.now(),
                )
                self.stats["moderation_failed"] += 1

    def _enqueue_pins(self, product_doc: dict) -> None:
        niche_id = product_doc.get("discovered_niche_id")
        if niche_id is None:
            return
        for account in self.db.accounts.find(
            {"niche_id": niche_id, "status": {"$in": ["ACTIVE", "WARMUP"]}}
        ):
            pin = {
                "account_id": str(account["_id"]),
                "product_id": product_doc["_id"],
                "status": "QUEUED",
                "attempt": {"count": 0, "last_error": None, "last_error_at": None,
                            "last_error_class": None, "next_attempt_at": None},
                "created_at": self.now(),
                "updated_at": self.now(),
            }
            try:
                self.db.pins.insert_one(pin)
                self.stats["pins_queued"] += 1
            except DuplicateKeyError:
                pass  # already queued for this account — the backstop working

    def _publication_stage(self) -> None:
        accounts = list(self.db.accounts.find({"status": {"$in": ["ACTIVE", "WARMUP"]}}))
        niches = {n["_id"]: n for n in self.db.niches.find()}
        for account in accounts:
            account_id = str(account["_id"])
            if not self.cfg.dry_run:
                try:
                    self.deps.token_store.access_token(account_id)
                except (KeyError, TokenDecryptionError) as exc:
                    # Missing OR corrupted credentials: isolate this account,
                    # alert loudly, never block the other niches.
                    self.stats["accounts_without_tokens"] = (
                        self.stats.get("accounts_without_tokens", 0) + 1
                    )
                    self._alert(
                        f"[{self.run_id}] skipping {account.get('name')!r}: {exc}"
                    )
                    continue
            if should_graduate(account, self.now()):
                self.db.accounts.update_one({"_id": account["_id"]}, {"$set": {"status": "ACTIVE"}})
                account = self.db.accounts.find_one({"_id": account["_id"]})
            # Bound = COMPLETED pins this run, not stage claims: one pin walks
            # enrich -> bridge -> pin -> verify (4 claims) inside this loop.
            pins_done = 0
            while pins_done < self.cfg.pins_per_account:
                fresh = self.db.accounts.find_one({"_id": account["_id"]})
                decision = decide(
                    fresh, now=self.now(), gemini_calls_today=self._gemini_calls_today()
                )
                if not decision.allowed:
                    self.stats["governor_blocks"] += 1
                    break
                events = DRY_RUN_EVENTS if self.cfg.dry_run else None
                claimed = self.pins.claim_next_for_account(
                    account_id, self.run_id, events=events, now=self.now()
                )
                if claimed is None:
                    break
                pin_doc, _event = claimed
                verified_before = self.stats.get("verified", 0)
                try:
                    self._run_pin_stage(pin_doc, fresh, niches.get(fresh.get("niche_id")) or {})
                except PinterestTokenExpired:
                    self.stats["token_refreshes"] += 1
                    try:
                        self.deps.token_store.refresh(account_id, now=self.now())
                        self._retry_after_refresh(pin_doc, fresh)
                    except Exception as exc:
                        self.pins.fail(
                            pin_doc["_id"], error=f"token refresh failed: {exc}",
                            error_class="TRANSIENT", run_id=self.run_id, now=self.now(),
                        )
                except Exception as exc:
                    error_class = self._error_class(exc)
                    self.pins.fail(
                        pin_doc["_id"], error=str(exc), error_class=error_class,
                        run_id=self.run_id, now=self.now(),
                    )
                    self.stats["pin_failed"] += 1
                    if error_class == "PERMANENT":
                        self._alert(
                            f"[{self.run_id}] pin DEAD: {str(exc)[:200]}"
                        )
                if self.stats.get("verified", 0) > verified_before:
                    pins_done += 1

    def _retry_after_refresh(self, pin_doc: dict, account: dict) -> None:
        """Token was refreshed: run the SAME stage again with the new token,
        against freshly-read account state (boards cache/timestamps may have
        been updated since this loop iteration started)."""
        niches = {n["_id"]: n for n in self.db.niches.find()}
        fresh_pin = self.db.pins.find_one({"_id": pin_doc["_id"]})
        fresh_account = self.db.accounts.find_one({"_id": account["_id"]})
        if fresh_pin and fresh_account and fresh_pin["status"] in ("PINNING", "VERIFYING"):
            self._run_pin_stage(
                fresh_pin, fresh_account, niches.get(fresh_account.get("niche_id")) or {}
            )

    def _run_pin_stage(self, pin_doc: dict, account: dict, niche: dict) -> None:
        status = pin_doc["status"]
        if status == "ENRICHING":
            self._stage_enrich(pin_doc, account, niche)
        elif status == "BRIDGING":
            self._stage_bridge(pin_doc, account)
        elif status == "PINNING":
            self._stage_pin(pin_doc, account)
        elif status == "VERIFYING":
            self._stage_verify(pin_doc)

    def _stage_enrich(self, pin_doc: dict, account: dict, niche: dict) -> None:
        product = self.db.products.find_one({"_id": pin_doc["product_id"]})
        boards = [b.get("name", "") for b in (account.get("boards_cache") or [])]
        self._count_gemini()
        content = self.deps.strategist.create((product or {}).get("raw") or {}, niche, boards)
        self.pins.transition(
            pin_doc["_id"], "ENRICH_OK", patch={"content": content.model_dump()},
            run_id=self.run_id, now=self.now(),
        )
        self.stats["enriched"] += 1

    def _stage_bridge(self, pin_doc: dict, account: dict) -> None:
        product = self.db.products.find_one({"_id": pin_doc["product_id"]})
        content = pin_doc["content"]
        repo = account["site"]["repo_full_name"]
        subdomain = repo.split("/")[-1]
        product_key = f"{product['source']}-{product['source_product_id']}"
        base = account["site"].get("custom_domain") or f"{subdomain}.github.io"
        base = base if base.startswith("http") else f"https://{base}"
        payload = _landing_payload(product, content, product_key)
        result = self.deps.bridge.push_product(repo, product_key, payload)
        json_url = f"{base}/products/{product_key}.json"
        if not self.deps.bridge.verify_deployed(json_url):
            raise ToolTransientError(f"bridge not deployed after push: {json_url}")
        bridge_url = f"{base}/?id={product_key}"
        self.pins.transition(
            pin_doc["_id"],
            "BRIDGE_OK",
            patch={"bridge": {
                "url": bridge_url, "json_url": json_url,
                "commit_sha": result["commit_sha"], "pushed_at": self.now(),
            }},
            run_id=self.run_id, now=self.now(),
        )
        self.stats["bridged"] += 1

    def _boards_map(self, account: dict) -> dict[str, str]:
        boards = account.get("boards_cache") or []
        fetched = account.get("boards_fetched_at")
        stale = (
            not boards
            or fetched is None
            or (self.now() - fetched).days > BOARDS_MAX_AGE_DAYS
        )
        if stale and not self.cfg.dry_run:
            tool = self._pinterest(account)
            boards = tool.list_boards()
            fetched_at = self.now()
            self.db.accounts.update_one(
                {"_id": account["_id"]},
                {"$set": {"boards_cache": boards, "boards_fetched_at": fetched_at}},
            )
            account["boards_cache"] = boards
            account["boards_fetched_at"] = fetched_at  # same-run stages must see freshness
        return {b.get("name", ""): b["id"] for b in account.get("boards_cache") or []}

    def _pinterest(self, account: dict) -> PinterestTool:
        token = self.deps.token_store.access_token(str(account["_id"]))
        return self.deps.pinterest_factory(token)

    def _stage_pin(self, pin_doc: dict, account: dict) -> None:
        content = pin_doc["content"]
        bridge_url = pin_doc["bridge"]["url"]
        boards = self._boards_map(account)
        board_id = boards.get(content.get("board_choice", ""))
        if not board_id:
            raise ToolPermanentError(
                f"board {content.get('board_choice')!r} not found on account "
                f"{account.get('name')!r} (available: {sorted(boards)})"
            )
        tool = self._pinterest(account)
        existing = tool.find_pin_by_link(board_id, bridge_url)  # reconcile first
        if existing:
            pin_id = existing["id"]
        else:
            product = self.db.products.find_one({"_id": pin_doc["product_id"]})
            image_url = ((product or {}).get("raw", {}).get("images") or [None])[0]
            if not image_url:
                raise ToolPermanentError("product has no image to pin")
            image_bytes = self.deps.image_fetcher(image_url)
            created = tool.create_pin(
                board_id=board_id,
                title=content["title"][:95],
                description=content["description"][:480],
                link=bridge_url,
                image_bytes=image_bytes,
                alt_text=content["title"][:95],
            )
            pin_id = created["pin_id"]
        self.pins.transition(
            pin_doc["_id"], "PIN_OK",
            patch={"pin": {"pin_id": pin_id, "url": f"https://www.pinterest.com/pin/{pin_id}/"}},
            run_id=self.run_id, now=self.now(),
        )
        self.stats["pinned"] += 1

    def _stage_verify(self, pin_doc: dict) -> None:
        pin_id = pin_doc["pin"]["pin_id"]
        # account fetched for the token
        account = self.db.accounts.find_one({"_id": _to_oid(pin_doc["account_id"])})
        tool = self._pinterest(account)
        fetched = tool.get_pin(pin_id)
        if not fetched.get("id"):
            raise ToolTransientError(f"pin {pin_id} not readable yet")
        self.pins.transition(
            pin_doc["_id"], "VERIFY_OK", run_id=self.run_id, now=self.now()
        )
        self.stats["verified"] += 1
        fresh = self.db.accounts.find_one({"_id": account["_id"]})
        self.db.accounts.update_one(
            {"_id": account["_id"]},
            {"$set": {"stats": bump_pin_stats(fresh.get("stats") or {}, self.now())}},
        )

    def _send_digest(self) -> None:
        s = dict(self.stats)
        mode = " (DRY RUN)" if self.cfg.dry_run else ""
        lines = [
            f"NeatSpace run {self.run_id}{mode}",
            f"discovered={s.get('discovered', 0)} new={s.get('new_products', 0)} "
            f"fetched={s.get('fetched', 0)} approved={s.get('approved', 0)} "
            f"rejected={s.get('rejected', 0)}",
            f"enriched={s.get('enriched', 0)} bridged={s.get('bridged', 0)} "
            f"pinned={s.get('pinned', 0)} verified={s.get('verified', 0)}",
        ]
        failures = s.get("fetch_failed", 0) + s.get("moderation_failed", 0) + s.get("pin_failed", 0)
        if failures:
            lines.append(f"failures={failures} (transient+permanent)")
        if s.get("critical_errors"):
            lines.append("CRITICAL errors occurred — check runs collection")
        self.deps.telegram("\n".join(lines))


def build_runner_args(inputs: dict) -> list[str]:
    """Translate GitHub workflow inputs into runner CLI args.

    Dispatch-semantics truth table (unit-tested in test_qa_audit.py):
      * scheduled runs send NO inputs -> live (no --dry-run),
      * manual dry_run=true/True/true-string -> --dry-run,
      * manual dry_run=false -> live override honored,
      * fetch_budget passes through verbatim when provided.
    """
    if not isinstance(inputs, dict):
        inputs = {}
    args: list[str] = []
    raw_dry = inputs.get("dry_run")
    is_dry = raw_dry is True or (isinstance(raw_dry, str) and raw_dry.strip().lower() == "true")
    if is_dry:
        args.append("--dry-run")
    budget = inputs.get("fetch_budget")
    if budget not in (None, "", False):
        args += ["--fetch-budget", str(budget)]
    return args


def cli_from_env() -> int:
    """Workflow entrypoint: reads toJSON(inputs) from RUNNER_INPUTS env so
    cron-vs-dispatch behavior lives in ONE tested place instead of bash."""
    import json
    import os

    try:
        inputs = json.loads(os.environ.get("RUNNER_INPUTS") or "{}")
    except json.JSONDecodeError:
        inputs = {}
    return main(build_runner_args(inputs))


def _to_oid(value):
    from bson import ObjectId

    return ObjectId(value) if isinstance(value, str) else value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NeatSpace cron runner")
    parser.add_argument("--dry-run", action="store_true",
                        help="full pipeline but Pinterest is never called")
    parser.add_argument("--fetch-budget", type=int, default=10)
    parser.add_argument("--pins-per-account", type=int, default=2)
    parser.add_argument("--db", help="override MONGO_DB from the environment")
    args = parser.parse_args(argv)

    import os

    from pinner.adapters.base import get_adapter
    from pinner.agents import build_agents
    from pinner.config import load_settings, require_credentials
    from pinner.crypto.tokens import load_master_key
    from pinner.repo.mongo import get_client
    from pinner.tools.bridge import BridgeTool as _Bridge
    from pinner.tools.pinterest import PinterestTool as _Pinterest
    from pinner.tools.pinterest import download_image as _download_image

    settings = load_settings()

    # Fail fast with NAMED missing secrets instead of opaque gateway errors.
    features = ["aliexpress", "gemini"]
    if not args.dry_run:
        features += ["pinterest"]
    require_credentials(settings, *features)
    if not settings.bridge_pat:
        import logging

        logging.warning(
            "BRIDGE_PAT empty (also checked legacy GITHUB_BRIDGE_PAT) — "
            "bridge stage will fail later; set it in GitHub Secrets"
        )

    db = get_client(settings.mongo_uri)[args.db or settings.mongo_db]

    def telegram(text: str) -> None:
        notify.send_telegram(
            text, bot_token=settings.telegram_bot_token, chat_id=settings.telegram_chat_id
        )

    model = os.environ.get("AGENT_MODEL") or DEFAULT_MODEL
    moderator, strategist = build_agents(settings.gemini_api_key, model=model)
    deps = RunnerDeps(
        adapter=get_adapter(
            "aliexpress",
            app_key=settings.aliexpress_app_key,
            app_secret=settings.aliexpress_app_secret,
            tracking_id=settings.aliexpress_tracking_id,
        ),
        moderator=moderator,
        strategist=strategist,
        bridge=_Bridge(settings.bridge_pat),
        token_store=TokenStore(
            db,
            load_master_key(settings.token_master_key),
            app_id=settings.pinterest_app_id,
            app_secret=settings.pinterest_app_secret,
        ),
        pinterest_factory=lambda token: _Pinterest(token),
        telegram=telegram,
        image_fetcher=_download_image,
    )
    config = RunnerConfig(
        dry_run=args.dry_run,
        fetch_budget=args.fetch_budget,
        pins_per_account=args.pins_per_account,
    )
    stats = Runner(db, deps, config=config).execute()
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
