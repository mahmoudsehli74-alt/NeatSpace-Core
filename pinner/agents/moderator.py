"""Moderator agent: global content-safety gate (multimodal, once per product)."""

from __future__ import annotations

from pinner.agents.client import (
    GeminiJsonClient,
    ImageBlob,
    ImageFetcher,
    default_image_fetcher,
)
from pinner.agents.guardrails import check_verdict
from pinner.agents.prompts import moderator_system, moderator_user
from pinner.agents.schemas import ModerationVerdict

MAX_IMAGES = 2


class Moderator:
    name = "moderator"

    def __init__(
        self,
        client: GeminiJsonClient,
        *,
        image_fetcher: ImageFetcher = default_image_fetcher,
    ) -> None:
        self._client = client
        self._fetch = image_fetcher

    def review(self, raw: dict) -> ModerationVerdict:
        """Vet one product (text + first images) against the global policy.

        Raises: AgentTransientError / AgentPermanentError (caller maps to the
        state machine's error classes), GuardrailError (contradictory verdict).
        """
        images: list[ImageBlob] = []
        for url in (raw.get("images") or [])[:MAX_IMAGES]:
            data = self._fetch(url)
            if data:
                mime = "image/png" if url.lower().endswith(".png") else "image/jpeg"
                images.append(ImageBlob(data, mime))
        verdict = self._client.generate(
            system=moderator_system(),
            user=moderator_user(raw),
            schema=ModerationVerdict,
            images=images,
        )
        check_verdict(verdict)
        return verdict
