"""Adapter protocol — Phase 2 contract.

Every store adapter satisfies the same interface; the orchestrator and agents
never know which store a product came from:

    search_products(niche_query: str, max_results: int) -> list[CandidateProduct]
    get_product_details(candidate) -> RawProduct          # normalized to products.raw shape
    build_affiliate_url(product_url: str) -> str          # tracking-id embedded

Registry: SOURCES = {"aliexpress": AliExpressAdapter} — adding Temu is one
module + one registry entry + zero orchestrator changes.
AliExpress specifics validated in Phase 0: MD5 signature generation and the
source_values parameter for link building (HTTP 200 with live tracking link).
"""
