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
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pinner.adapters.base import get_adapter  # noqa: E402
from pinner.config import load_settings  # noqa: E402
from pinner.imaging import to_vertical  # noqa: E402
from pinner.repo.mongo import get_client  # noqa: E402
from pinner.tools.pinterest import PinterestTool, download_image  # noqa: E402

STEP = "  ▸"


def banner(title: str) -> None:
    print(f"\n{'=' * 62}\n {title}\n{'=' * 62}")


class StageReport:
    """Persists per-stage evidence to Atlas (smoke_reports) so autonomous
    iteration can read live results without Actions log access."""

    def __init__(self, db, run_tag: str) -> None:
        self.db = db
        self.doc = {"run_tag": run_tag, "stages": {}, "pin_url": None,
                    "destination": None, "ok": False, "started": time.time()}

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

    def _flush(self) -> None:
        try:
            self.doc["updated"] = time.time()
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

    settings = load_settings()
    db = get_client(settings.mongo_uri)[args.db or settings.mongo_db]
    import uuid

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

    # DEFENSIVE EXTRACTION: try each candidate until one yields a usable
    # (title + images) payload. Vendor drift on one listing must never kill
    # the smoke run — it warns, dumps raw keys for forensics, and moves on.
    cand, raw = None, None
    for index, candidate in enumerate(candidates):
        try:
            probe = adapter.get_product_details(candidate)
        except Exception as exc:
            print(f"{STEP} candidate {index} ({candidate.source_product_id}) "
                  f"fetch failed: {type(exc).__name__}: {str(exc)[:120]} — skipping")
            continue
        title = (probe.get("title") or "").strip()
        images = [u for u in (probe.get("images") or [])
                  if isinstance(u, str) and u.startswith("http")]
        if not title or not images:
            print(f"{STEP} candidate {index} ({candidate.source_product_id}) "
                  f"malformed (title={'ok' if title else 'MISSING'}, "
                  f"images={len(images)}) — raw keys: {sorted(probe.keys())} — skipping")
            continue
        cand, raw = candidate, probe
        print(f"{STEP} candidate {index} accepted: {cand.source_product_id} — "
              f"{cand.title[:60]}")
        break
    if cand is None:
        print("!! no usable candidate in this batch — payload keys were dumped "
              "above for schema forensics; retry with different --keywords")
        report.fail("extract", "no usable candidate in batch")
        return 1
    report.record("discover", product_id=cand.source_product_id, title=cand.title[:120])
    print(f"{STEP} details: price={raw['price'].get('current')} "
          f"images={len(raw['images'])}")

    # ── 2. affiliate link ───────────────────────────────────────────────
    banner("2. AFFILIATE LINK (live IOP link.generate)")
    t0 = time.time()
    affiliate_url = adapter.build_affiliate_url(
        raw["source_url"] or cand.product_url, product_id=cand.source_product_id
    )
    print(f"{STEP} {affiliate_url}  ({time.time() - t0:.1f}s)")
    if not affiliate_url.startswith("https://"):
        print("!! affiliate link is not https — invalid for Pinterest")
        report.fail("affiliate", f"non-https link: {affiliate_url[:120]}")
        return 1
    report.record("affiliate", affiliate_url=affiliate_url)

    # ── 3. moderation (live, if key present) ────────────────────────────
    banner("3. GEMINI MODERATION (live)")
    if settings.gemini_api_key:
        from pinner.agents import build_agents

        moderator, _ = build_agents(settings.gemini_api_key)
        verdict = moderator.review(raw)
        print(f"{STEP} verdict={verdict.verdict} conf={verdict.confidence:.2f} "
              f"reasons={verdict.reasons}")
        if verdict.verdict != "APPROVE":
            print("!! product rejected by live moderation — smoke result: VALID (pipeline correct)")
            report.record("moderation", verdict=verdict.verdict)
            report.finish(ok=True)
            return 0
        report.record("moderation", verdict=verdict.verdict)
    else:
        print(f"{STEP} skipped (no GEMINI_API_KEY)")
        report.record("moderation", skipped=True)

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
    commit = bridge.push_product(repo, product_key, payload)
    print(f"{STEP} committed={commit['committed']} sha={commit['commit_sha']} "
          f"path={commit['path']}")
    json_url = f"https://{domain}/products/{product_key}.json"
    deployed = bridge.verify_deployed(json_url, attempts=6, wait_seconds=10)
    print(f"{STEP} deployed({json_url}): {deployed}")
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
