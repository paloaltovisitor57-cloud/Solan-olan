"""Live position monitor: values positions (executable quote first), tracks peaks, asks the exit
engine for a decision, and enforces exit-signal cooldowns."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from solana_sniper.config.settings import ExitConfig
from solana_sniper.domain.models import FeatureVector, Position, SwapQuote
from solana_sniper.domain.money import lamports_to_sol, q_eur
from solana_sniper.market_data.tracker import TokenTrack
from solana_sniper.portfolio.accounting import PortfolioAccount
from solana_sniper.positions.exit_engine import ExitDecision, ExitEngine, MonitorState


class PositionMonitor:
    def __init__(self, config: ExitConfig, account: PortfolioAccount) -> None:
        self._cfg = config
        self._account = account
        self.exit_engine = ExitEngine(config)
        self._states: dict[str, MonitorState] = {}

    def state_for(self, position_id: str) -> MonitorState:
        st = self._states.get(position_id)
        if st is None:
            st = MonitorState()
            self._states[position_id] = st
        return st

    def forget(self, position_id: str) -> None:
        self._states.pop(position_id, None)

    # ---------------------------------------------------------------- valuation
    def value(
        self,
        position: Position,
        *,
        now: datetime,
        sol_eur: Decimal,
        exit_quote: SwapQuote | None,
        max_quote_age_s: float,
        price_native: Decimal | None,
    ) -> Position:
        """Prefer a fresh executable quote; else displayed price, flagged non-executable."""
        if exit_quote is not None and exit_quote.is_fresh(now, max_quote_age_s):
            sol_out = lamports_to_sol(exit_quote.out_amount_raw)
            value = q_eur(sol_out * sol_eur)
            price = (sol_out / position.quantity_ui) if position.quantity_ui > 0 else Decimal(0)
            return self._account.mark_position(
                position.position_id, value_eur=value, price_native=price, at=now, executable=True
            )
        if price_native is not None:
            value = q_eur(price_native * position.quantity_ui * sol_eur)
            return self._account.mark_position(
                position.position_id,
                value_eur=value,
                price_native=price_native,
                at=now,
                executable=False,
            )
        return position

    # --------------------------------------------------------------- decisions
    def evaluate(
        self,
        position: Position,
        track: TokenTrack | None,
        features: FeatureVector | None,
        now: datetime,
    ) -> ExitDecision | None:
        state = self.state_for(position.position_id)
        decision = self.exit_engine.evaluate(position, state, track, features, now)
        if decision is None:
            return None
        if (
            state.last_exit_signal_at is not None
            and (now - state.last_exit_signal_at).total_seconds() < self._cfg.exit_signal_cooldown_s
        ):
            return None
        state.last_exit_signal_at = now
        state.exit_signals += 1
        return decision

    def trailing_threshold(
        self, position: Position, features: FeatureVector | None, now: datetime
    ) -> float:
        return self.exit_engine.trailing.threshold(position, features, now)[0]
