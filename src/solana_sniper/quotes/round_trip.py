"""Round-trip quote test: BUY quote, then estimate an immediate SELL of the received amount."""

from __future__ import annotations

from decimal import Decimal

from solana_sniper.config.settings import QuotesConfig
from solana_sniper.domain.clock import Clock
from solana_sniper.domain.models import RoundTripQuote, SwapQuote
from solana_sniper.domain.money import lamports_to_sol, raw_to_ui, sol_to_lamports, ui_to_raw
from solana_sniper.quotes.base import QuoteError, QuoteProvider
from solana_sniper.telemetry.logging import get_logger
from solana_sniper.telemetry.metrics import Metrics

log = get_logger(__name__)

WSOL = "So11111111111111111111111111111111111111112"


class RoundTripEvaluator:
    def __init__(
        self,
        provider: QuoteProvider,
        config: QuotesConfig,
        clock: Clock,
        metrics: Metrics | None = None,
    ) -> None:
        self._provider = provider
        self._cfg = config
        self._clock = clock
        self._metrics = metrics

    async def evaluate(self, mint: str, spend_sol: Decimal, token_decimals: int) -> RoundTripQuote:
        now = self._clock.now()
        lamports = sol_to_lamports(spend_sol)
        reasons: list[str] = []
        buy = await self._provider.quote(WSOL, mint, lamports, self._cfg.slippage_bps)
        if self._metrics:
            self._metrics.inc("quotes")
        expected_tokens = raw_to_ui(buy.out_amount_raw, token_decimals)
        entry_bps = round(buy.price_impact_pct * 100)
        if buy.price_impact_pct > self._cfg.max_price_impact_pct:
            reasons.append(
                f"entry price impact {buy.price_impact_pct:.1f}% "
                f"> {self._cfg.max_price_impact_pct}%"
            )
        sell: SwapQuote | None = None
        exit_sol: Decimal | None = None
        exit_bps: int | None = None
        exit_impact: float | None = None
        loss: float | None = None
        try:
            sell = await self._provider.quote(
                mint, WSOL, buy.out_amount_raw, self._cfg.slippage_bps
            )
            if self._metrics:
                self._metrics.inc("quotes")
        except QuoteError as exc:
            reasons.append(f"sell quote failed: {exc}")
            if self._metrics:
                self._metrics.inc("quote_failures")
        if sell is not None:
            exit_sol = lamports_to_sol(sell.out_amount_raw)
            exit_bps = round(sell.price_impact_pct * 100)
            exit_impact = sell.price_impact_pct
            loss = float((spend_sol - exit_sol) / spend_sol) if spend_sol > 0 else None
            if loss is not None and loss > self._cfg.round_trip_max_loss_pct:
                reasons.append(
                    f"round trip loss {loss:.1%} > {self._cfg.round_trip_max_loss_pct:.0%}"
                )
            if sell.price_impact_pct > self._cfg.max_price_impact_pct:
                reasons.append(f"exit price impact {sell.price_impact_pct:.1f}%")
        fees = buy.fee_lamports + (sell.fee_lamports if sell else 0)
        fees += 2 * (self._cfg.estimated_network_fee_lamports + self._cfg.priority_fee_lamports)
        return RoundTripQuote(
            mint=mint,
            quoted_at=now,
            spend_sol=spend_sol,
            buy=buy,
            sell=sell,
            expected_tokens_ui=expected_tokens,
            entry_slippage_bps=entry_bps,
            entry_price_impact_pct=buy.price_impact_pct,
            immediate_exit_sol=exit_sol,
            exit_slippage_bps=exit_bps,
            exit_price_impact_pct=exit_impact,
            round_trip_loss_pct=loss,
            total_fee_lamports=fees,
            viable=sell is not None and not reasons,
            reasons=tuple(reasons),
        )

    async def exit_quote(self, mint: str, quantity_ui: Decimal, token_decimals: int) -> SwapQuote:
        raw = ui_to_raw(quantity_ui, token_decimals)
        quote = await self._provider.quote(mint, WSOL, raw, self._cfg.slippage_bps)
        if self._metrics:
            self._metrics.inc("quotes")
        return quote

    def is_fresh(self, quote: SwapQuote) -> bool:
        return quote.is_fresh(self._clock.now(), self._cfg.max_quote_age_s)
