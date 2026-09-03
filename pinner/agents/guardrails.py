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


# --- repair pass (audit 2026-09-01) --------------------------------------------
# Live evidence: 5 of 8 pin docs went DEAD on REPAIRABLE strategist output —
# 3x malformed hashtags (e.g. '#selfcare mebd' — model emitted words after
# the tag), 2x board_choice hallucination ('Kitchen Hacks & Organization'
# does not exist on the account). Deterministic repair before the hard check
# keeps the safety gate intact while saving the product.


def _slugify_tag(text: str) -> str | None:
    """'#kitchen organization!' -> '#kitchenorganization'; None if nothing
    salvageable. Pure — used by repair_strategy and its tests."""
    stripped = text.strip()
    if not stripped.startswith("#"):
        stripped = "#" + stripped
    body = re.sub(r"[^A-Za-z0-9_]", "", stripped[1:])
    if not (2 <= len(body) <= 29):
        return None
    return f"#{body}"


def repair_strategy(content: StrategyContent, niche: dict, boards: list[str]) -> StrategyContent:
    """Best-effort deterministic repair of model output BEFORE the hard
    check_strategy gate. Returns a (possibly) corrected copy; callers must
    still run check_strategy — anything unrepairable still poisons.

    Repairs (each observed live):
      * malformed hashtags: salvage the tag body ('#selfcare mebd' ->
        '#selfcaremebd' if the trailing junk merges into one token, else
        drop); dedupe; clamp to the niche count range (pad from niche
        keywords, trim overflow).
      * hallucinated board_choice: exact match first, then
        case/insensitive/substring match against the REAL boards; if the
        model named a real board with different casing/wording, snap it.
        No match at all -> first board (the allocator orders uncovered
        boards first, so this is also the coverage-optimal choice).
    """
    data = content.model_dump()
    changed: list[str] = []

    # hashtags: repair, dedupe, count-clamp
    lo, hi = (niche.get("content_style") or {}).get("hashtag_count_range", [3, 6])
    lo = max(lo, 2)  # schema floor: StrategyContent enforces min_length=2
    repaired: list[str] = []
    for tag in data.get("hashtags") or []:
        fixed = tag if HASHTAG_RE.match(tag) else _slugify_tag(tag)
        # 'a me b' style junk ('#tag junk junk') can merge real words into
        # gibberish; only accept repairs that keep a single token
        if fixed and fixed not in repaired:
            repaired.append(fixed)
    if len(repaired) < lo:
        # pad in priority order: niche board_keywords first, then words
        # from the pin title itself — deterministic, never empty, on-topic.
        pad_candidates = [kw.replace(" ", "") for kw in niche.get("board_keywords") or []]
        pad_candidates += re.findall(r"[A-Za-z]{4,}", data.get("title") or "")
        for cand in pad_candidates:
            pad = _slugify_tag("#" + cand)
            if pad and pad not in repaired:
                repaired.append(pad)
            if len(repaired) >= lo:
                break
    if len(repaired) > hi:
        repaired = repaired[:hi]
    if repaired != data.get("hashtags"):
        changed.append("hashtags")
        data["hashtags"] = repaired

    # board_choice: snap hallucinations to a real board
    choice = data.get("board_choice") or ""
    if boards and choice not in boards:
        fixed = None
        if choice:
            lowered = {b.lower(): b for b in boards}
            fixed = lowered.get(choice.lower())
            if not fixed:
                for b in boards:
                    if choice.lower() in b.lower() or b.lower() in choice.lower():
                        fixed = b
                        break
        if fixed is None:
            fixed = boards[0]
        if fixed != choice:
            changed.append("board_choice")
            data["board_choice"] = fixed

    rebuilt = StrategyContent.model_validate(data)
    # repair metadata rides on a PrivateAttr — extra model fields would leak
    # into the Gemini response_schema as additionalProperties and the API
    # rejects the whole request (the 2026-09-02/03 outage).
    rebuilt._repairs = changed
    return rebuilt
