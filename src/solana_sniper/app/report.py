"""Final session report for paper runs (printed on Ctrl+C / duration end)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from solana_sniper.app.paper import PaperSession, SessionReport

if TYPE_CHECKING:
    from solana_sniper.app.bootstrap import Runtime


def build_session_report(runtime: Runtime, meta: PaperSession, *, reason: str) -> SessionReport:
    acct = runtime.account
    now = runtime.clock.now()
    snap = acct.snapshot(now)
    fx = runtime.engine.d.fx
    rate_now = fx.sol_eur()
    equity = snap.equity_eur
    start = meta.bankroll_eur
    ret = float((equity - start) / start) if start > 0 else 0.0
    governor = getattr(runtime.http, "governor", None)
    trips = 0
    if governor is not None:
        trips = sum(int(info.get("circuit_trips", 0)) for info in governor.health().values())
    integrity = runtime.repo.integrity_summary()
    counters = runtime.metrics.counters
    started_at: datetime | None = runtime.engine.started_at
    run_time = (now - started_at).total_seconds() if started_at else 0.0
    return SessionReport(
        session_id=meta.session_id,
        started_label=(
            f"{meta.bankroll_sol:.4f} SOL (€{meta.bankroll_eur:.2f} at €{meta.sol_eur_start:.2f}"
            f"/SOL, {meta.fx_source} rate)"
        ),
        finished_equity_eur=equity,
        sol_equivalent=(equity / rate_now) if rate_now > 0 else None,
        sol_eur_now=rate_now if rate_now > 0 else None,
        fx_now_live=fx.is_live,
        return_pct=ret,
        max_drawdown_pct=float(
            (snap.peak_equity_eur - equity) / snap.peak_equity_eur
            if snap.peak_equity_eur > 0 and snap.peak_equity_eur > equity
            else Decimal(0)
        )
        if snap.drawdown_pct <= 0
        else snap.drawdown_pct,
        trades=acct.wins + acct.losses,
        wins=acct.wins,
        losses=acct.losses,
        realized_pnl_eur=snap.realized_pnl_eur,
        unrealized_pnl_eur=snap.unrealized_pnl_eur,
        fees_eur=snap.fees_eur,
        slippage_eur=snap.slippage_eur,
        open_positions=snap.open_positions,
        signals=runtime.engine.stats.signals,
        storage_failures=runtime.repo.failures,
        dropped_writes=runtime.repo.dropped,
        dropped_by_kind=dict(integrity["dropped_by_kind"]),
        provider_outages=trips,
        rate_limits_handled=int(counters.get("rate_limited", 0)),
        degraded_checks=int(counters.get("checks_degraded", 0)),
        data_complete=bool(integrity["complete"]) and runtime.bus.dropped == 0,
        run_time_s=run_time,
        stop_reason=reason,
    )
