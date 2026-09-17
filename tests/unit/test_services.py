from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal

from solana_sniper.config.settings import DiscoveryConfig, MarketDataConfig
from solana_sniper.discovery.service import DiscoveryService
from solana_sniper.domain.clock import ManualClock
from solana_sniper.domain.models import MarketSnapshot, TokenInfo, TradeEvent
from solana_sniper.market_data.base import EmitSnapshot, EmitTrade
from solana_sniper.market_data.service import MarketDataService
from solana_sniper.telemetry.metrics import Metrics


async def test_discovery_dedupes_and_age_gates(clock: ManualClock) -> None:
    got: list[TokenInfo] = []

    async def sink(t: TokenInfo) -> None:
        got.append(t)

    svc = DiscoveryService(DiscoveryConfig(max_token_age_s=60), clock, Metrics(), sink)
    fresh = TokenInfo(mint="a", pool_created_at=clock.now() - timedelta(seconds=10))
    old = TokenInfo(mint="b", pool_created_at=clock.now() - timedelta(seconds=600))
    unknown_age = TokenInfo(mint="c")
    await svc.handle(fresh)
    await svc.handle(fresh)
    await svc.handle(old)
    await svc.handle(unknown_age)
    assert [t.mint for t in got] == ["a", "c"]
    assert svc.duplicates == 1 and svc.skipped_old == 1 and svc.accepted == 2


class FlakyPoller:
    name = "flaky"
    poll_interval_s = 0.01

    def __init__(self) -> None:
        self.calls = 0

    async def poll(self) -> list[TokenInfo]:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("provider exploded")
        return [TokenInfo(mint=f"m{self.calls}")]


async def test_polling_provider_errors_do_not_kill_loop(clock: ManualClock) -> None:
    got: list[TokenInfo] = []

    async def sink(t: TokenInfo) -> None:
        got.append(t)

    metrics = Metrics()
    svc = DiscoveryService(DiscoveryConfig(), clock, metrics, sink)
    poller = FlakyPoller()
    svc.add_polling(poller)
    task = asyncio.create_task(svc.run())
    for _ in range(50):
        await asyncio.sleep(0.02)
        if len(got) >= 2:
            break
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert poller.calls >= 3
    assert metrics.counters["provider_errors"] == 1
    assert got[0].mint == "m2"


class FakePoller:
    name = "fake"
    batch_size = 2

    def __init__(self, fail_batches: set[int] | None = None) -> None:
        self.batches: list[list[str]] = []
        self._fail = fail_batches or set()

    def supports(self, token: TokenInfo) -> bool:
        return token.mint != "unsupported"

    async def fetch(self, mints: Sequence[str]) -> list[MarketSnapshot]:
        self.batches.append(list(mints))
        if len(self.batches) in self._fail:
            raise RuntimeError("batch failed")
        return [
            MarketSnapshot(mint=m, observed_at=_NOW, source="fake", price_native=Decimal(1))
            for m in mints
        ]


class FakeStream:
    name = "stream"

    def __init__(self) -> None:
        self.subs: list[str] = []
        self.unsubs: list[str] = []

    def supports(self, token: TokenInfo) -> bool:
        return token.source == "pumpportal"

    async def subscribe(self, mints: Sequence[str]) -> None:
        self.subs.extend(mints)

    async def unsubscribe(self, mints: Sequence[str]) -> None:
        self.unsubs.extend(mints)

    def set_sinks(self, emit_snapshot: EmitSnapshot, emit_trade: EmitTrade) -> None:
        self.emit_snapshot = emit_snapshot


from datetime import UTC, datetime  # noqa: E402

_NOW = datetime(2026, 3, 1, tzinfo=UTC)


async def test_market_service_batches_with_priority(clock: ManualClock) -> None:
    snaps: list[MarketSnapshot] = []
    trades: list[TradeEvent] = []

    async def es(s: MarketSnapshot) -> None:
        snaps.append(s)

    async def et(t: TradeEvent) -> None:
        trades.append(t)

    priority = ["c"]
    svc = MarketDataService(
        MarketDataConfig(poll_interval_s=0.01),
        clock,
        Metrics(),
        emit_snapshot=es,
        emit_trade=et,
        priority_mints=lambda: priority,
    )
    poller = FakePoller(fail_batches={2})
    stream = FakeStream()
    svc.add_polling(poller)
    svc.add_streaming(stream)
    for m in ("a", "b", "c", "unsupported"):
        await svc.watch(TokenInfo(mint=m, source="pumpportal" if m == "a" else "x"))
    await svc.watch(TokenInfo(mint="a"))  # idempotent
    assert stream.subs == ["a"]
    await svc._poll_once(poller)
    assert poller.batches[0][0] == "c"  # priority first
    assert all("unsupported" not in b for b in poller.batches)
    assert {s.mint for s in snaps} == {"c", "a"} or {s.mint for s in snaps} == {"b", "c", "a"} - {
        "b"
    }
    await svc.unwatch("a")
    assert stream.unsubs == ["a"]
    await svc.unwatch("never")
    assert set(svc.watched()) == {"b", "c", "unsupported"}
