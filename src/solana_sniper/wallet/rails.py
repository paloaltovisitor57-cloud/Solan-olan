"""Safety rails for autonomous execution: every cap the user set, checked in one place before
anything is quoted, signed or sent. Pure functions over integers (lamports, raw units); the
executor feeds them the live numbers.

Rules:
* KILL stops everything (buys and sells).
* Disarmed (loss limit tripped, or `disarm`) stops new buys; exits continue when configured.
* A buy also needs: wallet balance above the fee reserve after the spend, per-trade cap, daily
  spend cap, max open positions, slippage / price-impact / priority-fee ceilings, and the total
  loss limit not reached.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RailsConfig:
    max_trade_lamports: int
    max_daily_spend_lamports: int
    max_total_loss_lamports: int | None  # None: cannot be armed
    max_open_positions: int
    reserve_lamports: int
    max_slippage_bps: int
    max_price_impact_pct: float
    max_priority_fee_lamports: int
    exits_continue_when_disarmed: bool = True


@dataclass(frozen=True, slots=True)
class Verdict:
    ok: bool
    reason: str = ""
    disarm: bool = False  # True: the loss limit was reached; the executor must disarm

    @classmethod
    def allow(cls) -> Verdict:
        return cls(True)

    @classmethod
    def block(cls, reason: str, *, disarm: bool = False) -> Verdict:
        return cls(False, reason, disarm)


@dataclass(frozen=True, slots=True)
class Exposure:
    """Live numbers the executor measures right before a trade."""

    armed: bool
    kill: bool
    wallet_lamports: int
    open_positions: int
    spent_today_lamports: int
    loss_lamports: int  # start balance - (wallet + open value); positive = loss


class SafetyRails:
    def __init__(self, config: RailsConfig) -> None:
        self.config = config

    # ------------------------------------------------------------ helpers
    def clamp_spend(self, recommended_lamports: int, wallet_lamports: int) -> int:
        """The most a buy may spend: the sizing engine's recommendation, capped per trade and
        by what the wallet holds above the fee reserve. Zero means: do not buy."""
        cap = min(recommended_lamports, self.config.max_trade_lamports)
        available = wallet_lamports - self.config.reserve_lamports
        return max(0, min(cap, available))

    @staticmethod
    def loss_lamports(
        start_balance_lamports: int, wallet_lamports: int, open_value_lamports: int
    ) -> int:
        return start_balance_lamports - (wallet_lamports + open_value_lamports)

    def loss_limit_reached(self, loss_lamports: int) -> bool:
        limit = self.config.max_total_loss_lamports
        return limit is not None and loss_lamports >= limit

    # ------------------------------------------------------------- checks
    def _check_quote(
        self, slippage_bps: int, price_impact_pct: float, priority_fee_lamports: int
    ) -> Verdict:
        c = self.config
        if slippage_bps > c.max_slippage_bps:
            return Verdict.block(f"slippage {slippage_bps}bps above cap {c.max_slippage_bps}bps")
        if price_impact_pct > c.max_price_impact_pct:
            return Verdict.block(
                f"price impact {price_impact_pct:.2f}% above cap {c.max_price_impact_pct:.2f}%"
            )
        if priority_fee_lamports > c.max_priority_fee_lamports:
            return Verdict.block(
                f"priority fee {priority_fee_lamports} above cap {c.max_priority_fee_lamports}"
            )
        return Verdict.allow()

    def check_buy(
        self,
        exposure: Exposure,
        *,
        spend_lamports: int,
        slippage_bps: int,
        price_impact_pct: float,
        priority_fee_lamports: int,
    ) -> Verdict:
        c = self.config
        if exposure.kill:
            return Verdict.block("KILL switch active")
        if c.max_total_loss_lamports is None:
            return Verdict.block("no total loss limit configured; run `solana-sniper arm`")
        if self.loss_limit_reached(exposure.loss_lamports):
            return Verdict.block(
                f"total loss limit reached ({exposure.loss_lamports} of "
                f"{c.max_total_loss_lamports} lamports lost)",
                disarm=True,
            )
        if not exposure.armed:
            return Verdict.block("not armed")
        if spend_lamports <= 0:
            return Verdict.block("nothing to spend after caps and reserve")
        if spend_lamports > c.max_trade_lamports:
            return Verdict.block(
                f"trade {spend_lamports} lamports above per-trade cap {c.max_trade_lamports}"
            )
        if exposure.wallet_lamports - spend_lamports < c.reserve_lamports:
            return Verdict.block(
                f"wallet {exposure.wallet_lamports} lamports would fall below the reserve "
                f"{c.reserve_lamports} after spending {spend_lamports}"
            )
        if exposure.spent_today_lamports + spend_lamports > c.max_daily_spend_lamports:
            return Verdict.block(
                f"daily spend cap {c.max_daily_spend_lamports} lamports would be exceeded "
                f"({exposure.spent_today_lamports} spent today)"
            )
        if exposure.open_positions >= c.max_open_positions:
            return Verdict.block(
                f"{exposure.open_positions} open positions (max {c.max_open_positions})"
            )
        return self._check_quote(slippage_bps, price_impact_pct, priority_fee_lamports)

    def check_sell(
        self,
        exposure: Exposure,
        *,
        slippage_bps: int,
        price_impact_pct: float,
        priority_fee_lamports: int,
    ) -> Verdict:
        if exposure.kill:
            return Verdict.block("KILL switch active")
        if not exposure.armed and not self.config.exits_continue_when_disarmed:
            return Verdict.block("not armed and exits are frozen")
        # exits are how losses are contained: only the KILL switch and a plainly bad quote stop
        # them; the per-trade, daily and loss caps are entry-side rails
        return self._check_quote(slippage_bps, price_impact_pct, priority_fee_lamports)
