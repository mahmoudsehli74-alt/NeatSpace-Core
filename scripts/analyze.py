"""Performance Analyst CLI: aggregate the learning loop and send the proposal
to Telegram for one-tap human approval. Never applies changes by itself.

Usage: python scripts/analyze.py [--account <name>]"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pinner.config import load_settings  # noqa: E402
from pinner.metrics import aggregate  # noqa: E402
from pinner.notify import send_telegram  # noqa: E402
from pinner.repo.mongo import get_client  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Weekly performance analysis -> Telegram")
    parser.add_argument("--account", help="limit to one account name")
    parser.add_argument("--db", help="override MONGO_DB")
    args = parser.parse_args()

    settings = load_settings()
    db = get_client(settings.mongo_uri)[args.db or settings.mongo_db]

    account_id = None
    if args.account:
        doc = db.accounts.find_one({"name": args.account})
        if doc is None:
            print(f"unknown account {args.account!r}")
            return 1
        account_id = str(doc["_id"])

    data = aggregate(db, account_id=account_id)
    print(json.dumps(data, indent=2))
    if data["pins_measured"] == 0:
        send_telegram(
            "[Analyst] No measurable pins yet — let the pipeline accrue data.",
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
        )
        return 0

    from pinner.agents.analyst import Analyst
    from pinner.agents.client import GeminiJsonClient

    client = GeminiJsonClient(settings.gemini_api_key)
    proposal = Analyst(client).review(data)

    lines = ["📊 Weekly Performance Proposal", "", proposal.summary, ""]
    for item in proposal.proposals:
        lines.append(f"• [{item.target}] {item.change}")
        lines.append(f"   why: {item.rationale}")
    lines += ["", f"Keep doing: {proposal.keep_doing}"]
    send_telegram(
        "\n".join(lines),
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
    )
    print("Proposal sent to Telegram.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
