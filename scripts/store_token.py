"""Store a Pinterest OAuth token pair for one account (go-live prerequisite).

The encrypted blobs land in oauth_tokens; the master key never leaves env.

Usage (from repo root, .env present):
    python scripts/store_token.py \
        --account "NeatSpace Kitchen" \
        --refresh-token <refresh_token> \
        [--access-token <access_token>]     # optional; auto-refreshed on first use
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pinner.config import load_settings  # noqa: E402
from pinner.crypto.tokens import encrypt_token, load_master_key  # noqa: E402
from pinner.repo.mongo import get_client  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Store Pinterest OAuth tokens")
    parser.add_argument("--account", required=True, help="account name, e.g. 'NeatSpace Kitchen'")
    parser.add_argument("--refresh-token", required=True)
    parser.add_argument("--access-token", default=None,
                        help="optional; if omitted the runner auto-refreshes on first use")
    parser.add_argument("--db", help="override MONGO_DB")
    args = parser.parse_args()

    settings = load_settings()
    key = load_master_key(settings.token_master_key)
    db = get_client(settings.mongo_uri)[args.db or settings.mongo_db]

    account = db.accounts.find_one({"name": args.account})
    if account is None:
        print(f"account not found: {args.account!r} (seed first: scripts/seed.py)")
        return 1

    doc = {
        "account_id": str(account["_id"]),
        "refresh_blob": encrypt_token(key, args.refresh_token),
    }
    if args.access_token:
        doc["access_blob"] = encrypt_token(key, args.access_token)
    db.oauth_tokens.update_one(
        {"account_id": doc["account_id"]}, {"$set": doc}, upsert=True
    )

    stored = ["refresh_blob" + ("" if args.access_token else " (bearer-fallback mode)")]
    if args.access_token:
        stored.append("access_blob")
    print(f"Stored encrypted {', '.join(stored)} for {args.account!r}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
