"""Prepares what a human needs to execute a confirmed signal: quote, route, and optionally an
UNSIGNED transaction built for the configured PUBLIC key. No key material is ever handled."""

from __future__ import annotations

from solana_sniper.domain.clock import Clock
from solana_sniper.domain.enums import SignalKind, SignalStatus
from solana_sniper.domain.models import BuySignal, ExecutionRecord, SellSignal, SwapQuote, new_id
from solana_sniper.quotes.base import QuoteProvider


class TransactionPreparer:
    def __init__(
        self,
        provider: QuoteProvider,
        clock: Clock,
        *,
        wallet_public_key: str | None,
        enabled: bool,
        simulated: bool,
        session_id: str,
    ) -> None:
        self._provider = provider
        self._clock = clock
        self._pubkey = wallet_public_key
        self._enabled = enabled and wallet_public_key is not None
        self._simulated = simulated
        self._session_id = session_id

    async def prepare(self, signal: BuySignal | SellSignal) -> ExecutionRecord:
        quote: SwapQuote | None
        if isinstance(signal, BuySignal):
            quote = signal.quote.buy
            side = SignalKind.BUY
            route = " > ".join(quote.route_labels) or "n/a"
            instructions = (
                f"BUY {signal.symbol or signal.mint}: swap {signal.quote.spend_sol} SOL → "
                f"~{signal.quote.expected_tokens_ui:,.0f} tokens via {route} "
                f"(slippage {quote.slippage_bps}bps, impact {quote.price_impact_pct:.2f}%)"
            )
        else:
            quote = signal.exit_quote
            side = SignalKind.SELL
            est = signal.estimated_sell_output_sol
            route = " > ".join(quote.route_labels) if quote else "n/a"
            instructions = (
                f"SELL {signal.symbol or signal.mint}: swap position → "
                f"~{est if est is not None else '?'} SOL via {route}"
            )
        unsigned: str | None = None
        note = ""
        if self._simulated:
            note = "dry-run: no transaction prepared"
        elif not self._enabled:
            note = (
                "unsigned tx preparation disabled "
                "(set quotes.prepare_unsigned_transaction and providers.wallet_public_key)"
            )
        elif quote is None:
            note = "no quote available to build a transaction"
        else:
            assert self._pubkey is not None
            unsigned = await self._provider.prepare_unsigned_swap(quote, self._pubkey)
            note = (
                "unsigned transaction ready: sign in your own wallet"
                if unsigned
                else "provider did not return a transaction"
            )
        return ExecutionRecord(
            record_id=new_id("exec"),
            signal_id=signal.signal_id,
            mint=signal.mint,
            side=side,
            created_at=self._clock.now(),
            status=SignalStatus.PENDING,
            simulated=self._simulated,
            unsigned_transaction_b64=unsigned,
            instructions=instructions,
            quote_id=quote.quote_id if quote else None,
            note=note,
            session_id=self._session_id,
        )
