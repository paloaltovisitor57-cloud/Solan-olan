from __future__ import annotations

from decimal import Decimal

from solana_sniper.domain.money import (
    D,
    bps,
    lamports_to_sol,
    q_eur,
    raw_to_ui,
    sol_to_lamports,
    ui_to_raw,
)


def test_float_conversion_is_exact_repr() -> None:
    assert D(0.1) == Decimal("0.1")
    assert D(1) == Decimal(1)
    assert D("2.5") == Decimal("2.5")


def test_lamports_roundtrip() -> None:
    sol = Decimal("1.234567891")
    assert lamports_to_sol(sol_to_lamports(sol)) == sol
    assert sol_to_lamports(Decimal("0.000000001")) == 1


def test_raw_ui_roundtrip() -> None:
    assert raw_to_ui(1_500_000, 6) == Decimal("1.5")
    assert ui_to_raw(Decimal("1.5"), 6) == 1_500_000


def test_bps() -> None:
    assert bps(Decimal("1"), Decimal("100")) == 100
    assert bps(Decimal("1"), Decimal("0")) == 0


def test_q_eur_rounding() -> None:
    assert q_eur(Decimal("1.123456789")) == Decimal("1.12345679")
