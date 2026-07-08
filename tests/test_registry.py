"""Tests for domain→source routing and the generic crawler's URL clustering."""

from __future__ import annotations

import pytest

from savana_scraper.core.config import load_settings
from savana_scraper.services.generic_source import (
    GenericProductSource,
    _bucket_of,
    _segments_of,
    _template_of,
)
from savana_scraper.services.registry import adapter_for_url, source_for_url
from savana_scraper.services.sources import ApiProductSource, BrowserProductSource


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "https://www.savana.com/activity/13070",
        "https://savana.com/",
        "https://shop.savana.com/x",
    ],
)
def test_registered_domain_matches_subdomains(url: str) -> None:
    assert adapter_for_url(url) == "savana.com"


def test_unregistered_domain_has_no_adapter() -> None:
    assert adapter_for_url("https://books.toscrape.com/") is None
    # Must not match on a suffix that merely ends with the registered name.
    assert adapter_for_url("https://notsavana.com/") is None


def test_auto_routes_registered_domain_to_its_adapter() -> None:
    source = source_for_url("https://www.savana.com/", load_settings())
    assert isinstance(source, ApiProductSource)


def test_auto_falls_back_to_generic_for_unknown_domain() -> None:
    source = source_for_url("https://books.toscrape.com/", load_settings())
    assert isinstance(source, GenericProductSource)


def test_explicit_source_overrides_the_registry() -> None:
    settings = load_settings(source="browser")
    assert isinstance(source_for_url("https://www.savana.com/", settings), BrowserProductSource)
    settings = load_settings(source="generic")
    assert isinstance(source_for_url("https://www.savana.com/", settings), GenericProductSource)


def test_unknown_source_name_raises() -> None:
    with pytest.raises(ValueError, match="Unknown source"):
        source_for_url("https://example.com/", load_settings(source="telepathy"))


# --------------------------------------------------------------------------- #
# URL clustering
# --------------------------------------------------------------------------- #
def test_segments_of_rejects_non_product_paths() -> None:
    assert _segments_of("https://x.com/cart") is None
    assert _segments_of("https://x.com/help-center/faq") is None
    assert _segments_of("https://x.com/") is None
    assert _segments_of("https://x.com/details/bag-1") == ["details", "bag-1"]


def test_bucket_separates_savana_products_from_categories() -> None:
    """Both are depth 2; only the first segment tells them apart."""
    product = _bucket_of(["details", "charm-totes-bag-1949872"])
    category = _bucket_of(["activity", "13070"])
    assert product != category


def test_template_wildcards_the_varying_position() -> None:
    """The id/slug is not always the last segment — see books.toscrape.com."""
    bucket = [
        ["catalogue", "a-light-in-the-attic_1000", "index.html"],
        ["catalogue", "tipping-the-velvet_999", "index.html"],
        ["catalogue", "soumission_998", "index.html"],
    ]
    assert _template_of(bucket) == "/catalogue/*/index.html"


def test_template_keeps_positions_that_never_vary() -> None:
    bucket = [["details", "bag-1"], ["details", "tote-2"]]
    assert _template_of(bucket) == "/details/*"
