"""Shared test fixtures and path setup."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the package is importable even without an editable install.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from savana_scraper.core.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointed at a temp dir so tests never touch real outputs."""
    return Settings(
        output_dir=tmp_path / "outputs",
        state_dir=tmp_path / "state",
        request_delay_s=0.0,
    )
