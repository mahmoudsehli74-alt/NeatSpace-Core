"""Strategist agent: SEO pin content per niche (title, description, hashtags,
board choice, landing angle) — schema-constrained, guardrail-checked."""

from __future__ import annotations

from pinner.agents.client import GeminiJsonClient
from pinner.agents.guardrails import check_strategy
from pinner.agents.prompts import strategist_system, strategist_user
from pinner.agents.schemas import StrategyContent


class Strategist:
    name = "strategist"

    def __init__(self, client: GeminiJsonClient) -> None:
        self._client = client

    def create(self, raw: dict, niche: dict, boards: list[str]) -> StrategyContent:
        """Generate pin copy for an APPROVED product. Raises transient/permanent
        agent errors or GuardrailError (poison this product)."""
        content = self._client.generate(
            system=strategist_system(niche),
            user=strategist_user(raw, boards),
            schema=StrategyContent,
        )
        check_strategy(content, niche, boards)
        return content
