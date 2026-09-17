"""Defensive parsing helpers for third-party JSON. Never trust shapes; return None on garbage."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


def as_dict(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def as_str(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == value:
        return int(value)
    if isinstance(value, str):
        try:
            return int(Decimal(value))
        except (InvalidOperation, ValueError):
            return None
    return None


def as_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                return None
            return Decimal(repr(value))
        if isinstance(value, int):
            return Decimal(value)
        if isinstance(value, str):
            d = Decimal(value.strip())
            if not d.is_finite():
                return None
            return d
    except (InvalidOperation, ValueError):
        return None
    return None


def as_float(value: Any) -> float | None:
    d = as_decimal(value)
    return float(d) if d is not None else None


def ts_from_ms(value: Any) -> datetime | None:
    ms = as_int(value)
    if ms is None or ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def ts_from_s(value: Any) -> datetime | None:
    s = as_float(value)
    if s is None or s <= 0:
        return None
    try:
        return datetime.fromtimestamp(s, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def ts_from_iso(value: Any) -> datetime | None:
    s = as_str(value)
    if s is None:
        return None
    try:
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt
