"""Merges discovery providers, dedupes mints and applies the age gate."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from solana_sniper.config.settings import DiscoveryConfig
from solana_sniper.discovery.base import PollingDiscoveryProvider, TokenDiscoveryProvider
from solana_sniper.domain.clock import Clock
from solana_sniper.domain.models import TokenInfo
from solana_sniper.telemetry.logging import get_logger
from solana_sniper.telemetry.metrics import Metrics
from solana_sniper.telemetry.redaction import safe_exception

log = get_logger(__name__)


class DiscoveryService:
    def __init__(
        self,
        config: DiscoveryConfig,
        clock: Clock,
        metrics: Metrics,
        sink: Callable[[TokenInfo], Awaitable[None]],
    ) -> None:
        self._config = config
        self._clock = clock
        self._metrics = metrics
        self._sink = sink
        self._streaming: list[TokenDiscoveryProvider] = []
        self._polling: list[PollingDiscoveryProvider] = []
        self._seen: dict[str, datetime] = {}
        self.accepted = 0
        self.skipped_old = 0
        self.duplicates = 0
        self.name = "discovery"
        self.kind = "http-poll"
        self._last_success_at: datetime | None = None

    def is_connected(self) -> bool:
        if not self._polling:
            return True  # streaming providers report their own connection state
        if self._last_success_at is None:
            return False
        age = (datetime.now(tz=UTC) - self._last_success_at).total_seconds()
        return age <= max(30.0, self._config.poll_interval_s * 4)

    def last_activity(self) -> datetime | None:
        return self._last_success_at

    def add_streaming(self, provider: TokenDiscoveryProvider) -> None:
        self._streaming.append(provider)

    def add_polling(self, provider: PollingDiscoveryProvider) -> None:
        self._polling.append(provider)

    @property
    def provider_names(self) -> list[str]:
        return [p.name for p in self._streaming] + [p.name for p in self._polling]

    async def handle(self, token: TokenInfo) -> None:
        now = self._clock.now()
        self._expire_seen(now)
        if token.mint in self._seen:
            self.duplicates += 1
            return
        age = token.age_seconds(now)
        if age is not None and age > self._config.max_token_age_s:
            self.skipped_old += 1
            self._seen[token.mint] = now
            return
        self._seen[token.mint] = now
        self.accepted += 1
        self._metrics.mark_discovery()
        await self._sink(token)

    def _expire_seen(self, now: datetime) -> None:
        if len(self._seen) < 2000:
            return
        ttl = self._config.dedupe_ttl_s
        self._seen = {m: t for m, t in self._seen.items() if (now - t).total_seconds() < ttl}

    async def _run_polling(self, provider: PollingDiscoveryProvider) -> None:
        interval = max(0.5, provider.poll_interval_s)
        while True:
            try:
                tokens = await provider.poll()

                self._last_success_at = datetime.now(tz=UTC)
                for token in tokens:
                    await self.handle(token)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._metrics.inc("provider_errors")
                log.warning(
                    "discovery_poll_error", provider=provider.name, error=safe_exception(exc)
                )
            await asyncio.sleep(interval)

    async def run(self) -> None:
        tasks = [
            asyncio.create_task(p.run(self.handle), name=f"disc-{p.name}") for p in self._streaming
        ]
        tasks += [
            asyncio.create_task(self._run_polling(p), name=f"disc-{p.name}") for p in self._polling
        ]
        if not tasks:
            log.warning("discovery_no_providers")
            await asyncio.Event().wait()
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
