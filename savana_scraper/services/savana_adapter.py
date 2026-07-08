"""Savana-specific adapter — the V1 concrete :class:`EcommerceAdapter`.

Discovery walks a category/listing page, triggering lazy-load (infinite scroll
and any "load more" button) until no new product links appear, then yields
de-duplicated :class:`ProductRef`s. Extraction renders a product page and hands
its HTML to the layered :class:`Extractor`.

Designed so a future ``EcommerceAdapter`` for another store only needs to change
selectors/discovery details, not the pipeline (V2 roadmap).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from urllib.parse import urljoin

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from savana_scraper.core.browser import BrowserManager, scroll_to_bottom
from savana_scraper.core.config import Settings
from savana_scraper.core.exceptions import ExtractionError
from savana_scraper.core.logging import get_logger
from savana_scraper.models import Product, ProductRef
from savana_scraper.services.adapter import EcommerceAdapter
from savana_scraper.services.extractor import Extractor
from savana_scraper.services.savana_ssr import SavanaSsrStrategy

log = get_logger(__name__)


class SavanaAdapter(EcommerceAdapter):
    """Discovers and extracts products from savana.com."""

    name = "savana"

    def __init__(self, settings: Settings, browser: BrowserManager) -> None:
        self._settings = settings
        self._browser = browser
        # Savana's SSR JSON is the highest-fidelity structured source; it runs
        # ahead of the generic JSON-LD/DOM/OG strategies.
        self._extractor = Extractor(settings, extra_strategies=[SavanaSsrStrategy()])

    # --------------------------------------------------------------------- #
    # Discovery
    # --------------------------------------------------------------------- #
    async def discover(self, page: Page, category_url: str) -> AsyncIterator[ProductRef]:
        """Yield unique product references found on ``category_url``."""
        await self._browser.goto(page, category_url)
        seen: set[str] = set()
        selector = self._settings.selectors.product_link
        # Product links render client-side; wait for the first one to appear.
        try:
            await page.wait_for_selector(selector, timeout=self._settings.nav_timeout_ms)
        except PlaywrightError:
            log.warning("No product links matched %r on %s", selector, category_url)

        stable_rounds = 0
        rounds = 0
        max_scrolls = self._settings.max_scrolls  # 0 = unlimited
        while max_scrolls == 0 or rounds <= max_scrolls:
            rounds += 1
            new_this_round = 0
            for href in await self._collect_hrefs(page, selector):
                absolute = urljoin(category_url, href)
                if not absolute.startswith(("http://", "https://")):
                    continue
                try:
                    ref = ProductRef(product_url=absolute)
                except ValueError:
                    continue
                key = ref.key()
                if key in seen:
                    continue
                seen.add(key)
                new_this_round += 1
                yield ref

            # Stop when the page stops producing new links across two rounds.
            stable_rounds = 0 if new_this_round else stable_rounds + 1
            if stable_rounds >= 2:
                break
            await self._load_more(page)

        log.info("[cyan]Discovered[/] %d unique products on %s", len(seen), category_url)

    async def _collect_hrefs(self, page: Page, selector: str) -> list[str]:
        try:
            hrefs: list[str] = await page.eval_on_selector_all(
                selector,
                "els => els.map(e => e.getAttribute('href')).filter(Boolean)",
            )
        except PlaywrightError:
            return []
        return hrefs

    async def _load_more(self, page: Page) -> None:
        """Advance lazy-loaded content: click a load-more button or scroll."""
        load_more = self._settings.selectors.load_more
        try:
            button = await page.query_selector(load_more)
            if button and await button.is_visible():
                await button.click()
            elif not await scroll_to_bottom(page):
                log.debug("No scrollable container found; cannot lazy-load further")
            await page.wait_for_timeout(int(self._settings.scroll_pause_s * 1000))
        except PlaywrightError:
            pass

    # --------------------------------------------------------------------- #
    # Extraction
    # --------------------------------------------------------------------- #
    async def extract(self, page: Page, ref: ProductRef) -> Product:
        """Render the product page and build a :class:`Product`."""
        url = str(ref.product_url)
        await self._browser.goto(page, url)
        try:
            html = await page.content()
        except PlaywrightError as e:
            raise ExtractionError(f"Could not read page content for {url}: {e}") from e

        fields = self._extractor.extract_fields(html, url)
        if not fields.is_complete():
            raise ExtractionError(
                f"Missing required fields for {url} "
                f"(name={fields.name!r}, image={fields.image_url!r})"
            )
        # is_complete() guarantees these are present; assert narrows for mypy.
        assert fields.name is not None and fields.image_url is not None
        try:
            return Product(
                name=fields.name,
                image_url=fields.image_url,
                product_url=url,
                mrp=fields.mrp,
                asp=fields.asp,
            )
        except ValueError as e:
            raise ExtractionError(f"Invalid product data for {url}: {e}") from e
