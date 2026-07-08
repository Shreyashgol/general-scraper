"""Resumable run state (Operational Green Flag: "Resume").

A run's progress is checkpointed to a small JSON file keyed by a run id derived
from the category URL. On restart we reload the set of already-processed product
keys and skip them, so an interrupted run continues instead of starting over.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import suppress
from pathlib import Path

from savana_scraper.core.logging import get_logger

log = get_logger(__name__)


def run_id_for(category_url: str) -> str:
    """Deterministic, filesystem-safe id for a category URL."""
    digest = hashlib.sha1(category_url.encode("utf-8")).hexdigest()[:12]
    return f"run_{digest}"


class RunState:
    """Tracks processed product keys and persists them to disk."""

    def __init__(self, path: Path, category_url: str) -> None:
        self._path = path
        self._category_url = category_url
        self._processed: set[str] = set()

    @classmethod
    def load_or_create(cls, state_dir: Path, category_url: str) -> RunState:
        path = state_dir / f"{run_id_for(category_url)}.json"
        state = cls(path, category_url)
        if path.exists():
            state._restore()
        return state

    def _restore(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._processed = set(data.get("processed", []))
            if self._processed:
                log.info(
                    "[yellow]Resuming[/] run %s — %d products already done",
                    self._path.stem,
                    len(self._processed),
                )
        except (OSError, json.JSONDecodeError) as e:  # pragma: no cover
            log.warning("Could not read state %s (%s); starting fresh", self._path, e)
            self._processed = set()

    def is_processed(self, key: str) -> bool:
        return key in self._processed

    def mark_processed(self, key: str) -> None:
        self._processed.add(key)

    @property
    def processed_count(self) -> int:
        return len(self._processed)

    def save(self) -> None:
        """Persist current progress (best-effort, atomic)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "category_url": self._category_url,
            "processed": sorted(self._processed),
        }
        tmp = self._path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as e:  # pragma: no cover
            log.warning("Could not save state %s: %s", self._path, e)

    def clear(self) -> None:
        """Delete the checkpoint (used when a run completes cleanly)."""
        self._processed.clear()
        with suppress(OSError):  # pragma: no cover
            self._path.unlink(missing_ok=True)
