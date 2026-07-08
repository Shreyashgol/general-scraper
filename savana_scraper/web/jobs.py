"""In-process job store and runner for the web platform.

A scrape can take seconds (registered adapter) or minutes (generic crawler), so
the HTTP request that starts one must not wait for it. Jobs run as asyncio tasks
against the same :class:`ScrapePipeline` the CLI uses.

Scope, stated plainly: jobs live in memory. A server restart loses them, and a
second worker process would not see them. That is the right trade for a
single-user tool; swapping this for Redis/Celery means reimplementing this one
module, nothing else.
"""

from __future__ import annotations

import asyncio
import csv
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from savana_scraper.core.config import Settings, load_settings
from savana_scraper.core.exceptions import NavigationError
from savana_scraper.core.logging import get_logger
from savana_scraper.core.robots import check as robots_check
from savana_scraper.services.pipeline import ScrapePipeline
from savana_scraper.services.registry import adapter_for_url

log = get_logger(__name__)

#: Hard ceiling on any single job, whatever the client asks for.
MAX_PRODUCTS_LIMIT = 5_000
#: Concurrent scrapes. Each generic job owns a Chromium instance, so keep it low.
MAX_CONCURRENT_JOBS = 2
#: Rows returned to the UI as a preview table.
PREVIEW_ROWS = 10


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    BLOCKED = "blocked"  # robots.txt disallows this URL


@dataclass
class Job:
    """One scrape request and everything the UI needs to render its state."""

    id: str
    url: str
    status: JobStatus = JobStatus.QUEUED
    max_products: int = 200
    # Which source handled it, and what the generic crawler inferred.
    source: str = ""
    adapter: str | None = None
    detected_pattern: str | None = None
    # Live counters, updated by the pipeline's progress hook.
    discovered: int = 0
    exported: int = 0
    failed: int = 0
    invalid: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    created_at: str = ""
    finished_at: str | None = None
    preview: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["has_csv"] = self.status is JobStatus.DONE and self.exported > 0
        return data


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class JobStore:
    """Creates, runs, and reads back scrape jobs."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or load_settings()
        self._jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
        self._csv_dir = self._settings.output_dir / "jobs"

    # ------------------------------------------------------------------ #
    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def csv_path(self, job_id: str) -> Path:
        return self._csv_dir / f"{job_id}.csv"

    async def create(self, url: str, max_products: int, ignore_robots: bool) -> Job:
        """Register a job and schedule it. Returns immediately."""
        job = Job(
            id=uuid.uuid4().hex[:12],
            url=url,
            max_products=min(max(max_products, 1), MAX_PRODUCTS_LIMIT),
            adapter=adapter_for_url(url),
            created_at=_now(),
        )
        self._jobs[job.id] = job

        if not ignore_robots:
            verdict = await robots_check(url, self._settings.user_agent)
            if not verdict.allowed:
                job.status = JobStatus.BLOCKED
                job.error = verdict.reason
                job.finished_at = _now()
                log.warning("Job %s blocked: %s", job.id, verdict.reason)
                return job
        else:
            job.warnings.append("robots.txt check skipped at the caller's request.")

        self._tasks[job.id] = asyncio.create_task(self._run(job))
        return job

    # ------------------------------------------------------------------ #
    async def _run(self, job: Job) -> None:
        async with self._semaphore:
            job.status = JobStatus.RUNNING
            log.info("Job %s started — %s", job.id, job.url)

            def on_progress(discovered: int, exported: int) -> None:
                job.discovered = discovered
                job.exported = exported

            settings = self._settings.model_copy(deep=True)
            settings.max_products = job.max_products
            self._csv_dir.mkdir(parents=True, exist_ok=True)

            pipeline = ScrapePipeline(
                settings,
                output_path=self.csv_path(job.id),
                on_progress=on_progress,
            )
            try:
                # resume=False: each job is a fresh scrape. Sharing the CLI's
                # checkpoint would make a repeat job for the same URL export
                # nothing, because every product was "already processed".
                report = await pipeline.run(job.url, resume=False)
            except NavigationError as e:
                # A failed seed navigation is the common "site blocks automation"
                # case (HTTP/2 resets, timeouts). Say so plainly rather than
                # leaking a Chromium error code — and never as a "done, 0 products".
                job.status = JobStatus.ERROR
                job.error = (
                    "Could not load the page. The site likely blocks automated "
                    "browsers, or the URL is unreachable. "
                    f"(details: {e})"
                )
                job.finished_at = _now()
                log.warning("Job %s could not load %s: %s", job.id, job.url, e)
                return
            except Exception as e:  # noqa: BLE001 - a job must never kill the server
                job.status = JobStatus.ERROR
                job.error = f"{type(e).__name__}: {e}"
                job.finished_at = _now()
                log.exception("Job %s failed", job.id)
                return

            job.source = report.source
            job.detected_pattern = report.detected_pattern
            job.discovered = report.discovered
            job.exported = report.exported
            job.failed = report.failed
            job.invalid = report.invalid
            job.warnings.extend(report.warnings)
            job.preview = self._read_preview(self.csv_path(job.id))
            job.status = JobStatus.DONE
            job.finished_at = _now()

            if job.exported == 0 and not job.warnings:
                job.warnings.append("The scrape completed but found no products.")
            log.info("Job %s done — %d products", job.id, job.exported)

    @staticmethod
    def _read_preview(path: Path) -> list[dict[str, str]]:
        if not path.exists():
            return []
        try:
            with path.open(newline="", encoding="utf-8") as fh:
                return [
                    row for _, row in zip(range(PREVIEW_ROWS), csv.DictReader(fh), strict=False)
                ]
        except OSError as e:
            log.warning("Could not read preview from %s: %s", path, e)
            return []
