"""Savana SSR-JSON extraction strategy — the site's real structured source.

savana.com renders no JSON-LD, but it embeds its backend API responses as SSR
state in the initial HTML. The product-detail node lives under the API key
``/n/api/trade/intention/item/detail`` and exposes *stable* field names
(``goodsName``, ``salesPrice``, ``promotePrice``, ``images[].picThumb`` …),
unlike the page's hashed, per-build CSS class names.

Field mapping (per the PRD):
    * goodsName    → name
    * images[0]    → image URL   (picThumb, highest-res thumbnail)
    * salesPrice   → MRP         (the value the site labels "MRP")
    * promotePrice → ASP         (the discounted selling price; falls back to
                                  salesPrice when there is no active promotion)
    * level2CatId  → category    (via :mod:`savana_scraper.services.taxonomy`)
    * level3CatId  → subcategory

The category ids appear *only here*, never in the listing API — which is why the
fast path has to visit product pages to fill those two columns.

This is the highest-priority strategy for :class:`SavanaAdapter`.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from savana_scraper.core.logging import get_logger
from savana_scraper.services.extractor import FieldSet
from savana_scraper.services.pricing import parse_price
from savana_scraper.services.taxonomy import SavanaTaxonomy

log = get_logger(__name__)

# The SSR cache key that holds the main product detail object.
DETAIL_KEY = '"/n/api/trade/intention/item/detail"'


def locate_detail(html: str) -> dict[str, Any] | None:
    """The product-detail object embedded in ``html``, or ``None``.

    Module-level because the API source needs it too: the category ids live in
    this blob and nowhere else, so the fast path must parse product pages to
    reach them.
    """
    idx = html.find(DETAIL_KEY)
    if idx == -1:
        return None
    brace = html.find("{", idx + len(DETAIL_KEY))
    if brace == -1:
        return None
    blob = _balanced_object(html, brace)
    if blob is None:
        return None
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, ValueError) as e:
        log.debug("Savana SSR detail JSON parse failed: %s", e)
        return None
    return data if isinstance(data, dict) and "goodsId" in data else None


class SavanaSsrStrategy:
    """Parse the embedded product-detail JSON from the rendered HTML."""

    def __init__(self, taxonomy: SavanaTaxonomy | None = None) -> None:
        self._taxonomy = taxonomy or SavanaTaxonomy()

    def parse(self, soup: BeautifulSoup, html: str, page_url: str) -> FieldSet:
        result = FieldSet()
        detail = self._locate_detail(html)
        if detail is None:
            return result

        result.name = _clean(detail.get("goodsName") or detail.get("shortGoodsName"))
        result.image_url = self._image(detail, page_url)
        # level1 is the storefront itself and level4 is finer than any shopper's
        # mental model; levels 2 and 3 are the useful pair.
        result.category = self._taxonomy.category(detail.get("level2CatId"))
        result.subcategory = self._taxonomy.subcategory(detail.get("level3CatId"))

        sales = parse_price(detail.get("salesPrice"))
        promote = parse_price(detail.get("promotePrice"))
        # salesPrice is the site's "MRP"; promotePrice is the sale price.
        result.mrp = sales
        if promote is not None and (sales is None or promote < sales):
            result.asp = promote
        else:
            result.asp = sales
        return result

    # ------------------------------------------------------------------ #
    def _locate_detail(self, html: str) -> dict[str, Any] | None:
        return locate_detail(html)

    @staticmethod
    def _image(detail: dict[str, Any], page_url: str) -> str | None:
        images = detail.get("images")
        if isinstance(images, list):
            for img in images:
                if isinstance(img, dict):
                    src = img.get("picThumb") or img.get("picUrl")
                    if src:
                        return urljoin(page_url, str(src))
        thumb = detail.get("goodsThumb")
        return urljoin(page_url, str(thumb)) if thumb else None


def _balanced_object(s: str, start: int) -> str | None:
    """Return the brace-balanced JSON object substring starting at ``start``.

    String-aware so braces inside quoted values do not unbalance the scan.
    """
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return s[start : i + 1]
    return None


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
