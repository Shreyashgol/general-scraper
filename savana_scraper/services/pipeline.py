"""The scrape pipeline — orchestration (Application layer).

Wires the phases together:
    stream (source, resume-filtered) → validate → export

It depends only on the :class:`ProductSource` abstraction plus the storage and
validation services, so swapping the data source (JSON API vs. browser) or the
output format never touches this file.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from savana_scraper.core.config import Settings
from savana_scraper.core.logging import get_logger
from savana_scraper.models import Product
from savana_scraper.services.adapter import ProductSource
from savana_scraper.services.exporter import CsvExporter
from savana_scraper.services.registry import source_for_url
from savana_scraper.services.validator import Validator
from savana_scraper.storage.state import RunState

log = get_logger(__name__)

#: Called with (discovered, exported) after each product; used for live progress.
ProgressHook = Callable[[int, int], None]

# Long crawls checkpoint to disk this often, so a crash at product 900 of 1200
# still leaves a usable CSV and a resumable state file.
CHECKPOINT_EVERY = 100


@dataclass
class RunReport:
    """Summary statistics for a completed run."""

    category_url: str
    output_path: Path | None = None
    source: str = ""
    detected_pattern: str | None = None
    discovered: int = 0
    exported: int = 0
    skipped_resume: int = 0
    duplicates: int = 0
    failed: int = 0
    invalid: int = 0
    warnings: list[str] = field(default_factory=list)


class ScrapePipeline:
    """Coordinates a single scrape end to end."""

    def __init__(
        self,
        settings: Settings,
        source: ProductSource | None = None,
        *,
        output_path: Path | None = None,
        on_progress: ProgressHook | None = None,
    ) -> None:
        self._settings = settings
        self._validator = Validator()
        # Source selection depends on the URL, so it is resolved in run() unless
        # the caller injected one (tests, or an explicit override).
        self._source = source
        self._output_path_override = output_path
        self._on_progress = on_progress

    async def run(self, category_url: str, *, resume: bool = True) -> RunReport:
        settings = self._settings
        settings.ensure_dirs()
        report = RunReport(category_url=category_url)

        source = self._source or source_for_url(category_url, settings)
        report.source = source.name

        state = RunState.load_or_create(settings.state_dir, category_url)
        if not resume:
            state.clear()

        output_path = self._output_path_override or self._timestamped_path()
        exporter = CsvExporter(output_path)
        report.output_path = output_path
        cap = settings.max_products

        log.info("Source: [bold]%s[/] (cap=%s)", source.name, cap or "unlimited")
        last_checkpoint = 0
        async for product in source.stream(category_url, skip=state.is_processed):
            report.discovered += 1
            self._process_one(product, exporter, state, report)
            if self._on_progress is not None:
                self._on_progress(report.discovered, exporter.count)

            if exporter.count - last_checkpoint >= CHECKPOINT_EVERY:
                last_checkpoint = exporter.count
                self._checkpoint(exporter, state)
            if cap and exporter.count >= cap:
                log.info("Reached max_products cap (%d)", cap)
                break

        # Failures/skips are counted by the source, which owns the work they name.
        report.failed = source.stats.failed
        report.skipped_resume = source.stats.skipped_resume
        report.exported = exporter.count
        # The generic source reports what it inferred and where it was unsure.
        report.detected_pattern = getattr(source, "detected_pattern", None)
        report.warnings.extend(getattr(source, "warnings", []))

        exporter.flush()
        state.save()
        self._log_summary(report)
        return report

    # ------------------------------------------------------------------ #
    def _process_one(
        self,
        product: Product,
        exporter: CsvExporter,
        state: RunState,
        report: RunReport,
    ) -> None:
        outcome = self._validator.validate(product)
        report.warnings.extend(outcome.warnings)
        if not outcome.ok:
            report.invalid += 1
            log.warning("Invalid product %s: %s", product.product_url, "; ".join(outcome.errors))
            return

        if exporter.add(product):
            log.debug("[green]✓[/] %s", product.name)
        else:
            report.duplicates += 1
        # Mark processed only after a successful, valid extraction so failures
        # are retried on the next run (resume correctness).
        state.mark_processed(product.key())

    @staticmethod
    def _checkpoint(exporter: CsvExporter, state: RunState) -> None:
        """Persist partial progress mid-run; both writes are atomic."""
        exporter.flush()
        state.save()
        log.info("Checkpoint — %d products written so far", exporter.count)

    def _timestamped_path(self) -> Path:
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        return self._settings.output_dir / f"savana_products_{ts}.csv"

    def _log_summary(self, report: RunReport) -> None:
        log.info(
            "[bold]Run complete[/] — discovered=%d exported=%d "
            "skipped(resume)=%d duplicates=%d invalid=%d failed=%d",
            report.discovered,
            report.exported,
            report.skipped_resume,
            report.duplicates,
            report.invalid,
            report.failed,
        )
