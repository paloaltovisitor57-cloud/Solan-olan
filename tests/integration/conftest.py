from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from solana_sniper.app.bootstrap import Runtime, build_runtime
from solana_sniper.config import load_settings
from solana_sniper.domain.clock import ManualClock
from solana_sniper.domain.enums import RunMode
from solana_sniper.market_data.synthetic import SyntheticTicker


class Harness:
    """Drives a fully wired synthetic runtime on a ManualClock, one tick at a time."""

    def __init__(self, runtime: Runtime, clock: ManualClock) -> None:
        self.runtime = runtime
        self.clock = clock
        self.engine = runtime.engine
        world = runtime.synthetic_world
        assert world is not None
        self.world = world
        self.ticker: SyntheticTicker | None = None
        for name, task in runtime.background:
            if name == "synthetic-ticker":
                self.ticker = task.__self__  # type: ignore[attr-defined]
        assert self.ticker is not None
        self._service_tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        await self.runtime.repo.init()
        self.runtime.repo.start()
        await self.runtime.repo.start_session("DRY_RUN", "test")
        await self.runtime._restore_account()
        self.runtime.bus.start()
        # discovery/market services register the synthetic emit callbacks when they run
        self._service_tasks.append(asyncio.create_task(self.runtime.discovery.run()))
        self._service_tasks.append(asyncio.create_task(self.runtime.market.run()))
        await asyncio.sleep(0)
        await self.engine._restore_positions()

    async def step(self, seconds: float = 0.5, ticks: int = 1) -> None:
        for _ in range(ticks):
            self.clock.advance(seconds)
            assert self.ticker is not None
            await self.ticker.tick_once()
            await self.engine.tick()
            # let spawned quote/metadata tasks finish deterministically
            for _ in range(4):
                await asyncio.sleep(0)
            await self.engine.tick()

    async def stop(self) -> None:
        for t in [*self.engine._tasks, *self._service_tasks]:
            t.cancel()
        await asyncio.gather(*self._service_tasks, return_exceptions=True)
        await self.runtime.bus.stop()
        await self.runtime.repo.end_session()
        await self.runtime.repo.close()
        await self.runtime.http.aclose()


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    settings = load_settings(Path("configs/synthetic.yaml"))
    settings.storage.database_url = f"sqlite+aiosqlite:///{tmp_path}/e2e.db"
    settings.dry_run.confirm_delay_s = 1.0
    settings.telemetry.log_file = None
    clock = ManualClock(datetime(2026, 3, 1, 12, 0, tzinfo=UTC))
    runtime = build_runtime(
        settings,
        mode=RunMode.DRY_RUN,
        session_id="e2e-test",
        clock=clock,
        synthetic_seed=3,
        quiet_alerts=True,
    )
    h = Harness(runtime, clock)
    await h.start()
    yield h
    await h.stop()
