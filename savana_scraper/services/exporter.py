"""CSV exporter (Infrastructure/storage concern).

Writes products to CSV with a stable column order and in-process de-duplication.
The write is atomic (temp file + os.replace) so a crash mid-write never leaves a
half-written or corrupt CSV behind.
"""

from __future__ import annotations

import csv
import os
import tempfile
from pathlib import Path

from savana_scraper.core.exceptions import ExportError
from savana_scraper.core.logging import get_logger
from savana_scraper.models import Product
from savana_scraper.models.product import CSV_FIELDS

log = get_logger(__name__)


class CsvExporter:
    """Accumulates unique products and writes them atomically to a CSV file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._rows: list[dict[str, str]] = []
        self._seen: set[str] = set()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def count(self) -> int:
        return len(self._rows)

    def add(self, product: Product) -> bool:
        """Add a product. Returns ``False`` if it was a duplicate (skipped)."""
        key = product.key()
        if key in self._seen:
            return False
        self._seen.add(key)
        self._rows.append(product.to_row())
        return True

    def add_many(self, products: list[Product]) -> int:
        """Add several products; returns how many were newly added."""
        return sum(1 for p in products if self.add(p))

    def flush(self) -> Path:
        """Write all accumulated rows to disk atomically and return the path."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=self._path.parent, prefix=f".{self._path.name}", suffix=".tmp"
            )
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(CSV_FIELDS))
                writer.writeheader()
                writer.writerows(self._rows)
            os.replace(tmp_name, self._path)
        except OSError as e:
            raise ExportError(f"Failed to write CSV to {self._path}: {e}") from e
        log.info("[green]Exported[/] %d products → %s", len(self._rows), self._path)
        return self._path
