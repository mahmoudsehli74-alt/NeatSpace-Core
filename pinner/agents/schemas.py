"""Agent output schemas (Pydantic) — the typed contracts between the LLM
stages and the state machine. These models are passed to Gemini as
response_schema, so the model is forced into the shape BEFORE our guardrails
run; guardrails then enforce what a schema cannot (semantics, niche policy)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ModerationVerdict(BaseModel):
    verdict: Literal["APPROVE", "REJECT"]
    reasons: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    risk_flags: list[str] = Field(
        default_factory=list,
        description="subset of: adult, weapons, counterfeit_risk, ip_risk, halal_violation",
    )


class StrategyContent(BaseModel):
    title: str = Field(max_length=95, description="Pinterest pin title")
    description: str = Field(max_length=480, description="Pinterest pin description")
    hashtags: list[str] = Field(min_length=2, max_length=8)
    board_choice: str = Field(description="must be one of the provided board names")
    landing_angle: str = Field(description="e.g. budget-luxury, problem-solver, gift-guide")
    disclosure: bool = Field(
        default=True, description="affiliate disclosure acknowledgment — must be true"
    )
