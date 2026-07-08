"""Layered field extraction.

Runtime priority (per 03_Runtime.md):
    1. Structured data   — JSON-LD (schema.org/Product) + embedded SPA JSON
    2. DOM extraction    — configured CSS selectors
    3. Fallback parser   — Open Graph meta + heuristics

Each strategy returns a partial :class:`FieldSet`; the :class:`Extractor`
merges them, letting higher-priority strategies win but lower ones fill gaps.
Everything operates on an HTML string, so extraction is fully unit-testable
without a live browser.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, fields
from decimal import Decimal
from typing import Any, Protocol
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from savana_scraper.core.config import Settings
from savana_scraper.core.logging import get_logger
from savana_scraper.services.breadcrumbs import (
    category_pair,
    crumbs_from_category_value,
    crumbs_from_dom,
    crumbs_from_json_ld,
    crumbs_from_schema_category,
)
from savana_scraper.services.page_signals import product_prices
from savana_scraper.services.pricing import parse_price

log = get_logger(__name__)


@dataclass
class FieldSet:
    """A partial set of extracted product fields; any may be ``None``."""

    name: str | None = None
    image_url: str | None = None
    mrp: Decimal | None = None
    asp: Decimal | None = None
    category: str | None = None
    subcategory: str | None = None

    def merge_gaps_from(self, other: FieldSet) -> None:
        """Fill only this set's empty fields from ``other`` (in place)."""
        for f in fields(self):
            if getattr(self, f.name) is None:
                setattr(self, f.name, getattr(other, f.name))

    def is_complete(self) -> bool:
        """True when the fields required to build a Product are present."""
        return bool(self.name and self.image_url)


class ExtractionStrategy(Protocol):
    """A single approach to pulling fields out of a page."""

    def parse(self, soup: BeautifulSoup, html: str, page_url: str) -> FieldSet: ...


# --------------------------------------------------------------------------- #
# Strategy 1: structured data
# --------------------------------------------------------------------------- #
class StructuredDataStrategy:
    """Extract from JSON-LD and embedded SPA JSON blobs."""

    def parse(self, soup: BeautifulSoup, html: str, page_url: str) -> FieldSet:
        result = FieldSet()
        for blob in self._json_ld_blocks(soup):
            product = self._find_product_node(blob)
            if product is not None:
                self._apply_schema_product(result, product, page_url)
                if result.is_complete():
                    return result
        return result

    @staticmethod
    def _json_ld_blocks(soup: BeautifulSoup) -> Iterable[Any]:
        for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
            text = tag.string or tag.get_text()
            if not text:
                continue
            try:
                yield json.loads(text)
            except (json.JSONDecodeError, ValueError):
                continue

    def _find_product_node(self, node: Any) -> dict[str, Any] | None:
        """Depth-first search for a schema.org Product node."""
        if isinstance(node, dict):
            type_ = node.get("@type")
            types = type_ if isinstance(type_, list) else [type_]
            if any(isinstance(t, str) and t.lower() == "product" for t in types):
                return node
            for value in node.values():
                found = self._find_product_node(value)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = self._find_product_node(item)
                if found is not None:
                    return found
        return None

    def _apply_schema_product(
        self, result: FieldSet, product: dict[str, Any], page_url: str
    ) -> None:
        result.name = result.name or _clean(product.get("name"))

        image = product.get("image")
        if isinstance(image, list):
            image = image[0] if image else None
        if isinstance(image, dict):
            image = image.get("url")
        if isinstance(image, str):
            result.image_url = result.image_url or urljoin(page_url, image)

        offers = product.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else None
        if isinstance(offers, dict):
            # Selling price.
            result.asp = result.asp or parse_price(offers.get("price"))
            spec = offers.get("priceSpecification")
            if isinstance(spec, list):
                spec = spec[0] if spec else None
            if isinstance(spec, dict):
                result.asp = result.asp or parse_price(spec.get("price"))
        # List / MRP-style price if the schema exposes one.
        result.mrp = result.mrp or parse_price(
            product.get("mrp") or product.get("listPrice") or product.get("highPrice")
        )

        # schema.org/Product.category is a path: "Women > Bags > Backpacks".
        if result.category is None:
            crumbs = crumbs_from_category_value(product.get("category"))
            result.category, result.subcategory = category_pair(crumbs, result.name)


