"""Structured logging via structlog. Console renderer for humans, JSON for machines.

Every sink scrubs credentials: structlog events pass through `scrub_event`, and each stdlib
handler gets a `ScrubbingFormatter` plus a `ScrubbingFilter` (so third-party loggers such as
httpx/websockets are covered too).
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog

from solana_sniper.telemetry.redaction import (
    ScrubbingFilter,
    ScrubbingFormatter,
    install_record_factory,
    scrub_event,
)

_CONFIGURED = False


def configure_logging(
    level: str = "INFO",
    json_output: bool = False,
    log_file: str | None = None,
    quiet_console: bool = False,
) -> None:
    """Configure stdlib + structlog once. quiet_console is used while the dashboard owns the TTY."""
    global _CONFIGURED
    install_record_factory()
    handlers: list[logging.Handler] = []
    if not quiet_console:
        handlers.append(logging.StreamHandler(sys.stderr))
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log_file, maxBytes=20 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
        )
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in handlers:
        h.setFormatter(ScrubbingFormatter("%(message)s"))
        h.addFilter(ScrubbingFilter())
        root.addHandler(h)
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "websockets", "sqlalchemy.engine", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    renderer: structlog.types.Processor
    if json_output:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=not quiet_console)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            scrub_event,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )
    _CONFIGURED = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    if not _CONFIGURED:
        configure_logging()
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
