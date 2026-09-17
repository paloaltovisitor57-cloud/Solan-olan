"""solana-sniper command line."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from solana_sniper import __version__
from solana_sniper.config import load_settings
from solana_sniper.domain.enums import RunMode
from solana_sniper.domain.money import q_display
from solana_sniper.storage.repository import Repository
from solana_sniper.telemetry.logging import configure_logging

app = typer.Typer(
    help="Live Solana new-token sniper: discover, score, size, confirm manually.",
    no_args_is_help=True,
)
console = Console()

ConfigOpt = Annotated[
    Path | None,
    typer.Option("--config", "-c", help="YAML config path (default configs/default.yaml)"),
]


def _settings(config: Path | None) -> object:
    return load_settings(config)


@app.callback()
def _root() -> None:
    """solana-sniper."""


@app.command()
def version() -> None:
    console.print(f"solana-sniper {__version__}")


@app.command()
def run(
    config: ConfigOpt = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Live data, simulated confirmations/fills")
    ] = False,
    no_dashboard: Annotated[
        bool, typer.Option("--no-dashboard", help="Plain log output instead of the TUI")
    ] = False,
    duration: Annotated[
        float | None, typer.Option("--duration", help="Stop after N seconds (testing)")
    ] = None,
    seed: Annotated[int, typer.Option("--seed", help="Synthetic world seed")] = 7,
) -> None:
    """Start the live engine (signal mode) or --dry-run (same engine, simulated confirmations)."""
    settings = load_settings(config)
    mode = RunMode.DRY_RUN if dry_run else RunMode.LIVE
    use_dashboard = settings.dashboard.enabled and not no_dashboard
    configure_logging(
        settings.telemetry.log_level,
        settings.telemetry.log_json,
        settings.telemetry.log_file,
        quiet_console=use_dashboard,
    )
    asyncio.run(_run(settings, mode, use_dashboard, duration, seed))


async def _run(
    settings: object, mode: RunMode, use_dashboard: bool, duration: float | None, seed: int
) -> None:
    from solana_sniper.app.bootstrap import build_runtime
    from solana_sniper.cli.commands import CommandHandler, StdinReader
    from solana_sniper.cli.dashboard import Dashboard
    from solana_sniper.config.settings import Settings

    assert isinstance(settings, Settings)
    runtime = build_runtime(settings, mode=mode, synthetic_seed=seed, quiet_alerts=False)
    stop = asyncio.Event()
    dashboard: Dashboard | None = None
    say: Callable[[str], None]
    if use_dashboard:
        dashboard = Dashboard(runtime.engine, settings.dashboard, console)
        runtime.terminal_alerts.set_sink(dashboard.push_alert)
        say = dashboard.push_message
    else:

        def say(msg: str) -> None:
            console.print(f"[bold]> {msg}[/]")

    runtime.install_signal_handlers(stop.set)
    await runtime.start()
    console.print(
        f"[bold]solana-sniper[/] {mode} session [cyan]{runtime.session_id}[/] "
        f"sources={settings.discovery.sources} quotes={settings.quotes.source} "
        f"bankroll=€{q_display(runtime.account.equity)}"
    )
    if mode is RunMode.LIVE:
        console.print(
            "[bold red]LIVE SIGNAL MODE[/]: nothing is signed or broadcast. Confirm with b N / s N."
        )
    handler = CommandHandler(runtime.engine, stop.set, say)
    reader = StdinReader()
    tasks: list[asyncio.Task[None]] = []
    with contextlib.suppress(RuntimeError):
        reader.start()
    tasks.append(asyncio.create_task(handler.run(reader), name="commands"))
    if dashboard is not None:
        tasks.append(asyncio.create_task(dashboard.run(), name="dashboard"))
    else:
        tasks.append(asyncio.create_task(_plain_status(runtime), name="status-printer"))
    waiter = asyncio.create_task(runtime.wait(), name="runtime-wait")
    try:
        if duration is not None:
            await asyncio.wait(
                {asyncio.create_task(stop.wait()), waiter},
                timeout=duration,
                return_when=asyncio.FIRST_COMPLETED,
            )
        else:
            await asyncio.wait(
                {asyncio.create_task(stop.wait()), waiter}, return_when=asyncio.FIRST_COMPLETED
            )
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        await runtime.stop()
        snap = runtime.account.snapshot(runtime.clock.now())
        console.print(
            f"[bold]stopped[/] equity=€{q_display(snap.equity_eur)} cash=€{q_display(snap.cash_eur)} "
            f"realized=€{q_display(snap.realized_pnl_eur)} signals={runtime.engine.stats.signals} "
            f"confirmed={runtime.engine.stats.confirmed} exits={runtime.engine.stats.exits} "
            f"rejected={runtime.engine.stats.rejected}"
        )
        lat = runtime.metrics.latencies
        console.print(
            "latency ms: "
            + "  ".join(f"{k}={v.summary()}" for k, v in lat.items() if v.summary().get("count"))
        )


async def _plain_status(runtime: object) -> None:
    from solana_sniper.app.bootstrap import Runtime

    assert isinstance(runtime, Runtime)
    printed = 0
    while True:
        await asyncio.sleep(1.0)
        recent = runtime.engine.stats.recent
        for line in recent[printed:]:
            console.print(line)
        printed = len(recent)
        if printed > 150:
            del recent[:-50]
            printed = len(recent)


def _open_repo(config: Path | None) -> tuple[Repository, object]:
    settings = load_settings(config)
    repo = Repository(settings.storage.database_url, session_id="cli")
    return repo, settings


@app.command()
def status(config: ConfigOpt = None) -> None:
    """Portfolio + activity summary from the database (works while `run` is active)."""

    async def go() -> None:
        repo, _ = _open_repo(config)
        await repo.init()
        snap = await repo.latest_portfolio_snapshot()
        counts = await repo.counts()
        sessions = await repo.list_sessions(limit=3)
        await repo.close()
        if snap is None:
            console.print("no portfolio snapshot yet")
        else:
            table = Table(title=f"portfolio @ {snap.at:%Y-%m-%d %H:%M:%S}")
            table.add_column("metric")
            table.add_column("value", justify="right")
            for k, v in (
                ("equity", snap.equity_eur),
                ("cash", snap.cash_eur),
                ("open exposure", snap.open_exposure_eur),
                ("open value", snap.open_value_eur),
                ("peak equity", snap.peak_equity_eur),
                ("realized pnl", snap.realized_pnl_eur),
                ("unrealized pnl", snap.unrealized_pnl_eur),
                ("fees", snap.fees_eur),
                ("slippage", snap.slippage_eur),
            ):
                table.add_row(k, f"€{q_display(v)}")
            table.add_row("drawdown", f"{snap.drawdown_pct:.1%}")
            table.add_row("open positions", str(snap.open_positions))
            table.add_row("wins/losses", f"{snap.wins}/{snap.losses}")
            console.print(table)
        console.print(f"db counts: {counts}")
        for s in sessions:
            console.print(
                f"session {s['session_id']} {s['mode']} started {s['started_at']} ended {s['ended_at']}"
            )

    asyncio.run(go())


@app.command()
def positions(
    config: ConfigOpt = None,
    all_: Annotated[bool, typer.Option("--all", help="Include closed")] = False,
) -> None:
    """List open (or all) positions."""

    async def go() -> None:
        repo, _ = _open_repo(config)
        await repo.init()
        rows = await repo.positions(open_only=not all_)
        await repo.close()
        table = Table(title="positions")
        for col in (
            "id",
            "sym",
            "state",
            "opened",
            "qty",
            "cost",
            "value",
            "peak",
            "pnl",
            "reason",
        ):
            table.add_column(col)
        for p in rows:
            pnl = p.realized_pnl_eur if p.realized_pnl_eur is not None else p.unrealized_pnl_eur
            table.add_row(
                p.position_id[-8:],
                p.symbol or p.mint[:6],
                str(p.state),
                f"{p.opened_at:%m-%d %H:%M:%S}",
                f"{p.quantity:,.0f}",
                f"€{q_display(p.cost_basis_eur)}",
                f"€{q_display(p.exit_value_eur if p.exit_value_eur is not None else p.current_value_eur)}",
                f"€{q_display(p.peak_value_eur)}",
                f"€{q_display(pnl)} ({p.pnl_pct:+.0%})",
                str(p.exit_reason or ""),
            )
        console.print(table if rows else "no positions")

    asyncio.run(go())


@app.command()
def candidates(config: ConfigOpt = None, limit: int = 20) -> None:
    """Most recently scored candidates."""

    async def go() -> None:
        repo, _ = _open_repo(config)
        await repo.init()
        rows = await repo.recent_scores(limit)
        await repo.close()
        table = Table(title="recent scores")
        for col in ("scored_at", "mint", "score", "session"):
            table.add_column(col)
        for r in rows:
            table.add_row(r["scored_at"], r["mint"], f"{r['score']:.0f}", r["session_id"])
        console.print(table if rows else "no scores yet")

    asyncio.run(go())


@app.command()
def portfolio(config: ConfigOpt = None) -> None:
    """Ledger-level portfolio view."""

    async def go() -> None:
        repo, _ = _open_repo(config)
        await repo.init()
        state = await repo.load_state()
        await repo.close()
        console.print(
            f"cash €{q_display(state.cash)}  realized €{q_display(state.realized_pnl)}  fees €{q_display(state.fees)}  "
            f"slippage €{q_display(state.slippage)}  peak €{q_display(state.peak_equity)}  W/L {state.wins}/{state.losses}  "
            f"open positions {len(state.positions)}  milestones {[str(m) for m in state.milestones_reached]}"
        )
        table = Table(title="ledger (last 25)")
        for col in ("seq", "at", "kind", "delta", "cash after", "pnl", "description"):
            table.add_column(col)
        for e in state.ledger[-25:]:
            table.add_row(
                str(e.seq),
                f"{e.at:%m-%d %H:%M:%S}",
                str(e.kind),
                f"{q_display(e.cash_delta_eur):+}",
                f"€{q_display(e.cash_after_eur)}",
                f"{q_display(e.realized_pnl_eur):+}",
                e.description[:60],
            )
        console.print(table)

    asyncio.run(go())


@app.command()
def inspect(token: str, config: ConfigOpt = None) -> None:
    """Everything recorded about a token mint."""

    async def go() -> None:
        repo, _ = _open_repo(config)
        await repo.init()
        history = await repo.token_history(token)
        await repo.close()
        console.print_json(json.dumps(history, default=str))

    asyncio.run(go())


@app.command()
def replay(session_id: str, config: ConfigOpt = None) -> None:
    """Re-run a recorded session's observations through the engine (debug decisions)."""
    from solana_sniper.app.replay import replay_session

    settings = load_settings(config)
    configure_logging(
        settings.telemetry.log_level,
        settings.telemetry.log_json,
        settings.telemetry.log_file,
        quiet_console=True,
    )
    summary = asyncio.run(replay_session(settings, session_id))
    console.print(
        f"replayed {summary.events} events / {summary.tokens} tokens -> signals={summary.signals} "
        f"confirmed={summary.confirmed} exits={summary.exits} final equity €{q_display(summary.final_equity)} "
        f"(replay session {summary.session_id})"
    )
    for line in summary.log[-40:]:
        console.print(line)


@app.command()
def sessions(config: ConfigOpt = None) -> None:
    """List recorded sessions (for replay)."""

    async def go() -> None:
        repo, _ = _open_repo(config)
        await repo.init()
        rows = await repo.list_sessions()
        await repo.close()
        for s in rows:
            console.print(
                f"{s['session_id']}  {s['mode']}  {s['started_at']}  ended={s['ended_at']}"
            )
        if not rows:
            console.print("no sessions recorded")

    asyncio.run(go())


@app.command()
def doctor(config: ConfigOpt = None) -> None:
    """Verify configuration, database, network, providers, credentials and quotes."""
    from solana_sniper.app.doctor import run_doctor

    settings = load_settings(config)
    results = asyncio.run(run_doctor(settings))
    table = Table(title="doctor")
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail")
    colors = {"PASS": "green", "FAIL": "red", "WARN": "yellow", "SKIP": "dim"}
    for r in results:
        table.add_row(r.name, f"[{colors[r.status]}]{r.status}[/]", r.detail)
    console.print(table)
    if any(r.status == "FAIL" for r in results):
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
