"""Wire live custom domains into every account record (launch step).

Sets accounts.site.custom_domain per niche and clears stale bridge caches so
the very next run builds pin URLs on the production domains. Idempotent.

Usage: python scripts/wire_domains.py \
    --kitchen neatspacekitchen.store \
    --aesthetics neatspaceaesthetics.site \
    --selfcare neatspaceselfcare.online
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pinner.config import load_settings  # noqa: E402
from pinner.repo.mongo import get_client  # noqa: E402

DOMAIN_FLAG = "--kitchen"


def apply_domains(db, mapping: dict[str, str]) -> dict:
    """Core, db-injectable for tests: sets domains, forces board refresh,
    clears stale bridge URLs on unpublished docs. Returns a summary."""
    summary = {"domains": {}, "bridges_reset": 0}
    for name, domain in mapping.items():
        result = db.accounts.update_one(
            {"name": name},
            {"$set": {
                "site.custom_domain": domain,
                "site.https_prefix": "https://",
                "boards_cache": [],            # force refresh against live boards
                "boards_fetched_at": None,
            }},
        )
        if result.matched_count == 0:
            summary["domains"][name] = "NOT FOUND"
            continue
        account = db.accounts.find_one({"name": name})
        summary["domains"][name] = {
            "domain": domain, "repo": account["site"]["repo_full_name"],
        }

    reset = db.pins.update_many(
        {"bridge.url": {"$exists": True},
         "status": {"$in": ["QUEUED", "ENRICHED", "BRIDGED", "PAUSED"]}},
        {"$unset": {"bridge": ""}},
    )
    summary["bridges_reset"] = reset.modified_count
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Wire custom domains per account")
    parser.add_argument("--kitchen", required=True)
    parser.add_argument("--aesthetics", required=True)
    parser.add_argument("--selfcare", required=True)
    parser.add_argument("--db")
    args = parser.parse_args()

    settings = load_settings()
    db = get_client(settings.mongo_uri)[args.db or settings.mongo_db]
    summary = apply_domains(db, {
        "NeatSpace Kitchen": args.kitchen,
        "NeatSpace Aesthetics": args.aesthetics,
        "NeatSpace Selfcare": args.selfcare,
    })
    import json

    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
