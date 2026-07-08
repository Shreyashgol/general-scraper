"""End-to-end crawls of the generic source against fixture storefronts.

Each fixture is a real static site served over HTTP and driven with a real
Chromium, exercising one lazy-load / pagination mechanism the crawler must
handle. These are the tests that would have caught the "only page 1" gap.
"""

from __future__ import annotations

import functools
import http.server
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from savana_scraper.core.config import load_settings
from savana_scraper.core.exceptions import NavigationError
from savana_scraper.models import Product
from savana_scraper.services.generic_source import GenericProductSource

PRODUCT_PAGE = """<!doctype html><html><body>
  <h1>{name}</h1>
  <img src="/static/{n}.jpg" width="600" height="800">
  <div class="product-main"><p class="price">£{price}.00</p></div>
</body></html>
"""


def _product_files(root: Path, count: int) -> None:
    products = root / "p"
    products.mkdir(parents=True, exist_ok=True)
    for n in range(1, count + 1):
        (products / f"item-{n}.html").write_text(
            PRODUCT_PAGE.format(name=f"Item {n}", n=n, price=10 + n)
        )


def _links(first: int, last: int) -> str:
    return "".join(f'<a href="/p/item-{n}.html">Item {n}</a>' for n in range(first, last + 1))


def build_load_more_site(root: Path) -> None:
    """Three products, a Load more button that reveals three more, then vanishes."""
    _product_files(root, 6)
    (root / "index.html").write_text(f"""<!doctype html><html><body>
      <h1>Shop</h1>
      <div id="grid">{_links(1, 3)}</div>
      <button class="load-more">Load more</button>
      <script>
        document.querySelector('.load-more').addEventListener('click', () => {{
          const grid = document.getElementById('grid');
          for (let n = 4; n <= 6; n++) {{
            const a = document.createElement('a');
            a.href = `/p/item-${{n}}.html`;
            a.textContent = `Item ${{n}}`;
            grid.appendChild(a);
          }}
          document.querySelector('.load-more').remove();
        }});
      </script>
    </body></html>
    """)


def build_numbered_site(root: Path) -> None:
    """Two listing pages linked only by numbered pagination — no 'next' anywhere."""
    _product_files(root, 6)
    (root / "list-1.html").write_text(f"""<!doctype html><html><body>
      <h1>Shop</h1>
      <div id="grid">{_links(1, 3)}</div>
      <nav class="pagination">
        <span class="current">1</span>
        <a href="/list-2.html">2</a>
      </nav>
    </body></html>
    """)
    (root / "list-2.html").write_text(f"""<!doctype html><html><body>
      <h1>Shop</h1>
      <div id="grid">{_links(4, 6)}</div>
      <nav class="pagination">
        <a href="/list-1.html">1</a>
        <span class="current">2</span>
      </nav>
    </body></html>
    """)


BREADCRUMB_PRODUCT_PAGE = """<!doctype html><html><body>
  <nav aria-label="Breadcrumb">
    <ol>
      <li><a href="/">Home</a></li>
      <li><a href="/women">Women</a></li>
      <li><a href="/women/bags">{subcategory}</a></li>
      <li><span>{name}</span></li>
    </ol>
  </nav>
  <h1>{name}</h1>
  <img src="/static/{n}.jpg" width="600" height="800">
  <div class="product-main"><p class="price">£{price}.00</p></div>
</body></html>
"""


def build_breadcrumb_site(root: Path) -> None:
    """A storefront that publishes a breadcrumb trail on every product page.

    Products alternate between two subcategories under one category, so a run
    that merely echoed the listing's own name would visibly collapse them.
    """
    products = root / "p"
    products.mkdir(parents=True, exist_ok=True)
    for n in range(1, 5):
        (products / f"item-{n}.html").write_text(
            BREADCRUMB_PRODUCT_PAGE.format(
                name=f"Item {n}",
                n=n,
                price=10 + n,
                subcategory="Backpacks" if n % 2 else "Tote Bags",
            )
        )
    (root / "index.html").write_text(f"""<!doctype html><html><body>
      <h1>Shop</h1>
      <div id="grid">{_links(1, 4)}</div>
    </body></html>
    """)


