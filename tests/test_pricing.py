"""Tests for price parsing — the trickiest shared helper."""

from __future__ import annotations

from decimal import Decimal

import pytest

from savana_scraper.services.pricing import parse_price


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("₹1,299.00", Decimal("1299.00")),
        ("$1,299", Decimal("1299")),
        ("1299", Decimal("1299")),
        ("Rs. 2.499,50", Decimal("2499.50")),  # European grouping
        ("999,99", Decimal("999.99")),  # comma decimal
        ("1,299", Decimal("1299")),  # comma thousands
        ("MRP 499", Decimal("499")),
        (1299, Decimal("1299")),
        (1299.5, Decimal("1299.5")),
        ("Sale price: 79.99 only", Decimal("79.99")),
    ],
)
def test_parse_price_valid(raw: object, expected: Decimal) -> None:
    assert parse_price(raw) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize("raw", [None, "", "free", "N/A", "---"])
def test_parse_price_none(raw: object) -> None:
    assert parse_price(raw) is None  # type: ignore[arg-type]