# --------------------------------------------------------------------------- #
# Strategy 1b: breadcrumbs (category / subcategory)
# --------------------------------------------------------------------------- #
class BreadcrumbStrategy:
    """Derive category and subcategory from the page's breadcrumb trail.

    Fills nothing else. Ordered after :class:`StructuredDataStrategy` so an
    explicit ``Product.category`` wins over a navigational trail, which sometimes
    reflects the path the user browsed rather than where the product lives.
    """

    def parse(self, soup: BeautifulSoup, html: str, page_url: str) -> FieldSet:
        result = FieldSet()
        crumbs = (
            crumbs_from_json_ld(soup) or crumbs_from_schema_category(soup) or crumbs_from_dom(soup)
        )
        if crumbs:
            # The trailing crumb is usually the product itself; the page's own
            # title is how we recognise it without seeing the merged FieldSet.
            result.category, result.subcategory = category_pair(crumbs, _page_title(soup))
        return result


def _page_title(soup: BeautifulSoup) -> str | None:
    """The product's display name, as this page states it."""
    heading = _clean(_select_text(soup, "h1"))
    return heading or _meta(soup, "og:title")


# --------------------------------------------------------------------------- #
# Strategy 2: DOM selectors
# --------------------------------------------------------------------------- #
class DomStrategy:
    """Extract using the configured CSS selectors."""

    def __init__(self, settings: Settings) -> None:
        self._sel = settings.selectors

    def parse(self, soup: BeautifulSoup, html: str, page_url: str) -> FieldSet:
        result = FieldSet()
        result.name = _clean(_select_text(soup, self._sel.name))
        result.image_url = self._select_image(soup, page_url)
        result.mrp = parse_price(_select_text(soup, self._sel.mrp))
        result.asp = parse_price(_select_text(soup, self._sel.asp))
        return result

    def _select_image(self, soup: BeautifulSoup, page_url: str) -> str | None:
        for node in _select_all(soup, self._sel.image):
            src = node.get("content") or node.get("src") or node.get("data-src")
            if src:
                return urljoin(page_url, str(src))
        return None


# --------------------------------------------------------------------------- #
# Strategy 3: fallback (Open Graph + heuristics)
# --------------------------------------------------------------------------- #
class FallbackStrategy:
    """Last resort: Open Graph meta tags and the document title."""

    def parse(self, soup: BeautifulSoup, html: str, page_url: str) -> FieldSet:
        result = FieldSet()
        result.name = _meta(soup, "og:title") or _clean(soup.title.string if soup.title else None)
        og_image = _meta(soup, "og:image")
        if og_image:
            result.image_url = urljoin(page_url, og_image)
        return result


# --------------------------------------------------------------------------- #
# Strategy 2b: site-agnostic heuristics
# --------------------------------------------------------------------------- #
# Images that are furniture, not product photography.
_IMAGE_NOISE = ("logo", "icon", "sprite", "placeholder", "avatar", "banner", "pixel")


