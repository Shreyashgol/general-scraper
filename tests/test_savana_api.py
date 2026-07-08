"""Tests for the goods-flow JSON API source."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest

from savana_scraper.core.config import load_settings
from savana_scraper.core.exceptions import ApiError
from savana_scraper.services.savana_api import (
    SavanaApiClient,
    flow_id_from_url,
    goods_to_product,
    product_url_for,
    slugify,
)


def _goods(goods_id: int, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "goodsId": goods_id,
        "goodsName": "Charm Totes Bag",
        "salePrice": "990",
        "promotePrice": "742",
        "imageList": [
            {"select": False, "goodsThumb": "https://img105.savana.com/a.jpg"},
            {"select": True, "goodsThumb": "https://img105.savana.com/b.jpg"},
        ],
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# URL / field mapping
# --------------------------------------------------------------------------- #
def test_flow_id_from_url() -> None:
    assert flow_id_from_url("https://www.savana.com/activity/13070") == "13070"
    assert flow_id_from_url("https://www.savana.com/activity/13070?x=1") == "13070"
    assert flow_id_from_url("https://www.savana.com/details/bag-1") is None


def test_slugify() -> None:
    assert slugify("Charm Totes Bag") == "charm-totes-bag"
    assert slugify("  Kitty  School/Backpack ") == "kitty-school-backpack"
    assert slugify("!!!") == ""


def test_product_url_for_falls_back_to_bare_id() -> None:
    url = product_url_for(_goods(1, goodsName="!!!"), "https://www.savana.com")
    assert url == "https://www.savana.com/details/1"


def test_goods_to_product_maps_prices_and_prefers_selected_image() -> None:
    product = goods_to_product(_goods(1949872), "https://www.savana.com")
    assert product is not None
    assert product.name == "Charm Totes Bag"
    # salePrice -> MRP, promotePrice -> ASP.
    assert product.mrp == Decimal("990")
    assert product.asp == Decimal("742")
    assert str(product.image_url) == "https://img105.savana.com/b.jpg"
    assert str(product.product_url) == "https://www.savana.com/details/charm-totes-bag-1949872"


def test_goods_to_product_without_promotion_uses_sale_price_for_asp() -> None:
    product = goods_to_product(_goods(1, promotePrice=None), "https://www.savana.com")
    assert product is not None
    assert product.mrp == product.asp == Decimal("990")


def test_goods_to_product_ignores_promotion_above_sale_price() -> None:
    product = goods_to_product(_goods(1, promotePrice="1200"), "https://www.savana.com")
    assert product is not None
    assert product.asp == Decimal("990")


@pytest.mark.parametrize(
    "override",
    [{"goodsName": ""}, {"imageList": []}, {"imageList": None}],
)
def test_goods_to_product_returns_none_when_unusable(override: dict[str, Any]) -> None:
    assert goods_to_product(_goods(1, **override), "https://www.savana.com") is None


# --------------------------------------------------------------------------- #
# Pagination — the stateful contract
# --------------------------------------------------------------------------- #
def _client_with(handler: Any, settings: Any = None) -> SavanaApiClient:
    return SavanaApiClient(settings or load_settings(), transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_iter_goods_feeds_visited_ids_back_and_drains_all_pages() -> None:
    """The server pages off visitedGoodsIdList; assert we actually send it."""
    sent_visited: list[list[int]] = []
    all_ids = list(range(1, 46))  # 45 goods over 3 pages of 20/20/5

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        visited = body["visitedGoodsIdList"]
        sent_visited.append(list(visited))
        remaining = [i for i in all_ids if i not in set(visited)]
        page = remaining[:20]
        return httpx.Response(
            200,
            json={
                "ret": 200,
                "data": {
                    "goodsList": [_goods(i) for i in page],
                    "hasNextPage": len(remaining) > 20,
                },
            },
        )

    async with _client_with(handler) as client:
        got = [g["goodsId"] async for g in client.iter_goods("13070")]

    assert got == all_ids
    # First request sends an empty visited list, later ones send what we've seen.
    assert sent_visited[0] == []
    assert sent_visited[1] == all_ids[:20]
    assert sent_visited[2] == all_ids[:40]


@pytest.mark.asyncio
async def test_iter_goods_stops_when_server_repeats_itself() -> None:
    """A server that ignores visitedGoodsIdList must not spin forever."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        # Always the same page, always claiming there is more.
        return httpx.Response(
            200,
            json={"ret": 200, "data": {"goodsList": [_goods(1), _goods(2)], "hasNextPage": True}},
        )

    async with _client_with(handler) as client:
        got = [g["goodsId"] async for g in client.iter_goods("13070")]

    assert got == [1, 2]
    assert calls == 2  # first page, then one repeat that trips the guard


@pytest.mark.asyncio
async def test_iter_goods_honours_max_pages_per_listing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        visited = set(json.loads(request.content)["visitedGoodsIdList"])
        page = [i for i in range(1, 101) if i not in visited][:20]
        return httpx.Response(
            200,
            json={
                "ret": 200,
                "data": {"goodsList": [_goods(i) for i in page], "hasNextPage": True},
            },
        )

    settings = load_settings()
    settings.api.max_pages_per_listing = 2
    async with _client_with(handler, settings) as client:
        got = [g["goodsId"] async for g in client.iter_goods("13070")]
    assert len(got) == 40


@pytest.mark.asyncio
async def test_iter_goods_raises_on_api_error_ret() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ret": 500, "msg": "boom"})

    async with _client_with(handler) as client:
        with pytest.raises(ApiError, match="ret=500"):
            [g async for g in client.iter_goods("13070")]


# --------------------------------------------------------------------------- #
# Listing harvest
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_harvest_listings_dedupes_and_puts_seed_first() -> None:
    html = '<a href="/activity/222">a</a><a href="/activity/111">b</a><a href="/activity/222">c</a>'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    async with _client_with(handler) as client:
        urls = await client.harvest_listings("https://www.savana.com/activity/111")

    assert urls == [
        "https://www.savana.com/activity/111",
        "https://www.savana.com/activity/222",
    ]


@pytest.mark.asyncio
async def test_harvest_listings_raises_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with _client_with(handler) as client:
        with pytest.raises(ApiError, match="Could not fetch seed page"):
            await client.harvest_listings("https://www.savana.com/")
