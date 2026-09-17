from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from solana_sniper.config.settings import Settings
from solana_sniper.domain.clock import ManualClock
from solana_sniper.domain.enums import SignalKind
from solana_sniper.domain.models import Fill, new_id
from solana_sniper.portfolio.accounting import PortfolioAccount


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(datetime(2026, 3, 1, 12, 0, tzinfo=UTC))


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture
def account(clock: ManualClock) -> PortfolioAccount:
    acct = PortfolioAccount(clock, session_id="test")
    acct.deposit(Decimal("50"), "starting bankroll")
    return acct


def make_fill(
    clock: ManualClock,
    *,
    side: SignalKind,
    mint: str = "MintAAAA",
    sol_amount: Decimal = Decimal("0.1"),
    token_amount: Decimal = Decimal("1000"),
    eur_amount: Decimal = Decimal("15"),
    sol_eur: Decimal = Decimal("150"),
    fee_eur: Decimal = Decimal("0.05"),
    slippage_eur: Decimal = Decimal("0.10"),
    signal_id: str | None = None,
) -> Fill:
    return Fill(
        fill_id=new_id("fill"),
        signal_id=signal_id or new_id("sig"),
        mint=mint,
        side=side,
        filled_at=clock.now(),
        sol_amount=sol_amount,
        token_amount=token_amount,
        eur_amount=eur_amount,
        sol_eur=sol_eur,
        fee_eur=fee_eur,
        slippage_cost_eur=slippage_eur,
        simulated=True,
    )
