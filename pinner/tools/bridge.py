"""GitHub Contents-API bridge tool — pushes per-product landing data.

Validated in Phase 0 (fetch SHA -> push base64 JSON, both create and update).
Productionized here with the crash-window contract made real:

  * Files are per-product: ``products/{source}-{product_id}.json`` — merge-safe,
    no whole-catalog rewrites, CDN-cacheable single-product fetches.
  * Content is CANONICAL (sorted keys, stable formatting), so re-pushing the
    same payload after a crash is detected by a GET and ADOPTED — the tool
    never commits identical content twice (reconcile-by-content).
  * ``verify_deployed`` polls the Pages URL so we never pin a dead link
    (Pinterest suppresses pins whose destination is slow/404).

Error taxonomy: 5xx/timeouts -> TransientError; 401/403/404-conflicts ->
PermanentError (via the shared pinner.errors classes).
"""

from __future__ import annotations

import base64
import json
import time

from pinner.errors import PermanentError, TransientError
from pinner.tools.http import Transport, httpx_transport

GITHUB_API = "https://api.github.com"


def canonical_product_json(payload: dict) -> bytes:
    """Deterministic serialization — the foundation of adopt-by-content."""
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    return (text + "\n").encode("utf-8")


def _headers(pat: str) -> dict:
    return {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


class BridgeTool:
    def __init__(self, pat: str, *, transport: Transport | None = None) -> None:
        self._pat = pat
        self._transport = transport or httpx_transport

    def push_product(
        self, repo_full_name: str, product_key: str, payload: dict, *, branch: str = "main"
    ) -> dict:
        """Commit products/{product_key}.json. Returns
        {commit_sha, path, committed, html_url}. ``committed`` is False when
        identical content was already live (idempotent adopt — the crash
        window between commit and status-write is harmless)."""
        path = f"products/{product_key}.json"
        content = canonical_product_json(payload)
        url = f"{GITHUB_API}/repos/{repo_full_name}/contents/{path}"

        status, existing_sha = 0, None
        reply = self._transport("GET", f"{url}?ref={branch}", headers=_headers(self._pat))
        status = reply.status
        if status == 200:
            body = reply.json()
            existing_sha = body.get("sha")
            remote = body.get("content") or ""
            try:
                remote_bytes = base64.b64decode(remote.replace("\n", ""))
            except ValueError:
                remote_bytes = b""
            if remote_bytes == content:
                return {
                    "commit_sha": existing_sha,
                    "path": path,
                    "committed": False,
                    "html_url": f"https://github.com/{repo_full_name}/blob/{branch}/{path}",
                }
        elif status != 404:
            self._raise(status, "read bridge file")

        put_body = {
            "message": f"chore: product {product_key}",
            "content": base64.b64encode(content).decode("ascii"),
            "branch": branch,
        }
        if existing_sha:
            put_body["sha"] = existing_sha
        reply = self._transport(
            "PUT", url, headers=_headers(self._pat), json_body=put_body
        )
        if reply.status not in (200, 201):
            self._raise(reply.status, "commit bridge file")
        body = reply.json()
        commit = body.get("commit") or {}
        return {
            "commit_sha": commit.get("sha"),
            "path": path,
            "committed": True,
            "html_url": commit.get("html_url"),
        }

    def verify_deployed(
        self,
        url: str,
        *,
        attempts: int = 12,
        wait_seconds: float = 20.0,
        sleeper=time.sleep,
    ) -> bool:
        """Poll the Pages URL until it answers 200 (CDN propagation).

        Live-measured 2026-09-03: GitHub Pages builds take 3-5 min after the
        Contents commit. The old 3x5s window lost that race every time —
        the runner marked ENRICHED pins as 'bridge not deployed' and the
        products had to wait a whole cron cycle to retry. 12x20s (4 min)
        matches the real build time; the runner's 25-min job timeout
        absorbs it comfortably."""
        for attempt in range(1, attempts + 1):
            reply = self._transport("GET", url)
            if reply.status == 200:
                return True
            if attempt < attempts:
                sleeper(wait_seconds)
        return False

    @staticmethod
    def _raise(status: int, action: str) -> None:
        message = f"[github] {action} failed with HTTP {status}"
        if status >= 500 or status == 429:
            raise TransientError(message)
        raise PermanentError(message)
