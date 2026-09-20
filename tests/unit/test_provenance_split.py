"""Market-data provenance is separate from execution provenance."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from solana_sniper.app.bootstrap import build_runtime
from solana_sniper.config import load_settings
from solana_sniper.domain.clock import ManualClock
from solana_sniper.domain.enums import ExecutionProvenance, MarketDataProvenance, RunMode
from solana_sniper.storage.migrations import run_migrations
from solana_sniper.storage.repository import Repository


def _runtime(config: str, tmp_path: Path, mode: RunMode) -> object:
    settings = load_settings(Path(config))
    settings.storage.database_url = f"sqlite+aiosqlite:///{tmp_path}/{mode.lower()}.db"
    settings.telemetry.log_file = None
    return build_runtime(
        settings,
        mode=mode,
        session_id="prov",
        clock=ManualClock(datetime(2026, 3, 1, tzinfo=UTC)),
        synthetic_seed=1,
        quiet_alerts=True,
    )


async def test_paper_with_live_providers_is_live_market_simulated_execution(
    tmp_path: Path,
) -> None:
    rt = _runtime("configs/default.yaml", tmp_path, RunMode.PAPER)  # real provider adapters
    tracker = rt.engine.d.outcomes  # type: ignore[attr-defined]
    assert tracker.market_data is MarketDataProvenance.LIVE
    assert tracker.execution is ExecutionProvenance.SIMULATED
    snap = rt.health.snapshot()  # type: ignore[attr-defined]
    assert snap["market_data_provenance"] == "LIVE" and snap["execution_provenance"] == "SIMULATED"
    await rt.http.aclose()  # type: ignore[attr-defined]
    await rt.repo.close()  # type: ignore[attr-defined]


async def test_synthetic_config_is_synthetic_market_simulated_execution(tmp_path: Path) -> None:
    rt = _runtime("configs/synthetic.yaml", tmp_path, RunMode.DRY_RUN)
    tracker = rt.engine.d.outcomes  # type: ignore[attr-defined]
    assert tracker.market_data is MarketDataProvenance.SYNTHETIC
    assert tracker.execution is ExecutionProvenance.SIMULATED
    await rt.http.aclose()  # type: ignore[attr-defined]
    await rt.repo.close()  # type: ignore[attr-defined]


async def test_live_signal_mode_is_manual_signal_execution(tmp_path: Path) -> None:
    rt = _runtime("configs/default.yaml", tmp_path, RunMode.LIVE)
    tracker = rt.engine.d.outcomes  # type: ignore[attr-defined]
    assert tracker.market_data is MarketDataProvenance.LIVE
    assert tracker.execution is ExecutionProvenance.MANUAL_SIGNAL
    await rt.http.aclose()  # type: ignore[attr-defined]
    await rt.repo.close()  # type: ignore[attr-defined]


async def test_migration_v4_backfills_legacy_outcome_rows(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path}/legacy.db"
    assert await run_migrations(url) == [1, 2, 3, 4, 5]
    engine = create_async_engine(url)
    legacy_payload = {
        "mint": "m",
        "symbol": None,
        "source": "s",
        "first_seen_at": {"__dt__": "2026-01-01T00:00:00+00:00"},
        "finalized_at": {"__dt__": "2026-01-01T01:00:00+00:00"},
        "horizon_s": 3600.0,
        "observations": 9,
        "first_price": {"__dec__": "1"},
        "max_multiple": 2.0,
        "time_to_peak_s": 1.0,
        "max_drawdown_from_peak": 0.1,
        "final_multiple": 1.5,
        "qualified": True,
        "qualified_multiple": None,
        "best_score": 70.0,
        "signalled": False,
        "entered": False,
        "closed_pnl_pct": None,
        "exit_reason": None,
        "liquidity_collapsed": False,
        "reject_reason": None,
        "simulated": True,
        "truncated": False,
    }
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM schema_version WHERE version >= 4"))
        columns = (
            "session_id, mint, symbol, source, first_seen_at, finalized_at, horizon_s, "
            "observations, max_multiple, qualified_multiple, final_multiple, "
            "max_drawdown_from_peak, time_to_peak_s, best_score, qualified, signalled, entered, "
            "closed_pnl_pct, exit_reason, liquidity_collapsed, reject_reason, simulated, "
            "truncated, market_provenance, execution_provenance, payload"
        )
        values = (
            "'old', 'm', NULL, 's', '2026-01-01 00:00:00.000000', '2026-01-01 01:00:00.000000', "
            "3600, 9, 2.0, NULL, 1.5, 0.1, 1.0, 70.0, 1, 0, 0, NULL, NULL, 0, NULL, 1, 0, "
            "NULL, NULL, :p"
        )
        await conn.execute(
            text(f"INSERT INTO outcomes ({columns}) VALUES ({values})"),
            {"p": json.dumps(legacy_payload)},
        )
    await engine.dispose()
    assert await run_migrations(url) == [4, 5]
    repo = Repository(url, session_id="reader")
    await repo.init()
    (row,) = await repo.outcomes()
    await repo.close()
    assert row.market_data == "UNKNOWN_LEGACY" and row.execution == "SIMULATED" and row.simulated
