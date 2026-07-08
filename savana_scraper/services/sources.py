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
from contextlib import AsyncExitStack
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
    SavanaCategoryEnricher,
    flow_id_from_url,
    goods_to_product,
    product_url_for,
)
from savana_scraper.services.taxonomy import SavanaTaxonomy

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
        taxonomy = SavanaTaxonomy.load(settings.taxonomy_path)

        async with AsyncExitStack() as stack:
            client = await stack.enter_async_context(SavanaApiClient(settings))
            # Products stream out one at a time, but their category pages are
            # fetched a batch at a time — so the enrichment's latency overlaps
            # instead of stacking up one product-page round-trip per product.
            enricher: SavanaCategoryEnricher | None = None
            if settings.api.fetch_categories:
                enricher = await stack.enter_async_context(
                    SavanaCategoryEnricher(settings, taxonomy)
                )
                log.info(
                    "Filling category/subcategory from product pages "
                    "(SAVANA_API_FETCH_CATEGORIES=false to skip and stay fast)"
                )
            batch_size = max(1, settings.api.detail_concurrency)

            listings = await self._listings_for(client, seed_url)
            for index, listing_url in enumerate(listings, start=1):
                flow_id = flow_id_from_url(listing_url)
                if flow_id is None:
                    log.warning("Skipping non-listing URL %s", listing_url)
                    continue

                log.info("[bold]Listing %d/%d[/] %s", index, len(listings), listing_url)
                batch: list[Product] = []
                try:
                    async for goods in client.iter_goods(flow_id):
                        product = self._to_product(goods, seen_goods, skip)
                        if product is None:
                            continue
                        if enricher is None:
                            yield product
                            continue

                        batch.append(product)
                        if len(batch) >= batch_size:
                            for enriched in await enricher.enrich(batch):
                                yield enriched
                            batch.clear()
                            self._refresh_warnings(taxonomy, enricher)
                except ApiError as e:
                    # One bad listing must not abort a multi-listing crawl.
                    self.stats.failed += 1
                    log.error("Listing %s failed: %s", listing_url, e)

                if enricher is not None and batch:
                    for enriched in await enricher.enrich(batch):
                        yield enriched
                    self._refresh_warnings(taxonomy, enricher)

    def _refresh_warnings(self, taxonomy: SavanaTaxonomy, enricher: SavanaCategoryEnricher) -> None:
        """Recompute run warnings after every batch.

        Not deferred to the end of ``stream``: the pipeline stops iterating the
        moment ``--max-products`` is hit, which suspends this generator forever,
        so anything appended after the loop would never reach the report.
        """
        self.warnings = list(taxonomy.warnings)
        if enricher.misses:
            self.warnings.append(
                f"{enricher.misses} product page(s) could not be read; "
                "those rows have no category/subcategory."
            )

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
                    # Refreshed per product for the same reason as the API source:
                    # a capped run abandons this generator without resuming it.
                    self.warnings = list(adapter.taxonomy.warnings)
                    if settings.request_delay_s > 0:
                        await asyncio.sleep(settings.request_delay_s)
