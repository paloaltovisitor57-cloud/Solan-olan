"""Manual-confirmation executor: holds pending orders until a human says `b N` / `s N`."""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal

from solana_sniper.config.settings import QuotesConfig
from solana_sniper.domain.clock import Clock
from solana_sniper.domain.enums import DecisionKind, DecisionSource, SignalKind, SignalStatus
from solana_sniper.domain.models import BuySignal, Fill, ManualDecision, SellSignal, new_id
from solana_sniper.domain.money import ZERO, lamports_to_sol, q_eur
from solana_sniper.execution.base import FillOverride, PendingOrder, Resolution


class OrderNotPendingError(Exception):
    pass


class ManualExecution:
    """Live signal mode. Confirming records the fill the user made in their own wallet.

    Without an override the fill is booked at the quoted amounts. The user can report actual
    amounts (`b 1 0.12 950000`) so accounting mirrors reality.
    """

    name = "manual"
    simulated = False

    def __init__(self, clock: Clock, quotes: QuotesConfig) -> None:
        self._clock = clock
        self._quotes = quotes
        self._orders: dict[str, PendingOrder] = {}
        self._next_ref = {SignalKind.BUY: 1, SignalKind.SELL: 1}
        self._lock = asyncio.Lock()
        self.history: list[Resolution] = []

    # ------------------------------------------------------------- submission
    async def submit_buy(self, signal: BuySignal) -> PendingOrder:
        async with self._lock:
            for o in self._orders.values():
                if (
                    o.kind is SignalKind.BUY
                    and o.mint == signal.mint
                    and o.status is SignalStatus.PENDING
                ):
                    raise ValueError(f"duplicate pending BUY for {signal.mint}")
            order = PendingOrder(
                ref=self._take_ref(SignalKind.BUY),
                order_id=new_id("ord"),
                kind=SignalKind.BUY,
                mint=signal.mint,
                symbol=signal.symbol,
                created_at=signal.created_at,
                expires_at=signal.expires_at,
                buy=signal,
            )
            self._orders[order.order_id] = order
            return order

    async def submit_sell(self, signal: SellSignal) -> PendingOrder:
        async with self._lock:
            for o in self._orders.values():
                if (
                    o.kind is SignalKind.SELL
                    and o.position_id == signal.position_id
                    and o.status is SignalStatus.PENDING
                ):
                    raise ValueError(f"duplicate pending SELL for position {signal.position_id}")
            order = PendingOrder(
                ref=self._take_ref(SignalKind.SELL),
                order_id=new_id("ord"),
                kind=SignalKind.SELL,
                mint=signal.mint,
                symbol=signal.symbol,
                created_at=signal.created_at,
                expires_at=signal.expires_at,
                sell=signal,
            )
            self._orders[order.order_id] = order
            return order

    def _take_ref(self, kind: SignalKind) -> int:
        ref = self._next_ref[kind]
        self._next_ref[kind] = ref + 1
        return ref

    # ------------------------------------------------------------------ views
    def pending(self) -> list[PendingOrder]:
        return [o for o in self._orders.values() if o.status is SignalStatus.PENDING]

    def find(self, kind: SignalKind, ref: int) -> PendingOrder | None:
        for o in self._orders.values():
            if o.kind is kind and o.ref == ref and o.status is SignalStatus.PENDING:
                return o
        return None

    # -------------------------------------------------------------- decisions
    async def decide(
        self,
        order: PendingOrder,
        kind: DecisionKind,
        source: DecisionSource,
        *,
        override: FillOverride | None = None,
        note: str = "",
    ) -> Resolution:
        async with self._lock:
            live = self._orders.get(order.order_id)
            if live is None or live.status is not SignalStatus.PENDING:
                raise OrderNotPendingError(order.order_id)
            now = self._clock.now()
            decision = ManualDecision(
                decision_id=new_id("dec"),
                signal_id=live.signal_id,
                kind=kind,
                source=source,
                decided_at=now,
                note=note,
            )
            fill: Fill | None = None
            if kind is DecisionKind.CONFIRM:
                if now > live.expires_at:
                    live.status = SignalStatus.EXPIRED
                    decision = ManualDecision(
                        decision.decision_id,
                        decision.signal_id,
                        DecisionKind.EXPIRE,
                        source,
                        now,
                        "signal expired before confirmation",
                    )
                else:
                    fill = self._build_fill(live, now, override)
                    live.status = SignalStatus.CONFIRMED
            elif kind is DecisionKind.EXPIRE:
                live.status = SignalStatus.EXPIRED
            else:
                live.status = SignalStatus.REJECTED
            self._orders.pop(live.order_id, None)
            res = Resolution(order=live, decision=decision, fill=fill)
            self.history.append(res)
            return res

    async def expire_stale(self, now: datetime) -> list[Resolution]:
        expired: list[Resolution] = []
        for order in list(self.pending()):
            if now > order.expires_at:
                expired.append(
                    await self.decide(
                        order, DecisionKind.EXPIRE, DecisionSource.SYSTEM, note="ttl elapsed"
                    )
                )
        return expired

    # ------------------------------------------------------------------ fills
    def _build_fill(
        self, order: PendingOrder, now: datetime, override: FillOverride | None
    ) -> Fill:
        if order.kind is SignalKind.BUY:
            assert order.buy is not None
            sig = order.buy
            sol = sig.quote.spend_sol
            tokens = sig.quote.expected_tokens_ui
            if override is not None:
                sol = override.sol_amount if override.sol_amount is not None else sol
                tokens = override.token_amount if override.token_amount is not None else tokens
            slippage = self._buy_slippage_cost(sig, sol)
            return Fill(
                fill_id=new_id("fill"),
                signal_id=sig.signal_id,
                mint=sig.mint,
                side=SignalKind.BUY,
                filled_at=now,
                sol_amount=sol,
                token_amount=tokens,
                eur_amount=q_eur(sol * sig.sol_eur),
                sol_eur=sig.sol_eur,
                fee_eur=q_eur(lamports_to_sol(sig.quote.total_fee_lamports // 2) * sig.sol_eur),
                slippage_cost_eur=slippage,
                simulated=self.simulated,
                tx_signature=override.tx_signature if override else None,
            )
        assert order.sell is not None
        sig_s = order.sell
        sol_out = (
            sig_s.estimated_sell_output_sol if sig_s.estimated_sell_output_sol is not None else ZERO
        )
        if override is not None and override.sol_amount is not None:
            sol_out = override.sol_amount
        tokens_sold = Decimal(sig_s.exit_quote.in_amount_raw) if sig_s.exit_quote else ZERO
        fee_lamports = sig_s.exit_quote.fee_lamports if sig_s.exit_quote else 0
        fee_lamports += (
            self._quotes.estimated_network_fee_lamports + self._quotes.priority_fee_lamports
        )
        return Fill(
            fill_id=new_id("fill"),
            signal_id=sig_s.signal_id,
            mint=sig_s.mint,
            side=SignalKind.SELL,
            filled_at=now,
            sol_amount=sol_out,
            token_amount=tokens_sold,
            eur_amount=q_eur(sol_out * sig_s.sol_eur),
            sol_eur=sig_s.sol_eur,
            fee_eur=q_eur(lamports_to_sol(fee_lamports) * sig_s.sol_eur),
            slippage_cost_eur=q_eur(
                (sig_s.current_value_eur - sol_out * sig_s.sol_eur)
                if sig_s.current_value_eur > sol_out * sig_s.sol_eur
                else ZERO
            ),
            simulated=self.simulated,
            tx_signature=override.tx_signature if override else None,
        )

    @staticmethod
    def _buy_slippage_cost(sig: BuySignal, sol: Decimal) -> Decimal:
        """Price impact expressed in EUR (what a zero-impact fill would have saved)."""
        impact = Decimal(str(sig.quote.entry_price_impact_pct)) / Decimal(100)
        return q_eur(sol * impact * sig.sol_eur)


class DryRunExecution(ManualExecution):
    """Same order book, but a background task confirms after a delay using pessimistic fills."""

    name = "dry-run"
    simulated = True

    def __init__(
        self,
        clock: Clock,
        quotes: QuotesConfig,
        *,
        confirm_delay_s: float = 2.0,
        auto_confirm_buys: bool = True,
        auto_confirm_sells: bool = True,
    ) -> None:
        super().__init__(clock, quotes)
        self._delay = confirm_delay_s
        self._auto_buys = auto_confirm_buys
        self._auto_sells = auto_confirm_sells
        self._extra_bps = quotes.extra_fill_slippage_bps

    def _build_fill(
        self, order: PendingOrder, now: datetime, override: FillOverride | None
    ) -> Fill:
        if override is None:
            haircut = Decimal(10_000 - self._extra_bps) / Decimal(10_000)
            if order.kind is SignalKind.BUY and order.buy is not None:
                override = FillOverride(token_amount=order.buy.quote.expected_tokens_ui * haircut)
            elif order.sell is not None and order.sell.estimated_sell_output_sol is not None:
                override = FillOverride(sol_amount=order.sell.estimated_sell_output_sol * haircut)
        return super()._build_fill(order, now, override)

    def due(self, now: datetime) -> list[PendingOrder]:
        out: list[PendingOrder] = []
        for order in self.pending():
            if (now - order.created_at).total_seconds() < self._delay:
                continue
            if order.kind is SignalKind.BUY and not self._auto_buys:
                continue
            if order.kind is SignalKind.SELL and not self._auto_sells:
                continue
            out.append(order)
        return out

    async def auto_confirm(self, now: datetime) -> list[Resolution]:
        results: list[Resolution] = []
        for order in self.due(now):
            results.append(
                await self.decide(
                    order, DecisionKind.CONFIRM, DecisionSource.DRY_RUN, note="dry-run auto-confirm"
                )
            )
        return results
