"""Tests for the layered extractor across all three strategies."""

from __future__ import annotations

from decimal import Decimal

from savana_scraper.core.config import Settings
from savana_scraper.services.extractor import Extractor

PAGE_URL = "https://www.savana.com/product/123"


def _extract(html: str) -> object:
    return Extractor(Settings()).extract_fields(html, PAGE_URL)


def test_structured_data_wins() -> None:
    html = """
    <html><head>
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Product",
     "name":"JSON-LD Shirt",
     "image":["https://cdn.savana.com/a.jpg"],
     "mrp":"1499",
     "offers":{"@type":"Offer","price":"1099","priceCurrency":"INR"}}
    </script>
    </head><body><h1>DOM Shirt</h1></body></html>
    """
    fs = _extract(html)
    assert fs.name == "JSON-LD Shirt"  # structured beats DOM
    assert fs.image_url == "https://cdn.savana.com/a.jpg"
    assert fs.asp == Decimal("1099")
    assert fs.mrp == Decimal("1499")


def test_dom_fallback_when_no_structured_data() -> None:
    html = """
    <html><body>
      <h1>DOM Only Shirt</h1>
      <div class="product-image"><img src="/img/b.jpg"></div>
      <span class="mrp">₹2,000</span>
      <span class="price--current">₹1,500</span>
    </body></html>
    """
    fs = _extract(html)
    assert fs.name == "DOM Only Shirt"
    # Relative image resolved against the page URL.
    assert fs.image_url == "https://www.savana.com/img/b.jpg"
    assert fs.mrp == Decimal("2000")
    assert fs.asp == Decimal("1500")


def test_open_graph_fallback() -> None:
    html = """
    <html><head>
      <meta property="og:title" content="OG Shirt">
      <meta property="og:image" content="https://cdn.savana.com/og.jpg">
    </head><body></body></html>
    """
    fs = _extract(html)
    assert fs.name == "OG Shirt"
    assert fs.image_url == "https://cdn.savana.com/og.jpg"


def test_gaps_filled_across_strategies() -> None:
    # Name from JSON-LD, image only available via OG meta.
    html = """
    <html><head>
      <meta property="og:image" content="https://cdn.savana.com/og.jpg">
      <script type="application/ld+json">
      {"@type":"Product","name":"Mixed Shirt",
       "offers":{"price":"500"}}
      </script>
    </head><body></body></html>
    """
    fs = _extract(html)
    assert fs.name == "Mixed Shirt"
    assert fs.image_url == "https://cdn.savana.com/og.jpg"
    assert fs.asp == Decimal("500")


def test_incomplete_page_reports_not_complete() -> None:
    fs = _extract("<html><body><p>nothing here</p></body></html>")
    assert not fs.is_complete()
