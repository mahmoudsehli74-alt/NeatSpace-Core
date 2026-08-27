"""Pinterest API v5 client — boards, multipart pin creation, verification,
token refresh, and the reconcile-by-bridge-url search.

Validated in Phase 0 (OAuth + scopes, board listing, automated test pin with
image + affiliate link, then delete). Productionized here with:

  * ``find_pin_by_link`` — the real reconciler for the dangerous window
    between "pin created" and "status written": on recovery we search the
    board's pins for our bridge URL and ADOPT the existing pin instead of
    creating a duplicate.
  * ``create_pin`` multipart upload (image BYTES, never hotlinked CDN URLs —
    marketplace hotlink protection breaks image_url pins).
  * ``refresh_access_token`` as a standalone function the runner calls when a
    request comes back 401 (Pinterest rotates refresh tokens — the runner
    persists the new pair).
  * Error taxonomy: 401 -> PinterestTokenExpired (refresh + retry),
    429/5xx/timeouts -> TransientError, 400/403/404 -> PermanentError.

Note: the multipart file part name defaults to "media_source" per the v5
OpenAPI spec; it is a constructor parameter so a single line fixes any
future field drift surfaced by the gated live test.
"""

from __future__ import annotations

import base64

from pinner.errors import PermanentError, TransientError
from pinner.tools.http import Transport, httpx_transport

PINTEREST_API = "https://api.pinterest.com/v5"
MAX_PAGES = 5


class PinterestTokenExpired(TransientError):
    """Access token expired/invalid — refresh it and retry the call."""


def _bearer(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


def _paged_get(transport: Transport, url: str, headers: dict) -> list[dict]:
    """Walk a bookmark-paginated v5 collection (max MAX_PAGES per call)."""
    items: list[dict] = []
    bookmark = None
    for _ in range(MAX_PAGES):
        query = f"{url}?page_size=100" + (f"&bookmark={bookmark}" if bookmark else "")
        reply = transport("GET", query, headers=headers)
        if reply.status != 200:
            _raise(reply.status, f"list {url}")
        body = reply.json()
        items.extend(body.get("items") or [])
        bookmark = body.get("bookmark")
        if not bookmark:
            break
    return items


def _raise(status: int, action: str) -> None:
    if status == 401:
        raise PinterestTokenExpired(f"[pinterest] {action}: token expired or invalid")
    message = f"[pinterest] {action} failed with HTTP {status}"
    if status == 429 or status >= 500:
        raise TransientError(message)
    raise PermanentError(message)


def refresh_access_token(
    app_id: str, app_secret: str, refresh_token: str, *, transport: Transport | None = None
) -> dict:
    """Exchange a refresh token for a new token pair. Pinterest ROTATES the
    refresh token — the caller must persist both new values."""
    transport = transport or httpx_transport
    basic = base64.b64encode(f"{app_id}:{app_secret}".encode()).decode("ascii")
    reply = transport(
        "POST",
        f"{PINTEREST_API}/oauth/token",
        headers={"Authorization": f"Basic {basic}"},
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
    )
    if reply.status != 200:
        _raise(reply.status, "refresh access token")
    body = reply.json()
    return {
        "access_token": body["access_token"],
        "refresh_token": body.get("refresh_token"),
        "expires_in": body.get("expires_in"),
    }


def download_image(url: str, *, transport: Transport | None = None) -> bytes:
    """Fetch product image bytes for multipart upload (never hotlink)."""
    transport = transport or httpx_transport
    try:
        reply = transport("GET", url)
    except PermanentError:
        raise
    except Exception as exc:  # httpx timeouts / egress drops from any transport
        raise TransientError(f"[pinterest] image fetch failed: {exc}") from exc
    if reply.status != 200:
        _raise(reply.status, f"download image {url[:60]}")
    if not reply.body:
        raise TransientError(f"[pinterest] empty image body from {url[:60]}")
    return reply.body


class PinterestTool:
    def __init__(
        self,
        access_token: str,
        *,
        transport: Transport | None = None,
        file_field: str = "media_source",
    ) -> None:
        self._token = access_token
        self._transport = transport or httpx_transport
        self._file_field = file_field

    def list_boards(self) -> list[dict]:
        """[{id, name}] for the token's account."""
        items = _paged_get(self._transport, f"{PINTEREST_API}/boards", _bearer(self._token))
        return [{"id": b["id"], "name": b.get("name", "")} for b in items]

    def list_board_pins(self, board_id: str) -> list[dict]:
        return _paged_get(
            self._transport, f"{PINTEREST_API}/boards/{board_id}/pins", _bearer(self._token)
        )

    def find_pin_by_link(self, board_id: str, link: str) -> dict | None:
        """Reconciler: locate an existing pin by its destination link."""
        for pin in self.list_board_pins(board_id):
            if pin.get("link") == link:
                return pin
        return None

    def create_pin(
        self,
        *,
        board_id: str,
        title: str,
        description: str,
        link: str,
        image_bytes: bytes,
        alt_text: str | None = None,
    ) -> dict:
        """Multipart pin creation with image bytes. Returns {pin_id, url}."""
        data = {"board_id": board_id, "title": title, "description": description, "link": link}
        if alt_text:
            data["alt_text"] = alt_text
        files = {self._file_field: ("product.jpg", image_bytes, "image/jpeg")}
        reply = self._transport(
            "POST",
            f"{PINTEREST_API}/pins",
            headers=_bearer(self._token),
            data=data,
            files=files,
        )
        if reply.status != 201:
            _raise(reply.status, "create pin")
        body = reply.json()
        return {
            "pin_id": body["id"],
            "url": f"https://www.pinterest.com/pin/{body['id']}/",
        }

    def get_pin_analytics(
        self,
        pin_id: str,
        *,
        start_date: str,
        end_date: str,
        metric_types: str = "IMPRESSION,PIN_CLICK,OUTBOUND_CLICK,SAVE",
    ) -> dict:
        """Daily analytics for a pin: {metric: total} over [start_date, end_date]
        (ISO dates). Tolerant parser for v5's all/daily_metrics shape."""
        import datetime as _dt

        start = _dt.date.fromisoformat(start_date)
        end = _dt.date.fromisoformat(end_date)
        url = (
            f"{PINTEREST_API}/pins/{pin_id}/analytics"
            f"?start_date={start.isoformat()}&end_date={end.isoformat()}"
            f"&metric_types={metric_types}&app_types=all"
        )
        reply = self._transport("GET", url, headers=_bearer(self._token))
        if reply.status != 200:
            _raise(reply.status, f"pin analytics {pin_id}")
        body = reply.json().get("all") or {}
        totals = {key: 0 for key in metric_types.split(",")}
        daily = body.get("daily_metrics") or []
        for day in daily:
            metrics = day.get("metrics") or {}
            for key in totals:
                value = metrics.get(key) or 0
                totals[key] += int(value if not isinstance(value, dict) else value.get("value", 0))
        lifetime = body.get("lifetime_metrics") or {}
        for key in totals:
            if totals[key] == 0 and key in lifetime:
                totals[key] = int(lifetime[key] or 0)
        return totals

    def get_pin(self, pin_id: str) -> dict:
        """Fetch a pin for post-create verification (id, board_id, ...)."""
        reply = self._transport(
            "GET", f"{PINTEREST_API}/pins/{pin_id}", headers=_bearer(self._token)
        )
        if reply.status != 200:
            _raise(reply.status, f"get pin {pin_id}")
        return reply.json()
