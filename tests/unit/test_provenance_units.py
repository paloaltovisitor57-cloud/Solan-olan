"""Findings 3 and 4 regression: explicit provenance and token units, with migration policy."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from typer.testing import CliRunner

from solana_sniper.cli.main import app
from solana_sniper.config.settings import QuotesConfig
from solana_sniper.domain.clock import ManualClock
from solana_sniper.domain.enums import (
    DecisionKind,
    DecisionSource,
    ExitReason,
    FillProvenance,
    SignalKind,
    TokenUnits,
    weakest_provenance,
)
from solana_sniper.domain.models import Fill, LedgerEntry, Position, new_id
from solana_sniper.domain.money import UnitError, raw_to_ui, ui_to_raw
from solana_sniper.execution.base import FillOverride
from solana_sniper.execution.manual import DryRunExecution, ManualExecution
from solana_sniper.portfolio.accounting import PortfolioAccount
from solana_sniper.storage.migrations import run_migrations, schema_version
from solana_sniper.storage.repository import Repository
from solana_sniper.storage.serialization import dataclass_from_dict, to_jsonable
from tests.conftest import make_fill
from tests.unit.test_execution import make_buy_signal, make_sell_signal


# ------------------------------------------------------------------ money units
def test_raw_ui_conversions_are_exact_for_several_decimal_scales() -> None:
    for decimals, ui in (
        (0, Decimal("12345")),
        (6, Decimal("1234.567891")),
        (9, Decimal("0.000000001")),
    ):
        raw = ui_to_raw(ui, decimals, exact=True)
        assert raw_to_ui(raw, decimals) == ui
    assert ui_to_raw(Decimal("1.5"), 0) == 1  # rounds down when not exact
    with pytest.raises(UnitError):
        ui_to_raw(Decimal("1.5"), 0, exact=True)
    for bad in (-1, 19):
        with pytest.raises(UnitError):
            raw_to_ui(1, bad)
    with pytest.raises(UnitError):
        raw_to_ui(-1, 6)


def test_fill_rejects_inconsistent_units_and_fake_verification(clock: ManualClock) -> None:
    ok = make_fill(clock, side=SignalKind.BUY, token_amount=Decimal("1.5"), decimals=9)
    assert ok.token_amount_raw == 1_500_000_000 and ok.units is TokenUnits.UI
    base = to_jsonable(ok)
    with pytest.raises(ValueError):
        replace(ok, token_amount_raw=1)
    with pytest.raises(ValueError):
        replace(ok, verified_onchain=True)
    with pytest.raises(ValueError):
        replace(ok, simulated=False)  # SIMULATED provenance requires simulated=True
    assert dataclass_from_dict(Fill, base) == ok


# --------------------------------------------------------- executor provenance
async def test_manual_confirmation_provenance_and_sell_units(clock: ManualClock) -> None:
    ex = ManualExecution(clock, QuotesConfig())
    order = await ex.submit_buy(make_buy_signal(clock))
    res = await ex.decide(order, DecisionKind.CONFIRM, DecisionSource.HUMAN)
    assert res.fill is not None
    assert res.fill.provenance is FillProvenance.ESTIMATED  # quoted amounts, not a wallet
    assert not res.fill.simulated and not res.fill.verified_onchain and not res.fill.is_verified
    assert res.fill.reported_tx_signature is None
    # user reports amounts + a signature string: USER_REPORTED, still unverified
    order2 = await ex.submit_buy(make_buy_signal(clock, mint="Other"))
    res2 = await ex.decide(
        order2,
        DecisionKind.CONFIRM,
        DecisionSource.HUMAN,
        override=FillOverride(token_amount_ui=Decimal("900"), reported_tx_signature="5sigSENTINEL"),
    )
    assert res2.fill is not None and res2.fill.provenance is FillProvenance.USER_REPORTED
    assert res2.fill.reported_tx_signature == "5sigSENTINEL" and not res2.fill.verified_onchain
    # a signature alone (no amounts) is also just user-reported, never verified
    order3 = await ex.submit_buy(make_buy_signal(clock, mint="Third"))
    res3 = await ex.decide(
        order3,
        DecisionKind.CONFIRM,
        DecisionSource.HUMAN,
        override=FillOverride(reported_tx_signature="x"),
    )
    assert res3.fill is not None and res3.fill.provenance is FillProvenance.USER_REPORTED
    assert not res3.fill.verified_onchain
    # sell fills take the position quantity in UI units, converted exactly at the token's decimals
    for decimals, qty in ((6, Decimal("1000")), (9, Decimal("0.123456789")), (0, Decimal("77"))):
        sell = replace(
            make_sell_signal(clock, position_id=f"pos_{decimals}"),
            quantity_ui=qty,
            token_decimals=decimals,
        )
        o = await ex.submit_sell(sell)
        r = await ex.decide(o, DecisionKind.CONFIRM, DecisionSource.HUMAN)
        assert r.fill is not None
        assert r.fill.token_amount_ui == qty and r.fill.token_decimals == decimals
        assert r.fill.token_amount_raw == ui_to_raw(qty, decimals, exact=True)
        assert raw_to_ui(r.fill.token_amount_raw, decimals) == qty
        assert r.fill.provenance is FillProvenance.ESTIMATED
    dry = DryRunExecution(clock, QuotesConfig(), confirm_delay_s=0)
    o4 = await dry.submit_buy(make_buy_signal(clock, mint="Dry"))
    r4 = (await dry.auto_confirm(clock.now()))[0]
    assert r4.order is o4 and r4.fill is not None
    assert r4.fill.provenance is FillProvenance.SIMULATED and r4.fill.simulated


def test_position_and_ledger_carry_weakest_provenance(clock: ManualClock) -> None:
    acct = PortfolioAccount(clock)
    acct.deposit(Decimal("50"))
    assert acct.ledger[0].provenance is FillProvenance.USER_REPORTED  # cash the user declared
    buy = make_fill(
        clock,
        side=SignalKind.BUY,
        eur_amount=Decimal("10"),
        provenance=FillProvenance.USER_REPORTED,
        decimals=9,
    )
    pos = acct.open_position(buy, symbol="T", entry_price_native=Decimal("1"))
    assert pos.provenance is FillProvenance.USER_REPORTED and pos.units is TokenUnits.UI
    assert pos.quantity_raw == buy.token_amount_raw and pos.token_decimals == 9 and pos.units_known
    assert not pos.is_verified
    sell = make_fill(
        clock,
        side=SignalKind.SELL,
        eur_amount=Decimal("12"),
        provenance=FillProvenance.ESTIMATED,
        decimals=9,
    )
    closed = acct.close_position(pos.position_id, sell, ExitReason.TIMEOUT)
    assert closed.provenance is FillProvenance.ESTIMATED  # weakest of the two fills
    assert [e.provenance for e in acct.ledger[1:]] == [
        FillProvenance.USER_REPORTED,
        FillProvenance.ESTIMATED,
    ]
    assert (
        weakest_provenance(FillProvenance.VERIFIED_ONCHAIN, FillProvenance.SIMULATED)
        is FillProvenance.SIMULATED
    )
    assert weakest_provenance() is FillProvenance.UNKNOWN_LEGACY


# ------------------------------------------------------------------- migration
async def test_migration_v2_flags_legacy_rows_without_guessing(
    tmp_path: Path, clock: ManualClock
) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path}/legacy.db"
    assert await run_migrations(url) == [1, 2, 3]
    engine = create_async_engine(url)
    now = datetime.now(tz=UTC).isoformat()
    legacy_fill = {
        "fill_id": "fill_old",
        "signal_id": "s",
        "mint": "m",
        "side": "SELL",
        "filled_at": {"__dt__": now},
        "sol_amount": {"__dec__": "0.1"},
        "token_amount": {"__dec__": "1000000000"},  # ambiguous: raw for old sells, UI for old buys
        "eur_amount": {"__dec__": "15"},
        "sol_eur": {"__dec__": "150"},
        "fee_eur": {"__dec__": "0"},
        "slippage_cost_eur": {"__dec__": "0"},
        "simulated": False,
        "tx_signature": "5abc",
    }
    legacy_pos = {
        "position_id": "pos_old",
        "mint": "m",
        "symbol": "T",
        "opened_at": {"__dt__": now},
        "entry_price_native": {"__dec__": "1"},
        "entry_sol_eur": {"__dec__": "150"},
        "quantity": {"__dec__": "1000"},
        "cost_basis_eur": {"__dec__": "15"},
        "entry_sol": {"__dec__": "0.1"},
        "state": "OPEN",
    }
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM schema_version WHERE version >= 2"))
        await conn.execute(
            text(
                "INSERT INTO fills (fill_id, session_id, signal_id, mint, side, filled_at, payload)"
                " VALUES (:a,:b,:c,:d,:e,:f,:g)"
            ),
            {
                "a": "fill_old",
                "b": "s",
                "c": "sig",
                "d": "m",
                "e": "SELL",
                "f": datetime.now(tz=UTC),
                "g": json.dumps(legacy_fill),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO positions (position_id, session_id, mint, state, payload, updated_at)"
                " VALUES (:a,:b,:c,:d,:e,:f)"
            ),
            {
                "a": "pos_old",
                "b": "s",
                "c": "m",
                "d": "OPEN",
                "e": json.dumps(legacy_pos),
                "f": datetime.now(tz=UTC),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO ledger (entry_id, seq, session_id, kind, at, cash_delta_eur,"
                " cash_after_eur, description, fee_eur, slippage_eur, realized_pnl_eur, provenance)"
                " VALUES ('led_old', 1, 's', 'BUY', :t, '-15', '35', 'old', '0', '0', '0', NULL)"
            ),
            {"t": datetime.now(tz=UTC)},
        )
    await engine.dispose()
    assert await run_migrations(url) == [2, 3]
    assert await schema_version(url) == 3
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        fill_payload = json.loads(
            (await conn.execute(text("SELECT payload FROM fills"))).scalar_one()
        )
        pos_payload = json.loads(
            (await conn.execute(text("SELECT payload FROM positions"))).scalar_one()
        )
        prov = (await conn.execute(text("SELECT provenance FROM ledger"))).scalar_one()
    await engine.dispose()
    assert (
        fill_payload["provenance"] == "UNKNOWN_LEGACY" and fill_payload["units"] == "UNKNOWN_LEGACY"
    )
    assert fill_payload["legacy_token_amount"] == {"__dec__": "1000000000"}  # kept, not interpreted
    assert fill_payload["reported_tx_signature"] == "5abc" and "verified" not in json.dumps(
        fill_payload
    ).lower().replace("unverified", "")
    assert (
        pos_payload["provenance"] == "UNKNOWN_LEGACY" and pos_payload["units"] == "UNKNOWN_LEGACY"
    )
    assert pos_payload["quantity_ui"] == {"__dec__": "1000"}
    assert prov == "UNKNOWN_LEGACY"
    # the repository loads the flagged legacy position and never reports it verified
    repo = Repository(url, session_id="check")
    await repo.init()
    state = await repo.load_state()
    await repo.close()
    assert state.ledger[0].provenance is FillProvenance.UNKNOWN_LEGACY
    legacy = state.positions[0]
    assert legacy.units is TokenUnits.UNKNOWN_LEGACY and not legacy.units_known
    assert legacy.provenance is FillProvenance.UNKNOWN_LEGACY and not legacy.is_verified
    assert legacy.quantity_ui == Decimal("1000")
    # loading an old fill payload from storage never fabricates units either
    old_fill = dataclass_from_dict(Fill, fill_payload)
    assert (
        old_fill.units is TokenUnits.UNKNOWN_LEGACY
        and old_fill.provenance is FillProvenance.UNKNOWN_LEGACY
    )


def test_cli_positions_and_portfolio_label_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: ManualClock
) -> None:
    import asyncio

    monkeypatch.setenv("SNIPER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("COLUMNS", "220")
    from solana_sniper.config import load_settings

    settings = load_settings(Path("configs/synthetic.yaml"))

    async def seed() -> None:
        repo = Repository(settings.storage.database_url, session_id="seed")
        await repo.init()
        acct = PortfolioAccount(clock)
        acct.deposit(Decimal("50"))
        pos = acct.open_position(
            make_fill(
                clock,
                side=SignalKind.BUY,
                eur_amount=Decimal("10"),
                provenance=FillProvenance.ESTIMATED,
            ),
            symbol="EST",
            entry_price_native=Decimal("1"),
        )
        await repo.save_position_now(pos)
        await repo.save_ledger_now(acct.ledger)
        await repo.close()

    asyncio.run(seed())
    runner = CliRunner()
    res = runner.invoke(app, ["positions", "-c", "configs/synthetic.yaml"])
    assert res.exit_code == 0, res.output
    assert (
        "ESTIMATED" in res.output
        and "on-chain verified" in res.output
        and "verified" in res.output.lower()
    )
    res2 = runner.invoke(app, ["portfolio", "-c", "configs/synthetic.yaml"])
    assert res2.exit_code == 0, res2.output
    assert (
        "nothing is" in res2.output
        and "USER_REPORTED 1" in res2.output
        and "ESTIMATED 1" in res2.output
    )


def test_position_defaults_are_legacy_never_verified(clock: ManualClock) -> None:
    pos = Position(
        position_id="p",
        mint="m",
        symbol=None,
        opened_at=clock.now(),
        entry_price_native=Decimal(1),
        entry_sol_eur=Decimal(1),
        quantity_ui=Decimal(1),
        cost_basis_eur=Decimal(1),
        entry_sol=Decimal(1),
    )
    assert (
        pos.provenance is FillProvenance.UNKNOWN_LEGACY and pos.units is TokenUnits.UNKNOWN_LEGACY
    )
    assert not pos.is_verified and not pos.units_known
    entry = LedgerEntry(
        entry_id=new_id("l"),
        seq=1,
        kind="BUY",
        at=clock.now() + timedelta(seconds=1),  # type: ignore[arg-type]
        cash_delta_eur=Decimal(0),
        cash_after_eur=Decimal(0),
        position_id=None,
        mint=None,
        description="",
    )
    assert entry.provenance is FillProvenance.UNKNOWN_LEGACY
