"""Deterministic output guardrails (PURE).

The response_schema constrains shape; these guardrails enforce what a schema
cannot: niche hashtag policy, board-membership, disclosure, output length
quality floors, banned-topic scans, and verdict self-consistency.

Mapping to the state machine (by design):
  * GuardrailError      -> error_class="PERMANENT" (poison this product)
  * AgentSchemaError    -> error_class="TRANSIENT" (model flake; engine retries
                           with backoff, poisons after max attempts)
"""

from __future__ import annotations

import re

from pinner.agents.schemas import ModerationVerdict, StrategyContent

# Never allowed in ANY agent output text (injection echo / platform risk).
GLOBAL_BANNED_PATTERNS = (
    "http://", "https://", "www.", ".com/", "porn", "xxx", "casino", "gore",
    "ignore previous", "ignore instruction", "system prompt",
)

HASHTAG_RE = re.compile(r"^#[A-Za-z0-9_]{2,29}$")


class GuardrailError(Exception):
    """Hard violation — this product must not be published."""


def _scan_banned(text: str) -> list[str]:
    lowered = text.lower()
    return [pattern for pattern in GLOBAL_BANNED_PATTERNS if pattern in lowered]


def check_verdict(verdict: ModerationVerdict) -> None:
    problems: list[str] = []
    if verdict.verdict == "REJECT" and not verdict.reasons:
        problems.append("REJECT verdict must include at least one reason")
    if verdict.verdict == "APPROVE":
        fatal = {"adult", "weapons"} & set(verdict.risk_flags)
        if fatal:
            problems.append(f"APPROVE contradicts fatal risk flags: {sorted(fatal)}")
        if verdict.confidence < 0.5:
            problems.append("APPROVE with confidence < 0.5 — must REJECT when unsure")
    if problems:
        raise GuardrailError("; ".join(problems))


def check_strategy(content: StrategyContent, niche: dict, boards: list[str]) -> None:
    problems: list[str] = []

    if not (20 <= len(content.title) <= 95):
        problems.append(f"title length {len(content.title)} outside 20..95")
    if not (50 <= len(content.description) <= 480):
        problems.append(f"description length {len(content.description)} outside 50..480")

    lo, hi = (niche.get("content_style") or {}).get("hashtag_count_range", [3, 6])
    if not (lo <= len(content.hashtags) <= hi):
        problems.append(f"hashtag count {len(content.hashtags)} outside niche range {lo}..{hi}")
    for tag in content.hashtags:
        if not HASHTAG_RE.match(tag):
            problems.append(f"malformed hashtag: {tag!r}")

    if boards and content.board_choice not in boards:
        problems.append(f"board_choice {content.board_choice!r} not in provided boards")

    if content.disclosure is not True:
        problems.append("affiliate disclosure must be true")

    for topic in (niche.get("banned_topics") or []):
        blob = f"{content.title} {content.description}".lower()
        if topic.lower() in blob:
            problems.append(f"banned niche topic in output: {topic!r}")

    problems.extend(_scan_banned(f"{content.title} {content.description}"))

    if problems:
        raise GuardrailError("; ".join(problems))
