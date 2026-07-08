"""Tests for the validation service."""

from __future__ import annotations

from decimal import Decimal

from savana_scraper.models import Product
from savana_scraper.services.validator import Validator


def _product(**kw: object) -> Product:
    base: dict[str, object] = {
        "name": "Shirt",
        "image_url": "https://cdn.savana.com/x.jpg",
        "product_url": "https://www.savana.com/product/1",
        "asp": Decimal("100"),
    }
    base.update(kw)
    return Product(**base)  # type: ignore[arg-type]


def test_valid_product_passes() -> None:
    outcome = Validator().validate(_product(mrp=Decimal("150")))
    assert outcome.ok
    assert not outcome.warnings


def test_no_price_fails() -> None:
    outcome = Validator().validate(_product(asp=None, mrp=None))
    assert not outcome.ok
    assert any("no price" in e for e in outcome.errors)


def test_asp_above_mrp_warns_but_passes() -> None:
    outcome = Validator().validate(_product(mrp=Decimal("50"), asp=Decimal("100")))
    assert outcome.ok
    assert any("swapped" in w for w in outcome.warnings)


def test_zero_price_warns() -> None:
    outcome = Validator().validate(_product(asp=Decimal("0"), mrp=Decimal("100")))
    assert outcome.ok
    assert any("zero" in w for w in outcome.warnings)
