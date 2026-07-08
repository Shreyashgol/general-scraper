"""Command-line interface (Presentation layer)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from savana_scraper import __version__
from savana_scraper.core.config import load_settings
from savana_scraper.core.logging import configure_logging
from savana_scraper.services.pipeline import RunReport, ScrapePipeline

app = typer.Typer(
    name="savana-scraper",
    help="Modular, reliable product scraper for savana.com.",
    add_completion=False,
    no_args_is_help=True,
)
console = Console()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"savana-scraper {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    _version: bool | None = typer.Option(
        None, "--version", callback=_version_callback, is_eager=True, help="Show version."
    ),
) -> None:
    """Savana Scraper."""


@app.command()
def scrape(
    category_url: str = typer.Argument(
        "https://www.savana.com/",
        help="Seed URL. A listing page, or the homepage to crawl the whole site.",
    ),
    output_dir: Path | None = typer.Option(
        None, "--output-dir", "-o", help="Directory for the CSV output."
    ),
    source: str = typer.Option(
        "auto",
        "--source",
        help="'auto' (registry lookup), 'api', 'browser', or 'generic' (any site).",
    ),
    no_crawl: bool = typer.Option(
        False, "--no-crawl", help="Only scrape the seed URL, don't follow other listings."
    ),
    max_listings: int = typer.Option(
        0, "--max-listings", help="Cap how many listings to drain (0 = unlimited)."
    ),
    headed: bool = typer.Option(
        False, "--headed", help="Show the browser window (browser source only)."
    ),
    max_products: int = typer.Option(
        0, "--max-products", "-n", help="Cap products scraped (0 = unlimited)."
    ),
    no_resume: bool = typer.Option(
        False, "--no-resume", help="Ignore any saved checkpoint and start fresh."
    ),
    log_level: str = typer.Option("INFO", "--log-level", help="DEBUG/INFO/WARNING/ERROR."),
) -> None:
    """Scrape products from savana.com into a CSV.

    By default this crawls every listing linked from the seed URL via the site's
    JSON API, which reaches thousands of products in minutes.
    """
    configure_logging(log_level)

    if source not in ("auto", "api", "browser", "generic"):
        console.print(f"[red]Unknown --source {source!r}.[/]")
        raise typer.Exit(code=2)

    overrides: dict[str, object] = {
        "headless": not headed,
        "max_products": max_products,
        "source": source,
        "crawl_site": not no_crawl,
        "max_listings": max_listings,
    }
    if output_dir is not None:
        overrides["output_dir"] = output_dir
    settings = load_settings(**overrides)

    pipeline = ScrapePipeline(settings)
    report = asyncio.run(pipeline.run(category_url, resume=not no_resume))
    _print_report(report)

    if report.exported == 0:
        raise typer.Exit(code=1)


def _print_report(report: RunReport) -> None:
    table = Table(title="Scrape Report", show_header=False, title_style="bold cyan")
    table.add_row("Category URL", report.category_url)
    table.add_row("Source", report.source)
    if report.detected_pattern:
        table.add_row("Detected pattern", report.detected_pattern)
    table.add_row("Output", str(report.output_path))
    table.add_row("Discovered", str(report.discovered))
    table.add_row("Exported", str(report.exported))
    table.add_row("Skipped (resume)", str(report.skipped_resume))
    table.add_row("Duplicates", str(report.duplicates))
    table.add_row("Invalid", str(report.invalid))
    table.add_row("Failed", str(report.failed))
    console.print(table)


if __name__ == "__main__":  # pragma: no cover
    app()
