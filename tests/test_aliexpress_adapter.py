"""AliExpress adapter contract tests (Phase 2).

Runs entirely on RECORDED fixture payloads via an injectable transport —
CI never makes live calls. The opt-in live smoke test at the bottom requires
real credentials AND ALIEXPRESS_LIVE=1 (run it manually after API drift).
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
import pytest

from pinner.adapters import aliexpress as ax
from pinner.adapters.base import (
    CandidateProduct,
    PermanentAdapterError,
    TransientAdapterError,
    get_adapter,
)

APP_KEY, APP_SECRET, TRACKING_ID = "key123", "s3cret", "neatspace-01"


def make_adapter(handler: Any) -> tuple[ax.AliExpressAdapter, list[dict]]:
    """Adapter whose transport records every posted form and replies via handler."""
    posted: list[dict] = []

    def transport(url: str, form: dict[str, str]) -> str:
        posted.append({"url": url, "form": dict(form)})
        return handler(form)

    return ax.AliExpressAdapter(APP_KEY, APP_SECRET, TRACKING_ID, transport=transport), posted


def _ok(method: str, result: dict) -> str:
    # Real API wrapper: "aliexpress.affiliate.link.generate" ->
    # "aliexpress_affiliate_link_generate_response" (method dots -> underscores)
    wrapper = f"{method.replace('.', '_')}_response"
    return json.dumps({wrapper: {"resp_result": {"code": 200, "msg": "success", "result": result}}})


PRODUCT_ITEM = {
    "product_id": 1005006123456789,
    "product_title": "Stainless Sink Caddy Organizer",
    "product_detail_url": "https://www.aliexpress.com/item/1005006123456789.html?spm=a2g0o.1",
    "product_image_url": "https://ae01.alicdn.com/kf/H1.jpg",
    "images": "https://ae01.alicdn.com/kf/H1.jpg;ae02.alicdn.com/kf/H2.jpg",
    "target_sale_price": "US $14.99",
    "target_original_price": "US $24.99",
    "lastest_volume": 1337,
    "evaluate_rate": "96.2%",
    "shop_id": 9087001,
    "shop_url": "https://www.aliexpress.com/store/9087001",
}


# --- signing & request construction ---------------------------------------------


def test_sign_md5_wraps_secret_and_sorts_keys():
    params = {"b": "2", "a": "1"}
    # algorithm restated independently: md5(secret + "a1" + "b2" + secret).upper
    import hashlib

    expected = hashlib.md5(b"s3cret" + b"a1" + b"b2" + b"s3cret").hexdigest().upper()
    assert ax.sign_md5(APP_SECRET, params) == expected
    assert ax.sign_md5(APP_SECRET, {"a": "1", "b": "2"}) == expected  # order-insensitive


def test_call_builds_and_signs_the_form():
    def handler(form):
        return _ok(
            "aliexpress.affiliate.link.generate",
            {"promotion_links": [{"promotion_link": "https://s.click.aliexpress.com/e/_X"}]},
        )

    adapter, posted = make_adapter(handler)
    adapter.build_affiliate_url("https://www.aliexpress.com/item/1.html")
    form = posted[0]["form"]
    assert posted[0]["url"] == ax.IOP_GATEWAY
    assert form["method"] == "aliexpress.affiliate.link.generate"
    assert form["app_key"] == APP_KEY
    assert form["format"] == "json" and form["v"] == "2.0" and form["sign_method"] == "md5"
    assert form["timestamp"].isdigit()  # milliseconds
    # lists travel as compact JSON strings (source_values — the current param)
    assert form["source_values"] == '["https://www.aliexpress.com/item/1.html"]'
    assert form["tracking_id"] == TRACKING_ID
    # the signature covers every field EXCEPT itself, and matches a recompute
    unsigned = {k: v for k, v in form.items() if k != "sign"}
    assert form["sign"] == ax.sign_md5(APP_SECRET, unsigned)


# --- response parsing --------------------------------------------------------------


def test_search_products_parses_candidates_and_page_size():
    adapter, posted = make_adapter(
        lambda form: _ok(
            "aliexpress.affiliate.product.query",
            {"products": [PRODUCT_ITEM], "total_results": 57},
        )
    )
    candidates = adapter.search_products("kitchen organizer", max_results=5)
    assert posted[0]["form"]["page_size"] == "5"
    assert len(candidates) == 1
    c = candidates[0]
    assert c.source == "aliexpress"
    assert c.source_product_id == "1005006123456789"
    assert c.product_url == "https://www.aliexpress.com/item/1005006123456789.html"
    assert c.image_url == "https://ae01.alicdn.com/kf/H1.jpg"


def test_get_product_details_normalizes_to_raw_contract():
    adapter, _ = make_adapter(
        lambda form: _ok("aliexpress.affiliate.productdetail.get", {"product_detail": PRODUCT_ITEM})
    )
    raw = adapter.get_product_details(
        CandidateProduct(
            source="aliexpress",
            source_product_id="1005006123456789",
            title="x",
            image_url=None,
            product_url="https://www.aliexpress.com/item/1005006123456789.html",
        )
    )
    assert raw["title"] == "Stainless Sink Caddy Organizer"
    assert raw["price"] == {"current": 14.99, "currency": "USD", "original": 24.99}
    assert raw["orders"] == 1337
    assert raw["rating"] == 96.2
    assert raw["shop_name"] == "shop-9087001"
    # images split on ';', scheme-less URLs upgraded to https
    assert raw["images"] == [
        "https://ae01.alicdn.com/kf/H1.jpg",
        "https://ae02.alicdn.com/kf/H2.jpg",
    ]
    assert raw["source_url"].startswith("https://www.aliexpress.com/item/1005006123456789.html")


def test_build_affiliate_url_returns_promotion_link():
    adapter, _ = make_adapter(
        lambda form: _ok(
            "aliexpress.affiliate.link.generate",
            {"promotion_links": [{"promotion_link": "https://s.click.aliexpress.com/e/_D123"}]},
        )
    )
    url = adapter.build_affiliate_url("https://www.aliexpress.com/item/1.html")
    assert url == "https://s.click.aliexpress.com/e/_D123"


def test_legacy_price_fields_are_tolerated():
    item = dict(
        PRODUCT_ITEM,
        sale_price="US $9.10",
        original_price="$19.00",
        sales_count=42,
        lastest_volume=None,
    )
    del item["target_sale_price"]
    del item["target_original_price"]
    raw = ax.normalize_product(item)
    assert raw["price"]["current"] == 9.10 and raw["price"]["original"] == 19.0
    assert raw["orders"] == 42


# --- error taxonomy ------------------------------------------------------------------


def test_iop_rate_limit_is_transient():
    adapter, _ = make_adapter(
        lambda form: json.dumps({"error_response": {"code": 20000000, "msg": "traffic limit"}})
    )
    with pytest.raises(TransientAdapterError):
        adapter.search_products("x")


def test_iop_business_error_is_permanent():
    adapter, _ = make_adapter(
        lambda form: json.dumps({"error_response": {"code": 20100, "msg": "invalid param"}})
    )
    with pytest.raises(PermanentAdapterError):
        adapter.build_affiliate_url("https://x/item/1.html")


def test_missing_wrapper_and_bad_codes():
    adapter, _ = make_adapter(lambda form: json.dumps({"unexpected": {}}))
    with pytest.raises(TransientAdapterError):
        adapter.search_products("x")
    adapter2, _ = make_adapter(
        lambda form: json.dumps(
            {"aliexpress_affiliate_product_query_response": {"resp_result": {"code": 401}}}
        )
    )
    with pytest.raises(PermanentAdapterError):
        adapter2.search_products("x")


def test_non_json_gateway_response_is_transient():
    adapter, _ = make_adapter(lambda form: "<html>502 Bad Gateway</html>")
    with pytest.raises(TransientAdapterError):
        adapter.search_products("x")


def test_no_promotion_link_is_permanent():
    adapter, _ = make_adapter(
        lambda form: _ok("aliexpress.affiliate.link.generate", {"promotion_links": []})
    )
    with pytest.raises(PermanentAdapterError):
        adapter.build_affiliate_url("https://x/item/1.html")


def test_default_transport_maps_httpx_errors_to_transient():
    def boom(request):
        raise httpx.ConnectError("no route")

    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(boom))

    with pytest.raises(TransientAdapterError):
        ax._default_transport(ax.IOP_GATEWAY, {"method": "x"}, client_factory=factory)


def test_registry_returns_aliexpress_adapter():
    adapter = get_adapter(
        "aliexpress", app_key=APP_KEY, app_secret=APP_SECRET, tracking_id=TRACKING_ID
    )
    assert adapter.name == "aliexpress"
    with pytest.raises(ValueError):
        get_adapter("temu", app_key="k", app_secret="s", tracking_id="t")


# --- opt-in live smoke test (NEVER runs in CI) ---------------------------------------


@pytest.mark.skipif(
    not (os.environ.get("ALIEXPRESS_APP_KEY") and os.environ.get("ALIEXPRESS_LIVE") == "1"),
    reason="live test: needs real credentials + ALIEXPRESS_LIVE=1",
)
def test_live_smoke():
    from pinner.config import load_settings

    settings = load_settings()
    adapter = ax.AliExpressAdapter(
        settings.aliexpress_app_key,
        settings.aliexpress_app_secret,
        settings.aliexpress_tracking_id,
    )
    candidates = adapter.search_products("kitchen organizer", max_results=3)
    assert candidates, "expected at least one live candidate"
    affiliate = adapter.build_affiliate_url(candidates[0].product_url)
    assert "s.click.aliexpress.com" in affiliate or TRACKING_ID in affiliate
    raw = adapter.get_product_details(candidates[0])
    assert raw["title"] and raw["images"] and raw["price"]["current"] is not None
