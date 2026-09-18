"""Health monitor: periodically writes a JSON heartbeat with everything `status.sh` shows.

The file is written atomically (tmp + rename) so readers never see a partial document.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from solana_sniper.domain.enums import CandidateState as S
from solana_sniper.domain.money import q_display
from solana_sniper.telemetry.logging import get_logger
from solana_sniper.telemetry.redaction import safe_url, scrub_text

if TYPE_CHECKING:
    from solana_sniper.app.bootstrap import Runtime

log = get_logger(__name__)


class ConnectionProbe(Protocol):
    """Anything that can report whether it is connected and when it last saw data."""

    name: str
    kind: str  # "websocket" | "http-poll" | "stream"

    def is_connected(self) -> bool: ...

    def last_activity(self) -> datetime | None: ...


def _age(now: datetime, when: datetime | None) -> float | None:
    return None if when is None else max(0.0, (now - when).total_seconds())


class HealthReporter:
    def __init__(
        self,
        runtime: Runtime,
        path: Path,
        *,
        interval_s: float = 5.0,
        stale_after_s: float = 20.0,
    ) -> None:
        self._runtime = runtime
        self._path = path
        self._interval = interval_s
        self._stale_after = stale_after_s
        self._probes: list[ConnectionProbe] = []
        self.writes = 0

    def add_probe(self, probe: ConnectionProbe) -> None:
        self._probes.append(probe)

    # ---------------------------------------------------------------- snapshot
    def snapshot(self) -> dict[str, Any]:
        rt = self._runtime
        engine = rt.engine
        now = rt.clock.now()
        wall = datetime.now(tz=UTC)
        acct_snap = rt.account.snapshot(now)
        stats = engine.stats
        providers: dict[str, Any] = {}
        any_connected = False
        for probe in self._probes:
            connected = probe.is_connected()
            any_connected = any_connected or connected
            providers[probe.name] = {
                "kind": probe.kind,
                "connected": connected,
                "last_activity_age_s": _age(wall, probe.last_activity()),
            }
        last_snapshot_age = _age(now, engine.last_snapshot_at)
        market_ok = last_snapshot_age is not None and last_snapshot_age <= self._stale_after
        db = rt.repo
        db_ok = db.last_flush_error is None and db.failures == 0
        tick_summary = rt.metrics.latencies["engine_tick"].summary()
        last_tick_age = _age(now, engine.last_tick_at)
        monitored = [c for c in engine.candidates.values() if not c.sm.is_terminal]
        positions = [
            {
                "position_id": p.position_id,
                "symbol": p.symbol,
                "mint": p.mint,
                "cost_eur": str(q_display(p.cost_basis_eur)),
                "value_eur": str(q_display(p.current_value_eur)),
                "pnl_pct": round(p.pnl_pct, 4),
                "held_s": round(p.holding_seconds(now)),
                "executable_value": p.value_is_executable,
                "data_stale": p.data_stale,
                "provenance": str(p.provenance),
                "units": str(p.units),
                "verified_onchain": False,
            }
            for p in rt.account.open_positions
        ]
        last_error = stats.last_error
        healthy = (
            (last_tick_age is not None and last_tick_age <= 5.0)
            and db_ok
            and (any_connected or not self._probes)
        )
        return {
            "written_at": wall.isoformat(),
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "mode": str(engine.mode),
            "session_id": engine.session_id,
            "started_at": engine.started_at.isoformat() if engine.started_at else None,
            "uptime_s": _age(now, engine.started_at),
            "healthy": healthy,
            "records_verified_onchain": False,  # this software never reconciles fills on-chain
            "engine": {
                "last_tick_age_s": last_tick_age,
                "tick_p50_ms": tick_summary.get("p50_ms"),
                "tick_p95_ms": tick_summary.get("p95_ms"),
                "evaluations": stats.evaluations,
            },
            "connections": providers,
            "market_data": {
                "ok": market_ok,
                "watched": len(rt.market.watched()),
                "last_snapshot_age_s": last_snapshot_age,
                "snapshots_total": rt.metrics.counters["snapshots"],
                "trades_total": rt.metrics.counters["trades"],
                "snapshots_last_minute": engine.snapshots_last_minute(now),
                "discovery_rate_per_s": round(rt.metrics.discovery_rate_per_s(), 3),
            },
            "database": {
                "url": safe_url(rt.settings.storage.database_url),
                "ok": db_ok,
                "last_flush_age_s": _age(wall, db.last_flush_at),
                "last_error": scrub_text(db.last_flush_error) if db.last_flush_error else None,
                "failures": db.failures,
                "dropped": db.dropped,
                "queued": db.queue_size,
            },
            "tokens_monitored": len(monitored),
            "qualified": sum(1 for c in monitored if c.state is S.QUALIFIED),
            "pending_signals": len(engine.d.execution.pending()),
            "open_positions": positions,
            "last_signal_at": stats.last_signal_at.isoformat() if stats.last_signal_at else None,
            "last_error": None
            if last_error is None
            else {
                "at": last_error.at.isoformat(),
                "component": last_error.component,
                "message": last_error.message,
            },
            "counters": {
                "discovered": stats.discovered,
                "rejected": stats.rejected,
                "qualified_total": stats.qualified,
                "signals": stats.signals,
                "confirmed": stats.confirmed,
                "cancelled": stats.cancelled,
                "exits": stats.exits,
                "outcomes_following": len(engine.d.outcomes),
                "outcomes_finalized": engine.d.outcomes.finalized_count,
            },
            "portfolio": {
                "equity_eur": str(q_display(acct_snap.equity_eur)),
                "cash_eur": str(q_display(acct_snap.cash_eur)),
                "open_exposure_eur": str(q_display(acct_snap.open_exposure_eur)),
                "peak_equity_eur": str(q_display(acct_snap.peak_equity_eur)),
                "drawdown_pct": round(acct_snap.drawdown_pct, 4),
                "realized_pnl_eur": str(q_display(acct_snap.realized_pnl_eur)),
                "unrealized_pnl_eur": str(q_display(acct_snap.unrealized_pnl_eur)),
                "wins": acct_snap.wins,
                "losses": acct_snap.losses,
            },
            "fx": {"sol_eur": str(rt.engine.d.fx.sol_eur()), "live": rt.engine.d.fx.is_live},
        }

    # ------------------------------------------------------------------ write
    def write_once(self) -> None:
        data = self.snapshot()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".status-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, default=str)
            os.replace(tmp, self._path)
            self.writes += 1
        except OSError as exc:
            log.warning("health_write_failed", path=str(self._path), error=str(exc))
            with __import__("contextlib").suppress(OSError):
                os.unlink(tmp)

    def write_final(self, reason: str) -> None:
        """Mark the heartbeat as stopped so status.sh distinguishes a clean stop from a crash."""
        try:
            data = self.snapshot()
        except Exception as exc:
            data = {"error": str(exc)}
        data.update({"healthy": False, "stopped": True, "stop_reason": reason})
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with __import__("contextlib").suppress(OSError):
            self._path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    async def run(self) -> None:
        while True:
            try:
                self.write_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("health_snapshot_failed", error=str(exc))
            await asyncio.sleep(self._interval)


def read_status(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def status_is_fresh(status: dict[str, Any], max_age_s: float = 30.0) -> bool:
    written = status.get("written_at")
    if not isinstance(written, str):
        return False
    try:
        age = (datetime.now(tz=UTC) - datetime.fromisoformat(written)).total_seconds()
    except ValueError:
        return False
    return age <= max_age_s and not status.get("stopped", False)
