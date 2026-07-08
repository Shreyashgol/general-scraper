"""Tests for the CSV exporter."""

from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

from savana_scraper.models import Product
from savana_scraper.models.product import CSV_FIELDS
from savana_scraper.services.exporter import CsvExporter


def _product(pid: str, name: str = "Shirt") -> Product:
    return Product(
        name=name,
        image_url=f"https://cdn.savana.com/{pid}.jpg",
        product_url=f"https://www.savana.com/product/{pid}",
        mrp=Decimal("150"),
        asp=Decimal("100"),
    )


def test_export_writes_header_and_rows(tmp_path: Path) -> None:
    out = tmp_path / "out.csv"
    exp = CsvExporter(out)
    exp.add(_product("1"))
    exp.add(_product("2"))
    exp.flush()

    with out.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0].keys()) == list(CSV_FIELDS)
    assert len(rows) == 2
    assert rows[0]["mrp"] == "150.00"


def test_duplicates_are_skipped(tmp_path: Path) -> None:
    exp = CsvExporter(tmp_path / "out.csv")
    assert exp.add(_product("1")) is True
    assert exp.add(_product("1", name="Other")) is False  # same URL key
    assert exp.count == 1


def test_flush_is_atomic_no_tmp_left(tmp_path: Path) -> None:
    exp = CsvExporter(tmp_path / "out.csv")
    exp.add(_product("1"))
    exp.flush()
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []
