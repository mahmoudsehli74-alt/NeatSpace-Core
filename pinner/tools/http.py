"""Typed HTTP transport seam shared by the bridge and Pinterest tools.

``httpx_transport`` is the production implementation; tests inject fakes that
return recorded ``HttpReply`` objects — CI never performs network I/O."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import httpx

from pinner.errors import TransientError


@dataclass(frozen=True)
class HttpReply:
    status: int
    body: bytes
    content_type: str = ""

    def json(self):
        import json

        return json.loads(self.body.decode("utf-8"))


Transport = Callable[..., HttpReply]


def httpx_transport(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    json_body: dict | None = None,
    data: dict | None = None,
    files: dict | None = None,
) -> HttpReply:
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            response = client.request(
                method, url, headers=headers, json=json_body, data=data, files=files
            )
            return HttpReply(
                response.status_code,
                response.content,
                response.headers.get("content-type", ""),
            )
    except httpx.HTTPError as exc:
        raise TransientError(f"http transport failed: {exc}") from exc
