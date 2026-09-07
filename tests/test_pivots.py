"""Auto-apply pivot engine tests (autopilot paradigm, 2026-09-07).

The analyst is an LLM; its proposals are UNTRUSTED text that now steers live
strategy. These tests pin the safety contract:
  * whitelist: only the 5 known targets write anything
  * tiny-sample floor: <50 impressions -> logged, never applied
  * guardrail-owned fields (hashtag range, banned topics, boards) untouchable
  * full decision record persists to strategy_pivots
  * tone_guidelines carries the pivot into the strategist's live prompt
"""

from __future__ import annotations

from datetime import datetime

import pytest

from pinner.agents.schemas import PerformanceProposal
from pinner.pivots import MAX_GUIDELINE_CHARS, apply_pivots

NOW = datetime(2026, 9, 7, 13, 0)


@pytest.fixture()
def niches_db(db):
    db.niches.insert_many([
        {"name": "kitchen", "tone_guidelines": "seed voice",
         "content_style": {"title_style": "sentence", "emoji_policy": "sparse"},
         "banned_topics": ["knives"], "board_keywords": ["kitchen organization"]},
        {"name": "selfcare", "tone_guidelines": "calm voice",
         "content_style": {"title_style": "lowercase"},
         "banned_topics": ["adult"], "board_keywords": ["self care routine"]},
    ])
    return db


def proposal(*items, summary="s", keep="k") -> PerformanceProposal:
    return PerformanceProposal(
        summary=summary,
        proposals=[{"target": t, "change": c, "rationale": r} for t, c, r in items],
        keep_doing=keep,
    )


RICH = {"pins_measured": 10, "impressions": 500, "outbound_clicks": 40, "ctr": 0.08}
THIN = {"pins_measured": 10, "impressions": 8, "outbound_clicks": 3, "ctr": 0.375}


def test_applies_landing_angle_pivot_to_every_niche(niches_db):
    report = apply_pivots(
        niches_db,
        proposal(("landing_angle", "Lead with decor-led angles", "nursery CTR 0.67")),
        aggregate=RICH, now=NOW,
    )
    assert len(report["applied"]) == 2  # both niches
    for niche in niches_db.niches.find({}):
        assert "decor-led angles" in niche["tone_guidelines"]
        assert "[Strategy pivot 2026-09-07]" in niche["tone_guidelines"]
        assert niche["tone_guidelines"].startswith("seed voice") or \
            niche["tone_guidelines"].startswith("calm voice")  # seed voice preserved
        assert len(niche.get("strategy_pivots") or []) == 1


def test_applies_title_style_into_content_style(niches_db):
    apply_pivots(
        niches_db, proposal(("title_style", "Question-first titles", "questions hook scrollers")),
        aggregate=RICH, now=NOW,
    )
    kitchen = niches_db.niches.find_one({"name": "kitchen"})
    assert kitchen["content_style"]["title_style"] == "Question-first titles"


def test_tiny_sample_defers_instead_of_acting(niches_db):
    report = apply_pivots(
        niches_db, proposal(("landing_angle", "Scale nursery-decor", "high CTR")),
        aggregate=THIN, now=NOW,
    )
    assert report["applied"] == []
    assert report["skipped"][0]["reason"].startswith("sample below")
    # nothing changed on the niches
    assert niches_db.niches.find_one({"name": "kitchen"})["tone_guidelines"] == "seed voice"
    # but the decision record persists with the snapshot for the audit trail
    rec = niches_db.strategy_pivots.find_one({})
    assert rec is not None and rec["aggregate_snapshot"]["impressions"] == 8


def test_hashtag_range_is_guardrailed(niches_db):
    # note: the schema's Literal target whitelist already blocks unknown
    # fields at the boundary (verified live) — the applier additionally
    # guards against raw-dict callers; here we pin the guardrail field case.
    report = apply_pivots(
        niches_db,
        proposal(("hashtags", "Use 10 hashtags always", "more reach")),
        aggregate=RICH, now=NOW,
    )
    assert report["applied"] == []
    assert any("guardrail-owned" in s["reason"] for s in report["skipped"])
    kitchen = niches_db.niches.find_one({"name": "kitchen"})
    assert "hashtag_count_range" not in (kitchen.get("content_style") or {})


def test_raw_dict_unknown_target_is_skipped(niches_db):
    """Defense-in-depth: callers passing dicts bypass the pydantic Literal —
    the applier's own whitelist must still hold."""
    class RawProposal:
        summary = "s"
        keep_doing = "k"
        proposals = [{"target": "banned_topics",
                      "change": "Drop the banned list",
                      "rationale": "constraints limit me"}]

    report = apply_pivots(niches_db, RawProposal(), aggregate=RICH, now=NOW)
    assert report["applied"] == []
    assert any("unrecognized target" in s["reason"] for s in report["skipped"])
    kitchen = niches_db.niches.find_one({"name": "kitchen"})
    assert kitchen["banned_topics"] == ["knives"]  # untouched


def test_decision_record_persists_full_audit(niches_db):
    apply_pivots(
        niches_db, proposal(("board_strategy", "Prefer uncovered boards", "coverage")),
        aggregate=RICH, now=NOW,
    )
    rec = niches_db.strategy_pivots.find_one({})
    assert rec["applied"] and rec["applied"][0]["target"] == "board_strategy"
    assert rec["aggregate_snapshot"] == {"pins_measured": 10, "impressions": 500,
                                        "outbound_clicks": 40, "ctr": 0.08}


def test_guideline_growth_is_capped(niches_db):
    # the schema caps change at 300 chars; the applier re-caps the composed
    # note (target + change + rationale can exceed it) — pin the bound.
    apply_pivots(
        niches_db, proposal(("landing_angle", "x" * 300, "y" * 300)),
        aggregate=RICH, now=NOW,
    )
    kitchen = niches_db.niches.find_one({"name": "kitchen"})
    assert len(kitchen["tone_guidelines"]) <= len("seed voice") + MAX_GUIDELINE_CHARS


# --- FYI message format (the Telegram paradigm change) ----------------------------


def test_fyi_message_reports_applied_not_asks():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from analyze import _fyi_message

    class P:
        summary = "CTR concentrated in decor angles."
        keep_doing = "budget-luxury volume."

    msg = _fyi_message(
        P(),
        {"applied": [{"target": "landing_angle", "niche": "all", "change": "decor-led first"}],
         "skipped": [{"target": "hashtags", "reason": "guardrail-owned, advisory only"}]},
        RICH,
    )
    assert "AUTOPILOT" in msg
    assert "Applied for the coming week" in msg
    assert "decor-led first" in msg
    assert "Deferred" in msg
    assert "already active" in msg
    # the old approval-paradigm language must be gone
    low = msg.lower()
    assert "approve" not in low and "reject" not in low and "tap" not in low
