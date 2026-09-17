from __future__ import annotations

from decimal import Decimal

import pytest

from solana_sniper.config.settings import RiskConfig, Settings
from solana_sniper.domain.clock import ManualClock
from solana_sniper.domain.enums import RiskProfileName
from solana_sniper.domain.money import q_eur
from solana_sniper.portfolio.accounting import PortfolioAccount, RecentPerformance
from solana_sniper.risk.engine import RiskEngine, SizingInputs
from solana_sniper.risk.milestones import MilestoneTracker


def _perf(losses: int = 0, wins: int = 0) -> RecentPerformance:
    return RecentPerformance(
        window=10,
        results=tuple([Decimal("-1")] * losses + [Decimal("1")] * wins),
        wins=wins,
        losses=losses,
        consecutive_losses=losses if wins == 0 else 0,
        consecutive_wins=wins,
        expectancy_eur=Decimal("0"),
        win_rate=0.5,
    )


def _inputs(**kw: object) -> SizingInputs:
    base: dict[str, object] = {
        "equity_eur": Decimal("50"),
        "available_cash_eur": Decimal("50"),
        "open_exposure_eur": Decimal("0"),
        "open_positions": 0,
        "score": 90.0,
        "liquidity_eur": Decimal("20000"),
        "entry_slippage_bps": 100,
        "exit_slippage_bps": 100,
        "drawdown_pct": 0.0,
        "recent": _perf(),
        "sol_eur": Decimal("150"),
    }
    base.update(kw)
    return SizingInputs(**base)  # type: ignore[arg-type]


def test_extreme_profile_sizes_half_equity(settings: Settings) -> None:
    engine = RiskEngine(settings.risk)
    sizing = engine.size(_inputs())
    assert sizing.recommended_eur == Decimal("25")
    assert sizing.recommended_sol == q_eur(Decimal("25") / Decimal("150"))
    assert sizing.profile == "EXTREME"
    assert sizing.tier == "micro"
    assert sizing.caps_applied == ()


def test_never_exceeds_available_cash(settings: Settings) -> None:
    engine = RiskEngine(settings.risk)
    sizing = engine.size(_inputs(available_cash_eur=Decimal("7")))
    assert sizing.recommended_eur == Decimal("7")
    assert "available_cash" in sizing.caps_applied


def test_total_exposure_cap(settings: Settings) -> None:
    engine = RiskEngine(settings.risk)
    sizing = engine.size(
        _inputs(open_exposure_eur=Decimal("40"), open_positions=1, available_cash_eur=Decimal("10"))
    )
    # 95% of 50 = 47.5 exposure room = 7.5
    assert sizing.recommended_eur == Decimal("7.5")
    assert "total_exposure" in sizing.caps_applied


def test_max_open_positions_blocks(settings: Settings) -> None:
    engine = RiskEngine(settings.risk)
    sizing = engine.size(_inputs(open_positions=2))
    assert sizing.recommended_eur == 0
    assert "max_open_positions" in sizing.caps_applied


def test_liquidity_cap(settings: Settings) -> None:
    engine = RiskEngine(settings.risk)
    sizing = engine.size(_inputs(liquidity_eur=Decimal("200")))
    assert sizing.recommended_eur == Decimal("6")
    assert "pool_liquidity" in sizing.caps_applied


def test_below_minimum_yields_zero(settings: Settings) -> None:
    engine = RiskEngine(settings.risk)
    sizing = engine.size(_inputs(liquidity_eur=Decimal("100")))
    assert sizing.recommended_eur == 0
    assert "below_min_position" in sizing.caps_applied


def test_multipliers_reduce_size(settings: Settings) -> None:
    engine = RiskEngine(settings.risk)
    full = engine.size(_inputs()).recommended_eur
    low_conf = engine.size(_inputs(score=60.0)).recommended_eur
    dd = engine.size(_inputs(drawdown_pct=0.5)).recommended_eur
    streak = engine.size(_inputs(recent=_perf(losses=3))).recommended_eur
    slip = engine.size(_inputs(entry_slippage_bps=600, exit_slippage_bps=600)).recommended_eur
    assert low_conf == full / 2
    assert dd == Decimal("8.75")  # floor multiplier 0.35
    assert streak < full
    assert slip < full
    assert engine.streak_multiplier(_perf(losses=10)) == settings.risk.streak_min_multiplier
    assert engine.streak_multiplier(_perf(wins=10)) == settings.risk.streak_max_multiplier


