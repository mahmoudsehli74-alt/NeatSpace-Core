"""AliExpress Portals (IOP) adapter — built on the Phase 0-validated protocol.

Live behavior validated by the user in Phase 0:
  * MD5 signature generation over sorted form parameters (secret-wrapped)
  * affiliate link generation via ``aliexpress.affiliate.link.generate`` with
    ``source_values`` (the current parameter; the old ``target_url``/
    ``target_values`` forms are retired) — HTTP 200 with a real tracking link

Design:
  * The HTTP layer is an injectable ``transport`` callable so contract tests
    run entirely on recorded fixtures — CI NEVER makes live calls.
  * ``tests/test_aliexpress_adapter.py`` contains an opt-in LIVE smoke test
    (requires ALIEXPRESS_APP_KEY etc. + ALIEXPRESS_LIVE=1) for manual runs.
  * Errors are classified Transient (retry) vs Permanent (poison) per the
    adapter contract, ready for engine.fail_doc(error_class=...).

Endpoints used:
  * aliexpress.affiliate.product.query     — keyword discovery
  * aliexpress.affiliate.productdetail.get — single product details
  * aliexpress.affiliate.link.generate     — affiliate URL wrapping
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

from pinner.adapters.base import (
    CandidateProduct,
    PermanentAdapterError,
    RawProduct,
    TransientAdapterError,
)

logger = logging.getLogger(__name__)

IOP_GATEWAY = "https://api-sg.aliexpress.com/sync"
SIGN_METHOD = "md5"
API_VERSION = "2.0"

# IOP error codes that mean "slow down / try again later" (traffic limiting).
TRANSIENT_IOP_CODES = {20000000}

Transport = Callable[[str, dict[str, str]], "tuple[int, str]"]


def sign_md5(app_secret: str, params: dict[str, str]) -> str:
    """IOP signature: MD5(secret + sorted(key+value pairs) + secret), upper hex."""
    joined = "".join(f"{key}{params[key]}" for key in sorted(params))
    return hashlib.md5((app_secret + joined + app_secret).encode("utf-8")).hexdigest().upper()


def _to_form_value(value: Any) -> str:
    if isinstance(value, (list, dict)):
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return str(value)


def _raw_snippet(text: str, limit: int = 500) -> str:
    """Safe one-line snippet of a raw gateway response for diagnostics."""
    compact = " ".join(str(text).split())
    return compact[:limit] + ("…" if len(compact) > limit else "")


def _error_fields(payload: dict) -> str | None:
    """Sniff top-level error fields AliExpress uses OUTSIDE error_response."""
    for key in ("error_code", "errorCode", "error_msg", "error_desc", "msg", "sub_msg"):
        if payload.get(key):
            return f"{key}={payload[key]!r}"
    return None


def _default_transport(
    url: str,
    form: dict[str, str],
    *,
    client_factory: Callable[..., httpx.Client] = httpx.Client,
) -> tuple[int, str]:
    """Returns (HTTP status, body). Status flows into every error message —
    ALIEXPRESS_PROXY_URL (http/https/socks5) routes via a proxy egress for
    IP-restriction diagnosis/workarounds."""
    import os

    proxy = os.environ.get("ALIEXPRESS_PROXY_URL") or None
    try:
        with client_factory(timeout=20.0, proxy=proxy) as client:
            response = client.post(url, data=form)
            return response.status_code, response.text
    except httpx.HTTPError as exc:
        raise TransientAdapterError("aliexpress", f"http transport failed: {exc}") from exc


def _money(value: Any) -> tuple[float | None, str | None]:
    """Parse AliExpress money strings: 'US $14.99', '$14.99', '14.99'."""
    if value is None:
        return None, None
    text = str(value).strip()
    currency = None
    for token in ("US $", "US$", "USD ", "$"):
        if text.upper().startswith(token.upper()):
            currency = "USD"
            text = text[len(token):].strip()
            break
    try:
        return float(text.replace(",", "")), currency
    except ValueError:
        return None, None


def _split_images(raw_images: Any, fallback: Any = None) -> list[str]:
    blob = raw_images or fallback or ""
    if isinstance(blob, list):
        parts = [str(u) for u in blob]
    else:
        parts = [part for part in str(blob).replace(",", ";").split(";") if part.strip()]
    urls = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if not part.startswith("http"):
            part = f"https://{part}"
        urls.append(part)
    return urls


def normalize_product(item: dict) -> RawProduct:
    """Map an AliExpress product payload to the products.raw contract.
    Tolerates field drift (target_* vs plain price keys, sales_count vs
    lastest_volume) — the schema evolved across API versions."""
    sale, currency = _money(item.get("target_sale_price") or item.get("sale_price"))
    original, _ = _money(item.get("target_original_price") or item.get("original_price"))
    orders = item.get("lastest_volume")
    if orders is None:
        orders = item.get("sales_count") or 0
    rating = item.get("evaluate_rate")
    if isinstance(rating, str):
        rating = rating.replace("%", "").strip()
        try:
            rating = float(rating)
        except ValueError:
            rating = None
    detail_url = (item.get("product_detail_url") or "").split("?")[0]
    shop_name = item.get("shop_name") or (
        f"shop-{item['shop_id']}" if item.get("shop_id") else "unknown-shop"
    )
    return {
        "title": (item.get("product_title") or "").strip(),
        "description": (item.get("product_title") or "")[:1000],
        "images": _split_images(item.get("images"), item.get("product_image_url")),
        "price": {
            "current": sale,
            "currency": currency or "USD",
            "original": original,
        },
        "rating": rating,
        "orders": orders,
        "shop_name": shop_name,
        "source_url": detail_url,
    }


class AliExpressAdapter:
    name = "aliexpress"

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        tracking_id: str,
        *,
        base_url: str = IOP_GATEWAY,
        transport: Transport | None = None,
    ) -> None:
        self._app_key = app_key
        self._app_secret = app_secret
        self._tracking_id = tracking_id
        self._base_url = base_url
        self._transport = transport or _default_transport

    # --- protocol plumbing ------------------------------------------------------

    def _call(self, method: str, business_params: dict[str, Any]) -> dict:
        form: dict[str, str] = {
            "method": method,
            "app_key": self._app_key,
            "timestamp": str(int(time.time() * 1000)),
            "format": "json",
            "v": API_VERSION,
            "sign_method": SIGN_METHOD,
        }
        for key, value in business_params.items():
            form[key] = _to_form_value(value)
        form["sign"] = sign_md5(self._app_secret, form)
        status, body = self._transport(self._base_url, form)
        return self._parse(method, status, body)

    def _parse(self, method: str, status: int, text: str) -> dict:
        if status != 200:
            snippet = _raw_snippet(text)
            message = f"http {status} (body={snippet})"
            logger.warning("[aliexpress] %s for %s", message, method)
            if status >= 500 or status == 429:
                raise TransientAdapterError("aliexpress", message)
            raise PermanentAdapterError("aliexpress", message)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise TransientAdapterError(
                "aliexpress", f"non-JSON gateway response (http {status}): {text[:120]!r}"
            ) from exc
        if "error_response" in payload:
            err = payload["error_response"] or {}
            code = err.get("code")
            message = f"iop error {code}: {err.get('msg') or err.get('sub_msg')}"
            if code in TRANSIENT_IOP_CODES:
                raise TransientAdapterError("aliexpress", message)
            raise PermanentAdapterError("aliexpress", message)
        wrapper = f"{method.replace('.', '_')}_response"
        resp = payload.get(wrapper)

        def reject(kind: str) -> PermanentAdapterError:
            """Opaque responses MUST surface status + raw body — the go-live
            incident ("biz error None: None") was an empty resp_result dict
            with zero diagnostics. Never again."""
            detail = _error_fields(payload) or "no recognizable fields"
            logger.warning(
                "[aliexpress] http %s | %s for %s | raw body: %s",
                status, kind, method, _raw_snippet(text),
            )
            return PermanentAdapterError(
                "aliexpress",
                f"{kind} [http {status}] (body={_raw_snippet(text, 220)}; {detail})",
            )

        if not isinstance(resp, dict):
            raise reject(f"unexpected schema (no {wrapper})")

        resp_result = resp.get("resp_result")

        # Shape A (documented): {resp_result:{code,msg,result}}
        if isinstance(resp_result, dict):
            if not resp_result:
                # THE go-live incident: HTTP 200, wrapper found, resp_result
                # present but EMPTY — gateway-level rejection (signature, IP,
                # or app authorization). Raw body must surface, not None: None.
                raise reject("empty resp_result envelope")
            code = resp_result.get("code")
            if code not in (200, 0):
                biz = f"biz error {code}: {resp_result.get('msg')}"
                logger.warning(
                    "[aliexpress] %s for %s | raw body: %s", biz, method, _raw_snippet(text)
                )
                if code in TRANSIENT_IOP_CODES:
                    raise TransientAdapterError("aliexpress", biz)
                raise PermanentAdapterError("aliexpress", biz)
            result = resp_result.get("result") or {}
        elif resp_result is None:
            # Shape B tolerated: some IOP deployments nest result directly
            # under the wrapper without a resp_result layer.
            direct_keys = {"products", "product_detail", "promotion_links", "result"}
            if direct_keys & set(resp.keys()):
                result = resp.get("result") or {
                    k: v for k, v in resp.items() if k in direct_keys and k != "result"
                }
                for candidate_key in ("products", "product_detail", "promotion_links"):
                    if candidate_key in resp:
                        result = {candidate_key: resp[candidate_key]}
                        break
            else:
                raise reject("schema drift: neither resp_result nor known result keys")
        else:
            raise reject("resp_result present but not an object")
        return result

    # --- adapter contract ---------------------------------------------------------

    def search_products(
        self, niche_query: str, *, max_results: int = 10
    ) -> list[CandidateProduct]:
        result = self._call(
            "aliexpress.affiliate.product.query",
            {
                "keywords": niche_query,
                "tracking_id": self._tracking_id,
                "ship_to_country": "US",
                "target_currency": "USD",
                "target_language": "EN",
                "page_no": 1,
                "page_size": max(1, min(max_results, 50)),
            },
        )
        items = result.get("products") or []
        candidates = []
        for item in items:
            product_id = str(item.get("product_id") or "")
            if not product_id:
                continue
            images = _split_images(item.get("images"), item.get("product_image_url"))
            candidates.append(
                CandidateProduct(
                    source=self.name,
                    source_product_id=product_id,
                    title=(item.get("product_title") or "").strip(),
                    image_url=images[0] if images else None,
                    product_url=(item.get("product_detail_url") or "").split("?")[0],
                )
            )
        return candidates

    def get_product_details(self, candidate: CandidateProduct) -> RawProduct:
        result = self._call(
            "aliexpress.affiliate.productdetail.get",
            {
                "product_ids": candidate.source_product_id,
                "tracking_id": self._tracking_id,
                "ship_to_country": "US",
                "target_currency": "USD",
                "target_language": "EN",
            },
        )
        item = result.get("product_detail") or (result.get("products") or [None])[0]
        if not isinstance(item, dict) or not item:
            raise PermanentAdapterError(
                "aliexpress", f"product not found: {candidate.source_product_id}"
            )
        raw = normalize_product(item)
        if not raw["source_url"]:
            raw["source_url"] = candidate.product_url
        return raw

    def build_affiliate_url(self, product_url: str) -> str:
        result = self._call(
            "aliexpress.affiliate.link.generate",
            {
                # source_values is the CURRENT parameter (target_url is retired)
                "source_values": [product_url],
                "tracking_id": self._tracking_id,
            },
        )
        links = result.get("promotion_links") or []
        url = (links[0] or {}).get("promotion_link") if links else None
        if not url:
            raise PermanentAdapterError(
                "aliexpress", f"no promotion link returned for {product_url}"
            )
        return url
