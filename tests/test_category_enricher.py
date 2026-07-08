"""Tests for the savana category enricher.

The listing API has no taxonomy, so these two columns are bought with one product
page fetch each. What matters is that the cost is bounded, that a failed fetch
degrades to a blank category instead of losing the product, and that unmapped ids
are reported.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from savana_scraper.core.config import load_settings
from savana_scraper.models import Product
from savana_scraper.services.savana_api import SavanaCategoryEnricher
from savana_scraper.services.taxonomy import SavanaTaxonomy

DETAIL_TEMPLATE = """
<html><body><script>
window.__cache = {{"/n/api/trade/intention/item/detail":{{
"goodsId":{goods_id},"goodsName":"Thing","salesPrice":990,
"level1CatId":2,"level2CatId":{level2},"level3CatId":{level3}}}}}
</script></body></html>
"""


def _product(n: int = 1) -> Product:
    return Product(
        name=f"Product {n}",
        image_url="https://img105.savana.com/a.jpg",
        product_url=f"https://www.savana.com/details/thing-{n}",
        mrp="990",
        asp="742",
    )


def _settings(**over: object):
    settings = load_settings()
    settings.api.delay_s = 0  # tests must not sleep
    for key, value in over.items():
        setattr(settings.api, key, value)
    return settings


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def test_fills_category_and_subcategory_from_detail_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=DETAIL_TEMPLATE.format(goods_id=1, level2=11, level3=57))

    async with SavanaCategoryEnricher(
        _settings(), SavanaTaxonomy(), transport=_transport(handler)
    ) as enricher:
        [enriched] = await enricher.enrich([_product()])

    assert enriched.category == "Bags"
    assert enriched.subcategory == "Backpacks"
    assert enricher.misses == 0


async def test_preserves_order_and_the_untouched_fields() -> None:
    """Enrichment is concurrent; the stream it feeds is not allowed to reorder."""

    def handler(request: httpx.Request) -> httpx.Response:
        goods_id = int(str(request.url).rsplit("-", 1)[1])
        # Later products answer with a different category, so a reorder shows up.
        level3 = 57 if goods_id % 2 else 54
        return httpx.Response(
            200, text=DETAIL_TEMPLATE.format(goods_id=goods_id, level2=11, level3=level3)
        )

    products = [_product(n) for n in range(1, 7)]
    async with SavanaCategoryEnricher(
        _settings(), SavanaTaxonomy(), transport=_transport(handler)
    ) as enricher:
        enriched = await enricher.enrich(products)

    assert [p.name for p in enriched] == [p.name for p in products]
    assert [p.subcategory for p in enriched] == ["Backpacks", "Tote Bags"] * 3
    # Listing-API fields survive the copy.
    assert enriched[0].mrp == products[0].mrp
    assert enriched[0].image_url == products[0].image_url


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404),
        httpx.Response(500),
        httpx.Response(200, text="<html>no ssr blob here</html>"),
    ],
)
async def test_unreadable_detail_page_keeps_the_product(response: httpx.Response) -> None:
    """A product the listing already described in full is never lost to a 404."""

    def handler(request: httpx.Request) -> httpx.Response:
        return response

    async with SavanaCategoryEnricher(
        _settings(), SavanaTaxonomy(), transport=_transport(handler)
    ) as enricher:
        [enriched] = await enricher.enrich([_product()])

    assert enriched.name == "Product 1"
    assert enriched.category is None and enriched.subcategory is None
    assert enricher.misses == 1


async def test_unmapped_id_is_reported_by_the_shared_taxonomy() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=DETAIL_TEMPLATE.format(goods_id=1, level2=11, level3=987654)
        )

    taxonomy = SavanaTaxonomy()
    async with SavanaCategoryEnricher(
        _settings(), taxonomy, transport=_transport(handler)
    ) as enricher:
        [enriched] = await enricher.enrich([_product()])

    assert enriched.subcategory == "cat:987654"
    assert any("987654" in w for w in taxonomy.warnings)


@pytest.mark.parametrize(("limit", "expected_peak"), [(2, 2), (5, 5)])
async def test_concurrency_is_bounded_by_config(limit: int, expected_peak: int) -> None:
    """detail_concurrency is the promise that keeps this polite.

    The handler must actually suspend, otherwise every request completes before
    the next begins and the peak would be 1 no matter what the semaphore says.
    """
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.01)  # let siblings pile up if nothing stops them
            return httpx.Response(
                200, text=DETAIL_TEMPLATE.format(goods_id=1, level2=11, level3=57)
            )
        finally:
            in_flight -= 1

    async with SavanaCategoryEnricher(
        _settings(detail_concurrency=limit), SavanaTaxonomy(), transport=_transport(handler)
    ) as enricher:
        await enricher.enrich([_product(n) for n in range(10)])

    assert peak == expected_peak


async def test_enrich_of_empty_batch_makes_no_requests() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request should be made for an empty batch")

    async with SavanaCategoryEnricher(
        _settings(), SavanaTaxonomy(), transport=_transport(handler)
    ) as enricher:
        assert await enricher.enrich([]) == []
