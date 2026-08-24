"""Run index migrations against the configured MongoDB.

Usage (from the repo root, with .env present or env vars set):
    python scripts/migrate.py            # uses MONGO_URI / MONGO_DB from env
    python scripts/migrate.py --db other # override the database name

Idempotent: safe to run on every deploy.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pinner.config import load_settings  # noqa: E402
from pinner.repo.mongo import get_client, migrate  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Create/refresh all indexes")
    parser.add_argument("--db", help="override MONGO_DB from the environment")
    args = parser.parse_args()

    settings = load_settings()
    db_name = args.db or settings.mongo_db

    client = get_client(settings.mongo_uri)
    try:
        report = migrate(client[db_name])
    finally:
        client.close()

    print(f"Migrated database {db_name!r}:")
    for collection, indexes in sorted(report.items()):
        print(f"  {collection}: {', '.join(sorted(indexes))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
