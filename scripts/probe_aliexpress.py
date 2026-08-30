"""One-call AliExpress forensics probe: raw discovery + a LIVE REQUEST
MATRIX for link.generate (gateway x promotion_link_type x params), persisted
to Atlas smoke_reports so results are readable without Actions logs."""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from pinner.adapters.aliexpress import AliExpressAdapter  # noqa: E402
from pinner.config import load_settings  # noqa: E402
from pinner.repo.mongo import get_client  # noqa: E402


def sign(secret: str, params: dict) -> str:
    joined = "".join(f"{k}{params[k]}" for k in sorted(params))
    return hashlib.md5((secret + joined + secret).encode()).hexdigest().upper()


def raw_link_call(settings, gateway: str, url: str, link_type: int,
                  extra: dict) -> tuple[int, str]:
    form = {
        "method": "aliexpress.affiliate.link.generate",
        "app_key": settings.aliexpress_app_key,
        "timestamp": str(int(time.time() * 1000)),
        "format": "json",
        "v": "2.0",
        "sign_method": "md5",
        # LIVE-VALIDATED 2026-08-30: plain string. JSON-array encoding
        # returns 405 "The result is empty" on every product (root cause of
        # the go-live incident).
        "source_values": url,
        "tracking_id": settings.aliexpress_tracking_id,
        "promotion_link_type": str(link_type),
        **{k: str(v) for k, v in extra.items()},
    }
    form["sign"] = sign(settings.aliexpress_app_secret, form)
    with httpx.Client(timeout=25.0) as client:
        response = client.post(gateway, data=form)
    return response.status_code, response.text


def main() -> int:
    parser = argparse.ArgumentParser(description="AliExpress raw-response probe")
    parser.add_argument("--keywords", default="kitchen organizer")
    args = parser.parse_args()

    settings = load_settings()
    masked = settings.aliexpress_app_key[:4] + "…" if settings.aliexpress_app_key else "(EMPTY)"
    print(f"app_key (masked): {masked}")
    print(f"tracking_id: {settings.aliexpress_tracking_id or '(EMPTY)'}\n")

    adapter = AliExpressAdapter(
        settings.aliexpress_app_key,
        settings.aliexpress_app_secret,
        settings.aliexpress_tracking_id,
    )

    # ── discovery ────────────────────────────────────────────────────────
    try:
        candidates = adapter.search_products(args.keywords, max_results=3)
    except Exception as exc:
        print(f"DISCOVERY FAILED: {type(exc).__name__}: {exc}")
        return 1
    print(f"DISCOVERY OK: {len(candidates)} candidates")
    for c in candidates:
        print(f"  - {c.source_product_id}: {c.title[:70]}")
        print(f"    url: {c.product_url[:110]}")

    if not candidates:
        return 1
    full = candidates[0].product_url
    stripped = full.split("?")[0]
    bare = f"https://www.aliexpress.com/item/{candidates[0].source_product_id}.html"

    # ── REQUEST MATRIX: gateway x link_type x url shape x extras ─────────
    print("\n== LINK REQUEST MATRIX ==")
    matrix: dict[str, str] = {}
    gateways = ["https://api-sg.aliexpress.com/sync",
                "https://api.aliexpress.com/sync"]
    shapes = [("verbatim-full", full), ("stripped", stripped), ("bare", bare)]

    # MULTIPLE PRODUCTS: a single ineligible product 405s every shape; three
    # products isolate product-eligibility from tracking-id validity.
    for c in candidates[1:3]:
        shapes.append(("alt-" + c.source_product_id[-6:], c.product_url.split("?")[0]))
    variants = [(0, {}), (2, {}),
                (0, {"target_currency": "USD", "target_language": "EN"}),
                (1, {})]
    for gateway in gateways:
        for link_type, extra in variants:
            for shape_label, url in shapes:
                label = (f"{gateway.split('//')[1].split('.')[0]}/t{link_type}"
                         f"{'/cur' if extra else ''}/{shape_label}")
                try:
                    status, body = raw_link_call(settings, gateway, url,
                                                 link_type, extra)
                    ok = '"promotion_link"' in body
                    verdict = "OK" if ok else "EMPTY/ERR"
                    snippet = body[:200]
                    matrix[label] = f"{verdict} http{status} {snippet}"
                    print(f"  {label:44s} {verdict} http{status} | {snippet[:130]}")
                except Exception as exc:
                    matrix[label] = f"ERR {type(exc).__name__}: {exc}"[:240]
                    print(f"  {label:44s} {matrix[label]}")

    # ── persist to Atlas ─────────────────────────────────────────────────
    try:
        db = get_client(settings.mongo_uri)[settings.mongo_db]
        db.smoke_reports.update_one(
            {"run_tag": "link-matrix-latest"},
            {"$set": {"matrix": matrix, "product_url": full,
                      "product_id": candidates[0].source_product_id,
                      "updated": time.time()}},
            upsert=True,
        )
        print("\nMatrix persisted to smoke_reports/link-matrix-latest")
    except Exception as exc:
        print(f"\n(matrix persist failed: {type(exc).__name__}: {exc})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
