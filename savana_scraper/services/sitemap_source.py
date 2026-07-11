"""Sitemap-driven product source — discovery for sites that share URL shapes.

The generic crawler infers which links are products by *shape*: it clusters
same-shaped URLs and samples each cluster. That fails on storefronts where a
product and a category are indistinguishable by path — beyoung.in serves both
``/black-jacquard-striped-t-shirt`` (product) and ``/t-shirts-for-men``
(category) as bare single-segment slugs. They collapse into one cluster, and a
sample seeded from the homepage (mostly category links) rejects the whole thing.

Such sites almost always publish the answer themselves. A ``products`` sitemap is
the site declaring *exactly* which URLs are products — more trustworthy than any
shape heuristic. So this source resolves the sitemap tree, follows only the
product-specific branch, and hands the URLs to the same extractor the generic
crawler uses. It still samples a few and asks :func:`is_product_page`, so a
mislabelled sitemap makes the run fall back rather than export garbage.

Discovery is authoritative but extraction is shared: we subclass
:class:`GenericProductSource` purely to reuse ``_extract``/``_emit`` and the
extractor wiring. Only :meth:`stream` — how the URLs are *found* — differs.
"""

from __future__ import annotations

import asyncio
import gzip
from collections.abc import AsyncIterator, Iterable
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from lxml import etree
from playwright.async_api import Page

from savana_scraper.core.browser import BrowserManager
from savana_scraper.core.logging import get_logger
from savana_scraper.models import Product, product_key
from savana_scraper.services.adapter import SkipPredicate
from savana_scraper.services.generic_source import (
    _SAMPLE_SIZE,
    GenericProductSource,
    _looks_like_product,
)

log = get_logger(__name__)

_FETCH_TIMEOUT_S = 20.0
# Product pages fetched at once on the fast HTTP path. Most storefronts render
# their product data server-side, so a plain concurrent GET replaces a
# one-page-per-second browser render — the network latency of many pages overlaps
# instead of stacking. Kept modest to stay polite to a single host.
_HTTP_CONCURRENCY = 8
# A sitemap tree can fan out widely (per-language, per-store-view). Bound the
# number of sitemap documents fetched so a pathological index cannot run away.
_MAX_SITEMAP_DOCS = 40
# Stop collecting product URLs past this — the pipeline caps products anyway, and
# a full catalogue can list hundreds of thousands.
_MAX_PRODUCT_URLS = 50_000
# Substrings that mark a nested sitemap as the product branch. Covers Magento
# (``products.xml``), Shopify (``sitemap_products_1.xml``) and Yoast/WooCommerce
# (``product-sitemap.xml``).
_PRODUCT_SITEMAP_HINTS = ("product",)


def _looks_gzipped(url: str, body: bytes) -> bool:
    """A ``.gz`` sitemap served as ``application/gzip`` won't be auto-inflated."""
    return url.lower().endswith(".gz") or body[:2] == b"\x1f\x8b"


def _localname(tag: object) -> str:
    """``loc`` from ``{http://www.sitemaps.org/schemas/sitemap/0.9}loc``."""
    return etree.QName(tag).localname if isinstance(tag, str) else ""


def _parse_sitemap(body: bytes) -> tuple[list[str], list[str]]:
    """Split a sitemap document into (nested sitemap locs, page locs).

    A ``<sitemapindex>`` yields child sitemaps; a ``<urlset>`` yields page URLs.
    Namespaces vary and some feeds are dirty, so we read by local-name and
    recover from parse errors instead of aborting the whole run.
    """
    try:
        root = etree.fromstring(body, parser=etree.XMLParser(recover=True))
    except etree.XMLSyntaxError as e:
        log.debug("Unparseable sitemap (%s)", e)
        return [], []
    if root is None:
        return [], []

    nested: list[str] = []
    pages: list[str] = []
    for element in root.iter():
        if _localname(element.tag) != "loc":
            continue
        loc = (element.text or "").strip()
        if not loc:
            continue
        parent = _localname(element.getparent().tag) if element.getparent() is not None else ""
        (nested if parent == "sitemap" else pages).append(loc)
    return nested, pages


