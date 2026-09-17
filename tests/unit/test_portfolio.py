from __future__ import annotations

from decimal import Decimal

import pytest

from solana_sniper.domain.clock import ManualClock
from solana_sniper.domain.enums import ExitReason, LedgerEntryKind, SignalKind
from solana_sniper.portfolio.accounting import (
    InsufficientCashError,
    PortfolioAccount,
    PositionAlreadyClosedError,
)
from tests.conftest import make_fill


def test_deposit_and_snapshot(account: PortfolioAccount) -> None:
    snap = account.snapshot()
    assert snap.cash_eur == Decimal("50")
    assert snap.equity_eur == Decimal("50")
    assert snap.peak_equity_eur == Decimal("50")
    assert account.ledger[0].kind is LedgerEntryKind.DEPOSIT


def test_open_mark_close_roundtrip(account: PortfolioAccount, clock: ManualClock) -> None:
    buy = make_fill(clock, side=SignalKind.BUY, eur_amount=Decimal("20"), fee_eur=Decimal("0.5"))
    pos = account.open_position(buy, symbol="TST", entry_price_native=Decimal("0.0001"))
    assert account.cash == Decimal("29.5")
    assert pos.cost_basis_eur == Decimal("20.5")
    assert account.equity == Decimal("49.5")  # fee is a realized cost immediately
    clock.advance(10)
    account.mark_position(
        pos.position_id,
        value_eur=Decimal("40"),
        price_native=Decimal("0.0002"),
        at=clock.now(),
        executable=True,
    )
    assert pos.peak_value_eur == Decimal("40")
    assert account.equity == Decimal("69.5")
    assert account.peak_equity == Decimal("69.5")
    account.mark_position(
        pos.position_id,
        value_eur=Decimal("30"),
        price_native=Decimal("0.00015"),
        at=clock.now(),
        executable=True,
    )
    assert pos.peak_value_eur == Decimal("40")
    assert account.drawdown_pct == pytest.approx(10 / 69.5)
    sell = make_fill(clock, side=SignalKind.SELL, eur_amount=Decimal("30"), fee_eur=Decimal("0.2"))
    closed = account.close_position(pos.position_id, sell, ExitReason.TRAILING_PEAK)
    assert closed.realized_pnl_eur == Decimal("29.8") - Decimal("20.5")
    assert account.cash == Decimal("29.5") + Decimal("29.8")
    assert account.realized_pnl == Decimal("9.3")
    assert account.wins == 1
    assert account.open_positions == []
    assert account.equity == account.cash
    kinds = [e.kind for e in account.ledger]
    assert kinds == [LedgerEntryKind.DEPOSIT, LedgerEntryKind.BUY, LedgerEntryKind.SELL]
    assert [e.seq for e in account.ledger] == [1, 2, 3]


def test_cash_never_negative(account: PortfolioAccount, clock: ManualClock) -> None:
    buy = make_fill(
        clock, side=SignalKind.BUY, eur_amount=Decimal("49.99"), fee_eur=Decimal("0.02")
    )
    with pytest.raises(InsufficientCashError):
        account.open_position(buy, symbol="TST", entry_price_native=Decimal("1"))
    assert account.cash == Decimal("50")
    assert account.positions == {}
    assert len(account.ledger) == 1


def test_cannot_close_twice(account: PortfolioAccount, clock: ManualClock) -> None:
    buy = make_fill(clock, side=SignalKind.BUY, eur_amount=Decimal("10"))
    pos = account.open_position(buy, symbol="TST", entry_price_native=Decimal("1"))
    sell = make_fill(clock, side=SignalKind.SELL, eur_amount=Decimal("5"))
    account.close_position(pos.position_id, sell, ExitReason.MAX_LOSS)
    with pytest.raises(PositionAlreadyClosedError):
        account.close_position(pos.position_id, sell, ExitReason.MAX_LOSS)
    with pytest.raises(PositionAlreadyClosedError):
        account.mark_position(
            pos.position_id,
            value_eur=Decimal("1"),
            price_native=Decimal("1"),
            at=clock.now(),
            executable=True,
        )
    assert account.losses == 1
    assert account.cash == Decimal("50") - Decimal("10.05") + Decimal("4.95")


def test_recent_performance_streaks(account: PortfolioAccount, clock: ManualClock) -> None:
    for pnl in (Decimal("-2"), Decimal("-1"), Decimal("3"), Decimal("-1"), Decimal("-1")):
        buy = make_fill(clock, side=SignalKind.BUY, eur_amount=Decimal("5"), fee_eur=Decimal("0"))
        pos = account.open_position(buy, symbol="T", entry_price_native=Decimal("1"))
        sell = make_fill(
            clock, side=SignalKind.SELL, eur_amount=Decimal("5") + pnl, fee_eur=Decimal("0")
        )
        account.close_position(pos.position_id, sell, ExitReason.TIMEOUT)
    perf = account.recent_performance()
    assert perf.consecutive_losses == 2
    assert perf.consecutive_wins == 0
    assert perf.wins == 1 and perf.losses == 4
    assert perf.expectancy_eur == Decimal("-0.4")


def test_restore_rebuilds_state(account: PortfolioAccount, clock: ManualClock) -> None:
    buy = make_fill(clock, side=SignalKind.BUY, eur_amount=Decimal("20"))
    pos = account.open_position(buy, symbol="TST", entry_price_native=Decimal("1"))
    fresh = PortfolioAccount(clock, "test")
    fresh.restore(
        cash=account.cash,
        ledger=account.ledger,
        positions=list(account.positions.values()),
        peak_equity=account.peak_equity,
        realized_pnl=account.realized_pnl,
        fees=account.fees_total,
        slippage=account.slippage_total,
        wins=0,
        losses=0,
        recent_results=[],
    )
    assert fresh.cash == account.cash
    assert fresh.open_positions[0].position_id == pos.position_id
    assert fresh.equity == account.equity
    with pytest.raises(InsufficientCashError):
        fresh.restore(
            cash=Decimal("-1"),
            ledger=[],
            positions=[],
            peak_equity=Decimal(0),
            realized_pnl=Decimal(0),
            fees=Decimal(0),
            slippage=Decimal(0),
            wins=0,
            losses=0,
            recent_results=[],
        )
