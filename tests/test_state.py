"""Tests for the resumable run-state store."""

from __future__ import annotations

from pathlib import Path

from savana_scraper.storage.state import RunState, run_id_for

CATEGORY = "https://www.savana.com/collections/men"


def test_run_id_is_deterministic() -> None:
    assert run_id_for(CATEGORY) == run_id_for(CATEGORY)
    assert run_id_for(CATEGORY) != run_id_for(CATEGORY + "x")


def test_mark_and_persist_roundtrip(tmp_path: Path) -> None:
    state = RunState.load_or_create(tmp_path, CATEGORY)
    state.mark_processed("k1")
    state.mark_processed("k2")
    state.save()

    reloaded = RunState.load_or_create(tmp_path, CATEGORY)
    assert reloaded.is_processed("k1")
    assert reloaded.is_processed("k2")
    assert reloaded.processed_count == 2


def test_clear_removes_checkpoint(tmp_path: Path) -> None:
    state = RunState.load_or_create(tmp_path, CATEGORY)
    state.mark_processed("k1")
    state.save()
    state.clear()

    reloaded = RunState.load_or_create(tmp_path, CATEGORY)
    assert reloaded.processed_count == 0
