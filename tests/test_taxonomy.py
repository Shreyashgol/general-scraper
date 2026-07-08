"""Tests for the savana category id → name map."""

from __future__ import annotations

import json
from pathlib import Path

from savana_scraper.services.taxonomy import SavanaTaxonomy


def test_known_ids_resolve_to_names() -> None:
    tax = SavanaTaxonomy()
    assert tax.category(11) == "Bags"
    assert tax.subcategory(57) == "Backpacks"
    assert tax.subcategory(54) == "Tote Bags"
    assert tax.warnings == []


def test_unknown_id_is_flagged_not_guessed() -> None:
    """An unmapped id must be visibly unmapped, never quietly wrong."""
    tax = SavanaTaxonomy()
    assert tax.subcategory(999_999) == "cat:999999"
    assert tax.category(888_888) == "cat:888888"

    warnings = tax.warnings
    assert len(warnings) == 2
    assert any("level3 category id 999999" in w for w in warnings)
    assert any("level2 category id 888888" in w for w in warnings)


def test_repeated_unknown_id_warns_once() -> None:
    tax = SavanaTaxonomy()
    for _ in range(5):
        tax.subcategory(999_999)
    assert len(tax.warnings) == 1


def test_missing_or_non_integer_id_yields_no_category() -> None:
    """A product page without the field is blank, and raises no warning."""
    tax = SavanaTaxonomy()
    assert tax.category(None) is None
    assert tax.subcategory("79") is None  # a string id is not a category id
    assert tax.category(True) is None  # bool is an int subclass; reject it
    assert tax.warnings == []


def test_override_file_extends_and_replaces(tmp_path: Path) -> None:
    path = tmp_path / "taxonomy.json"
    path.write_text(
        json.dumps(
            {"level2": {"11": "Handbags", "999": "Footwear"}, "level3": {"1000": "Sneakers"}}
        ),
        encoding="utf-8",
    )
    tax = SavanaTaxonomy.load(path)

    assert tax.category(11) == "Handbags"  # replaced
    assert tax.category(999) == "Footwear"  # added
    assert tax.subcategory(1000) == "Sneakers"
    assert tax.subcategory(57) == "Backpacks"  # built-in map still present
    assert tax.warnings == []


def test_unreadable_override_falls_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    assert SavanaTaxonomy.load(path).category(11) == "Bags"


def test_load_without_path_uses_defaults() -> None:
    assert SavanaTaxonomy.load(None).category(12) == "Clothing"
