"""Adapter registry — maps a URL's domain to the source that handles it best.

A registered domain gets a hand-built adapter: exact field mapping, and often a
private JSON API that is orders of magnitude faster than rendering pages. Any
other domain falls back to :class:`GenericProductSource`, which is best-effort
and says so.

Adding a site means adding one entry here. Nothing else in the stack changes.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlparse

from savana_scraper.core.config import Settings
from savana_scraper.core.logging import get_logger
from savana_scraper.services.adapter import ProductSource
from savana_scraper.services.generic_source import GenericProductSource
from savana_scraper.services.sources import ApiProductSource, BrowserProductSource

log = get_logger(__name__)

SourceFactory = Callable[[Settings], ProductSource]

#: Registrable domain → factory. Matched against the URL host and its parents,
#: so ``www.savana.com`` and ``savana.com`` both resolve to the Savana adapter.
REGISTRY: dict[str, SourceFactory] = {
    "savana.com": ApiProductSource,
}

#: Named sources the caller can force via ``settings.source``, bypassing lookup.
EXPLICIT_SOURCES: dict[str, SourceFactory] = {
    "api": ApiProductSource,
    "browser": BrowserProductSource,
    "generic": GenericProductSource,
}


def _domains_of(host: str) -> list[str]:
    """['www.shop.savana.com', 'shop.savana.com', 'savana.com'] — most specific first."""
    parts = host.split(".")
    return [".".join(parts[i:]) for i in range(len(parts) - 1)]


def adapter_for_url(url: str) -> str | None:
    """Return the registered domain handling ``url``, or ``None`` if unregistered."""
    host = (urlparse(url).hostname or "").lower()
    return next((d for d in _domains_of(host) if d in REGISTRY), None)


def source_for_url(url: str, settings: Settings) -> ProductSource:
    """Pick the best source for ``url``.

    An explicit ``settings.source`` of ``api``/``browser``/``generic`` wins; the
    default (``auto``) consults the registry and falls back to generic.
    """
    if settings.source != "auto":
        try:
            return EXPLICIT_SOURCES[settings.source](settings)
        except KeyError:
            raise ValueError(
                f"Unknown source {settings.source!r}; "
                f"expected 'auto' or one of {sorted(EXPLICIT_SOURCES)}"
            ) from None

    domain = adapter_for_url(url)
    if domain is not None:
        log.info("[green]Registered adapter[/] for %s", domain)
        return REGISTRY[domain](settings)

    log.info("[yellow]No adapter[/] for %s — using the generic crawler (best effort)", url)
    return GenericProductSource(settings)
