"""Strategist agent: SEO pin content per niche (title, description, hashtags,
board choice, landing angle) — schema-constrained, guardrail-checked."""

from __future__ import annotations

from pinner.agents.client import GeminiJsonClient
from pinner.agents.guardrails import check_strategy, repair_strategy
from pinner.agents.prompts import strategist_system, strategist_user
from pinner.agents.schemas import StrategyContent


class Strategist:
    name = "strategist"

    def __init__(self, client: GeminiJsonClient) -> None:
        self._client = client

    def create(self, raw: dict, niche: dict, boards: list[str]) -> StrategyContent:
        """Generate pin copy for an APPROVED product. Raises transient/permanent
        agent errors or GuardrailError (poison this product).

        Live-audit repair pass (2026-09-01): model output is deterministically
        repaired (malformed hashtags, hallucinated board_choice) BEFORE the
        hard gate. check_strategy still runs last — unrepairable output
        still poisons, safety never loosens."""
        content = self._client.generate(
            system=strategist_system(niche),
            user=strategist_user(raw, boards),
            schema=StrategyContent,
        )
        content = repair_strategy(content, niche, boards)
        check_strategy(content, niche, boards)
        return content
