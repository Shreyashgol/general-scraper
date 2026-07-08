"""Tests for the Savana SSR-JSON extraction strategy."""

from __future__ import annotations

from decimal import Decimal

from bs4 import BeautifulSoup

from savana_scraper.services.savana_ssr import SavanaSsrStrategy
from savana_scraper.services.taxonomy import SavanaTaxonomy

PAGE_URL = "https://www.savana.com/details/a-line-dress-1863632"

# A trimmed but structurally faithful slice of the real SSR payload, including
# a decoy brace inside a string value to exercise the string-aware scanner.
DETAIL_HTML = """
<html><body><script>
window.__cache = {"/n/api/trade/intention/item/detail":{"isWish":false,
"goodsId":1863632,"goodsName":"A-Line Dress","shortGoodsName":"A-Line Dress",
"isPromotion":true,"salesPrice":1490,"salesPriceText":"₹1,490",
"tooltip":"buy 1 get 1 {free}","promotePrice":1341,"discountValue":10,
"level1CatId":2,"level2CatId":12,"level3CatId":79,"level4CatId":521,
"priceMrpText":"MRP","images":[{"picId":1,"picThumb":"https://img105.savana.com/goods-pic/x_w1440_q90"}]}}
</script></body></html>
"""


def _parse(html: str):
    return SavanaSsrStrategy().parse(BeautifulSoup(html, "lxml"), html, PAGE_URL)


def test_extracts_all_fields_from_ssr_json() -> None:
    fs = _parse(DETAIL_HTML)
    assert fs.name == "A-Line Dress"
    assert fs.image_url == "https://img105.savana.com/goods-pic/x_w1440_q90"
    assert fs.mrp == Decimal("1490")  # salesPrice == MRP
    assert fs.asp == Decimal("1341")  # promotePrice == selling price
    assert fs.is_complete()


def test_maps_level2_and_level3_cat_ids_to_names() -> None:
    """level1 (the storefront) and level4 (too fine) are deliberately unused."""
    fs = _parse(DETAIL_HTML)
    assert fs.category == "Clothing"  # level2CatId 12
    assert fs.subcategory == "Dresses"  # level3CatId 79


def test_unmapped_cat_id_is_marked_and_reported() -> None:
    html = DETAIL_HTML.replace('"level3CatId":79', '"level3CatId":424242')
    taxonomy = SavanaTaxonomy()
    fs = SavanaSsrStrategy(taxonomy).parse(BeautifulSoup(html, "lxml"), html, PAGE_URL)

    assert fs.subcategory == "cat:424242"
    assert any("424242" in w for w in taxonomy.warnings)


def test_detail_without_cat_ids_leaves_categories_empty() -> None:
    html = DETAIL_HTML.replace('"level2CatId":12,', "").replace('"level3CatId":79,', "")
    fs = _parse(html)
    assert fs.category is None and fs.subcategory is None
    assert fs.is_complete()  # still a perfectly good product


def test_no_promotion_falls_back_to_sales_price() -> None:
    html = DETAIL_HTML.replace('"promotePrice":1341,', '"promotePrice":null,')
    fs = _parse(html)
    assert fs.mrp == Decimal("1490")
    assert fs.asp == Decimal("1490")


def test_missing_detail_returns_empty() -> None:
    fs = _parse("<html><body>no ssr here</body></html>")
    assert not fs.is_complete()
    assert fs.name is None
