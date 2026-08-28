"""Operator console: status, kill switch, dead-letter requeue.

Usage (from the repo root, .env present):
    python scripts/ops.py status
    python scripts/ops.py pause  --account "NeatSpace Kitchen"
    python scripts/ops.py resume --account "NeatSpace Kitchen"
    python scripts/ops.py requeue --collection pins --id <ObjectId>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pinner import ops  # noqa: E402
from pinner.config import load_settings  # noqa: E402
from pinner.repo.mongo import get_client  # noqa: E402


def _default(str_value: str) -> object:
    from bson import ObjectId

    try:
        return ObjectId(str_value)
    except Exception:
        return str_value


def main() -> int:
    parser = argparse.ArgumentParser(description="NeatSpace operator console")
    parser.add_argument("--db", help="override MONGO_DB from the environment")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="one-glance health snapshot")

    p_pause = sub.add_parser("pause", help="kill switch: pause account + its pins")
    p_pause.add_argument("--account", required=True)

    p_resume = sub.add_parser("resume", help="resume a paused account")
    p_resume.add_argument("--account", required=True)

    p_requeue = sub.add_parser("requeue", help="requeue a DEAD document with fresh attempts")
    p_requeue.add_argument("--collection", choices=["pins", "products"], required=True)
    p_requeue.add_argument("--id", required=True)

    p_reset = sub.add_parser("reset-products",
                             help="bulk-revive DEAD_FETCH products (incident-scoped)")
    p_reset.add_argument("--error", action="append", default=[],
                         help="only products whose last_error contains this substring "
                              "(repeatable). Default: MissingParameter")
    p_reset.add_argument("--all", action="store_true",
                         help="sweep ALL DEAD_FETCH products (explicit)")
    p_reset.add_argument("--limit", type=int, default=500)

    args = parser.parse_args()
    settings = load_settings()
    client = get_client(settings.mongo_uri)
    db = client[args.db or settings.mongo_db]
    try:
        if args.command == "status":
            print(json.dumps(ops.status_summary(db), indent=2, default=str))
        elif args.command == "pause":
            n = ops.pause_account(db, args.account)
            print(f"paused {n} pin(s) for {args.account!r}; account status -> PAUSED")
        elif args.command == "resume":
            n = ops.resume_account(db, args.account)
            print(f"resumed {n} pin(s) for {args.account!r}; account status -> ACTIVE")
        elif args.command == "reset-products":
            substrings = tuple(args.error) if args.error else (("MissingParameter",))
            if args.all:
                substrings = ()
            report = ops.reset_dead_products(db, error_substrings=substrings,
                                             limit=args.limit)
            print(f"reset {report['reset']} DEAD_FETCH product(s) -> PENDING_FETCH")
        elif args.command == "requeue":
            doc = ops.requeue(db, args.collection, _default(args.id))
            print(f"requeued {args.collection} {args.id} -> {doc['status']}")
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
