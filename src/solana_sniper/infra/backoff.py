"""Exponential backoff with jitter."""

from __future__ import annotations

import random


class Backoff:
    def __init__(self, minimum: float = 1.0, maximum: float = 30.0, factor: float = 2.0) -> None:
        self.minimum = minimum
        self.maximum = maximum
        self.factor = factor
        self._attempt = 0

    def reset(self) -> None:
        self._attempt = 0

    def next_delay(self) -> float:
        delay = min(self.maximum, self.minimum * (self.factor**self._attempt))
        self._attempt += 1
        jitter = random.uniform(0.0, delay * 0.25)
        return min(self.maximum, delay + jitter)

    @property
    def attempt(self) -> int:
        return self._attempt
