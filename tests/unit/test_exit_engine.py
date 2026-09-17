from __future__ import annotations

from decimal import Decimal

import pytest

from solana_sniper.config.settings import ExitConfig, TrailingConfig, TrailingTier
from solana_sniper.domain.clock import ManualClock
from solana_sniper.domain.enums import ExitReason, SignalKind, Urgency
from solana_sniper.domain.models import FeatureVector, MarketSnapshot, Position
from solana_sniper.market_data.tracker import TokenTrack, TokenTracker
from solana_sniper.portfolio.accounting import PortfolioAccount
from solana_sniper.positions.exit_engine import AdaptiveTrailing, ExitEngine, MonitorState
from solana_sniper.positions.monitor import PositionMonitor
from tests.conftest import make_fill
from tests.unit.helpers import make_token


def features(**kw: object) -> FeatureVector:
    base: dict[str, object] = {
        "mint": "MintTest",
        "computed_at": None,
        "observation_count": 10,
        "token_age_s": 100.0,
        "price_native": 1.0,
        "liquidity_usd": 20000.0,
        "momentum_10s": 0.0,
        "momentum_30s": 0.0,
        "momentum_60s": 0.0,
        "momentum_180s": None,
        "acceleration": 0.0,
        "liquidity_growth_60s": 0.0,
        "liquidity_acceleration": None,
        "volume_acceleration": None,
        "trade_velocity_per_min": 30.0,
        "buy_velocity_per_min": 20.0,
        "sell_velocity_per_min": 10.0,
        "buy_sell_imbalance": 0.3,
        "buy_ratio": 0.66,
        "unique_trader_growth": 0.1,
        "holder_growth": None,
        "drawdown_from_peak": 0.0,
        "seconds_since_peak": 0.0,
        "market_depth_usd": 20000.0,
        "estimated_slippage_bps": 100,
        "estimated_exit_slippage_bps": 100,
        "volatility_60s": 0.02,
        "data_age_s": 0.5,
        "stale": False,
    }
    base.update(kw)
    return FeatureVector(**base)  # type: ignore[arg-type]


def open_position(account: PortfolioAccount, clock: ManualClock, cost: str = "20") -> Position:
    fill = make_fill(
        clock,
        side=SignalKind.BUY,
        eur_amount=Decimal(cost),
        fee_eur=Decimal("0"),
        slippage_eur=Decimal("0"),
    )
    return account.open_position(fill, symbol="TST", entry_price_native=Decimal("0.001"))


def mark(account: PortfolioAccount, pos: Position, clock: ManualClock, value: str) -> None:
    account.mark_position(
        pos.position_id,
        value_eur=Decimal(value),
        price_native=Decimal("0.001"),
        at=clock.now(),
        executable=True,
    )


def test_trailing_tiers_tighten_with_profit() -> None:
    trailing = AdaptiveTrailing(TrailingConfig())
    assert trailing.base_threshold(1.0) == 0.30
    assert trailing.base_threshold(1.5) == 0.24
    assert trailing.base_threshold(2.5) == 0.18
    assert trailing.base_threshold(4.0) == 0.14
    assert trailing.base_threshold(7.0) == 0.10
    assert trailing.base_threshold(50.0) == 0.07


def test_adaptive_modifiers(account: PortfolioAccount, clock: ManualClock) -> None:
    trailing = AdaptiveTrailing(TrailingConfig())
    pos = open_position(account, clock)
    mark(account, pos, clock, "40")  # 2x peak -> base 0.18
    base, notes = trailing.threshold(pos, features(), clock.now())
    assert base == pytest.approx(0.18) and notes[0].startswith("base 18%")
    widened, _ = trailing.threshold(pos, features(volatility_60s=0.12), clock.now())
    assert widened == pytest.approx(0.18 * 1.5)  # capped by max_volatility_widen
    tightened, _ = trailing.threshold(
        pos, features(liquidity_usd=5000.0, momentum_30s=-0.1), clock.now()
    )
    assert tightened == pytest.approx(0.18 * 0.7 * 0.8)
    clock.advance(700)
    by_time, notes = trailing.threshold(pos, features(), clock.now())
    assert by_time == pytest.approx(0.18 * 0.85) and any("duration" in n for n in notes)
    cfg = TrailingConfig(min_drawdown_pct=0.2, max_drawdown_pct=0.25, max_volatility_widen=3.0)
    assert (
        AdaptiveTrailing(cfg).threshold(
            pos, features(liquidity_usd=1.0, momentum_30s=-1), clock.now()
        )[0]
        == 0.2
    )
    assert (
        AdaptiveTrailing(cfg).threshold(pos, features(volatility_60s=1.0), clock.now())[0] == 0.25
    )


