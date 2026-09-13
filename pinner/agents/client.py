"""Gemini client seam for the agent layer.

``GeminiJsonClient`` wraps google-genai with structured-output enforcement and
the error taxonomy the state machine needs. The underlying genai client is
injectable so CI tests never touch the network; live behavior is exercised
only by the opt-in AGENT_LIVE=1 tests.

Error mapping:
  * HTTP 429/5xx, timeouts, transport errors  -> AgentTransientError (retry)
  * 400/401/403/404, bad API key              -> AgentPermanentError (alert)
  * unparseable / schema-invalid model output -> AgentSchemaError (retry,
    engine poisons after max attempts — the "schema invalid x3" contract)
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}


class AgentTransientError(Exception):
    """Worth retrying (rate limit, 5xx, model flake)."""


class AgentPermanentError(Exception):
    """Not retryable (bad request, auth, policy) — alert the operator."""


class AgentSchemaError(AgentTransientError):
    """Model output failed schema validation — retry, poison after N."""


class ImageBlob:
    """Inlined image bytes for multimodal moderation."""

    def __init__(self, data: bytes, mime_type: str = "image/jpeg") -> None:
        self.data = data
        self.mime_type = mime_type


class RawModels(Protocol):
    def generate_content(self, *, model: str, contents: list, config: Any) -> Any: ...


class RawClient(Protocol):
    models: RawModels


class GeminiJsonClient:
    """Gemini JSON client with automatic API-key failover.

    Keys are tried in order (primary, then optional fallback(s)); when a call
    fails with a quota error (HTTP 429 / RESOURCE_EXHAUSTED) the client
    rotates to the next key and retries the SAME prompt, transparently. A
    non-quota failure (5xx, auth, schema) raises immediately — no rotation.
    When every key is quota-exhausted the last quota error bubbles up so the
    runner's day-scoped QUOTA_ATTEMPT_BUDGET (6h backoff) takes over.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        raw: RawClient | None = None,
        fallback_api_key: str = "",
        cooldown_seconds: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._keys: list[str] = [k for k in (api_key, fallback_api_key) if k]
        if not self._keys:
            raise ValueError("GeminiJsonClient requires at least one API key")
        self._key_index = 0
        self.model = model
        self._raw = raw
        self._raws: dict[str, RawClient] = {}
        # RPM governor: minimum gap between consecutive LLM HTTP attempts
        # (free-tier per-minute limits). Applies to every attempt — rotated
        # retries included — so neither key gets hammered. 0 disables it
        # entirely (unit tests stay instant).
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._clock = clock
        self._sleeper = sleeper
        self._last_call_ts: float | None = None

    @property
    def active_key(self) -> str:
        return self._keys[self._key_index]

    def _respect_rpm_cooldown(self) -> None:
        """Sleep out the remaining gap since the previous HTTP attempt."""
        if self.cooldown_seconds <= 0 or self._last_call_ts is None:
            return
        elapsed = self._clock() - self._last_call_ts
        remaining = self.cooldown_seconds - elapsed
        if remaining > 0:
            self._sleeper(remaining)

    def _stamp_call(self) -> None:
        self._last_call_ts = self._clock()

    def _rotate(self) -> bool:
        """Advance to the next key. Returns False when every key was tried."""
        if self._key_index >= len(self._keys) - 1:
            return False
        self._key_index += 1
        return True

    def _client(self) -> RawClient:
        if self._raw is not None:
            return self._raw
        key = self.active_key
        if key not in self._raws:
            from google import genai

            self._raws[key] = genai.Client(api_key=key)
        return self._raws[key]

    @staticmethod
    def _is_quota(exc: Exception) -> bool:
        """Quota-class failure: HTTP 429 / RESOURCE_EXHAUSTED (daily RPD or
        per-minute RPM). Only these justify burning the fallback key."""
        code = getattr(exc, "code", None)
        if code == 429:
            return True
        text = str(exc)
        return "429" in text and "RESOURCE_EXHAUSTED" in text

    def generate(
        self,
        *,
        system: str,
        user: str,
        schema: type[BaseModel],
        images: list[ImageBlob] | None = None,
    ) -> BaseModel:
        from google.genai import types

        parts: list[Any] = [types.Part.from_text(text=user)]
        for blob in images or []:
            parts.append(types.Part.from_bytes(data=blob.data, mime_type=blob.mime_type))
        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.2,
        )
        last_quota: Exception | None = None
        for _ in range(len(self._keys)):
            self._respect_rpm_cooldown()
            try:
                self._stamp_call()
                response = self._client().models.generate_content(
                    model=self.model, contents=parts, config=config
                )
            except Exception as exc:
                self._stamp_call()
                classified = self._classify(exc)
                if self._is_quota(exc):
                    if self._rotate():
                        last_quota = classified
                        continue  # same prompt, next key
                    # every key exhausted: restart from the primary next
                    # call (keys reset daily) and let the quota error
                    # bubble to the day-scoped attempt budget.
                    self._key_index = 0
                raise classified from exc
            parsed = getattr(response, "parsed", None)
            if isinstance(parsed, schema):
                return parsed
            text = getattr(response, "text", None) or ""
            try:
                return schema.model_validate_json(text)
            except ValidationError as exc:
                raise AgentSchemaError(f"model output failed {schema.__name__} validation") from exc
        # every key quota-exhausted — the quota error bubbles so the
        # caller's day-scoped attempt budget (never-poison 6h schedule)
        # takes over.
        assert last_quota is not None
        raise last_quota

    @staticmethod
    def _classify(exc: Exception) -> Exception:
        code = getattr(exc, "code", None)
        name = type(exc).__name__
        if code in TRANSIENT_HTTP_CODES:
            return AgentTransientError(f"gemini {code}: {exc}")
        if code in (400, 401, 403, 404):
            return AgentPermanentError(f"gemini {code}: {exc}")
        if "timeout" in str(exc).lower() or "temporarily" in str(exc).lower():
            return AgentTransientError(f"gemini {name}: {exc}")
        # Unknown transport-level failures: retry (engine caps attempts).
        return AgentTransientError(f"gemini {name}: {exc}")


def default_image_fetcher(url: str) -> bytes | None:
    """Best-effort image download for multimodal moderation (Phase 2 runs on
    GH Actions with network). Returns None on any failure — moderation then
    proceeds text-only rather than blocking the pipeline."""
    import httpx

    try:
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.content
    except httpx.HTTPError:
        return None


ImageFetcher = Callable[[str], "bytes | None"]
