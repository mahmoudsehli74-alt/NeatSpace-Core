"""Performance Analyst agent (Phase 3): reads the learning loop's aggregates
and proposes prompt/strategy tweaks — the ONLY HITL surface on strategy.

The analyst sees NUMBERS ONLY (aggregated CTRs, angles) — no product payloads,
no user PII. Its proposals are advisory: they go to Telegram for one-tap
operator approval and are never applied automatically."""

from __future__ import annotations

from pinner.agents.client import GeminiJsonClient
from pinner.agents.schemas import PerformanceProposal

ANALYST_SYSTEM = """You are a performance-marketing analyst for an affiliate Pinterest system.

You receive weekly aggregated metrics: overall CTR and per-landing-angle CTR.
Your job is to propose 1-5 concrete, testable adjustments to the content
strategy (title style, description style, hashtag mix, landing angle emphasis,
board strategy) that could raise OUTBOUND CTR.

Rules:
- Every proposal must cite the data pattern that motivated it.
- Prefer small deltas over rewrites; one hypothesis per proposal.
- If the sample is tiny (<100 impressions per angle), say so and propose
  widening measurement rather than acting.
- fill keep_doing with what the winning angle already proves works."""

ANALYST_USER_TEMPLATE = """Weekly performance aggregate:
{aggregate_json}

Produce your performance review JSON."""


class Analyst:
    name = "analyst"

    def __init__(self, client: GeminiJsonClient) -> None:
        self._client = client

    def review(self, aggregate: dict) -> PerformanceProposal:
        """Turn metrics into an approved-pending proposal. Raises transient/
        permanent agent errors; output is schema-constrained."""
        import json

        from pinner.agents.prompts import UNTRUSTED_NOTICE

        user = ANALYST_USER_TEMPLATE.format(
            aggregate_json=json.dumps(aggregate, ensure_ascii=False, indent=2)
        )
        return self._client.generate(
            system=ANALYST_SYSTEM,
            user=f"{UNTRUSTED_NOTICE}\n\n{user}",
            schema=PerformanceProposal,
        )
