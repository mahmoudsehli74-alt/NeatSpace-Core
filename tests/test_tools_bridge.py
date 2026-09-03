"""GitHub bridge tool tests — faked transport, CI never touches GitHub."""

from __future__ import annotations

import base64
import json

import pytest

from pinner.errors import PermanentError, TransientError
from pinner.tools.bridge import BridgeTool, canonical_product_json
from pinner.tools.http import HttpReply

REPO = "builder/neatspace-kitchen"
PAYLOAD = {"title": "Sink Caddy", "price": 14.99, "disclosure": "affiliate link"}


class FakeTransport:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def __call__(self, method, url, *, headers=None, json_body=None, data=None, files=None):
        self.calls.append(
            {"method": method, "url": url, "headers": headers,
             "json_body": json_body, "data": data, "files": files}
        )
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def gh_content_reply(canonical: bytes, sha: str = "file-sha-1") -> HttpReply:
    b64 = base64.b64encode(canonical).decode("ascii")
    wrapped = "\n".join(b64[i : i + 60] for i in range(0, len(b64), 60))  # GitHub wraps
    body = json.dumps({"sha": sha, "content": wrapped, "encoding": "base64"})
    return HttpReply(200, body.encode(), "application/json")


def commit_reply(sha: str = "commit-sha-9") -> HttpReply:
    body = json.dumps({"commit": {"sha": sha, "html_url": f"https://github.com/c/{sha}"}})
    return HttpReply(201, body.encode(), "application/json")


def test_push_new_file_creates_without_sha():
    fake = FakeTransport(HttpReply(404, b"{}"), commit_reply())
    tool = BridgeTool("pat", transport=fake)
    result = tool.push_product(REPO, "stub-store-hello-0001", PAYLOAD)

    assert result["committed"] is True
    assert result["commit_sha"] == "commit-sha-9"
    get, put = fake.calls[0], fake.calls[1]
    assert get["method"] == "GET" and "/contents/products/stub-store-hello-0001.json" in get["url"]
    assert "ref=main" in get["url"] and "Bearer pat" in get["headers"]["Authorization"]
    assert put["method"] == "PUT"
    body = put["json_body"]
    assert body["branch"] == "main" and "sha" not in body
    assert "chore: product stub-store-hello-0001" == body["message"]
    decoded = base64.b64decode(body["content"])
    assert decoded == canonical_product_json(PAYLOAD)
    assert json.loads(decoded)["title"] == "Sink Caddy"


def test_push_update_includes_existing_file_sha():
    different = canonical_product_json(dict(PAYLOAD, price=9.99))
    fake = FakeTransport(gh_content_reply(different, sha="old-sha"), commit_reply("new-sha"))
    tool = BridgeTool("pat", transport=fake)
    result = tool.push_product(REPO, "key-1", PAYLOAD)
    assert result["committed"] is True and result["commit_sha"] == "new-sha"
    assert fake.calls[1]["json_body"]["sha"] == "old-sha"


def test_push_identical_content_adopts_without_committing():
    identical = canonical_product_json(PAYLOAD)
    fake = FakeTransport(gh_content_reply(identical, sha="same-sha"))
    tool = BridgeTool("pat", transport=fake)
    result = tool.push_product(REPO, "key-1", PAYLOAD)
    assert result == {
        "commit_sha": "same-sha",
        "path": "products/key-1.json",
        "committed": False,
        "html_url": f"https://github.com/{REPO}/blob/main/products/key-1.json",
    }
    assert len(fake.calls) == 1  # GET only — the crash-window contract


def test_canonical_json_is_order_insensitive():
    a = {"title": "x", "price": 1, "images": ["a", "b"]}
    b = {"images": ["a", "b"], "price": 1, "title": "x"}
    assert canonical_product_json(a) == canonical_product_json(b)


@pytest.mark.parametrize(
    ("get_status", "put_status", "expected"),
    [(500, None, TransientError), (403, None, PermanentError),
     (404, 429, TransientError), (404, 403, PermanentError)],
)
def test_error_taxonomy(get_status, put_status, expected):
    replies = [HttpReply(get_status, b"{}")]
    if put_status:
        replies.append(HttpReply(put_status, b"{}"))
    tool = BridgeTool("pat", transport=FakeTransport(*replies))
    with pytest.raises(expected):
        tool.push_product(REPO, "key-1", PAYLOAD)


def test_verify_deployed_polls_until_200():
    sleeps: list[float] = []
    fake = FakeTransport(HttpReply(404, b""), HttpReply(502, b""), HttpReply(200, b"<html>"))
    tool = BridgeTool("pat", transport=fake)
    ok = tool.verify_deployed("https://neatspace-kitchen.github.io/p/k.json", sleeper=sleeps.append)
    assert ok is True and sleeps == [20.0, 20.0]
    assert fake.calls[0]["url"].startswith("https://neatspace-kitchen.github.io")


def test_verify_deployed_gives_up_after_attempts():
    sleeps: list[float] = []
    # LIVE 2026-09-03: Pages builds take 3-5 min — the window is now 12x20s
    fake = FakeTransport(*[HttpReply(404, b"")] * 12)
    tool = BridgeTool("pat", transport=fake)
    assert tool.verify_deployed("https://x.github.io/p/k.json", sleeper=sleeps.append) is False
    assert len(fake.calls) == 12 and set(sleeps) == {20.0}
