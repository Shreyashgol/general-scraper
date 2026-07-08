"""Integration tests that exercise a real Chromium via Playwright.

These validate the "Browser launch" and end-to-end "Extraction" acceptance
criteria. They are skipped automatically if the browser binary is missing so
the unit suite still runs in a bare environment.
"""

from __future__ import annotations

import pytest

from savana_scraper.core.browser import BrowserManager
from savana_scraper.core.config import Settings
from savana_scraper.services.extractor import Extractor

PRODUCT_HTML = """
<html><head>
  <script type="application/ld+json">
  {"@type":"Product","name":"Integration Shirt",
   "image":["https://cdn.savana.com/int.jpg"],
   "mrp":"1200",
   "offers":{"price":"900"}}
  </script>
</head><body><h1>Integration Shirt</h1></body></html>
"""


async def _chromium_available(settings: Settings) -> bool:
    try:
        async with BrowserManager(settings):
            return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.asyncio
async def test_browser_launches_and_renders() -> None:
    settings = Settings(headless=True)
    if not await _chromium_available(settings):
        pytest.skip("Chromium not installed for Playwright")

    async with BrowserManager(settings) as browser, browser.page() as page:
        await page.set_content(PRODUCT_HTML)
        html = await page.content()

    fs = Extractor(settings).extract_fields(html, "https://www.savana.com/product/int")
    assert fs.name == "Integration Shirt"
    assert fs.image_url == "https://cdn.savana.com/int.jpg"
    assert str(fs.asp) == "900"
    assert str(fs.mrp) == "1200"
