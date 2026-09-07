"""Performance Analyst CLI — AUTOPILOT paradigm (2026-09-07 operator directive).

The analyst's weekly proposals are now APPLIED AUTONOMOUSLY to the niche
strategy config (via pinner.pivots.apply_pivots — whitelisted fields only,
sample-size floor, full audit trail), and the Telegram message became an FYI
executive summary of what the system HAS ALREADY applied for the week ahead.
No approval buttons; no human-in-the-loop.

Usage: python scripts/analyze.py [--account <name>]"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pinner.config import load_settings  # noqa: E402
from pinner.metrics import aggregate  # noqa: E402
from pinner.notify import send_telegram  # noqa: E402
from pinner.pivots import apply_pivots  # noqa: E402
from pinner.repo.mongo import get_client  # noqa: E402


def _fyi_message(proposal, report: dict, data: dict) -> str:
    """Executive summary: what was measured, what the system already applied,
    what it deliberately deferred, and what to keep doing."""
    applied = report.get("applied") or []
    skipped = report.get("skipped") or []
    lines = [
        "📊 Weekly Performance Review — AUTOPILOT",
        "",
        f"Measured: {data['pins_measured']} pins, {data['impressions']} impressions, "
        f"{data['outbound_clicks']} outbound clicks (CTR {data['ctr']}).",
        "",
        f"Summary: {proposal.summary}",
        "",
    ]
    if applied:
        lines.append(f"✅ Applied for the coming week ({len(applied)} pivot(s)):")
        for a in applied[:5]:
            lines.append(f"  • [{a['target']}/{a.get('niche', 'all')}] {a['change'][:120]}")
    else:
        lines.append("ℹ️ No pivots applied this week (see deferred below).")
    if skipped:
        lines.append("")
        lines.append(f"⏸ Deferred ({len(skipped)}):")
        for s in skipped[:3]:
            lines.append(f"  • [{s['target']}] {s['reason'][:100]}")
    lines += ["", f"Keep doing: {proposal.keep_doing}",
              "", "This report is FYI — changes are already active. "
                  "Full audit trail in the strategy_pivots collection."]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Weekly analysis -> auto-apply -> FYI Telegram")
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

    from pinner.agents import DEFAULT_MODEL
    from pinner.agents.analyst import Analyst
    from pinner.agents.client import GeminiJsonClient

    # model is a required kwarg on GeminiJsonClient — the inaugural analyst
    # run died on exactly this missing kwarg. Same override as the runner.
    client = GeminiJsonClient(settings.gemini_api_key,
                              model=os.environ.get("AGENT_MODEL") or DEFAULT_MODEL)
    proposal = Analyst(client).review(data)

    # AUTOPILOT: apply immediately, then report what WAS done.
    report = apply_pivots(db, proposal, aggregate=data)
    print(json.dumps(report, indent=2, default=str))

    send_telegram(
        _fyi_message(proposal, report, data),
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
    )
    applied_n = len(report.get("applied") or [])
    skipped_n = len(report.get("skipped") or [])
    print(f"Autopilot pivots: {applied_n} applied, {skipped_n} deferred. FYI sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
