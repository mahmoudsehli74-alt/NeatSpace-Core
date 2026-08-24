"""Pinterest v5 client tests — faked transport, CI never touches Pinterest."""

from __future__ import annotations

import base64
import json

import pytest

from pinner.errors import PermanentError, TransientError
from pinner.tools.http import HttpReply
from pinner.tools.pinterest import (
    PinterestTokenExpired,
    PinterestTool,
    download_image,
    refresh_access_token,
)
from tests.test_tools_bridge import FakeTransport

TOKEN = "pta-xyz"


def ok(body: dict, status: int = 200) -> HttpReply:
    return HttpReply(status, json.dumps(body).encode(), "application/json")


# --- token refresh ------------------------------------------------------------------


def test_refresh_access_token_uses_basic_auth_and_form():
    fake = FakeTransport(ok({"access_token": "new-at", "refresh_token": "new-rt",
                             "expires_in": 2592000}))
    result = refresh_access_token("app-id", "app-secret", "old-rt", transport=fake)
    assert result == {"access_token": "new-at", "refresh_token": "new-rt",
                      "expires_in": 2592000}
    call = fake.calls[0]
    assert call["url"].endswith("/v5/oauth/token")
    basic = call["headers"]["Authorization"].removeprefix("Basic ")
    assert base64.b64decode(basic) == b"app-id:app-secret"
    assert call["data"] == {"grant_type": "refresh_token", "refresh_token": "old-rt"}


def test_refresh_failure_is_permanent():
    tool_transport = FakeTransport(ok({}, status=400))
    with pytest.raises(PermanentError):
        refresh_access_token("a", "b", "r", transport=tool_transport)


# --- boards -------------------------------------------------------------------------


def test_list_boards_walks_bookmark_pagination():
    fake = FakeTransport(
        ok({"items": [{"id": "1", "name": "Kitchen"}], "bookmark": "bm-1"}),
        ok({"items": [{"id": "2", "name": "Meal Prep"}]}),
    )
    boards = PinterestTool(TOKEN, transport=fake).list_boards()
    assert boards == [{"id": "1", "name": "Kitchen"}, {"id": "2", "name": "Meal Prep"}]
    assert "bookmark=bm-1" in fake.calls[1]["url"]


# --- reconcile by link ----------------------------------------------------------------


def test_find_pin_by_link_finds_existing_pin():
    pins = {"items": [
        {"id": "p1", "link": "https://other.example"},
        {"id": "p2", "link": "https://neatspace-kitchen.github.io/p/k.json"},
    ]}
    fake = FakeTransport(ok(pins))
    found = PinterestTool(TOKEN, transport=fake).find_pin_by_link(
        "board-1", "https://neatspace-kitchen.github.io/p/k.json"
    )
    assert found == {"id": "p2", "link": "https://neatspace-kitchen.github.io/p/k.json"}


def test_find_pin_by_link_returns_none_when_absent():
    fake = FakeTransport(ok({"items": [{"id": "p1", "link": "https://x"}]}))
    assert PinterestTool(TOKEN, transport=fake).find_pin_by_link("b", "https://missing") is None


# --- create pin ------------------------------------------------------------------------


def test_create_pin_multipart_with_image_bytes():
    fake = FakeTransport(ok({"id": "108999"}, status=201))
    result = PinterestTool(TOKEN, transport=fake).create_pin(
        board_id="board-1",
        title="The Sink Caddy",
        description="Rustproof and roomy.",
        link="https://neatspace-kitchen.github.io/p/k.json",
        image_bytes=b"JPEGDATA",
        alt_text="sink caddy",
    )
    assert result == {"pin_id": "108999", "url": "https://www.pinterest.com/pin/108999/"}
    call = fake.calls[0]
    assert call["url"].endswith("/v5/pins") and call["method"] == "POST"
    assert call["data"] == {
        "board_id": "board-1", "title": "The Sink Caddy",
        "description": "Rustproof and roomy.",
        "link": "https://neatspace-kitchen.github.io/p/k.json",
        "alt_text": "sink caddy",
    }
    name, payload, mime = call["files"]["media_source"]
    assert name == "product.jpg" and payload == b"JPEGDATA" and mime == "image/jpeg"


def test_create_pin_error_taxonomy():
    fake = FakeTransport(ok({}, status=401))
    with pytest.raises(PinterestTokenExpired):
        PinterestTool(TOKEN, transport=fake).create_pin(
            board_id="b", title="t", description="d", link="l", image_bytes=b"x"
        )
    fake = FakeTransport(ok({}, status=429))
    with pytest.raises(TransientError):
        PinterestTool(TOKEN, transport=fake).create_pin(
            board_id="b", title="t", description="d", link="l", image_bytes=b"x"
        )
    fake = FakeTransport(ok({}, status=403))
    with pytest.raises(PermanentError):
        PinterestTool(TOKEN, transport=fake).create_pin(
            board_id="b", title="t", description="d", link="l", image_bytes=b"x"
        )


def test_create_pin_file_field_is_configurable_for_drift():
    fake = FakeTransport(ok({"id": "1"}, status=201))
    PinterestTool(TOKEN, transport=fake, file_field="image").create_pin(
        board_id="b", title="t", description="d", link="l", image_bytes=b"x"
    )
    assert "image" in fake.calls[0]["files"]


# --- get pin + image download -------------------------------------------------------------


def test_get_pin_for_verification():
    fake = FakeTransport(ok({"id": "108999", "board_id": "board-1", "link": "https://x"}))
    pin = PinterestTool(TOKEN, transport=fake).get_pin("108999")
    assert pin["board_id"] == "board-1"
    assert fake.calls[0]["url"].endswith("/v5/pins/108999")
    fake = FakeTransport(ok({}, status=404))
    with pytest.raises(PermanentError):
        PinterestTool(TOKEN, transport=fake).get_pin("gone")


def test_download_image_returns_bytes():
    fake = FakeTransport(HttpReply(200, b"IMGDATA", "image/jpeg"))
    assert download_image("https://ae01.alicdn.com/kf/H1.jpg", transport=fake) == b"IMGDATA"
    fake = FakeTransport(HttpReply(200, b"", "image/jpeg"))
    with pytest.raises(TransientError):
        download_image("https://ae01.alicdn.com/kf/H1.jpg", transport=fake)
    fake = FakeTransport(HttpReply(404, b"", "text/plain"))
    with pytest.raises(PermanentError):
        download_image("https://ae01.alicdn.com/kf/H1.jpg", transport=fake)
