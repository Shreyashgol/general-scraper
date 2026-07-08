"""Domain-specific exception hierarchy.

A single root exception (:class:`ScraperError`) lets callers catch every
error this package raises while still allowing fine-grained handling.
"""

from __future__ import annotations


class ScraperError(Exception):
    """Base class for every error raised by the scraper."""


class BrowserError(ScraperError):
    """Raised when the browser fails to launch or a page operation fails."""


class NavigationError(BrowserError):
    """Raised when a page cannot be navigated to (timeout, network, HTTP error)."""


class ApiError(ScraperError):
    """Raised when the site's JSON API returns an error or unusable payload."""


class DiscoveryError(ScraperError):
    """Raised when product discovery from a category page fails."""


class ExtractionError(ScraperError):
    """Raised when a product page cannot be parsed into a product."""


class ValidationError(ScraperError):
    """Raised when an extracted product fails validation rules."""


class ExportError(ScraperError):
    """Raised when writing output fails."""
