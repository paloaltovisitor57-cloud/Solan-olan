"""Summaries over recorded forward outcomes: hit rates per score bucket with uncertainty.

This is the read side of the measurement loop. It answers "when the scorer said X, what
happened next?" on the rows the tracker recorded. It deliberately reports intervals and sample
sizes next to every rate, because with the sample sizes a single machine collects over days,
most differences between buckets are noise.

Nothing here feeds back into scoring automatically. Changing the strategy remains a human
decision made on top of these numbers, not something the engine does to itself.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from statistics import median

from solana_sniper.strategy.outcomes import Outcome

MULTIPLES: tuple[float, ...] = (2.0, 5.0, 10.0)
SCORE_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("score <40", -math.inf, 40.0),
    ("score 40-59", 40.0, 60.0),
    ("score 60-74", 60.0, 75.0),
    ("score 75-89", 75.0, 90.0),
    ("score 90+", 90.0, math.inf),
)
MIN_MEANINGFUL_N = 30


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a binomial proportion; (0, 1) when n == 0."""
    if n <= 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


@dataclass(frozen=True, slots=True)
class BucketStats:
    name: str
    n: int
    reached: dict[float, int]  # multiple -> count that reached it
    median_max_multiple: float | None
    median_final_multiple: float | None
    median_drawdown: float | None
    median_time_to_peak_s: float | None
    liquidity_collapsed: int
    entered: int
    median_closed_pnl_pct: float | None

    def rate(self, multiple: float) -> float:
        return self.reached.get(multiple, 0) / self.n if self.n else 0.0

    def interval(self, multiple: float) -> tuple[float, float]:
        return wilson_interval(self.reached.get(multiple, 0), self.n)

    @property
    def rug_rate(self) -> float:
        return self.liquidity_collapsed / self.n if self.n else 0.0

    @property
    def meaningful(self) -> bool:
        return self.n >= MIN_MEANINGFUL_N


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    total_rows: int
    used_rows: int
    excluded_truncated: int
    excluded_short: int
    simulated_rows: int  # execution simulated (paper/dry-run), whatever the market data
    live_rows: int  # execution by manual signal (live signal mode)
    horizon_s: float | None
    buckets: list[BucketStats] = field(default_factory=list)
    live_market_rows: int = 0  # real Solana market data (paper sessions included)
    synthetic_rows: int = 0  # the synthetic world
    legacy_rows: int = 0  # recorded before market provenance existed

    @property
    def mixed_provenance(self) -> bool:
        kinds = sum(1 for n in (self.live_market_rows, self.synthetic_rows, self.legacy_rows) if n)
        return kinds > 1

    @property
    def all_synthetic(self) -> bool:
        return self.used_rows > 0 and self.synthetic_rows == self.used_rows

    @property
    def all_simulated(self) -> bool:  # kept for older callers: execution simulated everywhere
        return self.used_rows > 0 and self.live_rows == 0


def _median(values: Iterable[float]) -> float | None:
    vals = [v for v in values if v is not None and math.isfinite(v)]
    return median(vals) if vals else None


def bucket_stats(name: str, rows: Sequence[Outcome]) -> BucketStats:
    return BucketStats(
        name=name,
        n=len(rows),
        reached={m: sum(1 for r in rows if r.reached(m)) for m in MULTIPLES},
        median_max_multiple=_median(r.max_multiple for r in rows),
        median_final_multiple=_median(r.final_multiple for r in rows),
        median_drawdown=_median(r.max_drawdown_from_peak for r in rows),
        median_time_to_peak_s=_median(
            r.time_to_peak_s for r in rows if r.time_to_peak_s is not None
        ),
        liquidity_collapsed=sum(1 for r in rows if r.liquidity_collapsed),
        entered=sum(1 for r in rows if r.entered),
        median_closed_pnl_pct=_median(
            r.closed_pnl_pct for r in rows if r.closed_pnl_pct is not None
        ),
    )


def summarize(
    outcomes: Sequence[Outcome],
    *,
    include_truncated: bool = False,
    min_observations: int = 5,
) -> EvaluationReport:
    """Group outcomes into score buckets and engine-decision groups.

    Rows finalised early at shutdown (`truncated`) and rows with too few observations to say
    anything about a peak are excluded unless asked for, and the report says how many.
    """
    truncated = [o for o in outcomes if o.truncated]
    kept = list(outcomes) if include_truncated else [o for o in outcomes if not o.truncated]
    short = [o for o in kept if o.observations < min_observations]
    used = [o for o in kept if o.observations >= min_observations]
    groups: list[tuple[str, Callable[[Outcome], bool]]] = [
        ("all followed", lambda o: True),
        ("rejected by checks", lambda o: o.reject_reason is not None),
        ("never qualified", lambda o: not o.qualified and o.reject_reason is None),
        ("qualified", lambda o: o.qualified),
        ("BUY signalled", lambda o: o.signalled),
        ("entered (position opened)", lambda o: o.entered),
    ]
    buckets = [bucket_stats(name, [o for o in used if pred(o)]) for name, pred in groups]
    buckets.append(bucket_stats("unscored", [o for o in used if o.best_score is None]))
    for name, lo, hi in SCORE_BUCKETS:
        buckets.append(
            bucket_stats(
                name, [o for o in used if o.best_score is not None and lo <= o.best_score < hi]
            )
        )
    horizons = {o.horizon_s for o in used}
    return EvaluationReport(
        total_rows=len(outcomes),
        used_rows=len(used),
        excluded_truncated=0 if include_truncated else len(truncated),
        excluded_short=len(short),
        simulated_rows=sum(1 for o in used if o.simulated),
        live_rows=sum(1 for o in used if not o.simulated),
        live_market_rows=sum(1 for o in used if o.market_data == "LIVE"),
        synthetic_rows=sum(1 for o in used if o.market_data == "SYNTHETIC"),
        legacy_rows=sum(1 for o in used if o.market_data == "UNKNOWN_LEGACY"),
        horizon_s=max(horizons) if len(horizons) == 1 else None,
        buckets=buckets,
    )
