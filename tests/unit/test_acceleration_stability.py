"""Acceleration must not let a windowing artefact or one noisy tick swing the score by the full
acceleration weight, while a sustained reversal still removes the credit.

Reproduces the live run: BONKCAT scored ~70.5 with acceleration +20.4% (full 10 points) and
~60.5 about one second later with acceleration -18.5% (0 points) although the price had not
reversed: the surge had merely rolled from the "current" 10 s window into the "previous" one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise

from solana_sniper.config.settings import EntryConfig, FiltersConfig
from solana_sniper.domain.clock import ManualClock
from solana_sniper.domain.models import MarketSnapshot
from solana_sniper.features.engine import FeatureEngine
from solana_sniper.filters.checks import TokenChecker
from solana_sniper.market_data.tracker import TokenTracker
from solana_sniper.strategy.scoring import EntryScorer
from tests.unit.helpers import make_token

MINT = "MintAcc"
STEP = 2.0


def _snap(clock: ManualClock, price: Decimal, i: int) -> MarketSnapshot:
    return MarketSnapshot(
        mint=MINT,
        observed_at=clock.now(),
        source="t",
        price_native=price,
        liquidity_usd=Decimal("20000") + Decimal(50 * i),
        buys_5m=30,
        sells_5m=10,
        volume_5m_usd=Decimal("5000"),
        unique_traders=20 + i,
    )


def _surge_track() -> tuple[ManualClock, TokenTracker]:
    """20 s flat, then +20% over 10 s (the moment BONKCAT-style acceleration peaks)."""
    clock = ManualClock(datetime(2026, 3, 1, 12, 0, tzinfo=UTC))
    tracker = TokenTracker()
    tracker.track(make_token(MINT, age_s=120.0, clock=clock))
    prices = [Decimal("1.000")] * 10 + [Decimal("1.000") + Decimal("0.04") * k for k in range(1, 6)]
    for i, p in enumerate(prices):
        tracker.add_snapshot(_snap(clock, p, i))
        clock.advance(STEP)
    clock.advance(-STEP)  # "now" is the last surge print
    return clock, tracker


def _acc_contribution(
    clock: ManualClock, tracker: TokenTracker
) -> tuple[float, float, float | None]:
    track = tracker.get(MINT)
    assert track is not None
    features = FeatureEngine(20.0).compute(track, clock.now())
    checks = TokenChecker(FiltersConfig()).evaluate(track, features, clock.now(), None)
    score = EntryScorer(EntryConfig(), FiltersConfig()).score(features, checks, clock.now(), None)
    acc = next(c for c in score.components if c.name == "acceleration")
    return score.score, acc.contribution, features.acceleration


def test_window_rollover_with_flat_price_does_not_collapse_acceleration_in_one_tick() -> None:
    clock, tracker = _surge_track()
    _, peak_contribution, peak_acc = _acc_contribution(clock, tracker)
    assert peak_acc is not None and peak_acc > 0.10 and peak_contribution >= 8.0
    top = Decimal("1.20")
    contributions = [peak_contribution]
    for i in range(1, 4):  # 6 s of flat prints at the top: nothing reversed
        clock.advance(STEP)
        tracker.add_snapshot(_snap(clock, top, 20 + i))
        _, contribution, _ = _acc_contribution(clock, tracker)
        contributions.append(contribution)
    steps = [a - b for a, b in pairwise(contributions)]
    assert max(steps) < 4.0, (contributions, steps)  # no single tick takes most of the credit
    assert contributions[-1] >= 3.0, contributions  # credit fades, it does not vanish in 6 s


def test_sustained_reversal_removes_acceleration_credit() -> None:
    clock, tracker = _surge_track()
    _, peak_contribution, _ = _acc_contribution(clock, tracker)
    top = Decimal("1.20")
    for i in range(1, 9):  # 16 s of falling prices: a genuine reversal
        clock.advance(STEP)
        tracker.add_snapshot(_snap(clock, top * (Decimal("0.97") ** i), 20 + i))
    _, contribution, acc = _acc_contribution(clock, tracker)
    assert peak_contribution >= 8.0
    assert acc is not None and acc < -0.05 and contribution < 2.5, (acc, contribution)


def test_one_noisy_print_is_damped_but_a_second_confirms_it() -> None:
    clock, tracker = _surge_track()
    _, peak_contribution, _ = _acc_contribution(clock, tracker)
    clock.advance(STEP)
    tracker.add_snapshot(_snap(clock, Decimal("1.08"), 21))  # one print 10% below the top
    _, after_one, _ = _acc_contribution(clock, tracker)
    assert peak_contribution - after_one < 5.0, (peak_contribution, after_one)
    for i in range(2, 6):  # the drop is confirmed by further prints: credit must go away
        clock.advance(STEP)
        tracker.add_snapshot(_snap(clock, Decimal("1.05"), 20 + i))
    _, after_many, _ = _acc_contribution(clock, tracker)
    assert after_many < after_one and after_many < 3.0, (after_one, after_many)
