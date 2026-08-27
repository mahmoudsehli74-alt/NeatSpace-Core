"""One-call AliExpress forensics probe: prints raw HTTP status, headers, and
body of a real discovery request — runnable locally AND in GitHub Actions
(.github/workflows/probe.yml). Never prints the app SECRET (only the signed
form and a masked key)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from pinner.adapters.aliexpress import AliExpressAdapter  # noqa: E402
from pinner.config import load_settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="AliExpress raw-response probe")
    parser.add_argument("--keywords", default="kitchen organizer")
    args = parser.parse_args()

    settings = load_settings()
    masked = settings.aliexpress_app_key[:4] + "…" if settings.aliexpress_app_key else "(EMPTY)"

    def forensic(url: str, form: dict) -> tuple[int, str]:
        with httpx.Client(timeout=20.0) as client:
            response = client.post(url, data=form)
        print("== REQUEST ==")
        print("POST", url)
        safe = {k: (v if k not in ("sign", "app_key") else v[:8] + "…") for k, v in form.items()}
        for key, value in safe.items():
            print(f"  {key}: {value}")
        print("\n== RESPONSE ==")
        print("HTTP", response.status_code)
        for header in ("content-type", "server", "x-request-id", "date"):
            if header in response.headers:
                print(f"  {header}: {response.headers[header]}")
        print("body:", response.text[:4000] or "(empty)")
        return response.status_code, response.text

    adapter = AliExpressAdapter(
        settings.aliexpress_app_key,
        settings.aliexpress_app_secret,
        settings.aliexpress_tracking_id,
        transport=forensic,
    )
    print(f"app_key (masked): {masked}")
    print(f"tracking_id: {settings.aliexpress_tracking_id or '(EMPTY)'}\n")
    try:
        candidates = adapter.search_products(args.keywords, max_results=3)
        print(f"\nPARSED OK: {len(candidates)} candidates")
        for c in candidates:
            print(f"  - {c.source_product_id}: {c.title[:60]}")
    except Exception as exc:
        print(f"\nCLASSIFIED FAILURE: {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
