"""Market-data continuity: throttled DexScreener polls are visible (not silently swallowed),
priority candidates are refreshed on a faster lane, and stale reasons carry the provider state."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx

from solana_sniper.config.settings import MarketDataConfig
from solana_sniper.domain.clock import ManualClock
from solana_sniper.domain.models import MarketSnapshot, TokenInfo, TradeEvent
from solana_sniper.infra.governor import RATE_LIMITED, HostPolicy, ProviderGovernor
from solana_sniper.infra.http import HttpClient, RateLimitedError
from solana_sniper.market_data.dexscreener import DexScreenerMarketData
from solana_sniper.market_data.service import MarketDataService
from solana_sniper.telemetry.metrics import Metrics
from tests.unit.helpers import make_token


def _client(handler: Any, **policy: Any) -> tuple[HttpClient, ProviderGovernor, Metrics]:
    metrics = Metrics()
    gov = ProviderGovernor(metrics=metrics, rng=lambda: 0.0)
    gov.register(
        "api.dexscreener.com", "dexscreener", HostPolicy(rate_per_s=1000, burst=1000, **policy)
    )
    return (
        HttpClient(transport=httpx.MockTransport(handler), metrics=metrics, governor=gov),
        gov,
        metrics,
    )


async def test_dexscreener_rate_limit_propagates_instead_of_a_silent_gap(
    clock: ManualClock,
) -> None:
    client, gov, _ = _client(lambda r: httpx.Response(429, headers={"retry-after": "8"}))
    dex = DexScreenerMarketData(client, "https://api.dexscreener.com", clock, 30)
    try:
        await dex.fetch(["MintA"])
        raised = False
    except RateLimitedError as exc:
        raised = True
        assert exc.retry_after_s == 8.0
    assert raised, "a throttled poll must surface, not look like a token with no data"
    assert gov.state("api.dexscreener.com") == RATE_LIMITED
    await client.aclose()


class RecordingProvider:
    """Polling provider that records which mints each poll asked for."""

    name = "recording"
    batch_size = 30

    def __init__(self) -> None:
        self.polls: list[list[str]] = []
        self.fail_with: Exception | None = None

    def supports(self, token: TokenInfo) -> bool:
        return True

    async def fetch(self, mints: list[str]) -> list[MarketSnapshot]:
        self.polls.append(list(mints))
        if self.fail_with is not None:
            raise self.fail_with
        return [
            MarketSnapshot(mint=m, observed_at=datetime.now(tz=UTC), source="rec") for m in mints
        ]


async def _service(
    clock: ManualClock, priority: list[str], provider: RecordingProvider, **cfg: Any
) -> tuple[MarketDataService, list[MarketSnapshot]]:
    got: list[MarketSnapshot] = []

    async def emit_snapshot(snap: MarketSnapshot) -> None:
        got.append(snap)

    async def emit_trade(trade: TradeEvent) -> None:
        return None

    svc = MarketDataService(
        MarketDataConfig(**cfg),
        clock,
        Metrics(),
        emit_snapshot=emit_snapshot,
        emit_trade=emit_trade,
        priority_mints=lambda: priority,
    )
    svc.add_polling(provider)  # type: ignore[arg-type]
    return svc, got


async def test_priority_lane_polls_latched_candidates_more_often(clock: ManualClock) -> None:
    provider = RecordingProvider()
    priority = ["MintHot"]
    svc, got = await _service(
        clock, priority, provider, poll_interval_s=1.0, priority_poll_interval_s=0.2
    )
    for i in range(5):
        await svc.watch(make_token(f"Mint{i}"))
    await svc.watch(make_token("MintHot"))
    task = asyncio.create_task(svc.run())
    await asyncio.sleep(1.1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    full = [p for p in provider.polls if len(p) > 1]
    hot_only = [p for p in provider.polls if p == ["MintHot"]]
    assert full and hot_only, provider.polls
    assert len(hot_only) >= 3 * len(full) - 1  # the hot mint is refreshed several times per sweep
    assert full[0][0] == "MintHot"  # and always first in the full sweep
    assert svc.priority_polls >= 3 and got


async def test_throttled_poll_is_counted_and_reported(clock: ManualClock) -> None:
    provider = RecordingProvider()
    provider.fail_with = RateLimitedError("dexscreener rate limited", retry_after_s=5.0)
    svc, got = await _service(clock, [], provider, poll_interval_s=1.0)
    await svc.watch(make_token("MintX"))
    await svc._poll_once(provider)  # type: ignore[arg-type]
    assert got == [] and svc.last_throttled_at is not None
    assert svc.last_error is not None and "rate limited" in svc.last_error
    assert svc._metrics.counters["market_polls_throttled"] == 1
    # the stale reason carries the provider state when a governor is attached
    gov = ProviderGovernor(metrics=Metrics(), rng=lambda: 0.0)
    gov.register("api.dexscreener.com", "dexscreener")
    gov.on_rate_limited("api.dexscreener.com", 5.0)
    svc.governor = gov
    assert svc.provider_note() == " (dexscreener RATE_LIMITED)"
    svc.governor = None
    assert svc.provider_note() == ""
