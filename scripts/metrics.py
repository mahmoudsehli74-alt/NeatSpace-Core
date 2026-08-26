"""Collect Pinterest metrics for all active accounts into pin_metrics.

Usage (from repo root, .env present):
    python scripts/metrics.py            # all ACTIVE/WARMUP accounts
    python scripts/metrics.py --db other
Idempotent: one snapshot per (pin, UTC day)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pinner.config import load_settings  # noqa: E402
from pinner.metrics import collect_account_metrics  # noqa: E402
from pinner.repo.mongo import get_client  # noqa: E402
from pinner.runner.tokens import TokenStore  # noqa: E402
from pinner.tools.pinterest import PinterestTool  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect Pinterest pin metrics")
    parser.add_argument("--db", help="override MONGO_DB")
    args = parser.parse_args()

    settings = load_settings()
    db = get_client(settings.mongo_uri)[args.db or settings.mongo_db]
    store = TokenStore(
        db,
        bytes.fromhex(settings.token_master_key),
        app_id=settings.pinterest_app_id,
        app_secret=settings.pinterest_app_secret,
    )

    reports = []
    for account in db.accounts.find({"status": {"$in": ["ACTIVE", "WARMUP"]}}):
        try:
            token = store.access_token(str(account["_id"]))
        except KeyError:
            reports.append({"account": account.get("name"), "error": "no stored token"})
            continue
        tool = PinterestTool(token)
        reports.append(collect_account_metrics(db, tool, account))

    print(json.dumps(reports, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