class HeuristicStrategy:
    """Site-agnostic extraction for storefronts with no JSON-LD.

    Used by the generic crawler, where we know nothing about the markup. Every
    rule here is a guess with a decent prior — microdata ``itemprop`` first,
    then class-name conventions, then structural position.
    """

    def parse(self, soup: BeautifulSoup, html: str, page_url: str) -> FieldSet:
        result = FieldSet()
        result.name = self._name(soup)
        result.image_url = self._image(soup, page_url)
        mrp, asp = self._prices(soup)
        result.mrp, result.asp = mrp, asp
        return result

    @staticmethod
    def _name(soup: BeautifulSoup) -> str | None:
        itemprop = soup.select_one("[itemprop='name']")
        if itemprop:
            text = _clean(itemprop.get("content") or itemprop.get_text(strip=True))
            if text:
                return text
        return _clean(_select_text(soup, "h1"))

    @staticmethod
    def _image(soup: BeautifulSoup, page_url: str) -> str | None:
        # Structured hints first.
        for selector, attr in (
            ("meta[property='og:image']", "content"),
            ("meta[name='twitter:image']", "content"),
            ("link[rel='image_src']", "href"),
            ("[itemprop='image']", "content"),
        ):
            node = soup.select_one(selector)
            if node:
                src = node.get(attr) or node.get("src")
                if src:
                    return urljoin(page_url, str(src))

        # Otherwise the biggest non-furniture <img> we can find.
        best: tuple[int, str] | None = None
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src")
            if not src or any(n in str(src).lower() for n in _IMAGE_NOISE):
                continue
            area = _int(img.get("width")) * _int(img.get("height"))
            if best is None or area > best[0]:
                best = (area, str(src))
        return urljoin(page_url, best[1]) if best else None

    @staticmethod
    def _prices(soup: BeautifulSoup) -> tuple[Decimal | None, Decimal | None]:
        """Highest price is the list price, lowest is what you pay.

        A single price means no visible discount, so MRP and ASP coincide — which
        is exactly how the Savana mapping treats an absent ``promotePrice``.
        :func:`product_prices` excludes prices belonging to related-item cards, so
        a neighbour's cheaper price can never become this product's ASP.
        """
        found = product_prices(soup)
        if not found:
            return None, None
        return max(found), min(found)


class Extractor:
    """Runs strategies in priority order and merges their output.

    ``extra_strategies`` are site-specific strategies (e.g. a Savana SSR-JSON
    parser) that run *before* the generic ones, so a site adapter can plug in
    its own high-fidelity structured source without changing this class.

    ``strategies`` replaces the chain wholesale — used by the generic crawler,
    which must not inherit another site's tuned CSS selectors.
    """

    def __init__(
        self,
        settings: Settings,
        extra_strategies: list[ExtractionStrategy] | None = None,
        strategies: list[ExtractionStrategy] | None = None,
    ) -> None:
        self._strategies: list[ExtractionStrategy] = strategies or [
            *(extra_strategies or []),
            StructuredDataStrategy(),
            BreadcrumbStrategy(),
            DomStrategy(settings),
            FallbackStrategy(),
        ]

    def extract_fields(self, html: str, page_url: str) -> FieldSet:
        soup = BeautifulSoup(html, "lxml")
        combined = FieldSet()
        for strategy in self._strategies:
            partial = strategy.parse(soup, html, page_url)
            combined.merge_gaps_from(partial)
            if _all_present(combined):
                break
        return combined


# --------------------------------------------------------------------------- #
# Small shared helpers
# --------------------------------------------------------------------------- #
def _all_present(fs: FieldSet) -> bool:
    return all(getattr(fs, f.name) is not None for f in fields(fs))


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(value: Any) -> int:
    """Parse an HTML width/height attribute; unusable values sort last."""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _select_all(soup: BeautifulSoup, selector: str) -> list[Any]:
    try:
        return soup.select(selector)
    except Exception:  # noqa: BLE001 - malformed selector should never crash a run
        return []


def _select_text(soup: BeautifulSoup, selector: str) -> str | None:
    for node in _select_all(soup, selector):
        text = str(node.get_text(strip=True))
        if text:
            return text
    return None


def _meta(soup: BeautifulSoup, prop: str) -> str | None:
    tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
    if tag:
        content = tag.get("content")
        if content:
            return str(content).strip()
    return None
