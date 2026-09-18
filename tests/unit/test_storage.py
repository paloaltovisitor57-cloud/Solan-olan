from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from solana_sniper.domain.clock import ManualClock
from solana_sniper.domain.enums import CandidateState, ExitReason, SignalKind, Venue
from solana_sniper.domain.models import (
    ErrorRecord,
    MarketSnapshot,
    MilestoneEvent,
    Position,
    SwapQuote,
    TokenInfo,
    TradeEvent,
    new_id,
)
from solana_sniper.portfolio.accounting import PortfolioAccount
from solana_sniper.storage.repository import Repository
from solana_sniper.storage.serialization import dataclass_from_dict, from_jsonable, to_jsonable
from solana_sniper.telemetry.metrics import Metrics
from tests.conftest import make_fill


def test_serialization_roundtrip(clock: ManualClock) -> None:
    pos = Position(
        position_id="pos_1",
        mint="m",
        symbol="S",
        opened_at=clock.now(),
        entry_price_native=Decimal("0.0001"),
        entry_sol_eur=Decimal("150"),
        quantity_ui=Decimal("1000.5"),
        cost_basis_eur=Decimal("20.25"),
        entry_sol=Decimal("0.135"),
        exit_reason=ExitReason.TIMEOUT,
        state=CandidateState.OPEN,
    )
    data = to_jsonable(pos)
    assert data["quantity_ui"] == {"__dec__": "1000.5"}
    assert data["exit_reason"] == "TIMEOUT"
    back = dataclass_from_dict(Position, data)
    assert back == pos
    assert back.opened_at.tzinfo is not None
    snap = MarketSnapshot(
        mint="m",
        observed_at=clock.now(),
        source="s",
        price_native=Decimal("1.5"),
        venue=Venue.RAYDIUM,
    )
    assert dataclass_from_dict(MarketSnapshot, to_jsonable(snap)) == snap
    assert from_jsonable({"a": [{"__dec__": "1"}]}) == {"a": [Decimal(1)]}


@pytest.fixture
async def repo(tmp_path: object) -> Repository:
    r = Repository(
        "sqlite+aiosqlite:///:memory:",
        session_id="sess_test",
        batch_size=10,
        flush_interval_s=0.05,
        metrics=Metrics(),
    )
    await r.init()
    r.start()
    return r


async def test_write_flush_and_counts(repo: Repository, clock: ManualClock) -> None:
    await repo.start_session("DRY_RUN", "configs/test.yaml")
    token = TokenInfo(
        mint="m1", symbol="S", venue=Venue.PUMP_FUN, source="test", discovered_at=clock.now()
    )
    repo.save_token(token)
    for i in range(25):
        repo.save_observation(
            MarketSnapshot(
                mint="m1",
                observed_at=clock.now() + timedelta(seconds=i),
                source="t",
                price_native=Decimal(i + 1),
            )
        )
    repo.save_trade(
        TradeEvent(
            mint="m1",
            observed_at=clock.now(),
            source="t",
            is_buy=True,
            sol_amount=Decimal(1),
            token_amount=Decimal(2),
            signature="sig",
        )
    )
    repo.save_quote(
        SwapQuote(
            quote_id="q1",
            provider="p",
            input_mint="a",
            output_mint="m1",
            in_amount_raw=1,
            out_amount_raw=2,
            other_amount_threshold_raw=1,
            slippage_bps=1,
            price_impact_pct=0.1,
            route_labels=("x",),
            fee_lamports=1,
            quoted_at=clock.now(),
            latency_ms=1,
            raw={"big": "payload"},
        ),
        "m1",
    )
    repo.save_error(ErrorRecord(at=clock.now(), component="test", message="boom"))
    repo.save_milestone(
        MilestoneEvent(
            milestone_eur=Decimal(50),
            equity_eur=Decimal(51),
            reached_at=clock.now(),
            direction="UP",
        )
    )
    repo.save_transition("m1", "DISCOVERED", "MONITORING", clock.now(), "first data")
    await repo.flush()
    counts = await repo.counts()
    assert (
        counts["tokens"] == 1
        and counts["observations"] == 25
        and counts["trades"] == 1
        and counts["errors"] == 1
    )
    stream = await repo.session_stream("sess_test")
    kinds = [k for _, k, _ in stream]
    assert (
        kinds[0] == "token"
        and kinds.count("observation") == 25
        and "quote" in kinds
        and "trade" in kinds
    )
    assert all(stream[i][0] <= stream[i + 1][0] for i in range(len(stream) - 1))
    quote_payload = next(p for _, k, p in stream if k == "quote")
    assert "raw" not in quote_payload and quote_payload["mint"] == "m1"
    history = await repo.token_history("m1")
    assert history["token"]["symbol"] == "S" and history["transitions"][0]["to"] == "MONITORING"
    sessions = await repo.list_sessions()
    assert sessions[0]["session_id"] == "sess_test" and sessions[0]["mode"] == "DRY_RUN"
    await repo.end_session()
    assert (await repo.list_sessions())[0]["ended_at"] is not None
    pruned = await repo.prune_observations(clock.now() + timedelta(seconds=10))
    assert pruned == 11  # 10 observations + 1 trade
    await repo.close()


