"""Tests for the product-page vs listing-page discriminator.

The scenarios below are the real shapes that broke earlier versions of the
generic crawler, reduced to their essentials.
"""

from __future__ import annotations

from decimal import Decimal

from bs4 import BeautifulSoup

from savana_scraper.services.page_signals import (
    free_price_nodes,
    has_product_markup,
    is_product_page,
    product_prices,
)

# A single product, price standing free of any link.
PDP = """
<html><body>
  <h1>A Light in the Attic</h1>
  <div class="product_main"><p class="price_color">£51.77</p></div>
</body></html>
"""

# A product page carrying a related-items sidebar: its own free price, plus two
# carded prices belonging to other books.
PDP_WITH_RELATED = """
<html><body>
  <h1>Tipping the Velvet</h1>
  <div class="product_main"><p class="price_color">£53.74</p></div>
  <aside>
    <article class="product_pod">
      <a href="/catalogue/other-book_1/index.html">Other</a>
      <p class="price_color">£51.77</p>
    </article>
    <article class="product_pod">
      <a href="/catalogue/third-book_2/index.html">Third</a>
      <p class="price_color">£12.00</p>
    </article>
  </aside>
</body></html>
"""

# A category page holding exactly one product — same h1 count and a similar
# price count to a product page. Only the free/carded split separates them.
CATEGORY_ONE_ITEM = """
<html><body>
  <h1>Academic</h1>
  <article class="product_pod">
    <a href="/catalogue/logan-kade_384/index.html">Logan Kade</a>
    <div class="product_price"><p class="price_color">£13.12</p></div>
  </article>
</body></html>
"""

CATEGORY_MANY = """
<html><body>
  <h1>Travel</h1>
  <article class="product_pod"><a href="/a">A</a><p class="price_color">£10.00</p></article>
  <article class="product_pod"><a href="/b">B</a><p class="price_color">£20.00</p></article>
  <article class="product_pod"><a href="/c">C</a><p class="price_color">£30.00</p></article>
</body></html>
"""


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


# --------------------------------------------------------------------------- #
# Page classification
# --------------------------------------------------------------------------- #
def test_plain_product_page_is_a_product() -> None:
    assert is_product_page(_soup(PDP))


def test_product_page_with_related_items_is_still_a_product() -> None:
    """The related-items carousel must not demote a real product page."""
    assert is_product_page(_soup(PDP_WITH_RELATED))


def test_category_page_holding_one_item_is_a_listing() -> None:
    """The case that fooled every price/heading-count heuristic."""
    assert not is_product_page(_soup(CATEGORY_ONE_ITEM))


def test_category_page_holding_many_items_is_a_listing() -> None:
    assert not is_product_page(_soup(CATEGORY_MANY))


def test_page_without_prices_is_not_a_product() -> None:
    assert not is_product_page(_soup("<html><body><h1>About us</h1></body></html>"))


def test_explicit_product_markup_overrides_the_card_heuristic() -> None:
    """A PDP declaring itself a Product wins even if every price looks carded."""
    html = '<html><head><meta property="og:type" content="product"></head>' + CATEGORY_MANY
    assert is_product_page(_soup(html))


def test_json_ld_product_counts_as_markup() -> None:
    html = '<html><head><script type="application/ld+json">{"@type":"Product"}</script></head><body></body></html>'
    assert has_product_markup(_soup(html))


def test_itemtype_product_counts_as_markup() -> None:
    html = '<html><body><div itemtype="https://schema.org/Product"></div></body></html>'
    assert has_product_markup(_soup(html))


# --------------------------------------------------------------------------- #
# Price selection
# --------------------------------------------------------------------------- #
def test_free_price_nodes_exclude_carded_ones() -> None:
    nodes = free_price_nodes(_soup(PDP_WITH_RELATED))
    assert [n.get_text(strip=True) for n in nodes] == ["£53.74"]


def test_product_prices_ignore_related_item_prices() -> None:
    """A neighbour's cheaper price must never become this product's ASP."""
    assert product_prices(_soup(PDP_WITH_RELATED)) == [Decimal("53.74")]


def test_product_prices_prefer_structured_markup() -> None:
    html = """
    <html><body>
      <span itemprop="price" content="99.00">99.00</span>
      <p class="price_color">£1.00</p>
    </body></html>
    """
    assert product_prices(_soup(html)) == [Decimal("99.00")]


def test_product_prices_empty_when_every_price_is_carded() -> None:
    assert product_prices(_soup(CATEGORY_MANY)) == []
