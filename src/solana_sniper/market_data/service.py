"""Drives polling providers in batches and streaming providers via subscriptions."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence

from solana_sniper.config.settings import MarketDataConfig
from solana_sniper.domain.clock import Clock
from solana_sniper.domain.models import MarketSnapshot, TokenInfo, TradeEvent
from solana_sniper.market_data.base import (
    PollingMarketDataProvider,
    StreamingMarketDataProvider,
)
from solana_sniper.telemetry.logging import get_logger
from solana_sniper.telemetry.metrics import Metrics

log = get_logger(__name__)

PriorityFn = Callable[[], Sequence[str]]


class MarketDataService:
    def __init__(
        self,
        config: MarketDataConfig,
        clock: Clock,
        metrics: Metrics,
        *,
        emit_snapshot: Callable[[MarketSnapshot], Awaitable[None]],
        emit_trade: Callable[[TradeEvent], Awaitable[None]],
        priority_mints: PriorityFn,
    ) -> None:
        self._config = config
        self._clock = clock
        self._metrics = metrics
        self._emit_snapshot = emit_snapshot
        self._emit_trade = emit_trade
        self._priority = priority_mints
        self._polling: list[PollingMarketDataProvider] = []
        self._streaming: list[StreamingMarketDataProvider] = []
        self._tracked: dict[str, TokenInfo] = {}
        self._sem = asyncio.Semaphore(max(1, config.max_concurrent_requests))
        self.polls = 0

    def add_polling(self, provider: PollingMarketDataProvider) -> None:
        self._polling.append(provider)

    def add_streaming(self, provider: StreamingMarketDataProvider) -> None:
        provider.set_sinks(self._emit_snapshot, self._emit_trade)
        self._streaming.append(provider)

    @property
    def provider_names(self) -> list[str]:
        return [p.name for p in self._polling] + [p.name for p in self._streaming]

    async def watch(self, token: TokenInfo) -> None:
        if token.mint in self._tracked:
            return
        self._tracked[token.mint] = token
        for s in self._streaming:
            if s.supports(token):
                await s.subscribe([token.mint])

    async def unwatch(self, mint: str) -> None:
        token = self._tracked.pop(mint, None)
        if token is None:
            return
        for s in self._streaming:
            if s.supports(token):
                await s.unsubscribe([mint])

    def watched(self) -> list[str]:
        return list(self._tracked)

    async def _poll_once(self, provider: PollingMarketDataProvider) -> None:
        # Priority mints (open positions, pending signals) go first so they are refreshed
        # even when the tracked set exceeds what one interval can cover.
        ordered: list[str] = []
        seen: set[str] = set()
        for m in list(self._priority()) + list(self._tracked):
            if m in self._tracked and m not in seen and provider.supports(self._tracked[m]):
                ordered.append(m)
                seen.add(m)
        if not ordered:
            return
        batches = [
            ordered[i : i + provider.batch_size]
            for i in range(0, len(ordered), provider.batch_size)
        ]

        async def run_batch(batch: list[str]) -> None:
            async with self._sem:
                try:
                    snaps = await provider.fetch(batch)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._metrics.inc("provider_errors")
                    log.warning("market_poll_error", provider=provider.name, error=str(exc))
                    return
            for snap in snaps:
                await self._emit_snapshot(snap)

        await asyncio.gather(*(run_batch(b) for b in batches))
        self.polls += 1

    async def _poll_loop(self, provider: PollingMarketDataProvider) -> None:
        interval = max(0.2, self._config.poll_interval_s)
        while True:
            started = self._clock.monotonic()
            await self._poll_once(provider)
            elapsed = self._clock.monotonic() - started
            await asyncio.sleep(max(0.05, interval - elapsed))

    async def run(self) -> None:
        tasks = [
            asyncio.create_task(self._poll_loop(p), name=f"md-{p.name}") for p in self._polling
        ]
        if not tasks:
            await asyncio.Event().wait()
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