def test_trailing_exit_triggers(account: PortfolioAccount, clock: ManualClock) -> None:
    engine = ExitEngine(ExitConfig())
    pos = open_position(account, clock)
    state = MonitorState()
    clock.advance(10)
    mark(account, pos, clock, "60")  # 3x -> threshold 14%
    assert engine.evaluate(pos, state, None, features(), clock.now()) is None
    mark(account, pos, clock, "53")  # -11.7%
    assert engine.evaluate(pos, state, None, features(), clock.now()) is None
    mark(account, pos, clock, "50")  # -16.7%
    d = engine.evaluate(pos, state, None, features(), clock.now())
    assert d is not None and d.reason is ExitReason.TRAILING_PEAK and d.urgency is Urgency.HIGH
    assert d.trailing_threshold_pct == pytest.approx(0.14)
    assert d.trailing_drawdown_pct == pytest.approx(1 / 6)


def test_trailing_respects_min_holding(account: PortfolioAccount, clock: ManualClock) -> None:
    engine = ExitEngine(ExitConfig(min_holding_s_before_trailing=30))
    pos = open_position(account, clock)
    mark(account, pos, clock, "40")
    mark(account, pos, clock, "20")
    assert engine.evaluate(pos, MonitorState(), None, features(), clock.now()) is None
    clock.advance(31)
    d = engine.evaluate(pos, MonitorState(), None, features(), clock.now())
    assert d is not None and d.reason is ExitReason.TRAILING_PEAK


def test_max_loss_and_timeout(account: PortfolioAccount, clock: ManualClock) -> None:
    engine = ExitEngine(ExitConfig(max_loss_pct=0.3, max_holding_s=100))
    pos = open_position(account, clock)
    clock.advance(10)
    mark(account, pos, clock, "13")  # -35%
    d = engine.evaluate(pos, MonitorState(), None, features(), clock.now())
    assert d is not None and d.reason is ExitReason.MAX_LOSS and d.urgency is Urgency.HIGH
    pos2 = open_position(account, clock)
    mark(account, pos2, clock, "20")
    assert engine.evaluate(pos2, MonitorState(), None, features(), clock.now()) is None
    clock.advance(100)
    d2 = engine.evaluate(pos2, MonitorState(), None, features(), clock.now())
    assert d2 is not None and d2.reason is ExitReason.TIMEOUT


def test_momentum_and_volume_collapse(account: PortfolioAccount, clock: ManualClock) -> None:
    engine = ExitEngine(ExitConfig(volume_collapse_min_holding_s=30))
    pos = open_position(account, clock)
    state = MonitorState()
    clock.advance(10)
    mark(account, pos, clock, "24")  # +20% peak -> prior gain satisfied
    mark(account, pos, clock, "23")
    d = engine.evaluate(pos, state, None, features(momentum_30s=-0.15), clock.now())
    assert d is not None and d.reason is ExitReason.MOMENTUM_DETERIORATION
    # without prior gain momentum drop is tolerated
    pos2 = open_position(account, clock)
    mark(account, pos2, clock, "20")
    assert (
        engine.evaluate(pos2, MonitorState(), None, features(momentum_30s=-0.15), clock.now())
        is None
    )
    # volume collapse after a surge
    state3 = MonitorState()
    pos3 = open_position(account, clock, cost="5")
    mark(account, pos3, clock, "5")
    assert (
        engine.evaluate(pos3, state3, None, features(trade_velocity_per_min=80.0), clock.now())
        is None
    )
    clock.advance(31)
    d3 = engine.evaluate(pos3, state3, None, features(trade_velocity_per_min=5.0), clock.now())
    assert d3 is not None and d3.reason is ExitReason.VOLUME_COLLAPSE


