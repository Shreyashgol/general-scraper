"""Retry helper (Operational Green Flag: "Retry").

Thin wrapper over tenacity so call sites stay declarative and the backoff
policy is defined in exactly one place.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from savana_scraper.core.exceptions import BrowserError, ExtractionError
from savana_scraper.core.logging import get_logger

log = get_logger(__name__)

T = TypeVar("T")

# Errors worth retrying: transient browser/navigation and extraction hiccups.
RETRYABLE = (BrowserError, ExtractionError)


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int,
    backoff_s: float,
    description: str = "operation",
) -> T:
    """Run ``fn`` with exponential backoff, retrying only transient errors."""
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=backoff_s, min=backoff_s),
        retry=retry_if_exception_type(RETRYABLE),
        reraise=True,
    ):
        with attempt:
            attempt_no = attempt.retry_state.attempt_number
            if attempt_no > 1:
                log.warning("Retry %d/%d for %s", attempt_no, max_attempts, description)
            return await fn()
    raise AssertionError("unreachable")  # pragma: no cover
