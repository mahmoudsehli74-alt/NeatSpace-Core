"""Seed niches + the three launch accounts.

Usage (from the repo root, .env present):
    python scripts/seed.py --github-user <your-github-username>

Idempotent: re-running refreshes config (repo names, quotas) but never
resets status, warm-up progress, or stats.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pinner.config import load_settings  # noqa: E402
from pinner.repo.mongo import get_client, migrate  # noqa: E402
from pinner.seeds import seed_accounts  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed niches and accounts")
    parser.add_argument("--github-user", required=True, help="GitHub owner of the bridge repos")
    parser.add_argument("--db", help="override MONGO_DB from the environment")
    args = parser.parse_args()

    settings = load_settings()
    db_name = args.db or settings.mongo_db
    client = get_client(settings.mongo_uri)
    try:
        migrate(client[db_name])
        report = seed_accounts(client[db_name], github_user=args.github_user)
    finally:
        client.close()

    print(f"Seeded database {db_name!r}:")
    print(f"  niches:   {', '.join(report['niches'])}")
    print(f"  accounts: {', '.join(report['accounts'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
