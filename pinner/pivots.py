"""Auto-apply pivot engine (2026-09-07): the analyst's weekly proposals go
from advisory-HITL to applied-autonomously.

Safety contract — the analyst is an LLM and its proposals are UNTRUSTED text:
  * Only WHITELISTED niche fields are touchable (tone_guidelines, the
    content_style subfields). Banned topics, board lists, schema, quotas are
    never writable by proposals.
  * Every applied pivot is capped (MAX_GUIDELINE_CHARS) and APPENDED to
    tone_guidelines as a dated strategy note — the seed voice is never lost,
    and the strategist prompt naturally carries the last pivots.
  * Every pivot (applied or skipped) is persisted to `strategy_pivots` with
    its aggregate snapshot: full audit history, revertable.
  * MIN_IMPRESSIONS floor: below it, proposals are logged but NOT applied —
    the tiny-sample rule the analyst itself is told to follow, enforced
    deterministically instead of hoped for.
"""

from __future__ import annotations

import logging
from datetime import datetime

from pymongo.errors import PyMongoError

logger = logging.getLogger(__name__)

MAX_GUIDELINE_CHARS = 400
MAX_PIVOTS_PER_WEEK = 5
MIN_IMPRESSIONS_TO_APPLY = 50

# ProposalItem.target -> (niche field, content_style subfield or None)
TARGET_FIELDS = {
    "title_style": ("content_style", "title_style"),
    "description_style": ("content_style", "description_style"),
    # hashtag range is guardrail-owned: proposals land as advisories only
    "hashtags": ("content_style", "hashtag_count_range"),
    "landing_angle": ("tone_guidelines", None),
    "board_strategy": ("tone_guidelines", None),
}
# Fields a proposal may NEVER write (guardrail/safety-owned):
PROTECTED = ("banned_topics", "board_keywords", "hashtag_count_range", "quotas")


def _utcnow() -> datetime:
    return datetime.utcnow()


def apply_pivots(db, proposal, *, aggregate: dict, now: datetime | None = None) -> dict:
    """Apply an analyst proposal to the niche strategy config. Returns a
    report {applied: [...], skipped: [...], reasons: {...}} — never raises on
    storage failure (logged; the FYI message reflects partial application)."""
    now = now or _utcnow()
    total_impressions = aggregate.get("impressions") or 0
    applied: list[dict] = []
    skipped: list[dict] = []

    for item in proposal.proposals:
        target = getattr(item, "target", None)
        change = getattr(item, "change", "")[:MAX_GUIDELINE_CHARS]
        rationale = getattr(item, "rationale", "")[:300]

        # guard 1: tiny sample -> log, do not act
        if total_impressions < MIN_IMPRESSIONS_TO_APPLY:
            skipped.append({"target": target, "reason": "sample below "
                f"{MIN_IMPRESSIONS_TO_APPLY} impressions — widening measurement, not acting"})
            continue
        # guard 2: whitelist
        if target not in TARGET_FIELDS:
            skipped.append({"target": target, "reason": "unrecognized target field"})
            continue

        niches = db.niches.find({})
        # a strategy pivot steers ALL niches' future pins (the aggregate is
        # global); per-niche scoping arrives when per-niche aggregates exist.
        for niche in niches:
            field, sub = TARGET_FIELDS[target]
            try:
                if field == "tone_guidelines" and sub is None:
                    note = (f"\n[Strategy pivot {now:%Y-%m-%d}] {target}: {change} "
                            f"(data: {rationale})")
                    db.niches.update_one(
                        {"_id": niche["_id"]},
                        {"$push": {"strategy_pivots": {
                            "target": target, "change": change, "rationale": rationale,
                            "applied_at": now, "week_impressions": total_impressions,
                        }}},
                    )
                    # the strategist prompt reads tone_guidelines — carry the
                    # pivot into the live voice, capped so the seed voice
                    # always dominates over accumulated pivots
                    new_guidelines = (niche.get("tone_guidelines", "")
                                     + note[:MAX_GUIDELINE_CHARS])
                    db.niches.update_one(
                        {"_id": niche["_id"]},
                        {"$set": {"tone_guidelines": new_guidelines,
                                  "updated_at": now}},
                    )
                else:
                    # content_style subfields: title_style/description_style are
                    # free text knobs; hashtag range stays PROTECTED (advisory)
                    if sub in PROTECTED or sub == "hashtag_count_range":
                        skipped.append({"target": target,
                                        "reason": f"{sub} is guardrail-owned, advisory only"})
                        continue
                    db.niches.update_one(
                        {"_id": niche["_id"]},
                        {"$set": {f"content_style.{sub}": change[:120],
                                  "updated_at": now}},
                    )
                applied.append({"niche": niche.get("name"), "target": target,
                                "change": change})
            except PyMongoError as exc:  # storage failure: log, keep going
                logger.warning("[pivot] storage failure for %s: %s", target, exc)
                skipped.append({"target": target, "reason": f"storage: {exc}"})

    # always persist the full decision record
    try:
        db.strategy_pivots.insert_one({
            "ts": now,
            "applied": applied,
            "skipped": skipped,
            "keep_doing": getattr(proposal, "keep_doing", ""),
            "summary": getattr(proposal, "summary", ""),
            "aggregate_snapshot": {
                "pins_measured": aggregate.get("pins_measured"),
                "impressions": aggregate.get("impressions"),
                "outbound_clicks": aggregate.get("outbound_clicks"),
                "ctr": aggregate.get("ctr"),
            },
        })
    except PyMongoError as exc:
        logger.warning("[pivot] could not persist decision record: %s", exc)
    return {"applied": applied, "skipped": skipped}
