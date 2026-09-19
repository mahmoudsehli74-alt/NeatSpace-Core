"""Brand-safety fortress tests (2026-09-14): religious-symbol merchandise is
hard-blocked deterministically — never delegated to LLM judgment.

Layers under test:
  * pinner.brand_safety.scan — pure regex blocklist (word-boundary,
    case-insensitive, false-positive-resistant: cross-stitch/crossbody do
    NOT match)
  * guardrails.check_strategy — generated copy with religious terms is
    poisoned (GuardrailError) even when the LLM "approved" it
  * moderator verdict override — religious_symbols risk flag is fatal to
    APPROVE
"""

from __future__ import annotations

import pytest

from pinner.agents import GuardrailError
from pinner.agents.guardrails import check_strategy, check_verdict
from pinner.agents.schemas import ModerationVerdict, StrategyContent
from pinner.brand_safety import scan


def test_blocklist_matches_operator_examples():
    hits = scan("Star of David Wall Decor", "Hamsa hand amulet",
                "Wooden Wall Cross for Christian Home")
    labels = " ".join(hits)
    assert "star_of_david" in labels
    assert "hamsa" in labels
    assert any("cross" in h for h in hits)
    assert "religious_term" in labels  # "Christian"


def test_blocklist_matches_menorah_rosary_case_insensitive():
    hits = scan("Hand-Painted MENORAH Candle Holder", "Rosary bead kit",
                "Protective Amulet Pendant")
    labels = " ".join(hits)
    assert "menorah" in labels
    assert "rosary" in labels
    assert "amulet" in labels


def test_cross_stitch_and_crossbody_do_not_false_positive():
    """Precision guard: 'cross stitch' craft kits and crossbody bags are
    legitimate catalog items — bare 'cross' must not match."""
    assert scan("Cross Stitch Embroidery Starter Kit for Beginners") == []
    assert scan("Crossbody Phone Bag with Adjustable Strap") == []


def test_clean_listing_scans_empty():
    assert scan("Stainless Steel Sink Caddy Organizer",
                "Rustproof sponge holder with drainage. #KitchenHacks") == []


def test_check_strategy_poisons_religious_copy():
    content = StrategyContent(
        title="Beautiful Crucifix Wall Art for Your Living Room",
        description=("A hand-finished crucifix that anchors the room. "
                     "A meaningful faith gift. Ships fast."),
        hashtags=["#homedecor", "#crucifix"],
        board_choice="Board",
        landing_angle="budget-luxury",
        disclosure=True,
    )
    with pytest.raises(GuardrailError) as err:
        check_strategy(content, {}, ["Board"])
    assert "brand-safety blocklist" in str(err.value)


def test_moderator_religious_flag_is_fatal_to_approve():
    verdict = ModerationVerdict(
        verdict="APPROVE", reasons=["clean"], confidence=0.99,
        risk_flags=["religious_symbols"])
    with pytest.raises(GuardrailError) as err:
        check_verdict(verdict)
    assert "religious_symbols" in str(err.value)
