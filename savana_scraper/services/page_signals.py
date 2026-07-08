"""Structural signals read off a rendered page.

Two questions get asked of an unknown storefront page, and both turn out to have
the same answer underneath:

    "Is this one product's page, or a grid listing many?"
    "Which of the prices on this page is *this* product's price?"

The insight: **on a listing, every price sits inside a small box that links
somewhere else. On a product page, the main price stands free.**

A product page with a "related items" carousel has both — carded prices for the
neighbours, one free-standing price of its own. That single distinction
classifies the page *and* picks the right price out of it.

Measured on books.toscrape.com:

    ================================  ======  ====  ======  =========
    page                              prices  free  carded  verdict
    ================================  ======  ====  ======  =========
    product (no sidebar)                   1     1       0  product
    product (related-books sidebar)        3     1       2  product
    category holding one book              2     0       2  listing
    category holding many                 22     0      22  listing
    homepage                              40     0      40  listing
    ================================  ======  ====  ======  =========

Without the free/carded split, that one-book category page is indistinguishable
from a product page — same heading count, same price count — and the crawler
exports category names as products.
"""

from __future__ import annotations

from decimal import Decimal

from bs4 import BeautifulSoup, Tag

from savana_scraper.services.pricing import parse_price

#: Elements that hold a price on storefronts with no structured data.
PRICE_NODE_SELECTOR = "[itemprop='price'], [data-price], [class*='price' i]"
#: Structured price sources, in descending order of trustworthiness.
STRUCTURED_PRICE_SELECTORS = ("[itemprop='price']", "meta[itemprop='price']", "[data-price]")

# Tags that can act as a "product card" wrapping a price.
_CARD_TAGS = frozenset({"li", "article", "div", "td", "tr", "section"})
# How far above a price we look for the card wrapping it.
_CARD_MAX_DEPTH = 4
# A card is a small box, not a whole page region. Past this, we've walked out.
_CARD_MAX_TEXT = 300


def card_href_for(price: Tag) -> str | None:
    """The link of the product card wrapping ``price``, or ``None`` if it stands free."""
    node: Tag | None = price
    for _ in range(_CARD_MAX_DEPTH):
        node = node.parent if node is not None else None
        if node is None:
            return None
        if node.name not in _CARD_TAGS:
            continue
        if len(node.get_text(strip=True)) > _CARD_MAX_TEXT:
            return None  # too big to be a card
        link = node.find("a", href=True)
        if isinstance(link, Tag):
            return str(link["href"])
    return None


def free_price_nodes(soup: BeautifulSoup) -> list[Tag]:
    """Price elements that are *not* inside a link card — i.e. this page's own."""
    return [node for node in soup.select(PRICE_NODE_SELECTOR) if card_href_for(node) is None]


def has_product_markup(soup: BeautifulSoup) -> bool:
    """Explicit, machine-readable "this page is one product" declarations."""
    og_type = soup.find("meta", attrs={"property": "og:type"})
    if og_type and str(og_type.get("content", "")).lower() == "product":
        return True
    if soup.select_one('[itemtype*="schema.org/Product" i]'):
        return True
    return any(
        '"product"' in (tag.string or "").lower()
        for tag in soup.find_all("script", attrs={"type": "application/ld+json"})
    )


def is_product_page(soup: BeautifulSoup) -> bool:
    """True when the page is about a single product rather than listing many."""
    if has_product_markup(soup):
        return True
    if not soup.select(PRICE_NODE_SELECTOR):
        return False
    # Every price belongs to someone else's card ⇒ this is a grid.
    return bool(free_price_nodes(soup))


def product_prices(soup: BeautifulSoup) -> list[Decimal]:
    """The prices belonging to *this* page's product, best source first.

    Structured markup wins outright. Otherwise we take only free-standing prices,
    which is what keeps a related-items carousel from contributing a neighbour's
    price to this product's MRP/ASP.
    """
    for selector in STRUCTURED_PRICE_SELECTORS:
        values = [
            price
            for node in soup.select(selector)
            if (price := _price_of(node)) is not None and price > 0
        ]
        if values:
            return values

    return [
        price
        for node in free_price_nodes(soup)
        if (price := _price_of(node)) is not None and price > 0
    ]


def _price_of(node: Tag) -> Decimal | None:
    raw = node.get("content") or node.get("data-price") or node.get_text(strip=True)
    return parse_price(str(raw)) if raw else None
