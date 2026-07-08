"""FastAPI application — the scrape platform's HTTP surface.

    POST /api/jobs           start a scrape, returns a job immediately
    GET  /api/jobs           list jobs, newest first
    GET  /api/jobs/{id}      poll status, live counts, warnings, preview rows
    GET  /api/jobs/{id}/csv  download the result

The SPA build (if present at ``frontend/dist``) is served from ``/``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from savana_scraper import __version__
from savana_scraper.core.logging import configure_logging
from savana_scraper.web.jobs import MAX_PRODUCTS_LIMIT, JobStatus, JobStore

configure_logging("INFO")

FRONTEND_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"

app = FastAPI(title="Scrape Platform", version=__version__)

# The SPA is served from a different origin in development (Vite on :5173).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

store = JobStore()


class ScrapeRequest(BaseModel):
    url: str = Field(..., description="A product or listing URL to scrape.")
    max_products: int = Field(200, ge=1, le=MAX_PRODUCTS_LIMIT)
    ignore_robots: bool = Field(
        False,
        description="Scrape even if robots.txt disallows it. Requires authorization.",
    )

    @field_validator("url")
    @classmethod
    def _must_be_http_url(cls, v: str) -> str:
        parsed = urlparse(v.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("url must be an absolute http(s) URL")
        return v.strip()


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.post("/api/jobs", status_code=202)
async def create_job(request: ScrapeRequest) -> dict[str, Any]:
    job = await store.create(request.url, request.max_products, request.ignore_robots)
    return job.to_dict()


@app.get("/api/jobs")
def list_jobs() -> list[dict[str, Any]]:
    return [job.to_dict() for job in store.list_jobs()]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job")
    return job.to_dict()


@app.get("/api/jobs/{job_id}/csv")
def download_csv(job_id: str) -> FileResponse:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job")
    if job.status is not JobStatus.DONE:
        raise HTTPException(status_code=409, detail=f"Job is {job.status.value}, not done")

    path = store.csv_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="This job produced no CSV")

    host = (urlparse(job.url).hostname or "products").replace(".", "_")
    return FileResponse(path, media_type="text/csv", filename=f"{host}_{job_id}.csv")


# Mounted last so it never shadows /api/*.
if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="spa")
