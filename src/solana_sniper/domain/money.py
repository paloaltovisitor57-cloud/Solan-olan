"""Decimal money helpers. All accounting uses Decimal; floats only for scoring/features."""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal

ZERO = Decimal(0)
ONE = Decimal(1)
LAMPORTS_PER_SOL = Decimal(1_000_000_000)
EUR_PLACES = Decimal("0.00000001")
DISPLAY_PLACES = Decimal("0.01")


def D(value: object) -> Decimal:  # noqa: N802 - short constructor by convention
    """Convert ints/floats/strings to Decimal without binary-float garbage."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value))
    if value is None:
        raise TypeError("cannot convert None to Decimal")
    return Decimal(str(value))


def q_eur(value: Decimal) -> Decimal:
    return value.quantize(EUR_PLACES, rounding=ROUND_HALF_EVEN)


def q_display(value: Decimal) -> Decimal:
    return value.quantize(DISPLAY_PLACES, rounding=ROUND_HALF_EVEN)


def lamports_to_sol(lamports: int) -> Decimal:
    return Decimal(lamports) / LAMPORTS_PER_SOL


def sol_to_lamports(sol: Decimal) -> int:
    return int((sol * LAMPORTS_PER_SOL).to_integral_value(rounding=ROUND_DOWN))


MAX_TOKEN_DECIMALS = 18


class UnitError(ValueError):
    """Invalid decimals, or a UI amount not representable exactly in raw units."""


def check_decimals(decimals: int) -> int:
    if isinstance(decimals, bool) or not isinstance(decimals, int):
        raise UnitError("token decimals must be an integer")
    if decimals < 0 or decimals > MAX_TOKEN_DECIMALS:
        raise UnitError(f"token decimals out of range: {decimals}")
    return decimals


def raw_to_ui(raw: int, decimals: int) -> Decimal:
    """Exact: raw / 10**decimals (Decimal division by a power of ten never rounds)."""
    check_decimals(decimals)
    if raw < 0:
        raise UnitError("raw token amount cannot be negative")
    return Decimal(raw).scaleb(-decimals)


def ui_to_raw(ui: Decimal, decimals: int, *, exact: bool = False) -> int:
    """UI amount -> raw integer. Rounds down unless exact=True, which raises on any remainder."""
    check_decimals(decimals)
    if ui < 0:
        raise UnitError("UI token amount cannot be negative")
    scaled = ui.scaleb(decimals)
    if exact and scaled != scaled.to_integral_value():
        raise UnitError("UI amount is not representable in raw units at the given decimals")
    return int(scaled.to_integral_value(rounding=ROUND_DOWN))


def pct(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator == 0:
        return ZERO
    return numerator / denominator


def bps(numerator: Decimal, denominator: Decimal) -> int:
    if denominator == 0:
        return 0
    ratio = numerator / denominator * Decimal(10_000)
    return int(ratio.to_integral_value(rounding=ROUND_HALF_EVEN))


def clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return max(low, min(high, value))


def fclamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
