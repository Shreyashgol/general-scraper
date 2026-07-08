"""Tests for the generic crawler's pagination following.

Three layers: the template filter that decides which links on page N are
products, the guard rails around the "next" link, and the browser-side JS that
finds it.
"""

from __future__ import annotations

from typing import Any

import pytest
from playwright.async_api import Error as PlaywrightError

from savana_scraper.core.browser import BrowserManager
from savana_scraper.core.config import Settings, load_settings
from savana_scraper.services.generic_source import (
    _LOAD_MORE_JS,
    _NEXT_PAGE_JS,
    GenericProductSource,
    _matches_template,
)


# --------------------------------------------------------------------------- #
# Template filter — applied to every listing page after the first
# --------------------------------------------------------------------------- #
def test_matches_template_wildcard_position() -> None:
    t = "/catalogue/*/index.html"
    assert _matches_template("https://x.com/catalogue/a-light_1000/index.html", t)
    assert _matches_template("https://x.com/catalogue/soumission_998/index.html", t)


def test_matches_template_rejects_other_depths() -> None:
    t = "/catalogue/*/index.html"
    # Pagination links themselves must never be mistaken for products.
    assert not _matches_template("https://x.com/catalogue/page-2.html", t)
    # Category pages sit deeper.
    assert not _matches_template("https://x.com/catalogue/category/books/travel_2/index.html", t)


def test_matches_template_respects_literal_positions() -> None:
    assert _matches_template("https://x.com/details/bag-1", "/details/*")
    assert not _matches_template("https://x.com/activity/13070", "/details/*")


def test_matches_template_rejects_blocklisted_paths() -> None:
    """A /cart link can never be a product, whatever its shape."""
    assert not _matches_template("https://x.com/cart/thing", "/*/*")


# --------------------------------------------------------------------------- #
# Next-link guard rails
# --------------------------------------------------------------------------- #
class _StubPage:
    """Stands in for a Playwright page: one canned answer from evaluate()."""

    def __init__(self, value: Any = None, *, raises: bool = False) -> None:
        self._value = value
        self._raises = raises

    async def evaluate(self, _script: str) -> Any:
        if self._raises:
            raise PlaywrightError("navigation destroyed the context")
        return self._value


def _source(settings: Settings | None = None) -> GenericProductSource:
    return GenericProductSource(settings or load_settings())


async def test_next_page_url_returns_the_link() -> None:
    page = _StubPage("https://x.com/catalogue/page-2.html")
    got = await _source()._next_page_url(page, "https://x.com/", set())  # type: ignore[arg-type]
    assert got == "https://x.com/catalogue/page-2.html"


async def test_next_page_url_strips_the_fragment() -> None:
    page = _StubPage("https://x.com/page-2.html#top")
    got = await _source()._next_page_url(page, "https://x.com/", set())  # type: ignore[arg-type]
    assert got == "https://x.com/page-2.html"


async def test_next_page_url_is_none_when_no_link() -> None:
    page = _StubPage(None)
    assert await _source()._next_page_url(page, "https://x.com/", set()) is None  # type: ignore[arg-type]


async def test_next_page_url_rejects_offsite_links() -> None:
    """A 'next' link to another host is not this listing's pagination."""
    page = _StubPage("https://evil.example/page-2.html")
    assert await _source()._next_page_url(page, "https://x.com/", set()) is None  # type: ignore[arg-type]


async def test_next_page_url_rejects_already_visited_pages() -> None:
    """Pagination widgets link back to page 1; a cycle here is an infinite crawl."""
    page = _StubPage("https://x.com/page-1.html")
    visited = {"https://x.com/page-1.html"}
    assert await _source()._next_page_url(page, "https://x.com/", visited) is None  # type: ignore[arg-type]


async def test_next_page_url_survives_a_playwright_error() -> None:
    page = _StubPage(raises=True)
    assert await _source()._next_page_url(page, "https://x.com/", set()) is None  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# The browser-side detector, against a real Chromium
# --------------------------------------------------------------------------- #
NEXT_FIXTURES = {
    "rel-next-anchor": '<a rel="next" href="https://x.com/p2">More</a>',
    "rel-next-link-tag": '<link rel="next" href="https://x.com/p2">',
    "li-next": '<li class="next"><a href="https://x.com/p2">next</a></li>',
    "aria-label": '<a aria-label="Next page" href="https://x.com/p2">›</a>',
    "text-next": '<a href="https://x.com/p2">Next</a>',
    "text-chevron": '<a href="https://x.com/p2">›</a>',
}

NEXT_FIXTURES.update(
    {
        # Numbered pagination: no "next" link anywhere, just 1 2 3.
        "numbered-nav": (
            '<nav class="pagination"><span class="current">1</span>'
            '<a href="https://x.com/p2">2</a><a href="https://x.com/p3">3</a></nav>'
        ),
        "numbered-pager": (
            '<ul class="pager"><li class="current">1</li>'
            '<li><a href="https://x.com/p2">2</a></li></ul>'
        ),
        "numbered-aria-current": (
            '<div class="pages"><a href="https://x.com/p1" aria-current="page">1</a>'
            '<a href="https://x.com/p2">2</a></div>'
        ),
    }
)

NO_NEXT_FIXTURES = {
    "no-links": "<p>last page</p>",
    # The exact-match rule earns its keep here.
    "next-day-delivery": '<a href="https://x.com/shipping">Next day delivery</a>',
    "previous-only": '<li class="previous"><a href="https://x.com/p1">previous</a></li>',
    # A bare "2" outside a pagination container could be anything.
    "bare-number": '<a href="https://x.com/two">2</a>',
    "product-named-2": '<div class="product"><a href="https://x.com/album-2">2</a></div>',
    # Last page: current is 2, there is no 3.
    "numbered-last-page": (
        '<nav class="pagination"><a href="https://x.com/p1">1</a>'
        '<span class="current">2</span></nav>'
    ),
}