def _is_product_sitemap(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(hint in path for hint in _PRODUCT_SITEMAP_HINTS)


async def _sitemap_roots(client: httpx.AsyncClient, seed_url: str) -> list[str]:
    """Sitemaps declared in robots.txt, falling back to ``/sitemap.xml``."""
    origin = urlparse(seed_url)
    base = f"{origin.scheme}://{origin.netloc}"
    roots: list[str] = []
    try:
        response = await client.get(urljoin(base, "/robots.txt"))
        if response.status_code < 400:
            for line in response.text.splitlines():
                key, _, value = line.partition(":")
                if key.strip().lower() == "sitemap" and value.strip():
                    roots.append(value.strip())
    except httpx.HTTPError as e:
        log.debug("robots.txt unreachable for sitemap discovery (%s)", e)
    return roots or [urljoin(base, "/sitemap.xml")]


async def discover_product_urls(
    seed_url: str, *, user_agent: str, max_urls: int = _MAX_PRODUCT_URLS
) -> tuple[list[str], str | None]:
    """Walk the site's sitemap tree and return its product page URLs.

    Only the *product* branch of the tree is descended: from an index, we follow
    sitemaps whose loc names them products (``products.xml`` and friends) when any
    exist, and collect page URLs only once we are inside that branch. A site whose
    sitemap makes no product/other distinction yields ``[]`` — the caller then
    falls back to shape-based discovery rather than treating every listed page,
    category and CMS URL alike, as a product.

    Returns ``(urls, description)`` where ``description`` names the sitemap the
    URLs came from, for the run's report.
    """
    async with httpx.AsyncClient(
        timeout=_FETCH_TIMEOUT_S,
        follow_redirects=True,
        headers={"user-agent": user_agent},
    ) as client:
        return await _collect_from_client(client, seed_url, max_urls)


async def _collect_from_client(
    client: httpx.AsyncClient, seed_url: str, max_urls: int
) -> tuple[list[str], str | None]:
    """The sitemap walk itself, over a caller-owned client (so tests can mock it)."""
    host = (urlparse(seed_url).hostname or "").lower()
    seen_docs: set[str] = set()
    product_urls: list[str] = []
    seen_urls: set[str] = set()
    source_sitemap: str | None = None

    roots = await _sitemap_roots(client, seed_url)
    # (url, in_product_branch) — page URLs only count once inside the branch.
    queue: list[tuple[str, bool]] = [(r, False) for r in roots]

    while queue and len(seen_docs) < _MAX_SITEMAP_DOCS and len(product_urls) < max_urls:
        url, in_branch = queue.pop(0)
        if url in seen_docs:
            continue
        seen_docs.add(url)
        try:
            response = await client.get(url)
        except httpx.HTTPError as e:
            log.debug("Sitemap %s unreachable (%s)", url, e)
            continue
        if response.status_code >= 400:
            continue
        body = response.content
        if _looks_gzipped(url, body):
            try:
                body = gzip.decompress(body)
            except (OSError, EOFError) as e:
                log.debug("Could not gunzip sitemap %s (%s)", url, e)
                continue

        nested, pages = _parse_sitemap(body)
        if nested:
            product_children = [n for n in nested if _is_product_sitemap(n)]
            # Descend only the product branch when the index distinguishes one;
            # otherwise carry the current branch flag downward.
            children = product_children or nested
            child_in_branch = in_branch or bool(product_children)
            queue.extend((n, child_in_branch) for n in children)
        if pages and in_branch:
            if source_sitemap is None:
                source_sitemap = url
            for loc in pages:
                if urlparse(loc).hostname != host or loc in seen_urls:
                    continue
                seen_urls.add(loc)
                product_urls.append(loc)
                if len(product_urls) >= max_urls:
                    break

    description = (
        f"sitemap {source_sitemap} ({len(product_urls)} product URLs)" if product_urls else None
    )
    return product_urls, description


class SitemapProductSource(GenericProductSource):
    """Discovers products from the site's product sitemap; extracts each page.

    Extraction takes the cheapest route that works. Most storefronts render their
    product data server-side, so the fast path is a **plain concurrent HTTP GET**
    — no browser, many pages in flight at once. Only when a sampled page does not
    yield product fields over raw HTTP (its data needs JavaScript) does it fall
    back to rendering each page in a browser.

    Falls back to the generic shape-based crawler when the site publishes no
    product sitemap, or when neither route parses the sitemap's URLs as products —
    so a wrong or stale sitemap degrades to best-effort rather than exporting
    non-products.
    """

    name = "sitemap"

    async def stream(
        self, seed_url: str, skip: SkipPredicate | None = None
    ) -> AsyncIterator[Product]:
        urls, description = await discover_product_urls(
            seed_url, user_agent=self._settings.user_agent
        )
        if not urls:
            log.info("No product sitemap for %s — falling back to the generic crawler", seed_url)
            async for product in super().stream(seed_url, skip):
                yield product
            return

        log.info("[cyan]Product sitemap[/] %s", description)

        # Fast path: fetch product pages over HTTP, concurrently, no browser.
        async with self._http_client() as client:
            if await self._http_sample_confirms(client, urls):
                self.detected_pattern = f"{description} — HTTP, {_HTTP_CONCURRENCY}x concurrent"
                async for product in self._stream_http(client, urls, skip):
                    yield product
                return

        # The pages need JavaScript to reveal their product data. Render them.
        log.info("Product pages need rendering — switching to the browser")
        async with (
            BrowserManager(self._settings) as browser,
            browser.page() as page,
        ):
            if not await self._sample_confirms_products(browser, page, urls):
                self.warnings.append(
                    f"The site's product sitemap listed {len(urls)} URLs, but a sample "
                    "did not parse as product pages; used the generic crawler instead."
                )
                fall_back = True
            else:
                self.detected_pattern = description
                seen: set[str] = set()
                async for product in self._emit(browser, page, urls, seen, skip):
                    yield product
                return

        if fall_back:
            async for product in super().stream(seed_url, skip):
                yield product

    # ------------------------------------------------------------------ #
    # Fast HTTP path
    # ------------------------------------------------------------------ #
    def _http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT_S,
            follow_redirects=True,
            headers={"user-agent": self._settings.user_agent},
        )

    async def _stream_http(
        self, client: httpx.AsyncClient, urls: list[str], skip: SkipPredicate | None
    ) -> AsyncIterator[Product]:
        """Fetch and extract product pages ``_HTTP_CONCURRENCY`` at a time.

        Deduping and the resume filter run *before* a URL joins a batch, so a
        resumed run pays no network cost for products it already has. A run capped
        by ``--max-products`` abandons this generator between batches, over-fetching
        by at most one batch.
        """
        seen: set[str] = set()
        batch: list[str] = []
        for url in urls:
            key = product_key(url)
            if key in seen:
                continue
            seen.add(key)
            if skip is not None and skip(key):
                self.stats.skipped_resume += 1
                continue
            batch.append(url)
            if len(batch) >= _HTTP_CONCURRENCY:
                async for product in self._fetch_batch(client, batch):
                    yield product
                batch = []
        if batch:
            async for product in self._fetch_batch(client, batch):
                yield product

    async def _fetch_batch(
        self, client: httpx.AsyncClient, urls: list[str]
    ) -> AsyncIterator[Product]:
        results = await asyncio.gather(*(self._http_extract(client, url) for url in urls))
        for product in results:
            if product is None:
                self.stats.failed += 1
            else:
                yield product

    async def _http_extract(self, client: httpx.AsyncClient, url: str) -> Product | None:
        html = await self._http_html(client, url)
        if html is None:
            return None
        return self._build_product(self._extractor.extract_fields(html, url), url)

    async def _http_html(self, client: httpx.AsyncClient, url: str) -> str | None:
        try:
            response = await client.get(url)
        except httpx.HTTPError as e:
            log.debug("Could not fetch %s over HTTP (%s)", url, e)
            return None
        if response.status_code >= 400:
            log.debug("HTTP %d for %s", response.status_code, url)
            return None
        return response.text

    async def _http_sample_confirms(self, client: httpx.AsyncClient, urls: Iterable[str]) -> bool:
        """Do the first few URLs yield product fields over plain HTTP (no browser)?"""
        sample = list(urls)[:_SAMPLE_SIZE]
        hits = 0
        for url in sample:
            html = await self._http_html(client, url)
            if html is None:
                continue
            fields = self._extractor.extract_fields(html, url)
            if _looks_like_product(fields, BeautifulSoup(html, "lxml")):
                hits += 1
        log.info("HTTP sample: %d/%d URLs parsed as products without a browser", hits, len(sample))
        return hits * 2 > len(sample)

    # ------------------------------------------------------------------ #
    # Browser fallback verification
    # ------------------------------------------------------------------ #
    async def _sample_confirms_products(
        self, browser: BrowserManager, page: Page, urls: Iterable[str]
    ) -> bool:
        """A strict majority of the first few *rendered* URLs must look like products."""
        sample = list(urls)[:_SAMPLE_SIZE]
        hits = 0
        for url in sample:
            result = await self._fields(browser, page, url)
            if result is not None and _looks_like_product(*result):
                hits += 1
        log.info("Rendered sample: %d/%d URLs look like products", hits, len(sample))
        return hits * 2 > len(sample)
