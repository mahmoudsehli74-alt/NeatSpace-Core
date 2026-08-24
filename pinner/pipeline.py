"""Hello pipeline — the Phase 1 exit gate.

A deterministic, fully-stubbed, RESUMABLE end-to-end walk: one fake product
traverses every state of both machines (ingest: PENDING_FETCH -> FETCHED ->
APPROVED; publication: QUEUED -> ENRICHING -> ... -> VERIFIED) using stub
externals — no network, no LLM, no cost, no nondeterminism.

Because all truth lives in Mongo (the whole thesis of this architecture),
``run_hello_pipeline`` is safe to call again after any interruption: it sweeps
expired leases like the real runner, reads current statuses, and continues
from wherever the documents are. The crash-injection suite exploits exactly
that — kill at any of 12 checkpoints, resume two "hours" later, and the walk
still completes with exactly one pin.

Stub semantics mirror the Phase 2 reconciliation contracts:
  * bridge commit_sha = hash(content)  -> re-pushing is a no-op (adopt-by-hash)
  * pin_id = hash(account, product)    -> re-creating adopts the existing pin
Phase 2 replaces the determinism with real reconciliation queries; the walk's
structure (claim -> work -> guarded write) is the runner's skeleton.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pymongo.errors import DuplicateKeyError

from pinner import seeds
from pinner.governor.quotas import bump_pin_stats, decide
from pinner.repo import engine
from pinner.repo.pins import MACHINE as PUB_MACHINE
from pinner.repo.pins import PinsRepo
from pinner.repo.products import MACHINE as INGEST_MACHINE
from pinner.repo.products import ProductsRepo
from pinner.statemachine import Role, state_role

HELLO_ACCOUNT = "NeatSpace Kitchen"
HELLO_NICHE = "kitchen"
HELLO_SOURCE = "stub-store"
HELLO_PRODUCT_ID = "hello-0001"
CLEAN_TITLE = "NeatSpace Hello Stainless Sink Caddy"
DIRTY_TITLE = "adult novelty gadget"

CRASH_CHECKPOINTS = (
    "after_fetch_claim", "after_fetch_work",
    "after_moderate_claim", "after_moderate_work",
    "after_enrich_claim", "after_enrich_work",
    "after_bridge_claim", "after_bridge_commit",
    "after_pin_claim", "after_pin_create",
    "after_verify_claim", "after_verify_work",
)


class SimulatedCrash(Exception):
    """A killed worker. The next run() call must recover from it."""

    def __init__(self, checkpoint: str) -> None:
        self.checkpoint = checkpoint
        super().__init__(f"simulated crash at {checkpoint}")


@dataclass
class HelloResult:
    outcome: str  # "verified" | "rejected" | "blocked"
    product_id: Any
    pin_id: Any | None
    product_states: list[str]
    pin_states: list[str]
    blocked_reason: str | None = None


def _zero_backoff(count: int) -> int:
    return 0  # stub world: recovered work is immediately re-claimable


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- Deterministic stub externals (Phase 2 swaps these for real tools) ---------------


def stub_fetch(product: dict) -> dict:
    """Stub adapter: normalizes a 'raw' payload."""
    title = product.get("raw", {}).get("title", CLEAN_TITLE)
    return {
        "title": title,
        "description": f"[stub] {title} — great value, high ratings.",
        "images": ["https://stub.cdn/img1.jpg"],
        "price": {"current": 14.99, "currency": "USD", "original": 24.99},
        "rating": 4.8,
        "orders": 1337,
    }


def stub_moderate(product: dict) -> tuple[bool, dict]:
    """Stub Moderator: global policy = reject adult/weapons, else approve."""
    title = (product.get("raw", {}).get("title") or "").lower()
    if "adult" in title or "weapon" in title:
        return False, {"verdict": "REJECT", "reasons": ["policy: adult"], "confidence": 0.99}
    return True, {"verdict": "APPROVE", "reasons": ["clean"], "confidence": 0.95}


def stub_enrich(pin: dict, niche: dict, product: dict) -> dict:
    """Stub Strategist: SEO copy derived from niche + product."""
    title = product["raw"]["title"]
    keywords = niche.get("board_keywords") or ["home"]
    return {
        "title": f"{title} — the {niche['name']} upgrade you didn't know you needed",
        "description": f"[stub] {niche['tone_guidelines'][:60]}...",
        "hashtags": [f"#{k.replace(' ', '')}" for k in keywords[:3]],
        "board_id": "stub-board-1",
        "board_name": keywords[0].title(),
        "landing_angle": "budget-luxury",
        "disclosure_included": True,
    }


def stub_bridge(pin: dict, account: dict) -> dict:
    """Stub bridge push: deterministic commit sha = hash(content) — re-pushing
    the same content is a no-op, which is the adopt-by-hash contract."""
    content = pin.get("content") or {}
    sha = _sha(str(sorted(content.items())))[:10]
    repo = account["site"]["repo_full_name"]
    subdomain = repo.split("/")[-1]
    return {
        "url": f"https://{subdomain}.github.io/p/{HELLO_SOURCE}-{HELLO_PRODUCT_ID}",
        "commit_sha": sha,
        "pushed_at": "stub",
    }


def stub_deploy_check(pin: dict) -> bool:
    """Stub 'GET bridge.url' — always live in the stub world."""
    return bool((pin.get("bridge") or {}).get("url"))


def stub_create_pin(pin: dict, account: dict) -> dict:
    """Stub Pinterest create: pin_id = hash(account, product) — deterministic,
    so re-creating after a crash ADOPTS the same pin instead of duplicating."""
    key = f"{account['_id']}:{pin['product_id']}"
    return {"pin_id": f"pin-{_sha(key)[:12]}", "url": f"https://pinterest.com/pin/{_sha(key)[:8]}"}


def stub_verify(pin: dict) -> bool:
    return bool(pin.get("pin", {}).get("pin_id"))


# --- The walk ------------------------------------------------------------------------


def _crash(crash_at: str | None, checkpoint: str) -> None:
    if crash_at == checkpoint:
        raise SimulatedCrash(checkpoint)


def _state_chain(db, entity: str, entity_id) -> list[str]:
    cursor = db.audit_log.find({"entity": entity, "entity_id": entity_id}).sort("$natural", 1)
    return [entry["to_state"] for entry in cursor]


def _ensure_pin_doc(db, account_id, product_id, now: datetime):
    doc = {
        "account_id": account_id,
        "product_id": product_id,
        "status": "QUEUED",
        "attempt": engine.fresh_attempt(),
        "created_at": now,
        "updated_at": now,
    }
    try:
        db.pins.insert_one(doc)
        return doc["_id"]
    except DuplicateKeyError:
        return db.pins.find_one({"account_id": account_id, "product_id": product_id})["_id"]


def run_hello_pipeline(
    db,
    *,
    run_id: str = "hello",
    now: datetime | None = None,
    crash_at: str | None = None,
    title: str = CLEAN_TITLE,
    github_user: str = "hello",
    backoff: Callable[[int], int] = _zero_backoff,
) -> HelloResult:
    """Drive one product through both machines to a terminal state.

    Resumable by construction: sweeps expired leases first (like the real
    runner), then continues from current document statuses. Raises
    SimulatedCrash when the named checkpoint is reached — the caller recovers
    by simply calling again with a later ``now``.
    """
    now = now if now is not None else engine.utcnow()
    products = ProductsRepo(db)
    pins = PinsRepo(db)

    # 0. Idempotent setup + crash recovery (the real runner's opening moves).
    seeds.seed_niches(db, now=now)
    seeds.seed_one_account(
        db, name=HELLO_ACCOUNT, niche=HELLO_NICHE,
        repo_full_name=f"{github_user}/neatspace-kitchen", now=now,
    )
    products.sweep(run_id=run_id, now=now, backoff=backoff)
    pins.sweep(run_id=run_id, now=now, backoff=backoff)

    _, product_doc = products.upsert_candidate(
        HELLO_SOURCE, HELLO_PRODUCT_ID, now=now
    )
    if title != CLEAN_TITLE:  # let tests steer moderation without re-upserting
        db.products.update_one(
            {"_id": product_doc["_id"]}, {"$set": {"raw.title": title}}
        )
    pid = product_doc["_id"]

    # 1. INGEST machine: fetch -> moderate -> approve/reject.
    while True:
        product = db.products.find_one({"_id": pid})
        status = product["status"]
        if status == "PENDING_FETCH":
            claimed = engine.claim_one(
                db, "products", INGEST_MACHINE, event="CLAIM_FETCH", run_id=run_id,
                extra_filter={"_id": pid}, now=now,
            )
            if claimed is None:
                raise RuntimeError("fetch claim unexpectedly lost")
            _crash(crash_at, "after_fetch_claim")
            raw = stub_fetch(claimed)
            _crash(crash_at, "after_fetch_work")
            products.transition(pid, "FETCH_OK", patch={"raw": raw}, run_id=run_id, now=now)
        elif status == "FETCHED":
            claimed = engine.claim_one(
                db, "products", INGEST_MACHINE, event="CLAIM_MODERATE", run_id=run_id,
                extra_filter={"_id": pid}, now=now,
            )
            if claimed is None:
                raise RuntimeError("moderation claim unexpectedly lost")
            _crash(crash_at, "after_moderate_claim")
            approved, verdict = stub_moderate(claimed)
            _crash(crash_at, "after_moderate_work")
            event = "MODERATE_APPROVE" if approved else "MODERATE_REJECT"
            products.transition(
                pid, event, patch={"moderation": verdict}, run_id=run_id, now=now
            )
        elif status == "APPROVED":
            break
        elif status == "REJECTED":
            return HelloResult(
                "rejected", pid, None,
                _state_chain(db, "products", pid), [],
            )
        elif state_role(INGEST_MACHINE, status) is Role.TERMINAL:
            raise RuntimeError(f"ingest dead-ended in {status}")
        else:
            raise RuntimeError(f"unexpected ingest status after sweep: {status}")

    account = db.accounts.find_one({"name": HELLO_ACCOUNT})
    niche = db.niches.find_one({"_id": account["niche_id"]})
    account_id = str(account["_id"])

    # 2. Governor gates publishing BEFORE any pin work happens.
    decision = decide(account, now=now)
    if not decision.allowed:
        return HelloResult(
            "blocked", pid, None,
            _state_chain(db, "products", pid), [],
            blocked_reason=decision.reason,
        )

    pin_id = _ensure_pin_doc(db, account_id, pid, now)

    # 3. PUBLICATION machine: enrich -> bridge -> pin -> verify.
    did_verify = False
    while True:
        pin = db.pins.find_one({"_id": pin_id})
        status = pin["status"]
        if status == "QUEUED":
            engine.claim_one(
                db, "pins", PUB_MACHINE, event="CLAIM_ENRICH", run_id=run_id,
                extra_filter={"_id": pin_id}, now=now,
            )
            _crash(crash_at, "after_enrich_claim")
            content = stub_enrich(pin, niche, db.products.find_one({"_id": pid}))
            _crash(crash_at, "after_enrich_work")
            pins.transition(
                pin_id, "ENRICH_OK", patch={"content": content}, run_id=run_id, now=now
            )
        elif status == "ENRICHED":
            engine.claim_one(
                db, "pins", PUB_MACHINE, event="CLAIM_BRIDGE", run_id=run_id,
                extra_filter={"_id": pin_id}, now=now,
            )
            _crash(crash_at, "after_bridge_claim")
            bridge = stub_bridge(pin, account)
            _crash(crash_at, "after_bridge_commit")
            pins.transition(
                pin_id, "BRIDGE_OK", patch={"bridge": bridge}, run_id=run_id, now=now
            )
        elif status == "BRIDGED":
            engine.claim_one(
                db, "pins", PUB_MACHINE, event="CLAIM_PIN", run_id=run_id,
                extra_filter={"_id": pin_id}, now=now,
            )
            _crash(crash_at, "after_pin_claim")
            if not stub_deploy_check(pin):  # never pin a dead link
                pins.fail(pin_id, error="bridge not deployed", run_id=run_id, now=now)
                continue
            created = stub_create_pin(pin, account)
            _crash(crash_at, "after_pin_create")
            pins.transition(
                pin_id, "PIN_OK", patch={"pin": created}, run_id=run_id, now=now
            )
        elif status == "PINNED":
            engine.claim_one(
                db, "pins", PUB_MACHINE, event="CLAIM_VERIFY", run_id=run_id,
                extra_filter={"_id": pin_id}, now=now,
            )
            _crash(crash_at, "after_verify_claim")
            ok = stub_verify(pin)
            _crash(crash_at, "after_verify_work")
            pins.transition(pin_id, "VERIFY_OK", run_id=run_id, now=now)
            did_verify = True
            if not ok:  # pragma: no cover - stub always verifies
                pins.fail(pin_id, error="verification failed", run_id=run_id, now=now)
        elif status == "VERIFIED":
            break
        elif state_role(PUB_MACHINE, status) is Role.TERMINAL:
            raise RuntimeError(f"publication dead-ended in {status}")
        else:
            raise RuntimeError(f"unexpected pin status after sweep: {status}")

    # 4. Stats bump only when THIS call performed the verification (idempotent
    #    re-runs of an already-VERIFIED pin never double-count).
    if did_verify:
        fresh = db.accounts.find_one({"_id": account["_id"]})
        db.accounts.update_one(
            {"_id": account["_id"]},
            {"$set": {"stats": bump_pin_stats(fresh.get("stats") or {}, now)}},
        )

    return HelloResult(
        "verified", pid, pin_id,
        _state_chain(db, "products", pid), _state_chain(db, "pins", pin_id),
    )
