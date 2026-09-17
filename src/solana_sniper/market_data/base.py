"""Market data provider interfaces."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from solana_sniper.domain.models import MarketSnapshot, TokenInfo, TradeEvent

EmitSnapshot = Callable[[MarketSnapshot], Awaitable[None]]
EmitTrade = Callable[[TradeEvent], Awaitable[None]]


class PollingMarketDataProvider(Protocol):
    """Batch REST provider. The service calls fetch() with the mints it wants refreshed."""

    name: str
    batch_size: int

    def supports(self, token: TokenInfo) -> bool: ...

    async def fetch(self, mints: Sequence[str]) -> list[MarketSnapshot]: ...


class StreamingMarketDataProvider(Protocol):
    """Push provider: the service tells it which mints to (un)subscribe; it emits data."""

    name: str

    def supports(self, token: TokenInfo) -> bool: ...

    async def subscribe(self, mints: Sequence[str]) -> None: ...

    async def unsubscribe(self, mints: Sequence[str]) -> None: ...

    def set_sinks(self, emit_snapshot: EmitSnapshot, emit_trade: EmitTrade) -> None: ...
