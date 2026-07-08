"""Savana JSON-API source — the fast path for large runs.

The storefront is a thin client over a public, unauthenticated endpoint:

    POST /n/api/buyer/guide/user/goods-flow/pageList

Its response carries every field the CSV needs, so a run costs one request per
*40 products* instead of one browser navigation per product. Field mapping
mirrors the SSR/detail mapping in :mod:`savana_scraper.services.savana_ssr`:

    * goodsName             → name
    * imageList[0].goodsThumb → image URL
    * salePrice             → MRP  (the site's list price)
    * promotePrice          → ASP  (discounted price; falls back to salePrice)

.. important::
   **Pagination is stateful.** The server decides "next page" from the
   ``visitedGoodsIdList`` you send, *not* from ``pageIndex`` alone. Omit it and
   every page returns the same first 20 goods, forever, with
   ``hasNextPage: true`` — a silent, infinite, duplicate-only loop.
   :meth:`SavanaApiClient.iter_goods` is the only place that contract lives.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Iterable
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from savana_scraper.core.config import Settings
from savana_scraper.core.exceptions import ApiError
from savana_scraper.core.logging import get_logger
from savana_scraper.models import Product
from savana_scraper.services.pricing import parse_price

log = get_logger(__name__)

# Listing pages are /activity/<id>; the API calls that a flow of type ACTIVITY.
# Used both to read the seed URL's own id and to harvest ids out of page HTML.
_ACTIVITY_RE = re.compile(r"/activity/(\d+)")
# Product URL slugs: lowercase words joined by hyphens, then the goodsId.
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")

_OK_RET = 200


def flow_id_from_url(listing_url: str) -> str | None:
    """Extract the activity/flow id from a listing URL, or ``None``."""
    match = _ACTIVITY_RE.search(urlparse(listing_url).path)
    return match.group(1) if match else None


def slugify(name: str) -> str:
    """Render a product name as the site's URL slug ('Charm Totes Bag' → 'charm-totes-bag')."""
    return _SLUG_STRIP_RE.sub("-", name.strip().lower()).strip("-")


def product_url_for(goods: dict[str, Any], base_url: str) -> str:
    """Build the canonical /details/<slug>-<goodsId> URL for a goods record.

    Only the trailing id is load-bearing (the site resolves any slug), but the
    real slug keeps the exported links identical to what a user would share.
    """
    goods_id = goods["goodsId"]
    slug = slugify(str(goods.get("goodsName") or ""))
    path = f"/details/{slug}-{goods_id}" if slug else f"/details/{goods_id}"
    return urljoin(base_url, path)


def _image_url(goods: dict[str, Any]) -> str | None:
    """Pick the main image, preferring the selected colourway."""
    images = goods.get("imageList")
    if not isinstance(images, list):
        return None
    candidates = [img for img in images if isinstance(img, dict)]
    # `select: true` marks the colourway the listing card renders.
    selected = next((img for img in candidates if img.get("select")), None)
    for img in ([selected] if selected else []) + candidates:
        src = img.get("goodsThumb") or img.get("skcMainImg") or img.get("colorPic")
        if src:
            return str(src)
    return None


def goods_to_product(goods: dict[str, Any], base_url: str) -> Product | None:
    """Map one API goods record to a :class:`Product`, or ``None`` if unusable."""
    name = str(goods.get("goodsName") or "").strip()
    image = _image_url(goods)
    if not name or not image or not goods.get("goodsId"):
        return None

    sale = parse_price(goods.get("salePrice"))
    promote = parse_price(goods.get("promotePrice"))
    # salePrice is the list price ("MRP"); promotePrice is what you actually pay.
    asp = promote if promote is not None and (sale is None or promote < sale) else sale

    try:
        return Product(
            name=name,
            image_url=image,
            product_url=product_url_for(goods, base_url),
            mrp=sale,
            asp=asp,
        )
    except ValueError as e:
        log.debug("Skipping goods %s: %s", goods.get("goodsId"), e)
        return None


