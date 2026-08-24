"""Adapter contract shared by every store adapter (Phase 2).

The orchestrator and agents know ONLY this interface — never which store a
product came from. Adding Temu/Amazon later = one module + one registry entry.

Error taxonomy (maps 1:1 onto the state machine's failure classes):
  * TransientAdapterError  -> error_class="TRANSIENT"  (retry with backoff)
  * PermanentAdapterError  -> error_class="PERMANENT"  (straight to DEAD)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from pinner.errors import PermanentError, TransientError

# Normalized product payload — exactly the shape stored in products.raw.
RawProduct = dict[str, Any]


class AdapterError(Exception):
    def __init__(self, source: str, message: str) -> None:
        self.source = source
        super().__init__(f"[{source}] {message}")


class TransientAdapterError(AdapterError, TransientError):
    """Rate limits, 5xx, timeouts, gateway hiccups — worth retrying."""


class PermanentAdapterError(AdapterError, PermanentError):
    """Bad params, product gone, policy rejection — retrying only burns quota."""


@dataclass(frozen=True)
class CandidateProduct:
    source: str
    source_product_id: str
    title: str
    image_url: str | None
    product_url: str


class StoreAdapter(Protocol):
    name: str

    def search_products(self, niche_query: str, *, max_results: int = 10) -> list[CandidateProduct]:
        """Discover products for a niche query."""
        ...

    def get_product_details(self, candidate: CandidateProduct) -> RawProduct:
        """Fetch + normalize one product into the products.raw shape."""
        ...

    def build_affiliate_url(self, product_url: str) -> str:
        """Wrap a canonical product URL with the affiliate tracking id."""
        ...


def get_adapter(source: str, *, app_key: str, app_secret: str, tracking_id: str) -> StoreAdapter:
    """Registry of live adapters. Import kept lazy so contract tests of a
    single adapter never pull the others' optional dependencies."""
    if source == "aliexpress":
        from pinner.adapters.aliexpress import AliExpressAdapter

        return AliExpressAdapter(app_key, app_secret, tracking_id)
    raise ValueError(f"no adapter registered for source {source!r}")