def test_tiers_scale_down_with_bankroll(settings: Settings) -> None:
    engine = RiskEngine(settings.risk)
    micro = engine.size(_inputs()).fraction_of_equity
    large = engine.size(
        _inputs(
            equity_eur=Decimal("10000"),
            available_cash_eur=Decimal("10000"),
            liquidity_eur=Decimal("10000000"),
        )
    )
    assert large.tier == "large"
    assert large.fraction_of_equity < micro
    assert large.recommended_eur == Decimal("10000") * Decimal("0.5") * Decimal("0.45")
    whale = engine.size(
        _inputs(
            equity_eur=Decimal("200000"),
            available_cash_eur=Decimal("200000"),
            liquidity_eur=Decimal("1e9"),
            open_positions=3,
        )
    )
    assert whale.tier == "whale"
    assert whale.recommended_eur > 0  # whale tier allows up to 4 positions


def test_hard_limits_apply_to_all_profiles(settings: Settings) -> None:
    cfg = RiskConfig(
        profile=RiskProfileName.EXTREME,
        profiles={
            RiskProfileName.EXTREME: settings.risk.profiles[RiskProfileName.EXTREME].model_copy(
                update={
                    "base_fraction": 5.0,
                    "max_fraction": 5.0,
                    "max_total_exposure_fraction": 5.0,
                }
            )
        },
    )
    engine = RiskEngine(cfg)
    sizing = engine.size(_inputs(liquidity_eur=None, available_cash_eur=Decimal("1000")))
    assert sizing.recommended_eur == Decimal("50") * Decimal("0.95")
    assert "hard_max_single_position" in sizing.caps_applied


def test_profiles_ordering(settings: Settings) -> None:
    sizes = {}
    for name in RiskProfileName:
        cfg = settings.risk.model_copy(update={"profile": name})
        sizes[name] = RiskEngine(cfg).size(_inputs()).recommended_eur
    assert (
        sizes[RiskProfileName.NORMAL]
        < sizes[RiskProfileName.AGGRESSIVE]
        < sizes[RiskProfileName.EXTREME]
    )


def test_milestones(clock: ManualClock) -> None:
    tracker = MilestoneTracker([Decimal(50), Decimal(100), Decimal(150)])
    events = tracker.update(Decimal("50"), clock.now())
    assert [e.milestone_eur for e in events] == [Decimal(50)]
    assert tracker.next_milestone(Decimal("50")) == Decimal(100)
    events = tracker.update(Decimal("160"), clock.now())
    assert [(e.milestone_eur, e.direction) for e in events] == [
        (Decimal(100), "UP"),
        (Decimal(150), "UP"),
    ]
    assert tracker.update(Decimal("160"), clock.now()) == []
    events = tracker.update(Decimal("120"), clock.now())
    assert [(e.milestone_eur, e.direction) for e in events] == [(Decimal(150), "DOWN")]
    assert tracker.highest_reached == Decimal(100)
    assert tracker.next_milestone(Decimal("1000")) is None
    restored = MilestoneTracker([Decimal(50), Decimal(100), Decimal(150)])
    restored.restore([Decimal(50), Decimal(100), Decimal(999)])
    assert restored.reached == {Decimal(50), Decimal(100)}


def test_sizing_after_bankroll_growth(settings: Settings, clock: ManualClock) -> None:
    """Growing bankroll must change the recommended size without touching config."""
    engine = RiskEngine(settings.risk)
    acct = PortfolioAccount(clock)
    acct.deposit(Decimal("50"))
    inputs = _inputs(
        equity_eur=acct.equity, available_cash_eur=acct.cash, recent=acct.recent_performance()
    )
    first = engine.size(inputs).recommended_eur
    acct.deposit(Decimal("250"))
    grown = engine.size(
        _inputs(
            equity_eur=acct.equity, available_cash_eur=acct.cash, recent=acct.recent_performance()
        )
    )
    assert grown.tier == "small"
    assert grown.recommended_eur > first
    assert grown.recommended_eur == pytest.approx(Decimal("300") * Decimal("0.5") * Decimal("0.85"))
