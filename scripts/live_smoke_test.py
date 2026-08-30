"""LIVE SMOKE TEST — one real item through the entire pipeline, no mocks.

Executes against production secrets (GitHub Actions / local .env):
  1. DISCOVER   real AliExpress search for a niche keyword
  2. LINK       real affiliate link via IOP link.generate (promotion_link_type 0)
  3. MODERATE   real Gemini verdict (schema-constrained) — skipped on missing key
  4. IMAGE      real download + 2:3 vertical compositor
  5. BRIDGE     real commit of products/<key>.json to the bridge repo
  6. PIN        real multipart creation on Pinterest -> prints pin_id + pin_url

Every stage prints its raw evidence. Exits non-zero on first failure.
Default Pinterest target = the account's FIRST board; pass --board-id to pin
somewhere specific. Safe to re-run: reconciles by link before creating.

Usage (Actions: live_smoke.yml | local: .env present):
    python scripts/live_smoke_test.py [--account "NeatSpace Kitchen"] [--board-id <id>]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# BOOT EVIDENCE: flush before ANY pinner import so even import-time crashes
# leave a readable trace in the committed report.
_BOOT = {"stage": "imports", "started": time.time()}
try:
    Path(".smoke").mkdir(parents=True, exist_ok=True)
    Path(".smoke/latest.json").write_text(json.dumps(_BOOT, default=str), encoding="utf-8")
except Exception:
    pass

from pinner.adapters.base import get_adapter  # noqa: E402
from pinner.config import load_settings  # noqa: E402
from pinner.imaging import to_vertical  # noqa: E402
from pinner.repo.mongo import get_client  # noqa: E402
from pinner.tools.pinterest import PinterestTool, download_image  # noqa: E402

_BOOT.update({"stage": "imports-complete", "ok": True})
try:
    Path(".smoke/latest.json").write_text(json.dumps(_BOOT, default=str), encoding="utf-8")
except Exception:
    pass

STEP = "  ▸"


def banner(title: str) -> None:
    print(f"\n{'=' * 62}\n {title}\n{'=' * 62}")


REPORT_PATH = Path(".smoke/latest.json")


class StageReport:
    """Dual-channel evidence: (1) Atlas smoke_reports for querying, (2) a
    local file flushed at BOOT (pre-network) and every stage, which the
    workflow commits back to the repo — readable even when the run dies
    before any Mongo connection."""

    def __init__(self, db, run_tag: str) -> None:
        self.db = db
        self.doc = {"run_tag": run_tag, "stages": {}, "pin_url": None,
                    "destination": None, "ok": False, "started": time.time()}
        self._file_flush()

    def record(self, stage: str, **fields) -> None:
        self.doc["stages"][stage] = {"ok": True, **fields}
        self._flush()

    def fail(self, stage: str, error: str, **fields) -> None:
        self.doc["stages"][stage] = {"ok": False, "error": str(error)[:600], **fields}
        self.doc["ok"] = False
        self._flush()

    def finish(self, *, ok: bool, pin_url: str | None = None,
               destination: str | None = None) -> None:
        self.doc["ok"] = ok
        self.doc["pin_url"] = pin_url
        self.doc["destination"] = destination
        self.doc["finished"] = time.time()
        self._flush()

    def _file_flush(self) -> None:
        try:
            self.doc["updated"] = time.time()
            REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
            REPORT_PATH.write_text(json.dumps(self.doc, indent=2, default=str),
                                   encoding="utf-8")
        except Exception:
            pass

    def _flush(self) -> None:
        self._file_flush()
        try:
            self.db.smoke_reports.update_one(
                {"run_tag": self.doc["run_tag"]}, {"$set": self.doc}, upsert=True
            )
        except Exception:  # reporting must never kill the run
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="One-item LIVE pipeline smoke test")
    parser.add_argument("--account", default=None, help="account name (default: first with token)")
    parser.add_argument("--board-id", default=None,
                        help="Pinterest board id (default: first board)")
    parser.add_argument("--keywords", default="kitchen organizer")
    parser.add_argument("--skip-pin", action="store_true",
                        help="stop after bridge commit (no Pinterest call)")
    parser.add_argument("--db")
    args = parser.parse_args()

    import uuid

    sha = os.environ.get("GITHUB_SHA", "")[:8]
    boot = StageReport.__new__(StageReport)
    boot.db = None
    boot.doc = {"run_tag": f"smoke-boot-{sha}", "stages": {"boot": {"ok": True}},
                "ok": None, "started": time.time()}
    boot._file_flush()
    print(f"{STEP} boot report flushed: {REPORT_PATH}")

    settings = load_settings()
    db = get_client(settings.mongo_uri)[args.db or settings.mongo_db]
    report = StageReport(db, f"smoke-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}")
    print(f"{STEP} report tag: {report.doc['run_tag']}")

    # ── 0. account selection ────────────────────────────────────────────
    banner("0. ACCOUNT")
    account = (
        db.accounts.find_one({"name": args.account})
        if args.account
        else db.accounts.find_one({"status": {"$in": ["ACTIVE", "WARMUP"]}})
    )
    if account is None:
        print("!! no account found")
        return 1
    repo = account["site"]["repo_full_name"]
    domain = account["site"].get("custom_domain") or f"{repo.split('/')[-1].lower()}.github.io"
    print(f"{STEP} account: {account['name']}")
    print(f"{STEP} repo: {repo} | domain: {domain}")
    report.record("account", account=account["name"], repo=repo, domain=domain)

    # ── 1. discover ─────────────────────────────────────────────────────
    banner("1. ALIEXPRESS DISCOVERY (live)")
    adapter = get_adapter(
        "aliexpress", app_key=settings.aliexpress_app_key,
        app_secret=settings.aliexpress_app_secret,
        tracking_id=settings.aliexpress_tracking_id,
    )
    t0 = time.time()
    try:
        candidates = adapter.search_products(args.keywords, max_results=5)
    except Exception as exc:
        print(f"!! discovery failed: {exc}")
        report.fail("discover", str(exc))
        return 1
    if not candidates:
        print("!! empty result — aborting")
        report.fail("discover", "empty result")
        return 1
    print(f"{STEP} {len(candidates)} candidates in {time.time() - t0:.1f}s")

    # DEFENSIVE CANDIDATE PIPE: each candidate must pass THREE gates — payload
    # extraction, live affiliate link generation, and live Gemini moderation.
    # A single brand-named listing (e.g. "Command" — a 3M trademark that the
    # Moderator correctly REJECTs) or one malformed payload must never kill
    # the run; the pipe advances to the next candidate. Green == a pin was
    # minted, nothing less.
    moderator = None
    if settings.gemini_api_key:
        from pinner.agents import build_agents

        moderator, _ = build_agents(settings.gemini_api_key)

    banner("2-3. CANDIDATE PIPE: details -> affiliate link -> moderation (live)")
    cand = raw = affiliate_url = None
    rejects: dict[str, str] = {}
    for index, candidate in enumerate(candidates):
        pid = candidate.source_product_id
        try:
            probe = adapter.get_product_details(candidate)
        except Exception as exc:
            print(f"{STEP} candidate {index} ({pid}) fetch failed: "
                  f"{type(exc).__name__}: {str(exc)[:120]} — skipping")
            rejects[pid] = f"fetch: {type(exc).__name__}: {str(exc)[:100]}"
            continue
        title = (probe.get("title") or "").strip()
        images = [u for u in (probe.get("images") or [])
                  if isinstance(u, str) and u.startswith("http")]
        if not title or not images:
            print(f"{STEP} candidate {index} ({pid}) malformed "
                  f"(title={'ok' if title else 'MISSING'}, images={len(images)}) "
                  f"— raw keys: {sorted(probe.keys())} — skipping")
            rejects[pid] = f"malformed payload (title={bool(title)}, images={len(images)})"
            continue
        report.record("discover", product_id=pid, title=candidate.title[:120])
        try:
            link = adapter.build_affiliate_url(
                probe.get("source_url") or candidate.product_url,
                product_id=pid,
            )
        except Exception as exc:
            print(f"{STEP} candidate {index} ({pid}) affiliate failed: "
                  f"{type(exc).__name__}: {str(exc)[:140]} — skipping")
            rejects[pid] = f"affiliate: {type(exc).__name__}: {str(exc)[:100]}"
            continue
        if not link.startswith("https://"):
            print(f"{STEP} candidate {index} ({pid}) non-https link — skipping")
            rejects[pid] = f"non-https link: {link[:80]}"
            continue
        report.record("affiliate", affiliate_url=link[:160])
        if moderator is not None:
            verdict = None
            for attempt in (1, 2):
                try:
                    verdict = moderator.review(probe)
                    break
                except Exception as exc:
                    # gemini free tier throws transient 503 "high demand"
                    # spikes — back off once, then let the next candidate
                    # (whose review comes later in time) carry on.
                    print(f"{STEP} candidate {index} ({pid}) moderation call "
                          f"failed (attempt {attempt}/2): {type(exc).__name__}: "
                          f"{str(exc)[:140]}")
                    if attempt == 1:
                        time.sleep(20)
            if verdict is None:
                rejects[pid] = "moderation unavailable (transient agent error)"
                continue
            print(f"{STEP} candidate {index} ({pid}) moderation: "
                  f"{verdict.verdict} conf={verdict.confidence:.2f} "
                  f"reasons={verdict.reasons}")
            if verdict.verdict != "APPROVE":
                rejects[pid] = f"{verdict.verdict}: {str(verdict.reasons)[:100]}"
                continue
        cand, raw, affiliate_url = candidate, probe, link
        print(f"{STEP} candidate {index} ACCEPTED: {pid} — {candidate.title[:60]}")
        break

    if cand is None:
        print("!! every candidate in the batch was rejected before pinning")
        report.fail("moderation",
                    f"all {len(candidates)} candidates rejected before pinning",
                    rejects=rejects)
        return 1
    report.record("discover", product_id=cand.source_product_id,
                  title=cand.title[:120], rejected_candidates=rejects)
    print(f"{STEP} details: price={raw['price'].get('current')} "
          f"images={len(raw['images'])}")
    print(f"{STEP} affiliate link: {affiliate_url}")
    report.record("affiliate", affiliate_url=affiliate_url)
    report.record("moderation",
                  verdict="APPROVE" if moderator is not None else "SKIPPED")

    # ── 4. image: real download + 2:3 ───────────────────────────────────
    banner("4. IMAGE PIPELINE (live download + 2:3 compositor)")
    import io

    from PIL import Image

    image_bytes = download_image(raw["images"][0])
    print(f"{STEP} downloaded {len(image_bytes)} bytes")
    vertical = to_vertical(image_bytes)
    w, h = Image.open(io.BytesIO(vertical)).size
    ratio_ok = abs(w / h - 2 / 3) < 0.02
    print(f"{STEP} composited {w}x{h} (ratio {w / h:.3f}, target 0.667) "
          f"-> {len(vertical)} bytes")
    if not ratio_ok:
        print("!! compositor produced non-2:3 output")
        report.fail("image", f"ratio {w / h:.3f}")
        return 1
    report.record("image", width=w, height=h, source_bytes=len(image_bytes))

    # ── 5. bridge commit (live) ─────────────────────────────────────────
    banner("5. BRIDGE COMMIT (live GitHub Contents API)")
    from pinner.tools.bridge import BridgeTool

    bridge = BridgeTool(settings.bridge_pat or settings.__dict__.get("bridge_pat", ""))
    product_key = f"{cand.source}-{cand.source_product_id}"
    payload = {
        "key": product_key,
        "title": cand.title,
        "description": f"[smoke] {raw['title']} — curated by NeatSpace.",
        "hashtags": ["#smoketest"],
        "landing_angle": "budget-luxury",
        "board_choice": "",
        "product": {"title": raw["title"], "price": raw["price"],
                    "image": raw["images"][0], "images": raw["images"][:5],
                    "source_url": raw["source_url"]},
        "affiliate_url": affiliate_url,
        "disclosure": "As an affiliate, we may earn from qualifying purchases.",
    }
    try:
        commit = bridge.push_product(repo, product_key, payload)
        print(f"{STEP} committed={commit['committed']} sha={commit['commit_sha']} "
              f"path={commit['path']}")
        json_url = f"https://{domain}/products/{product_key}.json"
        # Pages builds take 1-3 min after the Contents commit; poll 12x15s
        deployed = bridge.verify_deployed(json_url, attempts=12, wait_seconds=15)
        print(f"{STEP} deployed({json_url}): {deployed}")
    except Exception as exc:
        # A 403 here almost always means the fine-grained BRIDGE_PAT lacks
        # "Contents: Read and write" — surface it as a diagnosable stage
        # failure instead of an unhandled crash.
        print(f"!! bridge commit failed: {type(exc).__name__}: {exc}")
        report.fail("bridge", f"{type(exc).__name__}: {exc}",
                    hint="BRIDGE_PAT needs Contents:Read+write on the storefront repo "
                         "(fine-grained PAT permission)")
        return 1
    bridge_url = f"https://{domain}/?id={product_key}"
    print(f"{STEP} DESTINATION: {bridge_url}")
    report.record("bridge", commit_sha=commit["commit_sha"], json_url=json_url,
                  deployed=deployed, destination=bridge_url)

    if args.skip_pin:
        print("\nSMOKE OK (bridge-only mode)")
        return 0

    # ── 6. pin (live) ───────────────────────────────────────────────────
    banner("6. PINTEREST PIN (live v5, multipart 2:3)")
    from pinner.crypto.tokens import load_master_key
    from pinner.runner.tokens import TokenStore

    store = TokenStore(db, load_master_key(settings.token_master_key),
                       app_id=settings.pinterest_app_id,
                       app_secret=settings.pinterest_app_secret)
    access = store.access_token(str(account["_id"]))
    tool = PinterestTool(access)

    board_id = args.board_id
    if not board_id:
        boards = tool.list_boards()
        if not boards:
            print("!! account has no boards — create one on Pinterest first")
            report.fail("pin", "account has no boards")
            return 1
        board_id, board_name = boards[0]["id"], boards[0]["name"]
    else:
        board_name = next(
            (b["name"] for b in tool.list_boards() if b["id"] == board_id), str(board_id))
    print(f"{STEP} board: {board_name} ({board_id})")

    existing = tool.find_pin_by_link(board_id, bridge_url)
    if existing:
        pin = {"pin_id": existing["id"],
               "url": f"https://www.pinterest.com/pin/{existing['id']}/"}
        print(f"{STEP} RECONCILED existing pin (no duplicate created)")
    else:
        created = tool.create_pin(
            board_id=board_id, title=cand.title[:95],
            description=f"{raw['title']} — see details at the link.",
            link=bridge_url, image_bytes=vertical, alt_text=cand.title[:95])
        pin = created
        print(f"{STEP} CREATED")

    print(f"\n{'=' * 62}\n LIVE SMOKE RESULT\n{'=' * 62}")
    print(f"  pin_id      : {pin['pin_id']}")
    print(f"  pin_url     : {pin['url']}")
    print(f"  destination : {bridge_url}")
    print(f"  affiliate   : {affiliate_url}")
    print(f"{'=' * 62}")
    report.finish(ok=True, pin_url=pin["url"], destination=bridge_url)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except SystemExit:
        raise
    except Exception:  # last-resort: keep smoke_reports authoritative
        import traceback

        traceback.print_exc()
        code = 1
    raise SystemExit(code)
