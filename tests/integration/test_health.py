"""Heartbeat content from a live synthetic runtime."""

from __future__ import annotations

from pathlib import Path

from solana_sniper.app.health import HealthReporter, read_status, status_is_fresh
from tests.integration.conftest import Harness


async def test_health_snapshot_and_atomic_write(harness: Harness, tmp_path: Path) -> None:
    runtime = harness.runtime
    assert runtime.health is not None
    reporter = HealthReporter(runtime, tmp_path / "state" / "status.json", stale_after_s=10)
    reporter.add_probe(runtime.market)
    reporter.add_probe(runtime.discovery)
    for _ in range(400):
        await harness.step(0.5)
        if runtime.account.open_positions:
            break
    snap = reporter.snapshot()
    assert snap["mode"] == "DRY_RUN" and snap["session_id"] == "e2e-test"
    assert snap["tokens_monitored"] >= 1
    assert snap["market_data"]["watched"] >= 1 and snap["market_data"]["snapshots_total"] > 0
    assert snap["database"]["ok"] and snap["database"]["failures"] == 0
    assert snap["connections"]["market-data"]["connected"]  # no polling provider -> connected
    assert snap["open_positions"] and snap["open_positions"][0]["cost_eur"]
    assert snap["last_signal_at"] is not None
    assert snap["counters"]["signals"] >= 1 and snap["portfolio"]["equity_eur"]
    assert snap["engine"]["tick_p50_ms"] is not None
    reporter.write_once()
    stored = read_status(reporter._path)
    assert stored is not None and status_is_fresh(stored) and stored["pid"] == snap["pid"]
    assert not list((tmp_path / "state").glob(".status-*"))  # no temp files left behind
    reporter.write_final("test")
    final = read_status(reporter._path)
    assert final is not None and final["stopped"] and not status_is_fresh(final)
    # the engine records its last error for the heartbeat
    harness.engine._error("test", "boom")
    assert reporter.snapshot()["last_error"]["message"] == "boom"
