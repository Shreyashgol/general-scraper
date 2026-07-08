"""Tests for breadcrumb → (category, subcategory) reduction.

The rule under test: drop the root crumb, drop the trailing product name, take
the next two. Everything here pins a way the naive version gets it wrong.
"""

from __future__ import annotations

import json

import pytest
from bs4 import BeautifulSoup

from savana_scraper.services.breadcrumbs import (
    category_pair,
    crumbs_from_dom,
    crumbs_from_json_ld,
    crumbs_from_schema_category,
)
from savana_scraper.services.extractor import BreadcrumbStrategy


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def _breadcrumb_ld(*names: str) -> str:
    items = [
        {"@type": "ListItem", "position": i, "item": {"@id": f"/{n}", "name": n}}
        for i, n in enumerate(names, start=1)
    ]
    blob = {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": items}
    return f'<script type="application/ld+json">{json.dumps(blob)}</script>'


# --------------------------------------------------------------------------- #
# The reduction rule
# --------------------------------------------------------------------------- #
def test_drops_root_and_trailing_product_name() -> None:
    crumbs = ["Home", "Women", "Bags", "Backpacks", "Kitty School Backpack"]
    assert category_pair(crumbs, "Kitty School Backpack") == ("Women", "Bags")


def test_shallow_trail_yields_no_subcategory() -> None:
    """`Home > Dresses > <product>` has a category and honestly no subcategory."""
    assert category_pair(["Home", "Dresses", "Slit Dress"], "Slit Dress") == ("Dresses", None)


def test_trailing_product_name_is_never_a_subcategory() -> None:
    """Without the name check, the product itself becomes its own subcategory."""
    category, subcategory = category_pair(["Home", "Dresses", "Slit Dress"], "Slit Dress")
    assert subcategory != "Slit Dress"
    assert (category, subcategory) == ("Dresses", None)


def test_product_name_match_ignores_case_and_whitespace() -> None:
    crumbs = ["Home", "Bags", "Totes", "  Star   Totes Bag "]
    assert category_pair(crumbs, "Star Totes Bag") == ("Bags", "Totes")


def test_last_crumb_kept_when_it_is_not_the_product() -> None:
    assert category_pair(["Home", "Bags", "Totes"], "Star Totes Bag") == ("Bags", "Totes")


def test_root_crumb_only_stripped_at_the_front() -> None:
    """A "Shop" nested three deep is a real category, not a root."""
    assert category_pair(["Home", "Women", "Shop"], None) == ("Women", "Shop")


def test_trail_of_only_root_yields_nothing() -> None:
    assert category_pair(["Home"], None) == (None, None)


def test_empty_trail_yields_nothing() -> None:
    assert category_pair([], "Anything") == (None, None)


def test_blank_crumbs_are_ignored() -> None:
    assert category_pair(["", "  ", "Bags", "Totes"], None) == ("Bags", "Totes")


# --------------------------------------------------------------------------- #
# Sources, in priority order
# --------------------------------------------------------------------------- #
def test_json_ld_breadcrumbs_read_in_position_order() -> None:
    """`position` wins over document order."""
    items = [
        {"@type": "ListItem", "position": 3, "name": "Backpacks"},
        {"@type": "ListItem", "position": 1, "name": "Home"},
        {"@type": "ListItem", "position": 2, "name": "Bags"},
    ]
    blob = {"@type": "BreadcrumbList", "itemListElement": items}
    html = f'<script type="application/ld+json">{json.dumps(blob)}</script>'
    assert crumbs_from_json_ld(_soup(html)) == ["Home", "Bags", "Backpacks"]


def test_json_ld_name_read_from_nested_item() -> None:
    assert crumbs_from_json_ld(_soup(_breadcrumb_ld("Home", "Bags"))) == ["Home", "Bags"]


def test_malformed_json_ld_is_skipped_not_raised() -> None:
    html = '<script type="application/ld+json">{ not json</script>'
    assert crumbs_from_json_ld(_soup(html)) == []


def test_schema_product_category_path_is_split() -> None:
    blob = {"@type": "Product", "name": "X", "category": "Women > Bags > Backpacks"}
    html = f'<script type="application/ld+json">{json.dumps(blob)}</script>'
    assert crumbs_from_schema_category(_soup(html)) == ["Women", "Bags", "Backpacks"]


def test_schema_product_category_accepts_a_list() -> None:
    blob = {"@type": "Product", "name": "X", "category": ["Women", "Bags"]}
    html = f'<script type="application/ld+json">{json.dumps(blob)}</script>'
    assert crumbs_from_schema_category(_soup(html)) == ["Women", "Bags"]


def test_dom_breadcrumbs_deduplicate_nested_markup() -> None:
    """An <li> wrapping an <a> must not contribute its text twice."""
    html = """
    <nav aria-label="Breadcrumb">
      <ol>
        <li><a href="/">Home</a></li>
        <li><a href="/bags">Bags</a></li>
        <li><span>Backpacks</span></li>
      </ol>
    </nav>
    """
    assert crumbs_from_dom(_soup(html)) == ["Home", "Bags", "Backpacks"]


def test_dom_separators_are_not_crumbs() -> None:
    html = """
    <div class="breadcrumbs">
      <a href="/">Home</a> <span>/</span> <a href="/bags">Bags</a> <span>›</span>
      <span>Totes</span>
    </div>
    """
    assert crumbs_from_dom(_soup(html)) == ["Home", "Bags", "Totes"]


def test_single_link_in_matching_element_is_not_a_trail() -> None:
    """One crumb is a stray link, not a breadcrumb trail."""
    assert crumbs_from_dom(_soup('<div class="breadcrumb"><a href="/">Home</a></div>')) == []


def test_no_breadcrumbs_anywhere() -> None:
    soup = _soup("<html><body><h1>Product</h1></body></html>")
    assert crumbs_from_json_ld(soup) == []
    assert crumbs_from_dom(soup) == []
    assert crumbs_from_schema_category(soup) == []


# --------------------------------------------------------------------------- #
# The strategy that wires them together
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("html", "expected"),
    [
        (
            _breadcrumb_ld("Home", "Women", "Bags", "Backpacks") + "<h1>Kitty Backpack</h1>",
            ("Women", "Bags"),
        ),
        (
            '<nav aria-label="breadcrumb"><a href="/">Home</a><a href="/d">Dresses</a>'
            "<span>Slit Dress</span></nav><h1>Slit Dress</h1>",
            ("Dresses", None),
        ),
        ("<h1>Lonely Product</h1>", (None, None)),
    ],
)
def test_breadcrumb_strategy(html: str, expected: tuple[str | None, str | None]) -> None:
    fields = BreadcrumbStrategy().parse(_soup(html), html, "https://shop.example/p/1")
    assert (fields.category, fields.subcategory) == expected


def test_breadcrumb_strategy_fills_nothing_else() -> None:
    """It must not compete with the strategies that own name/image/prices."""
    html = _breadcrumb_ld("Home", "Bags", "Totes") + "<h1>Star Totes Bag</h1>"
    fields = BreadcrumbStrategy().parse(_soup(html), html, "https://shop.example/p/1")
    assert fields.name is None
    assert fields.image_url is None
    assert fields.mrp is None and fields.asp is None


def test_strategy_uses_og_title_when_no_h1() -> None:
    html = (
        _breadcrumb_ld("Home", "Dresses", "Slit Dress")
        + '<meta property="og:title" content="Slit Dress">'
    )
    fields = BreadcrumbStrategy().parse(_soup(html), html, "https://shop.example/p/1")
    assert (fields.category, fields.subcategory) == ("Dresses", None)