def build_decoy_site(root: Path) -> None:
    """A product literally named "2" must not be mistaken for a pagination link."""
    _product_files(root, 3)
    (root / "index.html").write_text(f"""<!doctype html><html><body>
      <h1>Shop</h1>
      <div id="grid">{_links(1, 3)}<a href="/p/item-1.html">2</a></div>
      <a href="/shipping.html">Next day delivery</a>
    </body></html>
    """)
    (root / "shipping.html").write_text("<html><body><h1>Shipping</h1></body></html>")


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler without the per-request stderr logging."""

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def serve(tmp_path: Path) -> Iterator[object]:
    """Serve a fixture directory on an ephemeral port; yields a builder → base URL."""
    servers: list[http.server.ThreadingHTTPServer] = []

    def _start(builder) -> str:  # type: ignore[no-untyped-def]
        root = tmp_path / builder.__name__
        root.mkdir()
        builder(root)
        handler = functools.partial(_QuietHandler, directory=str(root))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{server.server_port}"

    yield _start
    for server in servers:
        server.shutdown()


async def _crawl_products(seed: str) -> tuple[GenericProductSource, list[Product]]:
    settings = load_settings()
    settings.request_delay_s = 0.0
    settings.scroll_pause_s = 0.1
    source = GenericProductSource(settings)
    try:
        products = [p async for p in source.stream(seed)]
    except Exception as e:  # noqa: BLE001 - no browser binary in a bare environment
        if "executable doesn't exist" in str(e).lower():
            pytest.skip("Chromium not installed for Playwright")
        raise
    return source, products


async def _crawl(seed: str) -> tuple[GenericProductSource, list[str]]:
    source, products = await _crawl_products(seed)
    return source, [p.name for p in products]


async def test_load_more_button_reveals_the_rest(serve) -> None:  # type: ignore[no-untyped-def]
    """Clicking 'Load more' must be part of draining a listing page."""
    base = serve(build_load_more_site)
    source, names = await _crawl(f"{base}/index.html")

    assert sorted(names) == [f"Item {n}" for n in range(1, 7)]
    assert source.detected_pattern == "/p/*"
    assert source.warnings == []


async def test_numbered_pagination_is_followed(serve) -> None:  # type: ignore[no-untyped-def]
    """A listing with only '1 2 3' links and no 'next' must still be walked."""
    base = serve(build_numbered_site)
    source, names = await _crawl(f"{base}/list-1.html")

    assert sorted(names) == [f"Item {n}" for n in range(1, 7)]
    assert source.stats.failed == 0


async def test_decoys_do_not_derail_the_crawl(serve) -> None:  # type: ignore[no-untyped-def]
    """A product labelled "2" and a "Next day delivery" link are not pagination."""
    base = serve(build_decoy_site)
    source, names = await _crawl(f"{base}/index.html")

    assert sorted(names) == ["Item 1", "Item 2", "Item 3"]


async def test_breadcrumbs_become_category_and_subcategory(serve) -> None:  # type: ignore[no-untyped-def]
    """On an unknown storefront, the breadcrumb trail is the taxonomy.

    `Home > Women > Backpacks > Item 1` must yield ("Women", "Backpacks") — the
    root dropped, the product's own name never mistaken for a subcategory.
    """
    base = serve(build_breadcrumb_site)
    source, products = await _crawl_products(f"{base}/index.html")

    by_name = {p.name: p for p in products}
    assert sorted(by_name) == [f"Item {n}" for n in range(1, 5)]

    assert {p.category for p in products} == {"Women"}
    assert by_name["Item 1"].subcategory == "Backpacks"
    assert by_name["Item 2"].subcategory == "Tote Bags"
    # The trailing crumb is the product; it must never leak into a column.
    assert all(p.subcategory != p.name for p in products)
    assert source.warnings == []


async def test_unreachable_seed_raises_rather_than_yielding_nothing() -> None:
    """A seed that cannot be loaded must fail loudly, not report an empty run.

    This is the regression for a site that blocks automation (Myntra): the caller
    needs an exception to surface as an error, not a silent 'done, 0 products'.
    """
    settings = load_settings()
    settings.nav_timeout_ms = 3000
    source = GenericProductSource(settings)
    # Reserved TEST-NET-1 address — nothing answers, so navigation fails.
    with pytest.raises(NavigationError):
        try:
            _ = [p async for p in source.stream("http://192.0.2.1/shop")]
        except Exception as e:  # noqa: BLE001
            if "executable doesn't exist" in str(e).lower():
                pytest.skip("Chromium not installed for Playwright")
            raise
