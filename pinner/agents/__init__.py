"""Agent layer (Phase 2): Moderator (global safety gate) + Strategist (SEO copy).

LLM-for-judgment only — control flow lives in the deterministic state machine.
Product data is quarantined as untrusted user-role content (prompts.py);
outputs are schema-constrained then guardrail-checked before touching Mongo."""

from pinner.agents.client import (
    AgentPermanentError,
    AgentSchemaError,
    AgentTransientError,
    GeminiJsonClient,
    ImageBlob,
)
from pinner.agents.guardrails import GuardrailError
from pinner.agents.moderator import Moderator
from pinner.agents.schemas import ModerationVerdict, StrategyContent
from pinner.agents.strategist import Strategist

DEFAULT_MODEL = "gemini-3.6-flash"


def build_agents(
    api_key: str, *, model: str = DEFAULT_MODEL, raw=None
) -> tuple[Moderator, Strategist]:
    client = GeminiJsonClient(api_key, model=model, raw=raw)
    return Moderator(client), Strategist(client)


__all__ = [
    "AgentPermanentError",
    "AgentSchemaError",
    "AgentTransientError",
    "DEFAULT_MODEL",
    "GuardrailError",
    "GeminiJsonClient",
    "ImageBlob",
    "Moderator",
    "ModerationVerdict",
    "Strategist",
    "StrategyContent",
    "build_agents",
]
