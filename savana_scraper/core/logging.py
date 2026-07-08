"""Centralised Rich-based logging setup."""

from __future__ import annotations

import logging

from rich.console import Console
from rich.logging import RichHandler

console = Console(stderr=True)

_CONFIGURED = False

# httpx logs one INFO line per request, which buries our own output on a run
# that makes hundreds of API calls. Demote it; --log-level DEBUG brings it back.
_NOISY_LOGGERS = ("httpx", "httpcore")


def configure_logging(level: str = "INFO") -> None:
    """Install a single Rich handler on the root logger (idempotent)."""
    global _CONFIGURED
    if _CONFIGURED:
        logging.getLogger().setLevel(level.upper())
        _quieten(level)
        return

    logging.basicConfig(
        level=level.upper(),
        format="%(message)s",
        datefmt="[%X]",
        handlers=[
            RichHandler(
                console=console,
                rich_tracebacks=True,
                show_path=False,
                markup=True,
            )
        ],
    )
    _quieten(level)
    _CONFIGURED = True


def _quieten(level: str) -> None:
    """Hold chatty third-party loggers at WARNING unless we asked for DEBUG."""
    if level.upper() == "DEBUG":
        return
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a module-scoped logger."""
    return logging.getLogger(name)
