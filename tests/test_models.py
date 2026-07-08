"""Tests for the Product / ProductRef domain models."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from savana_scraper.models import Product, ProductRef
from savana_scraper.models.product import CSV_FIELDS


def test_product_to_row_field_order_and_formatting() -> None:
    p = Product(
        name="  Cool Shirt  ",
        image_url="https://cdn.savana.com/img/1.jpg",
        product_url="https://www.savana.com/product/1",
        mrp=Decimal("1299"),
        asp=Decimal("999.5"),
        category="Clothing",
        subcategory="T-Shirts",
    )
    row = p.to_row()
    assert list(row.keys()) == [
        "name",
        "category",
        "subcategory",
        "image_url",
        "mrp",
        "asp",
        "product_url",
    ]
    # The exporter writes CSV_FIELDS; a row that disagrees would silently drop
    # or blank a column.
    assert list(row.keys()) == list(CSV_FIELDS)
    assert row["name"] == "Cool Shirt"  # stripped
    assert row["category"] == "Clothing"
    assert row["subcategory"] == "T-Shirts"
    assert row["mrp"] == "1299.00"
    assert row["asp"] == "999.50"


def test_product_missing_price_is_empty_string() -> None:
    p = Product(
        name="X",
        image_url="https://cdn.savana.com/x.jpg",
        product_url="https://www.savana.com/product/x",
    )
    assert p.to_row()["mrp"] == ""
    assert p.to_row()["asp"] == ""


def test_product_without_taxonomy_exports_empty_category_columns() -> None:
    """A site that publishes no taxonomy yields blanks, never a guessed label."""
    p = Product(
        name="Slit Bodycon Dress",
        image_url="https://cdn.savana.com/x.jpg",
        product_url="https://www.savana.com/product/x",
    )
    assert p.category is None and p.subcategory is None
    assert p.to_row()["category"] == ""
    assert p.to_row()["subcategory"] == ""


def test_blank_name_rejected() -> None:
    with pytest.raises(ValidationError):
        Product(
            name="   ",
            image_url="https://cdn.savana.com/x.jpg",
            product_url="https://www.savana.com/p/x",
        )


def test_negative_price_rejected() -> None:
    with pytest.raises(ValidationError):
        Product(
            name="X",
            image_url="https://cdn.savana.com/x.jpg",
            product_url="https://www.savana.com/p/x",
            asp=Decimal("-5"),
        )


def test_key_ignores_query_and_trailing_slash() -> None:
    a = ProductRef(product_url="https://www.savana.com/product/1/")
    b = ProductRef(product_url="https://www.savana.com/product/1?ref=abc")
    assert a.key() == b.key()


def test_product_and_ref_keys_agree() -> None:
    ref = ProductRef(product_url="https://www.savana.com/product/42")
    prod = Product(
        name="Y",
        image_url="https://cdn.savana.com/y.jpg",
        product_url="https://www.savana.com/product/42",
    )
    assert ref.key() == prod.key()
