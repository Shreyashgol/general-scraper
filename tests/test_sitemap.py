"""Tests for sitemap-driven product discovery.

The tree is exercised with an in-memory ``httpx.MockTransport`` modelled on
beyoung.in's real layout: ``sitemap.xml`` (index) → ``products.xml`` /
``categories.xml`` (indexes) → a gzipped ``urlset`` shard each. The point the
tests pin is that only the *product* branch is descended, so category URLs never
reach the caller even though they share the product URLs' bare-slug shape.
"""

from __future__ import annotations

import gzip

import httpx
import pytest

from savana_scraper.core.config import load_settings
from savana_scraper.services.registry import adapter_for_url, source_for_url
from savana_scraper.services.sitemap_source import (
    SitemapProductSource,
    _collect_from_client,
    _is_product_sitemap,
    _looks_gzipped,
    _parse_sitemap,
)

_NS = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'


def _index(locs: list[str]) -> bytes:
    body = "".join(f"<sitemap><loc>{loc}</loc></sitemap>" for loc in locs)
    return f'<?xml version="1.0"?><sitemapindex {_NS}>{body}</sitemapindex>'.encode()


def _urlset(locs: list[str]) -> bytes:
    body = "".join(f"<url><loc>{loc}</loc></url>" for loc in locs)
    return f'<?xml version="1.0"?><urlset {_NS}>{body}</urlset>'.encode()


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def test_parse_splits_index_from_urlset() -> None:
    nested, pages = _parse_sitemap(_index(["https://x.com/products.xml"]))
    assert nested == ["https://x.com/products.xml"] and pages == []

    nested, pages = _parse_sitemap(_urlset(["https://x.com/a", "https://x.com/b"]))
    assert nested == [] and pages == ["https://x.com/a", "https://x.com/b"]


def test_parse_survives_garbage() -> None:
    assert _parse_sitemap(b"not xml at all") == ([], [])


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://x.com/products.xml", True),  # Magento
        ("https://x.com/sitemap_products_1.xml", True),  # Shopify
        ("https://x.com/product-sitemap.xml", True),  # Yoast / WooCommerce
        ("https://x.com/categories.xml", False),
        ("https://x.com/sitemap.xml", False),
    ],
)
def test_product_sitemap_detection(url: str, expected: bool) -> None:
    assert _is_product_sitemap(url) is expected


def test_looks_gzipped_by_extension_or_magic() -> None:
    assert _looks_gzipped("https://x.com/p.xml.gz", b"anything")
    assert _looks_gzipped("https://x.com/p.xml", b"\x1f\x8b\x08rest")
    assert not _looks_gzipped("https://x.com/p.xml", b"<?xml")


# --------------------------------------------------------------------------- #
# Full walk over a beyoung-shaped tree
# --------------------------------------------------------------------------- #
def _beyoung_routes() -> dict[str, tuple[str, bytes]]:
    prod_shard = gzip.compress(
        _urlset(
            [
                "https://www.beyoung.in/black-jacquard-striped-t-shirt",
                "https://www.beyoung.in/white-jacquard-striped-t-shirt",
                # An off-host stray must be dropped.
                "https://cdn.other.com/leak",
            ]
        )
    )
    cat_shard = gzip.compress(_urlset(["https://www.beyoung.in/t-shirts-for-men"]))
    return {
        "https://www.beyoung.in/robots.txt": (
            "text/plain",
            b"User-agent: *\nSitemap: https://www.beyoung.in/sitemap.xml\n",
        ),
        "https://www.beyoung.in/sitemap.xml": (
            "application/xml",
            _index(
                [
                    "https://www.beyoung.in/categories.xml",
                    "https://www.beyoung.in/products.xml",
                ]
            ),
        ),
        "https://www.beyoung.in/products.xml": (
            "application/xml",
            _index(["https://www.beyoung.in/api/products_1.xml.gz"]),
        ),
        "https://www.beyoung.in/categories.xml": (
            "application/xml",
            _index(["https://www.beyoung.in/api/categories.xml.gz"]),
        ),
        "https://www.beyoung.in/api/products_1.xml.gz": ("application/gzip", prod_shard),
        "https://www.beyoung.in/api/categories.xml.gz": ("application/gzip", cat_shard),
    }


