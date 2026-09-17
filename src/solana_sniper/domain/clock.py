"""Clock abstraction so engine logic is deterministic in tests and replay."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(tz=UTC)

    def monotonic(self) -> float:
        return time.monotonic()


class ManualClock:
    """A clock that only moves when told to. Used by tests and replay."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)
        self._mono = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._mono += seconds

    def set(self, when: datetime) -> None:
        delta = (when - self._now).total_seconds()
        if delta < 0:
            raise ValueError("ManualClock cannot move backwards")
        self.advance(delta)


def utcnow() -> datetime:
    return datetime.now(tz=UTC)
