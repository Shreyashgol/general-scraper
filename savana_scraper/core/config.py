"""Configuration — a single, config-driven source of truth.

Every tunable knob lives here so behaviour can change without touching code
(Green Flag: "Config driven"). Values can be overridden via environment
variables prefixed with ``SAVANA_`` or a ``.env`` file, e.g.
``SAVANA_HEADLESS=false``.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repository root (…/savana-scraper). Used to anchor default output/state dirs.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = PACKAGE_ROOT.parent


class SelectorConfig(BaseSettings):
    """CSS/attribute selectors, kept out of code so they can be tuned freely.

    The site is a JS-rendered SPA, so these are the DOM-fallback selectors used
    when structured data is unavailable. Override any of them via env, e.g.
    ``SAVANA_SEL_PRODUCT_LINK='a.card'``.
    """

    model_config = SettingsConfigDict(env_prefix="SAVANA_SEL_", extra="ignore")

    # Discovery: anchors on a category/listing page that point to product pages.
    # On savana.com product pages are ``/details/<slug>-<id>`` and listing pages
    # are ``/activity/<id>``.
    product_link: str = "a[href*='/details/']"
    # Infinite-scroll / load-more affordance (optional).
    load_more: str = "button[data-load-more], .load-more"

    # Extraction DOM fallbacks (used only when SSR JSON is unavailable). The
    # primary source is the SSR-embedded API JSON; Name/Image also resolve
    # cleanly from Open-Graph meta, so these stay deliberately generic.
    name: str = "h1, [data-testid='product-name'], [class*='goodsName'], [class*='title']"
    image: str = "meta[property='og:image'], img[data-main-image], .product-image img"
    mrp: str = "[data-mrp], .price--original, .mrp, del .price, s .price"
    asp: str = "[data-asp], [data-sale-price], .price--sale, .price--current, .selling-price"


class ApiConfig(BaseSettings):
    """Settings for savana.com's public JSON API (the ``api`` source).

    The storefront is a thin client over ``goods-flow/pageList``, which returns
    every field the CSV needs. Reading it directly avoids one browser navigation
    per product. Override via env, e.g. ``SAVANA_API_DELAY_S=0.5``.
    """

    model_config = SettingsConfigDict(env_prefix="SAVANA_API_", extra="ignore")

    base_url: str = "https://api-shop-in.savana.com"
    goods_flow_path: str = "/n/api/buyer/guide/user/goods-flow/pageList"

    # Sent as request headers; the API rejects/misroutes requests without them.
    country_language: str = "en-IN"
    h5_version: str = "6.39.0"

    # Politeness delay between API page requests (seconds).
    delay_s: float = 0.25
    timeout_s: float = 30.0
    # Safety valve on pagination depth per listing (0 = unlimited).
    max_pages_per_listing: int = 0

    # --- Category enrichment --------------------------------------------------
    # The listing API carries no category, so filling the category/subcategory
    # columns costs one product-page fetch each. Turn this off to restore the
    # ~50 products/sec listing-only path and export those two columns empty.
    fetch_categories: bool = True
    # Product pages fetched in parallel while enriching. Bounded to stay polite.
    detail_concurrency: int = 4


class Settings(BaseSettings):
    """Top-level runtime settings."""

    model_config = SettingsConfigDict(
        env_prefix="SAVANA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Target ---------------------------------------------------------------
    base_url: str = "https://www.savana.com"

    # --- Source ---------------------------------------------------------------
    # "auto"    — look the URL's domain up in the adapter registry, else generic.
    # "api"     — force Savana's goods-flow JSON API (fast).
    # "browser" — force the Savana Playwright adapter.
    # "generic" — force the site-agnostic crawler.
    source: str = "auto"

    # Auto-crawl: harvest listing URLs from the seed page and drain each one.
    crawl_site: bool = True
    # Cap on how many listings to drain (0 = unlimited).
    max_listings: int = 0

    # --- Browser --------------------------------------------------------------
    headless: bool = True
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    )
    viewport_width: int = 1366
    viewport_height: int = 900
    locale: str = "en-US"
    # Per-navigation timeout (ms).
    nav_timeout_ms: int = 30_000
    # Politeness delay between product page visits (seconds).
    request_delay_s: float = 1.0

    # --- Discovery ------------------------------------------------------------
    # Max scroll iterations when a listing lazy-loads products (0 = unlimited;
    # discovery still stops on its own once the page yields no new links).
    max_scrolls: int = 0
    # Max "next page" links the generic crawler will follow (0 = unlimited).
    # A safety valve: pagination on a large catalogue can run for hundreds of
    # pages, and an unbounded crawl with no --max-products is rarely intended.
    max_listing_pages: int = 50
    scroll_pause_s: float = 1.0
    # Hard cap on products per run (0 = unlimited).
    max_products: int = 0

    # --- Reliability ----------------------------------------------------------
    max_retries: int = 3
    retry_backoff_s: float = 2.0

    # --- Storage --------------------------------------------------------------
    output_dir: Path = PROJECT_ROOT / "outputs"
    state_dir: Path = PROJECT_ROOT / "storage" / "state"

    # --- Logging --------------------------------------------------------------
    log_level: str = "INFO"

    # --- Taxonomy -------------------------------------------------------------
    # Optional JSON file overriding/extending the savana category id → name map
    # (see services/taxonomy.py). Unmapped ids export as "cat:<id>".
    taxonomy_path: Path | None = None

    # --- Selectors ------------------------------------------------------------
    selectors: SelectorConfig = Field(default_factory=SelectorConfig)

    # --- API ------------------------------------------------------------------
    api: ApiConfig = Field(default_factory=ApiConfig)

    @property
    def viewport(self) -> dict[str, int]:
        return {"width": self.viewport_width, "height": self.viewport_height}

    def ensure_dirs(self) -> None:
        """Create output/state directories if they do not yet exist."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)


def load_settings(**overrides: object) -> Settings:
    """Build :class:`Settings`, applying explicit keyword overrides last."""
    return Settings(**overrides)  # type: ignore[arg-type]
