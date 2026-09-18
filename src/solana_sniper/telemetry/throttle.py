"""Log throttling for repetitive conditions (rate limits, provider errors, dropped rows).

The first occurrence of a key is reported immediately; further occurrences inside the window
are counted and summarised once per window instead of printing hundreds of near-identical lines.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(slots=True)
class ThrottleDecision:
    log: bool  # emit a line now
    suppressed: int  # occurrences swallowed since the last emitted line
    total: int  # occurrences ever seen for this key


class LogThrottle:
    def __init__(self, window_s: float = 60.0, now: Callable[[], float] = time.monotonic) -> None:
        self._window = window_s
        self._now = now
        self._last_emit: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}
        self._total: dict[str, int] = {}

    def hit(self, key: str) -> ThrottleDecision:
        now = self._now()
        self._total[key] = self._total.get(key, 0) + 1
        last = self._last_emit.get(key)
        if last is None or now - last >= self._window:
            suppressed = self._suppressed.pop(key, 0)
            self._last_emit[key] = now
            return ThrottleDecision(True, suppressed, self._total[key])
        self._suppressed[key] = self._suppressed.get(key, 0) + 1
        return ThrottleDecision(False, self._suppressed[key], self._total[key])

    def total(self, key: str) -> int:
        return self._total.get(key, 0)

    def reset(self, key: str) -> None:
        self._last_emit.pop(key, None)
        self._suppressed.pop(key, None)