class SavanaApiClient:
    """Async client for the goods-flow listing API."""

    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._api = settings.api
        # Injectable so tests can drive the pagination contract without a network.
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> SavanaApiClient:
        self._client = httpx.AsyncClient(
            timeout=self._api.timeout_s,
            headers=self._headers(),
            follow_redirects=True,
            transport=self._transport,
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        site = self._settings.base_url.rstrip("/")
        return {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json;charset=UTF-8",
            "origin": site,
            "referer": f"{site}/",
            "user-agent": self._settings.user_agent,
            "country-language": self._api.country_language,
            "h5-version": self._api.h5_version,
            "app_version": self._api.h5_version,
            "client_type": "h5",
            "x-platform": "web",
            "x-source": "h5",
        }

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise ApiError("Client not started; use 'async with SavanaApiClient(...)'")
        return self._client

    # ------------------------------------------------------------------ #
    # Listing discovery
    # ------------------------------------------------------------------ #
    async def harvest_listings(self, seed_url: str) -> list[str]:
        """Return every ``/activity/<id>`` listing URL linked from ``seed_url``.

        The seed's own listing (if it is one) is placed first so a run that is
        capped by ``--max-products`` still drains the URL the user asked for.
        """
        try:
            response = await self._http.get(seed_url)
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise ApiError(f"Could not fetch seed page {seed_url}: {e}") from e

        ordered: list[str] = []
        seen: set[str] = set()
        for flow_id in _seed_first(seed_url, _ACTIVITY_RE.findall(response.text)):
            if flow_id in seen:
                continue
            seen.add(flow_id)
            ordered.append(urljoin(self._settings.base_url, f"/activity/{flow_id}"))
        log.info("[cyan]Harvested[/] %d listing pages from %s", len(ordered), seed_url)
        return ordered

    # ------------------------------------------------------------------ #
    # Product streaming
    # ------------------------------------------------------------------ #
    async def iter_goods(self, flow_id: str) -> AsyncIterator[dict[str, Any]]:
        """Yield every goods record in a listing, draining its pagination.

        Maintains the ``visitedGoodsIdList`` the server needs to advance (see the
        module docstring). Stops on ``hasNextPage: false`` — and also whenever a
        page returns no *new* ids, which is what a broken/ignored visited-list
        looks like from the client side.
        """
        visited: list[int] = []
        seen: set[int] = set()
        page_index = 1
        max_pages = self._api.max_pages_per_listing

        while max_pages == 0 or page_index <= max_pages:
            payload = await self._page_list(flow_id, page_index, visited)
            goods_list = payload.get("goodsList") or []

            new_count = 0
            for goods in goods_list:
                goods_id = goods.get("goodsId")
                if not isinstance(goods_id, int) or goods_id in seen:
                    continue
                seen.add(goods_id)
                visited.append(goods_id)
                new_count += 1
                yield goods

            if not payload.get("hasNextPage"):
                break
            if new_count == 0:
                # Server kept saying "more" while repeating itself: stop rather
                # than loop forever on duplicates.
                log.warning(
                    "Listing %s returned no new goods on page %d; stopping early (total=%d)",
                    flow_id,
                    page_index,
                    len(seen),
                )
                break

            page_index += 1
            if self._api.delay_s > 0:
                await asyncio.sleep(self._api.delay_s)

        log.info(
            "[cyan]Listing %s[/] drained: %d goods over %d pages", flow_id, len(seen), page_index
        )

    async def _page_list(
        self, flow_id: str, page_index: int, visited: Iterable[int]
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "flowId": flow_id,
            "flowType": "ACTIVITY",
            "quickOptionId": None,
            "screenParam": {},
            "pgType": None,
            "multiTopGoodsIdList": [],
            "supportedFeatures": {},
            "pageIndex": page_index,
            # Load-bearing: the server pages off this list, not off pageIndex.
            "visitedGoodsIdList": list(visited),
        }
        url = urljoin(self._api.base_url, self._api.goods_flow_path)
        try:
            response = await self._http.post(url, json=body)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as e:
            raise ApiError(f"goods-flow request failed for flow {flow_id}: {e}") from e
        except ValueError as e:
            raise ApiError(f"goods-flow returned non-JSON for flow {flow_id}: {e}") from e

        if data.get("ret") != _OK_RET:
            raise ApiError(
                f"goods-flow error for flow {flow_id}: ret={data.get('ret')} msg={data.get('msg')!r}"
            )
        payload = data.get("data")
        if not isinstance(payload, dict):
            raise ApiError(f"goods-flow payload missing 'data' for flow {flow_id}")
        return payload


def _seed_first(seed_url: str, flow_ids: list[str]) -> list[str]:
    """Order flow ids so the seed URL's own listing (if any) comes first."""
    seed_flow = flow_id_from_url(seed_url)
    if seed_flow is None:
        return flow_ids
    return [seed_flow] + [f for f in flow_ids if f != seed_flow]
