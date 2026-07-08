"""Playwright browser management (Infrastructure layer).

:class:`BrowserManager` is an async context manager that owns the Playwright
lifecycle and hands out configured pages. It knows nothing about products —
that separation keeps the domain testable and the browser swappable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from types import TracebackType

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from savana_scraper.core.config import Settings
from savana_scraper.core.exceptions import BrowserError, NavigationError
from savana_scraper.core.logging import get_logger

log = get_logger(__name__)

# Scroll the element that actually owns the scrollbar. Many storefronts (savana
# among them) set ``html { overflow-y: hidden }`` and scroll ``<body>`` or an
# inner div, which makes ``window.scrollTo`` and ``mouse.wheel`` silent no-ops.
# Probe the usual roots, then fall back to the tallest overflowing element.
_SCROLL_TO_BOTTOM_JS = """() => {
  const roots = [document.scrollingElement, document.documentElement, document.body];
  for (const el of roots) {
    if (el && el.scrollHeight > el.clientHeight + 1) {
      el.scrollTop = el.scrollHeight;
      return true;
    }
  }
  let best = null;
  for (const el of document.querySelectorAll('*')) {
    if (el.scrollHeight <= el.clientHeight + 1 || el.clientHeight < 200) continue;
    if (!['auto', 'scroll', 'overlay'].includes(getComputedStyle(el).overflowY)) continue;
    if (!best || el.scrollHeight > best.scrollHeight) best = el;
  }
  if (best) {
    best.scrollTop = best.scrollHeight;
    return true;
  }
  return false;
}"""


async def scroll_to_bottom(page: Page) -> bool:
    """Scroll the page's real scroll container to the bottom.

    Returns ``False`` when nothing on the page can scroll — the caller should
    stop trying to lazy-load rather than spin.
    """
    try:
        return bool(await page.evaluate(_SCROLL_TO_BOTTOM_JS))
    except PlaywrightError:
        return False


class BrowserManager:
    """Owns a single Chromium browser + context for the run's duration."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def __aenter__(self) -> BrowserManager:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.stop()

    async def start(self) -> None:
        """Launch the browser and create a configured context."""
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self._settings.headless,
            )
            self._context = await self._browser.new_context(
                user_agent=self._settings.user_agent,
                viewport=self._settings.viewport,  # type: ignore[arg-type]
                locale=self._settings.locale,
            )
            self._context.set_default_navigation_timeout(self._settings.nav_timeout_ms)
            self._context.set_default_timeout(self._settings.nav_timeout_ms)
            log.info("[green]Browser launched[/] (headless=%s)", self._settings.headless)
        except PlaywrightError as e:  # pragma: no cover - environment dependent
            await self.stop()
            raise BrowserError(f"Failed to launch browser: {e}") from e

    async def stop(self) -> None:
        """Tear down context, browser, and Playwright — best effort."""
        for closer, obj in (
            ("context", self._context),
            ("browser", self._browser),
        ):
            if obj is not None:
                try:
                    await obj.close()
                except PlaywrightError as e:  # pragma: no cover
                    log.warning("Error closing %s: %s", closer, e)
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except PlaywrightError as e:  # pragma: no cover
                log.warning("Error stopping playwright: %s", e)
        self._context = self._browser = self._playwright = None

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise BrowserError("Browser not started; use 'async with BrowserManager(...)'")
        return self._context

    @asynccontextmanager
    async def page(self) -> AsyncIterator[Page]:
        """Yield a fresh page and guarantee it is closed afterwards."""
        page = await self.context.new_page()
        try:
            yield page
        finally:
            with suppress(PlaywrightError):  # pragma: no cover
                await page.close()

    async def goto(self, page: Page, url: str, *, wait_until: str = "domcontentloaded") -> None:
        """Navigate, translating Playwright failures into domain errors."""
        try:
            response = await page.goto(url, wait_until=wait_until)  # type: ignore[arg-type]
        except PlaywrightTimeout as e:
            raise NavigationError(f"Timed out navigating to {url}") from e
        except PlaywrightError as e:
            raise NavigationError(f"Failed to navigate to {url}: {e}") from e
        if response is not None and response.status >= 400:
            raise NavigationError(f"HTTP {response.status} for {url}")