def _client_for(routes: dict[str, tuple[str, bytes]]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        entry = routes.get(str(request.url))
        if entry is None:
            return httpx.Response(404)
        content_type, body = entry
        return httpx.Response(200, content=body, headers={"content-type": content_type})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_walk_returns_only_product_urls() -> None:
    async with _client_for(_beyoung_routes()) as client:
        urls, description = await _collect_from_client(
            client, "https://www.beyoung.in/", max_urls=100
        )
    assert urls == [
        "https://www.beyoung.in/black-jacquard-striped-t-shirt",
        "https://www.beyoung.in/white-jacquard-striped-t-shirt",
    ]
    # The category shard is never descended, and the off-host stray is dropped.
    assert "t-shirts-for-men" not in " ".join(urls)
    assert "cdn.other.com" not in " ".join(urls)
    assert description is not None and "products_1" in description


async def test_walk_without_product_sitemap_yields_nothing() -> None:
    """A flat sitemap with no product branch must not masquerade as products."""
    routes = {
        "https://shop.example/robots.txt": ("text/plain", b""),
        "https://shop.example/sitemap.xml": (
            "application/xml",
            _urlset(["https://shop.example/about", "https://shop.example/some-page"]),
        ),
    }
    async with _client_for(routes) as client:
        urls, description = await _collect_from_client(
            client, "https://shop.example/", max_urls=100
        )
    assert urls == [] and description is None


async def test_walk_respects_max_urls() -> None:
    async with _client_for(_beyoung_routes()) as client:
        urls, _ = await _collect_from_client(client, "https://www.beyoung.in/", max_urls=1)
    assert len(urls) == 1


# --------------------------------------------------------------------------- #
# Fast HTTP extraction path
# --------------------------------------------------------------------------- #
_PRODUCT_HTML = """<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"Party Mode On Tee",
 "image":"https://www.beyoung.in/img/party.jpg",
 "offers":{"@type":"Offer","price":"449","priceCurrency":"INR"}}
</script></head><body><h1>Party Mode On Tee</h1></body></html>"""


def _html_client(html: str) -> httpx.AsyncClient:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_http_fast_path_extracts_products_without_a_browser(settings) -> None:  # type: ignore[no-untyped-def]
    source = SitemapProductSource(settings)
    urls = ["https://www.beyoung.in/a", "https://www.beyoung.in/b"]
    async with _html_client(_PRODUCT_HTML) as client:
        assert await source._http_sample_confirms(client, urls)
        products = [p async for p in source._stream_http(client, urls, None)]
    assert len(products) == 2  # deduped by URL, both kept
    assert all(p.asp == 449 for p in products)
    assert {str(p.product_url) for p in products} == set(urls)


async def test_http_sample_rejects_pages_without_product_data(settings) -> None:  # type: ignore[no-untyped-def]
    """A page whose data needs JS parses to nothing over HTTP → sample fails → render."""
    source = SitemapProductSource(settings)
    async with _html_client("<html><body>loading…</body></html>") as client:
        assert not await source._http_sample_confirms(client, ["https://www.beyoung.in/a"])


# --------------------------------------------------------------------------- #
# Registry wiring
# --------------------------------------------------------------------------- #
def test_beyoung_routes_to_sitemap_source() -> None:
    assert adapter_for_url("https://www.beyoung.in/") == "beyoung.in"
    source = source_for_url("https://www.beyoung.in/", load_settings())
    assert isinstance(source, SitemapProductSource)


def test_sitemap_is_a_forceable_source() -> None:
    source = source_for_url("https://anything.example/", load_settings(source="sitemap"))
    assert isinstance(source, SitemapProductSource)
