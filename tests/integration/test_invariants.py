"""Invariants from spec §32."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest

from solana_sniper.config.settings import QuotesConfig
from solana_sniper.domain.clock import ManualClock
from solana_sniper.domain.enums import CandidateState as S
from solana_sniper.domain.enums import (
    DecisionKind,
    DecisionSource,
    ExitReason,
    FillProvenance,
    SignalKind,
)
from solana_sniper.domain.models import Fill, new_id
from solana_sniper.execution.base import ExecutionInterface
from solana_sniper.execution.manual import ManualExecution, OrderNotPendingError
from solana_sniper.portfolio.accounting import (
    InsufficientCashError,
    PortfolioAccount,
    PositionAlreadyClosedError,
)
from tests.conftest import make_fill
from tests.integration.conftest import Harness
from tests.unit.test_execution import make_buy_signal

SRC = Path("src/solana_sniper")
# The only places allowed to hold key material, sign or broadcast: the wallet package and the
# autonomous executor. Everything else (engine, strategy, storage, CLI, dashboard, manual and
# dry-run execution) must stay free of it, so that paper, dry-run and signal modes can never
# reach a key even by accident.
SIGNING_LAYER = ("wallet/", "execution/autonomous.py")
FORBIDDEN = re.compile(
    r"sendTransaction|signTransaction|sendRawTransaction|private_key|secret_key|Keypair", re.I
)


def _allowed_to_sign(path: Path) -> bool:
    rel = path.relative_to(SRC).as_posix()
    return any(rel.startswith(prefix) for prefix in SIGNING_LAYER)


def test_signing_and_broadcast_code_is_confined_to_the_wallet_layer() -> None:
    """Outside the signing layer no module may name sendTransaction/signTransaction or handle
    key material (a comment that says never/must not is allowed, so the boundary can be
    documented where it matters)."""
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        if _allowed_to_sign(path):
            continue
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if (
                FORBIDDEN.search(line)
                and "never" not in line.lower()
                and "must not" not in line.lower()
            ):
                offenders.append(f"{path}:{i}: {line.strip()}")
    assert offenders == [], offenders


def test_signing_layer_is_exactly_where_declared() -> None:
    """The reverse direction: the modules that do sign are the ones the README and the
    dashboard boundary name; key handling has not moved elsewhere under another name."""
    signing_modules = sorted(
        p.relative_to(SRC).as_posix()
        for p in SRC.rglob("*.py")
        if _allowed_to_sign(p) and FORBIDDEN.search(p.read_text())
    )
    assert signing_modules, "the wallet layer must exist and be the one that signs"
    assert all(m.startswith(SIGNING_LAYER) for m in signing_modules)
    web = SRC / "web"
    assert not any(FORBIDDEN.search(p.read_text()) for p in web.rglob("*.py"))


def test_execution_interface_has_no_broadcast_method() -> None:
    names = set(dir(ExecutionInterface)) | set(dir(ManualExecution))
    assert not any(
        "broadcast" in n.lower() or "sign" in n.lower() or "send_tx" in n.lower() for n in names
    )


def test_no_private_key_settings() -> None:
    from solana_sniper.config.settings import Settings

    fields = {
        name
        for model in (
            Settings,
            *[
                f.annotation
                for f in Settings.model_fields.values()
                if hasattr(f.annotation, "model_fields")
            ],
        )
        for name in getattr(model, "model_fields", {})
    }
    assert not any("private" in f or "secret_key" in f or "seed_phrase" in f for f in fields)


def test_cash_never_negative(clock: ManualClock) -> None:
    acct = PortfolioAccount(clock)
    acct.deposit(Decimal("10"))
    with pytest.raises(InsufficientCashError):
        acct.open_position(
            make_fill(
                clock, side=SignalKind.BUY, eur_amount=Decimal("10"), fee_eur=Decimal("0.01")
            ),
            symbol="x",
            entry_price_native=Decimal(1),
        )
    assert acct.cash == Decimal("10")


def test_position_cannot_close_twice(clock: ManualClock) -> None:
    acct = PortfolioAccount(clock)
    acct.deposit(Decimal("10"))
    pos = acct.open_position(
        make_fill(clock, side=SignalKind.BUY, eur_amount=Decimal("5")),
        symbol="x",
        entry_price_native=Decimal(1),
    )
    acct.close_position(
        pos.position_id,
        make_fill(clock, side=SignalKind.SELL, eur_amount=Decimal("5")),
        ExitReason.TIMEOUT,
    )
    with pytest.raises(PositionAlreadyClosedError):
        acct.close_position(
            pos.position_id,
            make_fill(clock, side=SignalKind.SELL, eur_amount=Decimal("5")),
            ExitReason.TIMEOUT,
        )


async def test_rejected_signal_cannot_execute(clock: ManualClock) -> None:
    ex = ManualExecution(clock, QuotesConfig())
    order = await ex.submit_buy(make_buy_signal(clock))
    res = await ex.decide(order, DecisionKind.REJECT, DecisionSource.HUMAN)
    assert res.fill is None
    with pytest.raises(OrderNotPendingError):
        await ex.decide(order, DecisionKind.CONFIRM, DecisionSource.HUMAN)
    assert ex.find(SignalKind.BUY, order.ref) is None


async def test_stale_quote_is_not_current(clock: ManualClock) -> None:
    sig = make_buy_signal(clock)
    assert sig.quote.buy.is_fresh(clock.now(), 8)
    clock.advance(9)
    assert not sig.quote.buy.is_fresh(clock.now(), 8)


async def test_no_duplicate_entries_and_exposure_limits(harness: Harness) -> None:
    engine = harness.engine
    settings = harness.runtime.settings
    hard = settings.risk.hard_limits
    profile = settings.risk.profiles[settings.risk.profile]
    seen_mints_with_open_signal: dict[str, int] = {}
    for _ in range(1200):
        await harness.step(0.5)
        acct = harness.runtime.account
        assert acct.cash >= 0
        assert acct.open_exposure <= acct.peak_equity * Decimal(
            str(max(profile.max_total_exposure_fraction, hard.max_total_exposure_fraction))
        ) + Decimal("0.01")
        assert len(acct.open_positions) <= engine.d.risk.max_open_positions(acct.equity)
        pending = engine.d.execution.pending()
        buys = [o.mint for o in pending if o.kind is SignalKind.BUY]
        assert len(buys) == len(set(buys))  # never two pending buys for one mint
        for o in pending:
            if o.kind is SignalKind.BUY:
                seen_mints_with_open_signal[o.mint] = seen_mints_with_open_signal.get(o.mint, 0) + 1
        for cand in engine.candidates.values():
            if cand.state in (S.BUY_SIGNAL, S.AWAITING_CONFIRMATION):
                assert cand.sm.entry_signal_count >= 1
            # a mint with an open position never has a pending buy
            if cand.position_id and acct.positions[cand.position_id].is_open:
                assert cand.mint not in buys
        for pos in acct.positions.values():
            if pos.state is S.CLOSED:
                assert pos.closed_at is not None and pos.realized_pnl_eur is not None
    assert engine.stats.signals >= 1
    for cand in engine.candidates.values():
        assert cand.sm.entry_signal_count <= 3


def test_fill_never_simulated_in_manual_mode(clock: ManualClock) -> None:
    ex = ManualExecution(clock, QuotesConfig())
    assert not ex.simulated
    f = Fill(
        fill_id=new_id("f"),
        signal_id="s",
        mint="m",
        side=SignalKind.BUY,
        filled_at=clock.now(),
        sol_amount=Decimal(1),
        token_amount_ui=Decimal(1),
        token_amount_raw=10**6,
        token_decimals=6,
        eur_amount=Decimal(1),
        sol_eur=Decimal(1),
        fee_eur=Decimal(0),
        slippage_cost_eur=Decimal(0),
        provenance=FillProvenance.ESTIMATED,
        simulated=False,
    )
    assert not f.simulated and f.provenance is FillProvenance.ESTIMATED and not f.is_verified
