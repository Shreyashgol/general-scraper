"""robots.txt compliance check.

The CLI scrapes sites the operator chose. The web platform scrapes whatever URL
a *user* pastes in — a different trust model, which needs a guardrail. This
module answers one question: does this site's robots.txt permit our user-agent
to fetch this URL?

Callers may override the answer, but the override has to be explicit and made by
a human. A missing or unreachable robots.txt is treated as "allowed", which is
the convention every major crawler follows.
"""

from __future__ import annotations

from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from savana_scraper.core.logging import get_logger

log = get_logger(__name__)

_TIMEOUT_S = 10.0


class RobotsVerdict:
    """Whether a URL may be fetched, and why."""

    def __init__(self, allowed: bool, reason: str) -> None:
        self.allowed = allowed
        self.reason = reason

    def __bool__(self) -> bool:
        return self.allowed


async def check(url: str, user_agent: str) -> RobotsVerdict:
    """Consult ``<origin>/robots.txt`` for ``url``.

    Network or parse failures resolve to *allowed* — we do not let a flaky
    robots.txt block a run, and we say so in the reason.
    """
    origin = urlparse(url)
    if origin.scheme not in ("http", "https") or not origin.netloc:
        return RobotsVerdict(False, f"Not an http(s) URL: {url}")

    robots_url = urljoin(f"{origin.scheme}://{origin.netloc}", "/robots.txt")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S, follow_redirects=True) as client:
            response = await client.get(robots_url, headers={"user-agent": user_agent})
    except httpx.HTTPError as e:
        log.warning("robots.txt unreachable at %s (%s); allowing", robots_url, e)
        return RobotsVerdict(True, "robots.txt unreachable; allowed by convention")

    if response.status_code >= 400:
        return RobotsVerdict(True, f"robots.txt returned HTTP {response.status_code}; allowed")

    parser = RobotFileParser()
    parser.parse(response.text.splitlines())
    if parser.can_fetch(user_agent, url):
        return RobotsVerdict(True, "allowed by robots.txt")
    return RobotsVerdict(
        False,
        f"{robots_url} disallows this user-agent from fetching {url}",
    )