async def test_restart_recovery_rebuilds_portfolio(repo: Repository, clock: ManualClock) -> None:
    account = PortfolioAccount(clock, "sess_test")
    account.deposit(Decimal("50"))
    buy = make_fill(clock, side=SignalKind.BUY, eur_amount=Decimal("20"), fee_eur=Decimal("0.5"))
    pos = account.open_position(buy, symbol="TST", entry_price_native=Decimal("0.001"))
    account.mark_position(
        pos.position_id,
        value_eur=Decimal("30"),
        price_native=Decimal("0.0015"),
        at=clock.now(),
        executable=True,
    )
    await repo.save_ledger_now(account.ledger)
    await repo.save_position_now(pos)
    await repo.save_fill_now(buy)
    await repo.save_account_state_now(
        cash=account.cash,
        peak_equity=account.peak_equity,
        realized_pnl=account.realized_pnl,
        fees=account.fees_total,
        slippage=account.slippage_total,
        wins=0,
        losses=0,
        recent_results=[],
        milestones_reached=[Decimal(50)],
    )
    # simulate a crash: brand new account object restored from storage
    state = await repo.load_state()
    assert state.has_account
    restored = PortfolioAccount(clock, "sess_new")
    restored.restore(
        cash=state.cash,
        ledger=state.ledger,
        positions=state.positions,
        peak_equity=state.peak_equity,
        realized_pnl=state.realized_pnl,
        fees=state.fees,
        slippage=state.slippage,
        wins=state.wins,
        losses=state.losses,
        recent_results=state.recent_results,
    )
    assert restored.cash == Decimal("29.5")
    assert len(restored.open_positions) == 1
    rp = restored.open_positions[0]
    assert (
        rp.position_id == pos.position_id
        and rp.peak_value_eur == Decimal("30")
        and rp.quantity_ui == pos.quantity_ui
    )
    assert restored.equity == Decimal("59.5") and restored.peak_equity == Decimal("59.5")
    assert state.milestones_reached == [Decimal(50)]
    # closing the restored position persists as CLOSED and no longer loads as open
    sell = make_fill(clock, side=SignalKind.SELL, eur_amount=Decimal("28"), fee_eur=Decimal("0.2"))
    closed = restored.close_position(rp.position_id, sell, ExitReason.TRAILING_PEAK)
    await repo.save_position_now(closed)
    await repo.save_ledger_now(restored.ledger[-1:])
    assert (await repo.positions(open_only=True)) == []
    assert len(await repo.positions(open_only=False)) == 1
    await repo.close()


async def test_recovery_from_ledger_without_summary(repo: Repository, clock: ManualClock) -> None:
    account = PortfolioAccount(clock, "sess_test")
    account.deposit(Decimal("50"))
    buy = make_fill(clock, side=SignalKind.BUY, eur_amount=Decimal("10"), fee_eur=Decimal("0"))
    pos = account.open_position(buy, symbol="TST", entry_price_native=Decimal("1"))
    sell = make_fill(clock, side=SignalKind.SELL, eur_amount=Decimal("14"), fee_eur=Decimal("0"))
    account.close_position(pos.position_id, sell, ExitReason.TIMEOUT)
    await repo.save_ledger_now(account.ledger)
    state = await repo.load_state()
    assert state.has_account and state.cash == Decimal("54") and state.realized_pnl == Decimal("4")
    assert state.wins == 1 and state.positions == []
    empty = Repository("sqlite+aiosqlite:///:memory:", session_id="x")
    await empty.init()
    fresh = await empty.load_state()
    assert not fresh.has_account and fresh.cash == 0
    await empty.close()
    await repo.close()


async def test_persist_queue_drop_and_snapshot(repo: Repository, clock: ManualClock) -> None:
    snap = PortfolioAccount(clock, "sess_test").snapshot()
    repo.save_portfolio_snapshot(snap)
    await repo.flush()
    latest = await repo.latest_portfolio_snapshot()
    assert latest is not None and latest.cash_eur == Decimal(0)
    # telemetry budget exhausted: the drop is counted per kind and degrades the repository
    repo._max_telemetry = 1
    repo.save_error(ErrorRecord(at=datetime.now(tz=UTC), component="a", message="1"))
    repo.save_error(ErrorRecord(at=datetime.now(tz=UTC), component="a", message="2"))
    assert repo.dropped == 1 and repo.dropped_by_kind == {"error": 1} and repo.degraded
    await repo.close()


def test_new_id_unique() -> None:
    assert new_id("a") != new_id("a") and new_id("a").startswith("a_")
