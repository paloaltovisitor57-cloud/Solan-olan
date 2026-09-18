from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from solana_sniper.config.loader import set_database_override
from solana_sniper.config.paths import set_home_override
from solana_sniper.config.settings import Settings
from solana_sniper.domain.clock import ManualClock
from solana_sniper.domain.enums import FillProvenance, SignalKind
from solana_sniper.domain.models import Fill, new_id
from solana_sniper.domain.money import ui_to_raw
from solana_sniper.portfolio.accounting import PortfolioAccount


@pytest.fixture(autouse=True)
def isolated_runtime_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own runtime home so nothing touches the real one. Tests that need a
    different home set SNIPER_HOME themselves (they run after this fixture)."""
    home = tmp_path / "sniper-home"
    monkeypatch.setenv("SNIPER_HOME", str(home))
    set_home_override(None)
    set_database_override(None)
    return home


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
    decimals: int = 6,
    provenance: FillProvenance = FillProvenance.SIMULATED,
) -> Fill:
    """Test fill; `token_amount` is in UI units and converted exactly to raw at `decimals`."""
    raw = ui_to_raw(token_amount, decimals, exact=True)
    return Fill(
        fill_id=new_id("fill"),
        signal_id=signal_id or new_id("sig"),
        mint=mint,
        side=side,
        filled_at=clock.now(),
        sol_amount=sol_amount,
        token_amount_ui=token_amount,
        token_amount_raw=raw,
        token_decimals=decimals,
        eur_amount=eur_amount,
        sol_eur=sol_eur,
        fee_eur=fee_eur,
        slippage_cost_eur=slippage_eur,
        provenance=provenance,
        simulated=provenance is FillProvenance.SIMULATED,
    )
