"""Tests for the FastAPI surface — validation, the robots gate, and job lookup.

The scrape itself is never run here: :class:`JobStore` is exercised with a stub
robots check, and the pipeline is not reached.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from savana_scraper.core.robots import RobotsVerdict
from savana_scraper.web import app as web_app
from savana_scraper.web.jobs import JobStatus


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A client whose jobs never actually scrape."""

    async def never_run(self: Any, job: Any) -> None:
        return None

    monkeypatch.setattr(web_app.store, "_run", never_run.__get__(web_app.store))
    web_app.store._jobs.clear()  # noqa: SLF001 - module-level singleton
    return TestClient(web_app.app)


def _allow(monkeypatch: pytest.MonkeyPatch) -> None:
    async def allowed(url: str, ua: str) -> RobotsVerdict:
        return RobotsVerdict(True, "allowed by robots.txt")

    monkeypatch.setattr("savana_scraper.web.jobs.robots_check", allowed)


def _deny(monkeypatch: pytest.MonkeyPatch) -> None:
    async def denied(url: str, ua: str) -> RobotsVerdict:
        return RobotsVerdict(False, "robots.txt disallows this user-agent")

    monkeypatch.setattr("savana_scraper.web.jobs.robots_check", denied)


def test_health(client: TestClient) -> None:
    assert client.get("/api/health").json()["status"] == "ok"


@pytest.mark.parametrize("bad_url", ["ftp://x.com/a", "not-a-url", "", "/relative"])
def test_rejects_non_http_urls(client: TestClient, bad_url: str) -> None:
    assert client.post("/api/jobs", json={"url": bad_url}).status_code == 422


def test_rejects_max_products_over_the_ceiling(client: TestClient) -> None:
    body = {"url": "https://example.com/", "max_products": 10_000}
    assert client.post("/api/jobs", json=body).status_code == 422


def test_robots_disallow_blocks_the_job(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _deny(monkeypatch)
    job = client.post("/api/jobs", json={"url": "https://example.com/"}).json()
    assert job["status"] == JobStatus.BLOCKED
    assert "disallows" in job["error"]
    assert job["has_csv"] is False


def test_ignore_robots_bypasses_the_gate_and_warns(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _deny(monkeypatch)
    body = {"url": "https://example.com/", "ignore_robots": True}
    job = client.post("/api/jobs", json=body).json()
    assert job["status"] != JobStatus.BLOCKED
    assert any("robots.txt check skipped" in w for w in job["warnings"])


def test_registered_domain_is_reported_on_the_job(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _allow(monkeypatch)
    job = client.post("/api/jobs", json={"url": "https://www.savana.com/"}).json()
    assert job["adapter"] == "savana.com"

    job = client.post("/api/jobs", json={"url": "https://books.toscrape.com/"}).json()
    assert job["adapter"] is None


def test_unknown_job_is_404(client: TestClient) -> None:
    assert client.get("/api/jobs/deadbeef").status_code == 404
    assert client.get("/api/jobs/deadbeef/csv").status_code == 404


def test_csv_download_conflicts_while_job_is_unfinished(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _allow(monkeypatch)
    job = client.post("/api/jobs", json={"url": "https://example.com/"}).json()
    response = client.get(f"/api/jobs/{job['id']}/csv")
    assert response.status_code == 409
    assert "not done" in response.json()["detail"]


async def test_navigation_failure_marks_job_error_not_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocked/unreachable seed must surface as `error`, never `done, 0`.

    Regression for the Myntra case, where a failed navigation was laundered into a
    successful empty run.
    """
    from savana_scraper.core.exceptions import NavigationError
    from savana_scraper.web.jobs import Job, JobStatus, JobStore

    async def boom(self: Any, url: str, *, resume: bool = True) -> Any:
        raise NavigationError("Failed to navigate: net::ERR_HTTP2_PROTOCOL_ERROR")

    monkeypatch.setattr("savana_scraper.services.pipeline.ScrapePipeline.run", boom)

    store = JobStore()
    job = Job(id="test1", url="https://blocked.example/shop")
    store._jobs[job.id] = job  # noqa: SLF001
    await store._run(job)  # noqa: SLF001

    assert job.status is JobStatus.ERROR
    assert job.exported == 0
    assert job.error is not None and "blocks automated browsers" in job.error
    assert job.to_dict()["has_csv"] is False
