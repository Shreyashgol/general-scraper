"""Concrete :class:`ProductSource` implementations.

``ApiProductSource``     — streams products from the goods-flow JSON API.
``BrowserProductSource`` — renders the listing, then every product page.

Both present the same interface, so :class:`~savana_scraper.services.pipeline.ScrapePipeline`
never branches on which one it was handed. Each consults the resume predicate
*before* the expensive step (a page navigation, or building a Product) so a
resumed run does not redo work it already paid for.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from savana_scraper.core.browser import BrowserManager
from savana_scraper.core.config import Settings
from savana_scraper.core.exceptions import ApiError, ScraperError
from savana_scraper.core.logging import get_logger
from savana_scraper.core.retry import with_retry
from savana_scraper.models import Product, product_key
from savana_scraper.services.adapter import ProductSource, SkipPredicate
from savana_scraper.services.savana_adapter import SavanaAdapter
from savana_scraper.services.savana_api import (
    SavanaApiClient,
    flow_id_from_url,
    goods_to_product,
    product_url_for,
)

log = get_logger(__name__)


class ApiProductSource(ProductSource):
    """Streams products from savana.com's goods-flow API across many listings."""

    name = "savana-api"

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings

    async def stream(
        self, seed_url: str, skip: SkipPredicate | None = None
    ) -> AsyncIterator[Product]:
        settings = self._settings
        seen_goods: set[int] = set()

        async with SavanaApiClient(settings) as client:
            listings = await self._listings_for(client, seed_url)
            for index, listing_url in enumerate(listings, start=1):
                flow_id = flow_id_from_url(listing_url)
                if flow_id is None:
                    log.warning("Skipping non-listing URL %s", listing_url)
                    continue

                log.info("[bold]Listing %d/%d[/] %s", index, len(listings), listing_url)
                try:
                    async for goods in client.iter_goods(flow_id):
                        product = self._to_product(goods, seen_goods, skip)
                        if product is not None:
                            yield product
                except ApiError as e:
                    # One bad listing must not abort a multi-listing crawl.
                    self.stats.failed += 1
                    log.error("Listing %s failed: %s", listing_url, e)

    def _to_product(
        self, goods: dict[str, Any], seen_goods: set[int], skip: SkipPredicate | None
    ) -> Product | None:
        goods_id = goods["goodsId"]
        # Listings overlap heavily; dedupe across the whole run.
        if goods_id in seen_goods:
            return None
        seen_goods.add(goods_id)

        if skip is not None and skip(product_key(product_url_for(goods, self._settings.base_url))):
            self.stats.skipped_resume += 1
            return None
        return goods_to_product(goods, self._settings.base_url)

    async def _listings_for(self, client: SavanaApiClient, seed_url: str) -> list[str]:
        settings = self._settings
        if not settings.crawl_site:
            return [seed_url]

        listings = await client.harvest_listings(seed_url)
        if not listings:
            log.warning("No listings harvested from %s; falling back to the seed URL", seed_url)
            return [seed_url] if flow_id_from_url(seed_url) else []

        cap = settings.max_listings
        if cap and len(listings) > cap:
            log.info("Capping crawl at %d of %d listings", cap, len(listings))
            return listings[:cap]
        return listings


class BrowserProductSource(ProductSource):
    """Renders the listing page and every product page with Playwright."""

    name = "savana-browser"

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings

    async def stream(
        self, seed_url: str, skip: SkipPredicate | None = None
    ) -> AsyncIterator[Product]:
        settings = self._settings
        async with BrowserManager(settings) as browser:
            adapter = SavanaAdapter(settings, browser)

            async with browser.page() as listing_page:
                refs = [ref async for ref in adapter.discover(listing_page, seed_url)]

            async with browser.page() as page:
                for ref in refs:
                    if skip is not None and skip(ref.key()):
                        self.stats.skipped_resume += 1
                        continue
                    try:
                        yield await with_retry(
                            lambda r=ref: adapter.extract(page, r),  # type: ignore[misc]
                            max_attempts=settings.max_retries,
                            backoff_s=settings.retry_backoff_s,
                            description=f"extract {ref.product_url}",
                        )
                    except ScraperError as e:
                        self.stats.failed += 1
                        log.error("Failed to extract %s: %s", ref.product_url, e)
                    if settings.request_delay_s > 0:
                        await asyncio.sleep(settings.request_delay_s)
