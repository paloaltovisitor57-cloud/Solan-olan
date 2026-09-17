"""Dataclass <-> JSON-friendly dict conversion with Decimal/datetime fidelity."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, get_args, get_origin, get_type_hints


def to_jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return {"__dec__": str(value)}
    if isinstance(value, datetime):
        return {"__dt__": value.astimezone(UTC).isoformat()}
    if isinstance(value, StrEnum):
        return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [to_jsonable(v) for v in value]
    return value


def from_jsonable(value: Any) -> Any:
    """Inverse of to_jsonable for scalar wrappers; containers are recursed."""
    if isinstance(value, dict):
        if set(value) == {"__dec__"}:
            return Decimal(value["__dec__"])
        if set(value) == {"__dt__"}:
            return datetime.fromisoformat(value["__dt__"])
        return {k: from_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_jsonable(v) for v in value]
    return value


def _coerce(value: Any, annotation: Any) -> Any:
    origin = get_origin(annotation)
    if (origin is not None and origin.__name__ == "UnionType") or str(origin) == "typing.Union":
        args = [a for a in get_args(annotation) if a is not type(None)]
        if value is None:
            return None
        return _coerce(value, args[0]) if len(args) == 1 else value
    if origin is tuple:
        (inner, *_rest) = get_args(annotation) or (Any,)
        return tuple(_coerce(v, inner) for v in (value or []))
    if origin is list:
        (inner,) = get_args(annotation) or (Any,)
        return [_coerce(v, inner) for v in (value or [])]
    if origin is dict:
        return dict(value or {})
    if isinstance(annotation, type):
        if issubclass(annotation, StrEnum) and isinstance(value, str):
            return annotation(value)
        if dataclasses.is_dataclass(annotation) and isinstance(value, dict):
            return dataclass_from_dict(annotation, value)
        if annotation is Decimal and isinstance(value, str | int | float):
            return Decimal(str(value))
        if annotation is datetime and isinstance(value, str):
            return datetime.fromisoformat(value)
    return value


def dataclass_from_dict[T](cls: type[T], data: dict[str, Any]) -> T:
    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(cls):  # type: ignore[arg-type]
        if f.name not in data:
            continue
        kwargs[f.name] = _coerce(from_jsonable(data[f.name]), hints.get(f.name, Any))
    return cls(**kwargs)
