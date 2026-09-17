"""Dynamic bankroll/risk engine.

Position size is derived from current equity and scaled by: bankroll tier, signal confidence,
drawdown, recent results and expected slippage; then capped by profile limits, hard limits,
total exposure, pool liquidity and available cash. Nothing here ever exceeds available cash.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from solana_sniper.config.settings import RiskConfig, RiskProfile, RiskTier
from solana_sniper.domain.models import PositionSizing
from solana_sniper.domain.money import ZERO, D, fclamp, q_eur
from solana_sniper.portfolio.accounting import RecentPerformance


@dataclass(frozen=True, slots=True)
class SizingInputs:
    equity_eur: Decimal
    available_cash_eur: Decimal
    open_exposure_eur: Decimal
    open_positions: int
    score: float
    liquidity_eur: Decimal | None
    entry_slippage_bps: int | None
    exit_slippage_bps: int | None
    drawdown_pct: float
    recent: RecentPerformance
    sol_eur: Decimal


def _interp(x: float, x0: float, y0: float, x1: float, y1: float) -> float:
    if x1 == x0:
        return y1 if x >= x1 else y0
    if x <= x0:
        return y0
    if x >= x1:
        return y1
    t = (x - x0) / (x1 - x0)
    return y0 + t * (y1 - y0)


class RiskEngine:
    def __init__(self, config: RiskConfig) -> None:
        self.config = config

    @property
    def profile(self) -> RiskProfile:
        return self.config.profiles[self.config.profile]

    def tier_for(self, equity: Decimal) -> RiskTier:
        chosen = self.config.tiers[0]
        for tier in self.config.tiers:
            if equity >= tier.min_equity_eur:
                chosen = tier
        return chosen

    def max_open_positions(self, equity: Decimal) -> int:
        tier = self.tier_for(equity)
        if tier.max_open_positions is not None:
            return tier.max_open_positions
        return self.profile.max_open_positions

    # ------------------------------------------------------------ multipliers
    def confidence_multiplier(self, score: float) -> float:
        c = self.config
        return _interp(
            score, c.confidence_min_score, c.confidence_min_multiplier, c.confidence_full_score, 1.0
        )

    def drawdown_multiplier(self, drawdown_pct: float) -> float:
        c = self.config
        if drawdown_pct <= c.drawdown_start:
            return 1.0
        return _interp(
            drawdown_pct, c.drawdown_start, 1.0, c.drawdown_full, c.drawdown_floor_multiplier
        )

    def streak_multiplier(self, recent: RecentPerformance) -> float:
        c = self.config
        mult = 1.0 - c.loss_step_multiplier * recent.consecutive_losses
        mult += c.win_step_multiplier * recent.consecutive_wins
        mult = fclamp(mult, c.streak_min_multiplier, c.streak_max_multiplier)
        if len(recent.results) >= 3 and recent.expectancy_eur < 0:
            mult *= c.expectancy_negative_multiplier
        return fclamp(mult, c.streak_min_multiplier, c.streak_max_multiplier)

    def slippage_multiplier(self, entry_bps: int | None, exit_bps: int | None) -> float:
        c = self.config
        total = (entry_bps or 0) + (exit_bps or 0)
        return _interp(
            float(total),
            float(c.slippage_penalty_start_bps),
            1.0,
            float(c.slippage_penalty_full_bps),
            c.slippage_penalty_floor,
        )

    # ------------------------------------------------------------------ sizing
    def size(self, inputs: SizingInputs) -> PositionSizing:
        c = self.config
        profile = self.profile
        hard = c.hard_limits
        equity = inputs.equity_eur
        tier = self.tier_for(equity)
        caps: list[str] = []
        multipliers = {
            "tier": tier.fraction_multiplier,
            "confidence": self.confidence_multiplier(inputs.score),
            "drawdown": self.drawdown_multiplier(inputs.drawdown_pct),
            "streak": self.streak_multiplier(inputs.recent),
            "slippage": self.slippage_multiplier(
                inputs.entry_slippage_bps, inputs.exit_slippage_bps
            ),
        }

        def finish(amount: Decimal, fraction: Decimal) -> PositionSizing:
            amount = q_eur(max(ZERO, amount))
            sol = q_eur(amount / inputs.sol_eur) if inputs.sol_eur > 0 else ZERO
            return PositionSizing(
                recommended_eur=amount,
                recommended_sol=sol,
                fraction_of_equity=fraction,
                equity_eur=equity,
                available_cash_eur=inputs.available_cash_eur,
                caps_applied=tuple(caps),
                multipliers=multipliers,
                profile=str(c.profile),
                tier=tier.name,
            )

        if equity <= 0:
            caps.append("no_equity")
            return finish(ZERO, ZERO)
        if inputs.open_positions >= self.max_open_positions(equity):
            caps.append("max_open_positions")
            return finish(ZERO, ZERO)

        combined = 1.0
        for m in multipliers.values():
            combined *= m
        fraction = D(profile.base_fraction) * D(combined)
        amount = equity * fraction

        limit = equity * D(profile.max_fraction)
        if amount > limit:
            amount = limit
            caps.append("profile_max_fraction")
        limit = equity * D(hard.max_single_position_fraction)
        if amount > limit:
            amount = limit
            caps.append("hard_max_single_position")
        exposure_fraction = min(
            profile.max_total_exposure_fraction, hard.max_total_exposure_fraction
        )
        exposure_room = equity * D(exposure_fraction) - inputs.open_exposure_eur
        if amount > exposure_room:
            amount = exposure_room
            caps.append("total_exposure")
        if inputs.liquidity_eur is not None:
            liq_cap = inputs.liquidity_eur * D(hard.max_fraction_of_pool_liquidity)
            if amount > liq_cap:
                amount = liq_cap
                caps.append("pool_liquidity")
        if amount > hard.max_position_eur:
            amount = hard.max_position_eur
            caps.append("hard_max_position")
        if amount > inputs.available_cash_eur:
            amount = inputs.available_cash_eur
            caps.append("available_cash")
        if amount < hard.min_position_eur:
            caps.append("below_min_position")
            return finish(ZERO, ZERO)
        final_fraction = q_eur(amount / equity) if equity > 0 else ZERO
        return finish(amount, final_fraction)
