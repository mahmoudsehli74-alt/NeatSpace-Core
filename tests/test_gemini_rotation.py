"""Gemini API-key failover tests (autopilot capacity, 2026-09-13).

Contract: on a quota-class failure (429 / RESOURCE_EXHAUSTED) the client
rotates to the next key and retries the SAME prompt; non-quota failures
raise immediately; when every key is exhausted the quota error bubbles so
the runner's day-scoped QUOTA_ATTEMPT_BUDGET takes over.
"""

from __future__ import annotations

import pytest

from pinner.agents import AgentTransientError, GeminiJsonClient
from pinner.agents.schemas import StrategyContent


class QuotaError(Exception):
    code = 429

    def __str__(self):
        return "429 RESOURCE_EXHAUSTED. You exceeded your current quota."


class ServerError(Exception):
    code = 503

    def __str__(self):
        return "503 UNAVAILABLE. high demand."


def _ok_response(title="Rotated Find"):
    from types import SimpleNamespace

    from pinner.agents.schemas import StrategyContent

    return SimpleNamespace(
        parsed=StrategyContent(
            title=title,
            description="A quiet little upgrade for the room." * 2,
            hashtags=["#find", "#decor"],
            board_choice="Board",
            landing_angle="budget-luxury",
            disclosure=True,
        ),
        text=None,
    )


class ScriptedRaw:
    """Fake genai client: behaviors popped per call (exceptions raise)."""

    def __init__(self, *behaviors):
        self.behaviors = list(behaviors)
        self.calls = 0
        self.last_model = None

    @property
    def models(self):
        return self

    def generate_content(self, *, model, contents, config):
        self.calls += 1
        self.last_model = model
        behavior = self.behaviors.pop(0) if self.behaviors else _ok_response()
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


def _client(raw, **kw):
    return GeminiJsonClient("key-primary", model="test-model", raw=raw,
                            fallback_api_key=kw.get("fallback", "key-fallback"))


def test_rotates_to_fallback_key_on_429_and_succeeds():
    """THE operator scenario: key1 429s, key2 answers 200 — same prompt,
    no exception reaches the caller."""
    raw = ScriptedRaw(QuotaError("429 RESOURCE_EXHAUSTED"), _ok_response())
    client = _client(raw)
    result = client.generate(system="s", user="u", schema=StrategyContent)
    assert result.title == "Rotated Find"
    assert raw.calls == 2                       # 1 failed attempt + 1 success
    assert client.active_key == "key-fallback"  # sticky on the working key


def test_all_keys_exhausted_bubbles_quota_error():
    """Graceful degradation: both keys 429 -> the quota error bubbles (the
    runner's day-scoped budget then applies the 6h schedule)."""
    raw = ScriptedRaw(QuotaError("429"), QuotaError("429"))
    client = _client(raw)
    with pytest.raises(AgentTransientError) as err:
        client.generate(system="s", user="u", schema=StrategyContent)
    assert "429" in str(err.value) and "RESOURCE_EXHAUSTED" in str(err.value)
    assert raw.calls == 2


def test_non_quota_error_raises_without_rotation():
    """503/other failures must NOT burn the fallback key."""
    raw = ScriptedRaw(ServerError("503"))
    client = _client(raw)
    with pytest.raises(AgentTransientError) as err:
        client.generate(system="s", user="u", schema=StrategyContent)
    assert "503" in str(err.value)
    assert client.active_key == "key-primary"   # never rotated
    assert raw.calls == 1


def test_single_key_client_bubbles_on_429():
    """No fallback configured -> 429 bubbles after the single attempt (the
    pre-rotation behavior, required for graceful degradation)."""
    raw = ScriptedRaw(QuotaError("429"), _ok_response())
    client = GeminiJsonClient("key-primary", model="test-model", raw=raw)
    with pytest.raises(AgentTransientError):
        client.generate(system="s", user="u", schema=StrategyContent)
    assert raw.calls == 1                       # never retried


def test_rotation_wraps_for_daily_reset():
    """Cycling semantics: after ALL keys exhaust in one call, the index
    resets to the primary (keys reset daily — primary-first maximizes the
    reset hit), and the next call recovers on whichever key has quota."""
    raw = ScriptedRaw(QuotaError("429"), QuotaError("429"), QuotaError("429"),
                      _ok_response())
    client = _client(raw)
    with pytest.raises(AgentTransientError):
        client.generate(system="s", user="u", schema=StrategyContent)
    assert raw.calls == 2                       # both keys tried once
    assert client.active_key == "key-primary"   # reset for the next call
    # next call: primary still exhausted -> rotates -> fallback recovers
    result = client.generate(system="s", user="u", schema=StrategyContent)
    assert result.title == "Rotated Find"
    assert client.active_key == "key-fallback"


def test_client_requires_at_least_one_key():
    with pytest.raises(ValueError):
        GeminiJsonClient("", model="m", fallback_api_key="")
