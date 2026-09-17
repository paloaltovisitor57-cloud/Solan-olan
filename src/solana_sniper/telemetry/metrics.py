"""In-process counters and latency samples. Cheap enough for hot paths."""

from __future__ import annotations

import statistics
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class LatencySeries:
    samples: deque[float] = field(default_factory=lambda: deque(maxlen=500))

    def record(self, ms: float) -> None:
        self.samples.append(ms)

    def summary(self) -> dict[str, float]:
        if not self.samples:
            return {"count": 0}
        data = sorted(self.samples)
        n = len(data)
        return {
            "count": n,
            "p50_ms": round(statistics.median(data), 1),
            "p95_ms": round(data[min(n - 1, int(n * 0.95))], 1),
            "max_ms": round(data[-1], 1),
            "last_ms": round(self.samples[-1], 1),
        }


class Metrics:
    """Counters + latency series. Names are free-form strings; keep them stable."""

    COUNTERS = (
        "tokens_discovered",
        "tokens_rejected",
        "tokens_qualified",
        "signals_generated",
        "signals_confirmed",
        "signals_rejected",
        "signals_expired",
        "exit_signals",
        "snapshots",
        "trades",
        "quotes",
        "quote_failures",
        "provider_errors",
        "stale_suppressions",
        "positions_opened",
        "positions_closed",
        "ws_reconnects",
        "rate_limited",
    )

    LATENCIES = (
        "discovery_to_first_data",
        "data_to_features",
        "features_to_signal",
        "signal_to_quote",
        "quote_latency",
        "provider_latency",
        "storage_flush",
    )

    def __init__(self) -> None:
        self.counters: dict[str, int] = defaultdict(int)
        for c in self.COUNTERS:
            self.counters[c] = 0
        self.latencies: dict[str, LatencySeries] = defaultdict(LatencySeries)
        for latency in self.LATENCIES:
            self.latencies[latency] = LatencySeries()
        self.gauges: dict[str, float] = {}
        self.started_at = time.monotonic()
        self._discovery_timestamps: deque[float] = deque(maxlen=2000)

    def inc(self, name: str, by: int = 1) -> None:
        self.counters[name] += by

    def observe(self, name: str, ms: float) -> None:
        self.latencies[name].record(ms)

    def gauge(self, name: str, value: float) -> None:
        self.gauges[name] = value

    def mark_discovery(self) -> None:
        self._discovery_timestamps.append(time.monotonic())
        self.inc("tokens_discovered")

    def discovery_rate_per_s(self, window_s: float = 60.0) -> float:
        cutoff = time.monotonic() - window_s
        recent = sum(1 for t in self._discovery_timestamps if t >= cutoff)
        return recent / window_s

    def snapshot(self) -> dict[str, Any]:
        return {
            "uptime_s": round(time.monotonic() - self.started_at, 1),
            "counters": dict(self.counters),
            "latencies": {k: v.summary() for k, v in self.latencies.items()},
            "gauges": dict(self.gauges),
            "discovery_rate_per_s": round(self.discovery_rate_per_s(), 3),
        }


class PipelineTimer:
    """Tracks per-mint stage timestamps so we can measure discovery→data→features→signal→quote."""

    def __init__(self, metrics: Metrics) -> None:
        self._metrics = metrics
        self._stages: dict[str, dict[str, float]] = {}

    def mark(self, mint: str, stage: str) -> None:
        now = time.monotonic()
        stages = self._stages.setdefault(mint, {})
        if stage in stages:
            return
        stages[stage] = now
        pairs = {
            "first_data": ("discovered", "discovery_to_first_data"),
            "features": ("first_data", "data_to_features"),
            "signal": ("features", "features_to_signal"),
            "quote": ("signal", "signal_to_quote"),
        }
        if stage in pairs:
            prev, metric = pairs[stage]
            if prev in stages:
                self._metrics.observe(metric, (now - stages[prev]) * 1000.0)

    def reset_stage(self, mint: str, stage: str) -> None:
        stages = self._stages.get(mint)
        if stages:
            stages.pop(stage, None)

    def forget(self, mint: str) -> None:
        self._stages.pop(mint, None)
