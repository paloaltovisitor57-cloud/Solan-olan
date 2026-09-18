"""Credential redaction for logs, exceptions and diagnostic output.

Defence in depth:
* `safe_url` keeps scheme, host and a sanitised path (bot tokens / webhook tokens masked) and
  drops the whole query string, so an endpoint stays identifiable without its credentials.
* `scrub_text` masks userinfo, query values, known token path patterns and every value in the
  `SecretRegistry` (API keys, webhook tokens, ... registered at configuration load time).
* `safe_exception` renders an exception chain with every message scrubbed.
* `scrub_event` (structlog processor) and `ScrubbingFormatter` (stdlib logging) apply the same
  scrubbing to everything that reaches a log sink, whichever logging API produced it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, MutableMapping
from typing import Any
from urllib.parse import urlsplit

MASK = "***"
MIN_SECRET_LENGTH = 8

_USERINFO_RE = re.compile(r"(://)[^/\s@]+@")
_BOT_TOKEN_RE = re.compile(r"/bot[0-9]+:[A-Za-z0-9_-]+")
_WEBHOOK_RE = re.compile(r"(/api/webhooks/[0-9]+/)[A-Za-z0-9_.-]+")
_QUERY_VALUE_RE = re.compile(r"([?&][A-Za-z0-9_.\-\[\]]+=)[^&\s#'\"<>]+")
# `name: value` / `name=value` pairs in free text whose name suggests a credential.
# An optional auth scheme word (Bearer/Basic/Token) is kept; the credential after it is masked.
_KV_RE = re.compile(
    r"(?i)\b((?:x-api-key|api[-_]?key|authorization|token|access[-_]?token|secret|"
    r"secret[-_]?key|password|passwd|pwd|signature|cookie|webhook)\s*[:=]\s*"
    r"(?:(?:bearer|basic|token)\s+)?)[^\s,;'\"&<>]+"
)


class SecretRegistry:
    """Known secret values. Registered at config load; every log sink scrubs them."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()

    def register(self, value: str | None) -> None:
        if value and len(value) >= MIN_SECRET_LENGTH:
            self._secrets.add(value)

    def clear(self) -> None:
        self._secrets.clear()

    def __len__(self) -> int:
        return len(self._secrets)

    def scrub(self, text: str) -> str:
        if not self._secrets or not text:
            return text
        for secret in sorted(self._secrets, key=len, reverse=True):
            if secret in text:
                text = text.replace(secret, MASK)
        return text


registry = SecretRegistry()


def register_secret(value: str | None) -> None:
    registry.register(value)
    if value and ":" in value:  # Telegram-style "<id>:<secret>" tokens: mask the secret half too
        registry.register(value.split(":", 1)[1])


def register_url_secrets(url: str | None) -> None:
    """Register credentials embedded in a URL (userinfo password, every query value)."""
    if not url:
        return
    try:
        parts = urlsplit(url)
    except ValueError:
        return
    if parts.password:
        registry.register(parts.password)
    if parts.username and parts.password:
        registry.register(f"{parts.username}:{parts.password}")
    for piece in parts.query.split("&"):
        if "=" in piece:
            registry.register(piece.split("=", 1)[1])
    m = _BOT_TOKEN_RE.search(parts.path)
    if m:
        token = m.group(0).removeprefix("/bot")
        registry.register(token)
        registry.register(token.split(":", 1)[1])
    m2 = _WEBHOOK_RE.search(parts.path)
    if m2:
        registry.register(parts.path[m2.end(1) :])


def scrub_text(text: str) -> str:
    if not text:
        return text
    text = registry.scrub(text)
    text = _USERINFO_RE.sub(r"\1" + MASK + "@", text)
    text = _BOT_TOKEN_RE.sub("/bot" + MASK, text)
    text = _WEBHOOK_RE.sub(r"\1" + MASK, text)
    text = _QUERY_VALUE_RE.sub(r"\1" + MASK, text)
    text = _KV_RE.sub(r"\1" + MASK, text)
    return text


def safe_url(url: str | None) -> str:
    """scheme://host/path with token-bearing path segments masked and the query removed."""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return scrub_text(url)
    if not parts.scheme or parts.hostname is None:
        return scrub_text(url)
    path = _WEBHOOK_RE.sub(r"\1" + MASK, _BOT_TOKEN_RE.sub("/bot" + MASK, parts.path))
    path = registry.scrub(path)
    host = parts.hostname
    if parts.port:
        host = f"{host}:{parts.port}"
    suffix = "?<redacted>" if parts.query else ""
    return f"{parts.scheme}://{host}{path}{suffix}"


def safe_exception(exc: BaseException, *, depth: int = 4) -> str:
    """`Type: message` for the exception and its cause chain, every message scrubbed."""
    parts: list[str] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and len(parts) < depth and id(current) not in seen:
        seen.add(id(current))
        message = scrub_text(str(current))
        parts.append(f"{type(current).__name__}: {message}" if message else type(current).__name__)
        current = current.__cause__ or current.__context__
    return " <- ".join(parts)


def scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, BaseException):
        return safe_exception(value)
    if isinstance(value, Mapping):
        return {k: scrub_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(scrub_value(v) for v in value)
    return value


def scrub_event(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: scrub every value (including rendered exception text)."""
    for key in list(event_dict):
        event_dict[key] = scrub_value(event_dict[key])
    return event_dict


_DEFAULT_FORMATTER = logging.Formatter()
_ORIGINAL_FACTORY = logging.getLogRecordFactory()


def _scrubbing_record_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
    """LogRecord factory: scrub message and traceback text at creation so *every* handler,
    including ones registered by third parties or tests, only ever sees scrubbed records."""
    record = _ORIGINAL_FACTORY(*args, **kwargs)
    try:
        message = record.getMessage()
    except (TypeError, ValueError):
        message = str(record.msg)
    record.msg = scrub_text(message)
    record.args = ()
    if record.exc_info and record.exc_info[0] is not None and not record.exc_text:
        record.exc_text = scrub_text(_DEFAULT_FORMATTER.formatException(record.exc_info))
    return record


def install_record_factory() -> None:
    if logging.getLogRecordFactory() is not _scrubbing_record_factory:
        logging.setLogRecordFactory(_scrubbing_record_factory)


class ScrubbingFormatter(logging.Formatter):
    """Standard-library formatter that scrubs the fully rendered record (message + traceback)."""

    def format(self, record: logging.LogRecord) -> str:
        return scrub_text(super().format(record))


class ScrubbingFilter(logging.Filter):
    """Belt and braces for handlers whose formatter we do not control: scrub msg/args in place."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            message = str(record.msg)
        record.msg = scrub_text(message)
        record.args = ()
        return True