LOAD_MORE_YES = {
    "data-attribute": "<button data-load-more>Whatever</button>",
    "class-hook": '<button class="load-more">x</button>',
    "text-load-more": "<button>Load more</button>",
    "role-button-show-more": '<a role="button" href="#">Show More Products</a>',
}

LOAD_MORE_NO = {
    # An exhausted button would otherwise be clicked forever.
    "disabled": '<button class="load-more" disabled>Load more</button>',
    "aria-disabled": '<button data-load-more aria-disabled="true">Load more</button>',
    "hidden": '<button class="load-more" style="display:none">Load more</button>',
    # Anchored to the start of the label, so this is not a match.
    "download-more": "<button>Download more brochures</button>",
    "absent": "<p>end</p>",
}


@pytest.fixture
async def browser() -> Any:
    settings = Settings()
    manager = BrowserManager(settings)
    try:
        await manager.start()
    except Exception:  # noqa: BLE001 - environment without the browser binary
        pytest.skip("Chromium not installed for Playwright")
    yield manager
    await manager.stop()


@pytest.mark.parametrize("name", sorted(NEXT_FIXTURES))
async def test_next_page_js_finds_the_link(browser: BrowserManager, name: str) -> None:
    async with browser.page() as page:
        await page.set_content(f"<html><body>{NEXT_FIXTURES[name]}</body></html>")
        assert await page.evaluate(_NEXT_PAGE_JS) == "https://x.com/p2"


@pytest.mark.parametrize("name", sorted(NO_NEXT_FIXTURES))
async def test_next_page_js_finds_nothing(browser: BrowserManager, name: str) -> None:
    async with browser.page() as page:
        await page.set_content(f"<html><body>{NO_NEXT_FIXTURES[name]}</body></html>")
        assert await page.evaluate(_NEXT_PAGE_JS) is None


async def test_next_page_js_prefers_rel_next_over_link_text(browser: BrowserManager) -> None:
    """Semantic markup wins: the text fallback must not shadow rel=next."""
    html = '<a href="https://x.com/wrong">next</a><a rel="next" href="https://x.com/right">go</a>'
    async with browser.page() as page:
        await page.set_content(f"<html><body>{html}</body></html>")
        assert await page.evaluate(_NEXT_PAGE_JS) == "https://x.com/right"


async def test_next_page_js_prefers_rel_next_over_numbers(browser: BrowserManager) -> None:
    html = (
        '<nav class="pagination"><a href="https://x.com/wrong">2</a></nav>'
        '<a rel="next" href="https://x.com/right">go</a>'
    )
    async with browser.page() as page:
        await page.set_content(f"<html><body>{html}</body></html>")
        assert await page.evaluate(_NEXT_PAGE_JS) == "https://x.com/right"


async def test_next_page_js_reads_the_current_page_from_the_url(browser: BrowserManager) -> None:
    """With no `.current` marker, /page-2 must ask for the anchor labelled 3."""
    nav = (
        '<nav class="pagination"><a href="/catalogue/page-2.html">2</a>'
        '<a href="/catalogue/page-3.html">3</a></nav>'
    )
    async with browser.page() as page:
        await page.route(
            "**/*",
            lambda route: route.fulfill(content_type="text/html", body=f"<html>{nav}</html>"),
        )
        await page.goto("https://x.com/catalogue/page-2.html")
        assert await page.evaluate(_NEXT_PAGE_JS) == "https://x.com/catalogue/page-3.html"


async def test_next_page_js_reads_a_page_query_parameter(browser: BrowserManager) -> None:
    nav = '<nav class="pagination"><a href="https://x.com/list?page=6">6</a></nav>'
    async with browser.page() as page:
        await page.route(
            "**/*",
            lambda route: route.fulfill(content_type="text/html", body=f"<html>{nav}</html>"),
        )
        await page.goto("https://x.com/list?page=5")
        assert await page.evaluate(_NEXT_PAGE_JS) == "https://x.com/list?page=6"


# --------------------------------------------------------------------------- #
# The load-more detector
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", sorted(LOAD_MORE_YES))
async def test_load_more_js_clicks(browser: BrowserManager, name: str) -> None:
    async with browser.page() as page:
        await page.set_content(f"<html><body>{LOAD_MORE_YES[name]}</body></html>")
        assert await page.evaluate(_LOAD_MORE_JS) is True


@pytest.mark.parametrize("name", sorted(LOAD_MORE_NO))
async def test_load_more_js_declines(browser: BrowserManager, name: str) -> None:
    """Disabled, hidden, absent, or merely similar-sounding controls are left alone."""
    async with browser.page() as page:
        await page.set_content(f"<html><body>{LOAD_MORE_NO[name]}</body></html>")
        assert await page.evaluate(_LOAD_MORE_JS) is False


async def test_load_more_js_actually_fires_the_click_handler(browser: BrowserManager) -> None:
    """Returning true is worthless if the control was never really clicked."""
    html = """
      <button class="load-more">Load more</button>
      <script>
        window.__clicked = false;
        document.querySelector('.load-more').addEventListener('click', () => {
          window.__clicked = true;
        });
      </script>
    """
    async with browser.page() as page:
        await page.set_content(f"<html><body>{html}</body></html>")
        assert await page.evaluate(_LOAD_MORE_JS) is True
        assert await page.evaluate("window.__clicked") is True
