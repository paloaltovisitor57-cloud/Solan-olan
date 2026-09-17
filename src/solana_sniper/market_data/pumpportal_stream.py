"""PumpPortal trade stream as a StreamingMarketDataProvider. Derives snapshots from trades."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from solana_sniper.discovery.pumpportal import PUMP_INITIAL_VIRTUAL_SOL, PumpPortalClient
from solana_sniper.domain.enums import Venue
from solana_sniper.domain.models import MarketSnapshot, TokenInfo, TradeEvent
from solana_sniper.market_data.base import EmitSnapshot, EmitTrade
from solana_sniper.portfolio.fx import FxProvider


class PumpPortalMarketData:
    name = "pumpportal"

    def __init__(self, client: PumpPortalClient, fx: FxProvider) -> None:
        self._client = client
        self._fx = fx
        self._emit_snapshot: EmitSnapshot | None = None
        self._emit_trade: EmitTrade | None = None
        client.on_trade(self._on_trade)

    def supports(self, token: TokenInfo) -> bool:
        return token.venue in (Venue.PUMP_FUN, Venue.PUMP_SWAP) or token.source == "pumpportal"

    def set_sinks(self, emit_snapshot: EmitSnapshot, emit_trade: EmitTrade) -> None:
        self._emit_snapshot = emit_snapshot
        self._emit_trade = emit_trade

    async def subscribe(self, mints: Sequence[str]) -> None:
        await self._client.subscribe_trades(list(mints))

    async def unsubscribe(self, mints: Sequence[str]) -> None:
        await self._client.unsubscribe_trades(list(mints))

    async def _on_trade(self, trade: TradeEvent) -> None:
        if self._emit_trade is not None:
            await self._emit_trade(trade)
        if self._emit_snapshot is None or trade.price_native is None:
            return
        sol_usd = self._fx.sol_usd_cached()
        real_sol = None
        if trade.pool_sol_reserves is not None:
            real_sol = max(Decimal(0), trade.pool_sol_reserves - PUMP_INITIAL_VIRTUAL_SOL)
        liquidity_usd = (real_sol * 2 * sol_usd) if (real_sol is not None and sol_usd) else None
        snap = MarketSnapshot(
            mint=trade.mint,
            observed_at=trade.observed_at,
            source="pumpportal",
            price_native=trade.price_native,
            price_usd=(trade.price_native * sol_usd) if sol_usd else None,
            liquidity_native=real_sol,
            liquidity_usd=liquidity_usd,
            market_cap_usd=(trade.market_cap_sol * sol_usd)
            if (trade.market_cap_sol is not None and sol_usd)
            else None,
            venue=Venue.PUMP_FUN,
        )
        await self._emit_snapshot(snap)