def test_liquidity_collapse_is_urgent(account: PortfolioAccount, clock: ManualClock) -> None:
    engine = ExitEngine(ExitConfig())
    pos = open_position(account, clock)
    state = MonitorState()
    tracker = TokenTracker()
    track: TokenTrack = tracker.track(make_token(clock=clock))
    mark(account, pos, clock, "20")
    for liq in ("20000", "21000", "8000"):
        clock.advance(5)
        track.add_snapshot(
            MarketSnapshot(
                mint="MintTest",
                observed_at=clock.now(),
                source="t",
                price_native=Decimal("0.001"),
                liquidity_usd=Decimal(liq),
            )
        )
        d = engine.evaluate(pos, state, track, features(), clock.now())
    assert (
        d is not None and d.reason is ExitReason.LIQUIDITY_COLLAPSE and d.urgency is Urgency.URGENT
    )
    assert "-62%" in d.detail


def test_abnormal_and_stale(account: PortfolioAccount, clock: ManualClock) -> None:
    engine = ExitEngine(ExitConfig(stale_exit_after_s=30))
    pos = open_position(account, clock)
    mark(account, pos, clock, "20")
    d = engine.evaluate(pos, MonitorState(), None, features(momentum_10s=3.5), clock.now())
    assert d is not None and d.reason is ExitReason.ABNORMAL_EVENT and d.urgency is Urgency.URGENT
    tracker = TokenTracker()
    track = tracker.track(make_token(clock=clock))
    track.add_snapshot(
        MarketSnapshot(
            mint="MintTest",
            observed_at=clock.now(),
            source="t",
            price_native=Decimal(1),
            spread_bps=2500,
        )
    )
    d2 = engine.evaluate(pos, MonitorState(), track, features(), clock.now())
    assert d2 is not None and d2.reason is ExitReason.ABNORMAL_EVENT
    state = MonitorState()
    assert (
        engine.evaluate(pos, state, None, features(stale=True, data_age_s=25), clock.now()) is None
    )
    clock.advance(31)
    d3 = engine.evaluate(pos, state, None, features(stale=True, data_age_s=56), clock.now())
    assert d3 is not None and d3.reason is ExitReason.DATA_STALE
    assert engine.evaluate(pos, state, None, features(), clock.now()) is None
    assert state.stale_since is None


def test_monitor_cooldown_and_valuation(account: PortfolioAccount, clock: ManualClock) -> None:
    monitor = PositionMonitor(ExitConfig(exit_signal_cooldown_s=10, max_loss_pct=0.1), account)
    pos = open_position(account, clock)
    clock.advance(5)
    from solana_sniper.domain.models import SwapQuote, new_id

    quote = SwapQuote(
        quote_id=new_id("q"),
        provider="t",
        input_mint="MintTest",
        output_mint="sol",
        in_amount_raw=1,
        out_amount_raw=100_000_000,
        other_amount_threshold_raw=0,
        slippage_bps=0,
        price_impact_pct=0,
        route_labels=(),
        fee_lamports=0,
        quoted_at=clock.now(),
        latency_ms=1,
    )
    valued = monitor.value(
        pos,
        now=clock.now(),
        sol_eur=Decimal("150"),
        exit_quote=quote,
        max_quote_age_s=8,
        price_native=Decimal("1"),
    )
    assert valued.current_value_eur == Decimal("15") and valued.value_is_executable
    clock.advance(9)
    valued = monitor.value(
        pos,
        now=clock.now(),
        sol_eur=Decimal("150"),
        exit_quote=quote,
        max_quote_age_s=8,
        price_native=Decimal("0.00001"),
    )
    assert not valued.value_is_executable  # stale quote ignored, displayed price used
    assert valued.current_value_eur == Decimal("0.00001") * Decimal(1000) * Decimal(150)
    d = monitor.evaluate(pos, None, features(), clock.now())
    assert d is not None and d.reason is ExitReason.MAX_LOSS
    assert monitor.evaluate(pos, None, features(), clock.now()) is None  # cooldown
    clock.advance(11)
    assert monitor.evaluate(pos, None, features(), clock.now()) is not None
    assert monitor.state_for(pos.position_id).exit_signals == 2
    monitor.forget(pos.position_id)
    assert monitor.state_for(pos.position_id).exit_signals == 0
    untouched = monitor.value(
        pos,
        now=clock.now(),
        sol_eur=Decimal("150"),
        exit_quote=None,
        max_quote_age_s=8,
        price_native=None,
    )
    assert untouched is pos


def test_custom_tiers_config_validation() -> None:
    with pytest.raises(ValueError):
        TrailingConfig(
            tiers=[
                TrailingTier(min_multiple=2, drawdown_pct=0.1),
                TrailingTier(min_multiple=1, drawdown_pct=0.2),
            ]
        )
    with pytest.raises(ValueError):
        TrailingConfig(tiers=[])
