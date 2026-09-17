from __future__ import annotations

from decimal import Decimal

import pytest

from solana_sniper.domain.clock import ManualClock
from solana_sniper.features.engine import FeatureEngine
from solana_sniper.market_data.tracker import TokenTracker
from tests.unit.helpers import feed_path, make_token


def test_momentum_and_acceleration(clock: ManualClock) -> None:
    tracker = TokenTracker()
    tracker.track(make_token(clock=clock))
    # 5s steps: 0,5,10,...,60s => 13 points; steady 2% per step
    prices = [str(Decimal("1.00") * (Decimal("1.02") ** i)) for i in range(13)]
    track = feed_path(
        tracker, clock, "MintTest", prices, liquidity=[str(20000 + 500 * i) for i in range(13)]
    )
    engine = FeatureEngine(stale_after_s=20)
    f = engine.compute(track, clock.now())
    assert f.observation_count == 13
    assert f.momentum_10s == pytest.approx(1.02**2 - 1, rel=1e-6)
    assert f.momentum_30s == pytest.approx(1.02**6 - 1, rel=1e-6)
    assert f.momentum_60s == pytest.approx(1.02**12 - 1, rel=1e-6)
    assert f.momentum_180s is None  # not enough history
    assert f.acceleration == pytest.approx(0.0, abs=1e-3)  # constant growth = no acceleration
    assert f.liquidity_growth_60s == pytest.approx(6000 / 20000, rel=1e-6)
    assert f.trade_velocity_per_min == pytest.approx(28 / 5)
    assert f.buy_ratio == pytest.approx(20 / 28)
    assert f.drawdown_from_peak == pytest.approx(0.0)
    assert f.seconds_since_peak == 0.0
    assert not f.stale and f.data_age_s == 0.0
    assert f.token_age_s == pytest.approx(120.0)
    assert f.volatility_60s is not None and f.volatility_60s > 0
    d = f.as_dict()
    assert d["mint"] == "MintTest" and d["stale"] is False


def test_drawdown_and_stale(clock: ManualClock) -> None:
    tracker = TokenTracker()
    tracker.track(make_token(clock=clock))
    track = feed_path(tracker, clock, "MintTest", ["1", "2", "4", "3", "2"])
    engine = FeatureEngine(stale_after_s=10)
    f = engine.compute(track, clock.now())
    assert f.drawdown_from_peak == pytest.approx(0.5)
    assert f.seconds_since_peak == pytest.approx(10.0)
    assert f.momentum_10s == pytest.approx(-0.5)
    clock.advance(11)
    f2 = engine.compute(track, clock.now())
    assert f2.stale and f2.data_age_s == pytest.approx(11.0)


def test_trade_based_velocity_and_participation(clock: ManualClock) -> None:
    tracker = TokenTracker()
    tracker.track(make_token(clock=clock))
    track = feed_path(tracker, clock, "MintTest", ["1"] * 25, step_s=5, trades_per_step=4)
    engine = FeatureEngine(stale_after_s=20)
    f = engine.compute(track, clock.now())
    # last 60s = 13 steps * 4 trades (inclusive window) -> velocity from trades
    assert f.trade_velocity_per_min is not None and f.trade_velocity_per_min >= 48
    assert f.buy_velocity_per_min is not None and f.sell_velocity_per_min is not None
    assert f.buy_ratio == pytest.approx(0.75)
    assert f.buy_sell_imbalance == pytest.approx(0.5)
    assert f.unique_trader_growth is not None and f.unique_trader_growth > 0
    assert f.volume_acceleration == pytest.approx(0.0, abs=0.2)


def test_no_data_gives_none_features(clock: ManualClock) -> None:
    tracker = TokenTracker()
    track = tracker.track(make_token())
    f = FeatureEngine(stale_after_s=5).compute(track, clock.now())
    assert f.price_native is None and f.momentum_10s is None and f.trade_velocity_per_min is None
    assert f.stale and f.token_age_s is None and f.observation_count == 0
