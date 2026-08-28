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

    # ── 1. discover ─────────────────────────────────────────────────────
    banner("1. ALIEXPRESS DISCOVERY (live)")
    adapter = get_adapter(
        "aliexpress", app_key=settings.aliexpress_app_key,
        app_secret=settings.aliexpress_app_secret,
        tracking_id=settings.aliexpress_tracking_id,
    )
    t0 = time.time()
    candidates = adapter.search_products(args.keywords, max_results=3)
    if not candidates:
        print("!! empty result — aborting")
        return 1
    cand = candidates[0]
    print(f"{STEP} {len(candidates)} candidates in {time.time() - t0:.1f}s")
    print(f"{STEP} chosen: {cand.source_product_id} — {cand.title[:60]}")

    raw = adapter.get_product_details(cand)
    assert raw["title"] and raw["images"], "details incomplete"
    print(f"{STEP} details: price={raw['price'].get('current')} images={len(raw['images'])}")

    # ── 2. affiliate link ───────────────────────────────────────────────
    banner("2. AFFILIATE LINK (live IOP link.generate)")
    t0 = time.time()
    affiliate_url = adapter.build_affiliate_url(
        raw["source_url"] or cand.product_url, product_id=cand.source_product_id
    )
    print(f"{STEP} {affiliate_url}  ({time.time() - t0:.1f}s)")
    assert affiliate_url.startswith("https://"), "affiliate link not https"

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
            return 0
    else:
        print(f"{STEP} skipped (no GEMINI_API_KEY)")

    # ── 4. image: real download + 2:3 ───────────────────────────────────
    banner("4. IMAGE PIPELINE (live download + 2:3 compositor)")
    import io

    from PIL import Image

    image_bytes = download_image(raw["images"][0])
    print(f"{STEP} downloaded {len(image_bytes)} bytes")
    vertical = to_vertical(image_bytes)
    w, h = Image.open(io.BytesIO(vertical)).size
    print(f"{STEP} composited {w}x{h} (ratio {w / h:.3f}, target 0.667) "
          f"-> {len(vertical)} bytes")
    assert abs(w / h - 2 / 3) < 0.02

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
