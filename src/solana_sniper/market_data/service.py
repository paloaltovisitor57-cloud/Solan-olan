"""Drives polling providers in batches and streaming providers via subscriptions."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from solana_sniper.config.settings import MarketDataConfig
from solana_sniper.domain.clock import Clock
from solana_sniper.domain.models import MarketSnapshot, TokenInfo, TradeEvent
from solana_sniper.infra.http import ProviderUnavailableError, RateLimitedError
from solana_sniper.market_data.base import (
    PollingMarketDataProvider,
    StreamingMarketDataProvider,
)
from solana_sniper.telemetry.logging import get_logger
from solana_sniper.telemetry.metrics import Metrics
from solana_sniper.telemetry.redaction import safe_exception
from solana_sniper.telemetry.throttle import LogThrottle

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
        self._throttle = LogThrottle(60.0)
        self._sem = asyncio.Semaphore(max(1, config.max_concurrent_requests))
        self.polls = 0
        self.name = "market-data"
        self.kind = "http-poll"
        self._last_success_at: datetime | None = None
        self.last_throttled_at: datetime | None = None
        self.last_error: str | None = None
        self.governor: Any = None  # set by the bootstrap: provider health for stale reasons
        self.priority_polls = 0

    def is_connected(self) -> bool:
        """True when a poll succeeded recently (or no polling provider is configured)."""
        if not self._polling:
            return True
        if self._last_success_at is None:
            return False
        age = (datetime.now(tz=UTC) - self._last_success_at).total_seconds()
        return age <= max(10.0, self._config.poll_interval_s * 4)

    def last_activity(self) -> datetime | None:
        return self._last_success_at

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

    async def _poll_once(
        self, provider: PollingMarketDataProvider, *, only_priority: bool = False
    ) -> None:
        # Priority mints (open positions, latched/qualified candidates, pending signals) go first
        # so they are refreshed even when the tracked set exceeds what one interval can cover;
        # the priority lane polls just them on a faster cadence.
        ordered: list[str] = []
        seen: set[str] = set()
        universe = (
            list(self._priority())
            if only_priority
            else list(self._priority()) + list(self._tracked)
        )
        for m in universe:
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
                except (RateLimitedError, ProviderUnavailableError) as exc:
                    self.last_throttled_at = datetime.now(tz=UTC)
                    self.last_error = safe_exception(exc)[:160]
                    self._metrics.inc("market_polls_throttled")
                    log.debug(
                        "market_poll_throttled", provider=provider.name, error=safe_exception(exc)
                    )
                    return
                except Exception as exc:
                    self._metrics.inc("provider_errors")
                    decision = self._throttle.hit(provider.name)
                    if decision.log:
                        log.warning(
                            "market_poll_error",
                            provider=provider.name,
                            error=safe_exception(exc),
                            suppressed_since_last=decision.suppressed,
                        )
                    return
            if snaps:
                self._last_success_at = datetime.now(tz=UTC)
            for snap in snaps:
                await self._emit_snapshot(snap)

        await asyncio.gather(*(run_batch(b) for b in batches))
        if only_priority:
            self.priority_polls += 1
        else:
            self.polls += 1

    async def _poll_loop(self, provider: PollingMarketDataProvider) -> None:
        interval = max(0.2, self._config.poll_interval_s)
        while True:
            started = self._clock.monotonic()
            await self._poll_once(provider)
            elapsed = self._clock.monotonic() - started
            await asyncio.sleep(max(0.05, interval - elapsed))

    async def _priority_loop(self, provider: PollingMarketDataProvider) -> None:
        """Faster refresh for the few mints an entry or exit decision depends on."""
        interval = max(0.1, self._config.priority_poll_interval_s)
        while True:
            started = self._clock.monotonic()
            if self._priority():
                await self._poll_once(provider, only_priority=True)
            elapsed = self._clock.monotonic() - started
            await asyncio.sleep(max(0.05, interval - elapsed))

    def provider_note(self) -> str:
        """Short provider-health suffix for stale reasons, e.g. ' (dexscreener RATE_LIMITED)'."""
        if self.governor is None:
            return ""
        limited = [
            f"{name} {info['state']}"
            for name, info in self.governor.health().items()
            if info.get("state") != "HEALTHY"
        ]
        return f" ({', '.join(limited)})" if limited else ""

    async def run(self) -> None:
        tasks = [
            asyncio.create_task(self._poll_loop(p), name=f"md-{p.name}") for p in self._polling
        ]
        tasks += [
            asyncio.create_task(self._priority_loop(p), name=f"md-prio-{p.name}")
            for p in self._polling
            if self._config.priority_poll_interval_s < self._config.poll_interval_s
        ]
        if not tasks:
            await asyncio.Event().wait()
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
