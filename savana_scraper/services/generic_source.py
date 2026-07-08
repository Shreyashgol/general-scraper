"""Generic storefront source — the fallback for sites we have no adapter for.

Field *extraction* generalises well: enterprise storefronts almost always ship
``schema.org/Product`` JSON-LD, and :class:`Extractor` already reads it.

Link *discovery* does not. Nothing tells us which anchors on a listing page are
products. Picking the largest group of same-shaped URLs is not enough — on
savana.com's homepage the biggest group is 70 ``/activity/<id>`` category links,
which beats the 40 ``/details/<slug>-<id>`` product links.

So we cluster candidate URLs by shape, then *verify* the top clusters by fetching
a couple of pages from each and asking the extractor whether they actually look
like products. The cluster whose sample yields real product fields wins. Guessing
becomes measuring, at a cost of a few page loads.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from savana_scraper.core.browser import BrowserManager, scroll_to_bottom
from savana_scraper.core.config import Settings
from savana_scraper.core.exceptions import NavigationError, ScraperError
from savana_scraper.core.logging import get_logger
from savana_scraper.models import Product, product_key
from savana_scraper.services.adapter import ProductSource, SkipPredicate
from savana_scraper.services.extractor import (
    BreadcrumbStrategy,
    Extractor,
    FallbackStrategy,
    FieldSet,
    HeuristicStrategy,
    StructuredDataStrategy,
)
from savana_scraper.services.page_signals import is_product_page

log = get_logger(__name__)

# Path segments that are never product listings — cheap, high-precision filter.
_NON_PRODUCT_SEGMENTS = frozenset(
    {
        "cart",
        "checkout",
        "login",
        "signin",
        "signup",
        "register",
        "account",
        "me",
        "help",
        "help-center",
        "support",
        "faq",
        "contact",
        "about",
        "privacy",
        "terms",
        "policy",
        "blog",
        "news",
        "press",
        "careers",
        "search",
        "wishlist",
        "orders",
        "track",
        "returns",
        "sitemap",
    }
)
# How many shape-clusters to sample before giving up.
_MAX_CANDIDATE_CLUSTERS = 4
# Pages fetched per cluster when verifying it holds products. A strict majority
# must pass, so one ambiguous page cannot validate a whole cluster.
_SAMPLE_SIZE = 3
# A cluster needs at least this many distinct URLs to be worth sampling.
_MIN_CLUSTER_SIZE = 3


def _segments_of(url: str) -> list[str] | None:
    """Path segments of ``url``, or ``None`` if it is obviously not a product."""
    segments = [s for s in urlparse(url).path.split("/") if s]
    if not segments:
        return None
    if any(s.lower() in _NON_PRODUCT_SEGMENTS for s in segments):
        return None
    return segments


def _bucket_of(segments: list[str]) -> tuple[int, str]:
    """Coarse bucket: URLs only get compared if they share depth and first segment.

    Without the first-segment split, savana's ``/details/<slug>`` and
    ``/activity/<id>`` — both depth 2 — would merge into one cluster and the
    category links would drown out the products.
    """
    return (len(segments), segments[0] if len(segments) > 1 else "")


def _template_of(bucket: list[list[str]]) -> str:
    """Derive ``/catalogue/*/index.html`` from the URLs in a bucket.

    A position is a wildcard when its value varies across the bucket, literal
    when every URL agrees. This is what lets us cluster sites whose id/slug sits
    anywhere in the path, not just at the end.
    """
    depth = len(bucket[0])
    parts = []
    for i in range(depth):
        distinct = {segments[i] for segments in bucket}
        parts.append(next(iter(distinct)) if len(distinct) == 1 else "*")
    return "/" + "/".join(parts)


def _matches_template(url: str, template: str) -> bool:
    """Does ``url``'s path fit the wildcard template we detected? (``/catalogue/*/index.html``)"""
    segments = _segments_of(url)
    if segments is None:
        return False
    parts = template.strip("/").split("/")
    if len(parts) != len(segments):
        return False
    return all(p == "*" or p == s for p, s in zip(parts, segments, strict=True))


def _looks_like_product(fields: FieldSet, soup: BeautifulSoup) -> bool:
    """Complete fields *and* a page that is about a single product."""
    has_fields = bool(fields.name and fields.image_url and (fields.asp or fields.mrp))
    return has_fields and is_product_page(soup)


# Finds the "next page" link, in descending order of confidence:
#   1. rel=next / aria hooks  — semantic, unambiguous
#   2. exact link text        — "next", "›", "»"
#   3. numbered pagination    — the anchor labelled current+1
#
# Tier 3 exists because plenty of storefronts render only "1 2 3 …" with no next
# link at all. It is deliberately the last resort: a bare "2" anywhere on the page
# could be anything, so the anchor must sit inside a pagination container.
#
# Returns an absolute URL because `.href` resolves against the document.
_NEXT_PAGE_JS = """() => {
  const semantic = [
    "link[rel='next']",
    "a[rel='next']",
    "a[aria-label*='next' i]",
    "li.next > a",
    ".next > a",
    ".pagination a.next",
    "a.pagination__next",
  ];
  for (const selector of semantic) {
    const el = document.querySelector(selector);
    if (el && el.href) return el.href;
  }

  // Exact matches only: a link reading "next day delivery" is not pagination.
  const labels = new Set(['next', 'next page', 'next →', '›', '»', '>']);
  for (const a of document.querySelectorAll('a[href]')) {
    const text = (a.textContent || '').trim().toLowerCase();
    if (labels.has(text)) return a.href;
  }

  // --- Numbered pagination ------------------------------------------------
  const inPagination = (el) => {
    for (let node = el, i = 0; node && i < 4; node = node.parentElement, i++) {
      if (node.tagName === 'NAV') return true;
      const id = (node.getAttribute && node.getAttribute('id')) || '';
      const cls = (node.className && node.className.baseVal !== undefined)
        ? node.className.baseVal            // SVG elements have an SVGAnimatedString
        : (node.className || '');
      if (/pag(er|ination)?\\b|\\bpages?\\b/i.test(String(cls) + ' ' + id)) return true;
    }
    return false;
  };

  const currentPage = () => {
    const marked = document.querySelector(
      '[aria-current="page"], .pagination .current, .pagination .active, .pager .current'
    );
    if (marked) {
      const n = parseInt((marked.textContent || '').trim(), 10);
      if (Number.isInteger(n)) return n;
    }
    const url = location.href;
    const m = url.match(/[?&]page=(\\d+)/) || url.match(/\\/page[-\\/](\\d+)/);
    return m ? parseInt(m[1], 10) : 1;
  };

  const wanted = String(currentPage() + 1);
  for (const a of document.querySelectorAll('a[href]')) {
    if ((a.textContent || '').trim() !== wanted) continue;
    if (!inPagination(a)) continue;
    return a.href;
  }
  return null;
}"""

# Clicks a "load more" affordance. Returns true when something was actually
# clicked, so the caller knows whether to expect new content.
#
# Explicit data attributes and class hooks first, then a text match anchored to
# the *start* of the label — "Load more" yes, "Download more brochures" no. The
# element must be visible and enabled, otherwise the exhausted button at the
# bottom of the last page would be clicked forever.
_LOAD_MORE_JS = """() => {
  const explicit = document.querySelector(
    '[data-load-more], .load-more, .js-load-more, button[class*="load-more" i]'
  );
  const labelled = () => {
    const label = /^(load|show|view|see)\\s+more\\b/i;
    const clickable = document.querySelectorAll('button, a, [role="button"]');
    return [...clickable].find((el) => label.test((el.textContent || '').trim()));
  };

  const target = explicit || labelled();
  if (!target) return false;
  if (target.disabled || target.getAttribute('aria-disabled') === 'true') return false;

  // offsetParent is null for display:none (and for position:fixed, hence the rect).
  const rect = target.getBoundingClientRect();
  const hidden = target.offsetParent === null && rect.width === 0 && rect.height === 0;
  if (hidden || (rect.width === 0 && rect.height === 0)) return false;

  target.scrollIntoView({ block: 'center' });
  target.click();
  return true;
}"""


class GenericProductSource(ProductSource):
    """Renders any storefront, infers which links are products, extracts each one."""

    name = "generic"

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings
        # Deliberately excludes DomStrategy: its CSS selectors are tuned for
        # savana.com and would be noise (or worse, wrong) on an unknown site.
        self._extractor = Extractor(
            settings,
            strategies=[
                StructuredDataStrategy(),
                BreadcrumbStrategy(),
                HeuristicStrategy(),
                FallbackStrategy(),
            ],
        )
        #: Set during a run; surfaced to the API so the UI can show what happened.
        self.warnings: list[str] = []
        self.detected_pattern: str | None = None

    async def stream(
        self, seed_url: str, skip: SkipPredicate | None = None
    ) -> AsyncIterator[Product]:
        # Two pages, deliberately. Sampling candidate clusters navigates the
        # product page away; the listing page has to stay put so we can read its
        # "next page" link afterwards.
        async with (
            BrowserManager(self._settings) as browser,
            browser.page() as listing,
            browser.page() as product_page,
        ):
            # The seed is different from every later page: if we cannot even load
            # it, that is a failed run, not an empty one. Letting NavigationError
            # escape means the caller reports an error instead of quietly
            # exporting zero products.
            await browser.goto(listing, seed_url)
            candidates = await self._drain_and_collect(listing, seed_url)

            # The seed may itself be a single product page.
            if not candidates:
                product = await self._extract(browser, product_page, seed_url)
                if product is not None:
                    self.detected_pattern = "single product page"
                    yield product
                else:
                    self.warnings.append(
                        "Found no product links, and the page itself did not parse as a product."
                    )
                return

            template = await self._detect_template(browser, product_page, seed_url, candidates)
            if template is None:
                self.warnings.append(
                    "Found links, but no group of them parsed as product pages. "
                    "This site likely needs a dedicated adapter."
                )
                return
            self.detected_pattern = template

            async for item in self._walk_pages(
                browser, listing, product_page, seed_url, candidates, template, skip
            ):
                yield item

    async def _walk_pages(
        self,
        browser: BrowserManager,
        listing: Page,
        product_page: Page,
        seed_url: str,
        candidates: list[str],
        template: str,
        skip: SkipPredicate | None,
    ) -> AsyncIterator[Product]:
        """Extract this listing page's products, follow "next", repeat.

        Pagination is interleaved with extraction rather than crawled up front, so
        a run capped by ``--max-products`` stops as soon as the pipeline stops
        pulling — it never walks 50 pages to fill a 20-product request. The
        generator is simply abandoned mid-page.
        """
        seen_products: set[str] = set()
        visited_pages = {seed_url.rstrip("/")}
        max_pages = self._settings.max_listing_pages
        page_url = seed_url
        pages = 1

        while True:
            urls = [u for u in candidates if _matches_template(u, template)]
            log.info("Listing page %d (%s): %d product links", pages, page_url, len(urls))
            async for product in self._emit(browser, product_page, urls, seen_products, skip):
                yield product

            if max_pages and pages >= max_pages:
                self.warnings.append(
                    f"Stopped after {pages} listing pages (max_listing_pages). "
                    "There may be more products."
                )
                return

            next_url = await self._next_page_url(listing, page_url, visited_pages)
            if next_url is None:
                log.info(
                    "No further pagination after %s — %d products seen",
                    page_url,
                    len(seen_products),
                )
                return

            visited_pages.add(next_url.rstrip("/"))
            page_url = next_url
            pages += 1
            candidates = await self._load_and_collect(browser, listing, page_url)

    async def _emit(
        self,
        browser: BrowserManager,
        page: Page,
        urls: list[str],
        seen: set[str],
        skip: SkipPredicate | None,
    ) -> AsyncIterator[Product]:
        """Extract each URL once, honouring the resume filter and politeness delay."""
        for url in urls:
            key = product_key(url)
            if key in seen:
                continue  # pagination overlap, or a product linked twice
            seen.add(key)
            if skip is not None and skip(key):
                self.stats.skipped_resume += 1
                continue
            product = await self._extract(browser, page, url)
            if product is None:
                self.stats.failed += 1
            else:
                yield product
            if self._settings.request_delay_s > 0:
                await asyncio.sleep(self._settings.request_delay_s)

    # ------------------------------------------------------------------ #
    # Discovery
    # ------------------------------------------------------------------ #
    async def _next_page_url(
        self, listing: Page, current_url: str, visited: set[str]
    ) -> str | None:
        """The next pagination link, or ``None`` when the listing is exhausted.

        Rejects off-host links and anything already visited: pagination widgets
        happily link back to page 1, and a cycle here is an infinite crawl.
        """
        try:
            found = await listing.evaluate(_NEXT_PAGE_JS)
        except PlaywrightError:
            return None
        if not found:
            return None

        next_url = str(found).split("#")[0]
        if urlparse(next_url).hostname != urlparse(current_url).hostname:
            return None
        if next_url.rstrip("/") in visited:
            log.debug("Pagination loops back to %s; stopping", next_url)
            return None
        return next_url

    async def _click_load_more(self, page: Page) -> bool:
        """Click a 'load more' affordance if one is visible and enabled."""
        try:
            clicked = bool(await page.evaluate(_LOAD_MORE_JS))
        except PlaywrightError:
            return False
        if clicked:
            log.debug("Clicked a load-more control")
        return clicked

    async def _advance(self, page: Page) -> bool:
        """Reveal more of a lazy-loaded listing. False when nothing more can be done.

        A "load more" button is an explicit affordance, so it wins over scrolling —
        a site with one usually will not grow on scroll alone.
        """
        return await self._click_load_more(page) or await scroll_to_bottom(page)

    async def _load_and_collect(self, browser: BrowserManager, page: Page, url: str) -> list[str]:
        """Navigate to a *subsequent* listing page and collect its links.

        Tolerant by design: one unreachable page N should end pagination, not fail
        a run that already produced products. The seed page is loaded separately,
        where a navigation failure is fatal.
        """
        try:
            await browser.goto(page, url)
        except NavigationError as e:
            log.warning("Could not load listing page %s: %s", url, e)
            return []
        return await self._drain_and_collect(page, url)

    async def _drain_and_collect(self, page: Page, url: str) -> list[str]:
        """Return the already-loaded page's same-host links, after lazy-load.

        Drains both lazy-load mechanisms — infinite scroll and "load more" — until
        two consecutive rounds produce no new links.
        """
        host = urlparse(url).hostname
        seen: set[str] = set()
        stable_rounds = 0
        rounds = 0
        max_scrolls = self._settings.max_scrolls

        while max_scrolls == 0 or rounds <= max_scrolls:
            rounds += 1
            before = len(seen)
            for href in await self._hrefs(page):
                parsed = urlparse(href)
                if parsed.scheme not in ("http", "https") or parsed.hostname != host:
                    continue
                seen.add(href.split("#")[0].split("?")[0])

            stable_rounds = 0 if len(seen) > before else stable_rounds + 1
            if stable_rounds >= 2:
                break
            if not await self._advance(page):
                break
            await page.wait_for_timeout(int(self._settings.scroll_pause_s * 1000))

        log.info("Collected %d same-host links from %s", len(seen), url)
        return sorted(seen)

    @staticmethod
    async def _hrefs(page: Page) -> list[str]:
        try:
            # `.href` resolves relative URLs against the document for us.
            hrefs: list[str] = await page.eval_on_selector_all(
                "a[href]", "els => els.map(e => e.href).filter(Boolean)"
            )
        except PlaywrightError:
            return []
        return hrefs

    async def _detect_template(
        self, browser: BrowserManager, page: Page, seed_url: str, candidates: list[str]
    ) -> str | None:
        """Cluster by URL shape, sample each cluster, return the product pattern.

        Returning the *pattern* rather than a fixed URL list is what makes
        pagination cheap: every later listing page is filtered through the same
        template, with no re-sampling.
        """
        segments_by_bucket: dict[tuple[int, str], list[list[str]]] = defaultdict(list)
        urls_by_bucket: dict[tuple[int, str], list[str]] = defaultdict(list)
        for url in candidates:
            if url.rstrip("/") == seed_url.rstrip("/"):
                continue
            segments = _segments_of(url)
            if segments is None:
                continue
            bucket = _bucket_of(segments)
            segments_by_bucket[bucket].append(segments)
            urls_by_bucket[bucket].append(url)

        ranked = sorted(
            (b for b in urls_by_bucket.items() if len(b[1]) >= _MIN_CLUSTER_SIZE),
            key=lambda kv: len(kv[1]),
            reverse=True,
        )[:_MAX_CANDIDATE_CLUSTERS]

        if not ranked:
            return None

        for bucket, urls in ranked:
            pattern = _template_of(segments_by_bucket[bucket])
            sample = urls[:_SAMPLE_SIZE]
            hits = 0
            for url in sample:
                result = await self._fields(browser, page, url)
                if result is not None and _looks_like_product(*result):
                    hits += 1
            log.info(
                "Cluster %s (%d urls): %d/%d sampled pages look like products",
                pattern,
                len(urls),
                hits,
                len(sample),
            )
            if hits * 2 > len(sample):  # strict majority
                log.info("[cyan]Detected product pattern[/] %s — %d URLs", pattern, len(urls))
                return pattern

        patterns = ", ".join(_template_of(segments_by_bucket[b]) for b, _ in ranked)
        self.warnings.append(
            f"Sampled the {len(ranked)} largest link groups ({patterns}); "
            "none looked like product pages."
        )
        return None

    # ------------------------------------------------------------------ #
    # Extraction
    # ------------------------------------------------------------------ #
    async def _fields(
        self, browser: BrowserManager, page: Page, url: str
    ) -> tuple[FieldSet, BeautifulSoup] | None:
        """Render ``url`` and return its fields plus the parsed document."""
        try:
            await browser.goto(page, url)
            html = await page.content()
        except (NavigationError, PlaywrightError) as e:
            log.debug("Could not load %s: %s", url, e)
            return None
        return self._extractor.extract_fields(html, url), BeautifulSoup(html, "lxml")

    async def _extract(self, browser: BrowserManager, page: Page, url: str) -> Product | None:
        result = await self._fields(browser, page, url)
        if result is None:
            return None
        fields, _ = result
        if not fields.is_complete():
            log.warning("Skipping %s — missing required fields", url)
            return None
        assert fields.name is not None and fields.image_url is not None
        try:
            return Product(
                name=fields.name,
                image_url=fields.image_url,
                product_url=url,
                mrp=fields.mrp,
                asp=fields.asp,
                category=fields.category,
                subcategory=fields.subcategory,
            )
        except (ValueError, ScraperError) as e:
            log.warning("Invalid product data for %s: %s", url, e)
            return None
