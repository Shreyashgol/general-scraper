"""E-commerce adapter interface.

This is the seam that makes the V2 roadmap ("Universal Ecommerce Adapter")
cheap: V1 ships :class:`~savana_scraper.services.savana_adapter.SavanaAdapter`,
and any future site becomes a new implementation of this same protocol. The
pipeline depends only on this abstraction (Dependency Inversion).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from playwright.async_api import Page

from savana_scraper.models import Product, ProductRef


@dataclass
class SourceStats:
    """Counters a source accumulates while streaming, read by the pipeline."""

    #: Products whose extraction failed after all retries.
    failed: int = 0
    #: Products skipped because a previous run already processed them.
    skipped_resume: int = 0


#: Returns ``True`` when a product key was already processed (resume filter).
SkipPredicate = Callable[[str], bool]


class ProductSource(ABC):
    """A strategy that streams fully-populated products from a seed URL.

    Two implementations ship in V1: the JSON-API source (fields come from the
    listing endpoint) and the browser source (renders every product page). The
    pipeline depends only on this protocol, so the choice is pure config.
    """

    #: Human-readable name, used in logs.
    name: str = "generic"

    def __init__(self) -> None:
        self.stats = SourceStats()
        #: Run-level notes for the report — what was inferred, or could not be.
        self.warnings: list[str] = []

    @abstractmethod
    def stream(self, seed_url: str, skip: SkipPredicate | None = None) -> AsyncIterator[Product]:
        """Yield products reachable from ``seed_url`` (async generator).

        Implementations de-duplicate within a run, consult ``skip`` *before* doing
        any expensive per-product work, and stop when exhausted. The pipeline
        applies the global cap, validation, and export.
        """
        raise NotImplementedError


class EcommerceAdapter(ABC):
    """Site-specific strategy for discovering and extracting products."""

    #: Human-readable name, used in logs and output filenames.
    name: str = "generic"

    @abstractmethod
    def discover(self, page: Page, category_url: str) -> AsyncIterator[ProductRef]:
        """Yield product references found on ``category_url`` (async generator)."""
        raise NotImplementedError

    @abstractmethod
    async def extract(self, page: Page, ref: ProductRef) -> Product:
        """Visit ``ref`` and return a fully-populated :class:`Product`."""
        raise NotImplementedError
