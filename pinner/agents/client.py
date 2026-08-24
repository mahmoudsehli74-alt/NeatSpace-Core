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
    def __init__(self, api_key: str, *, model: str, raw: RawClient | None = None) -> None:
        self._api_key = api_key
        self.model = model
        self._raw = raw

    def _client(self) -> RawClient:
        if self._raw is None:
            from google import genai

            self._raw = genai.Client(api_key=self._api_key)
        return self._raw

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
        try:
            response = self._client().models.generate_content(
                model=self.model, contents=parts, config=config
            )
        except Exception as exc:  # classified below
            raise self._classify(exc) from exc

        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, schema):
            return parsed
        text = getattr(response, "text", None) or ""
        try:
            return schema.model_validate_json(text)
        except ValidationError as exc:
            raise AgentSchemaError(f"model output failed {schema.__name__} validation") from exc

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
