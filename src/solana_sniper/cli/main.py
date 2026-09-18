"""solana-sniper command line."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
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
from solana_sniper.telemetry.redaction import safe_url

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
    quiet: Annotated[
        bool, typer.Option("--quiet", help="No per-event stdout (service mode)")
    ] = False,
) -> None:
    """Start the live engine (signal mode) or --dry-run (same engine, simulated confirmations)."""
    settings = load_settings(config)
    mode = RunMode.DRY_RUN if dry_run else RunMode.LIVE
    use_dashboard = settings.dashboard.enabled and not no_dashboard and not quiet
    configure_logging(
        settings.telemetry.log_level,
        settings.telemetry.log_json,
        settings.telemetry.log_file,
        quiet_console=use_dashboard,
    )
    try:
        asyncio.run(_run(settings, mode, use_dashboard, duration, seed, quiet))
    finally:
        logging.shutdown()


async def _run(
    settings: object,
    mode: RunMode,
    use_dashboard: bool,
    duration: float | None,
    seed: int,
    quiet: bool = False,
) -> None:
    from solana_sniper.app.bootstrap import build_runtime
    from solana_sniper.app.command_file import FileCommandSource
    from solana_sniper.cli.commands import CommandHandler, StdinReader
    from solana_sniper.cli.dashboard import Dashboard
    from solana_sniper.config.paths import COMMANDS_FILE, state_path
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
    # headless confirmations: `./cmd.sh b 1` appends to the commands file
    commands_path = state_path(settings.home, COMMANDS_FILE)
    commands_file = FileCommandSource(commands_path)
    tasks.append(asyncio.create_task(commands_file.run(handler.handle), name="file-commands"))
    console.print(f"[dim]command file: {commands_path}[/]")
    if dashboard is not None:
        tasks.append(asyncio.create_task(dashboard.run(), name="dashboard"))
    elif not quiet:
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
        await runtime.stop(reason="signal" if stop.is_set() else "duration elapsed")
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
            table = Table(
                title=f"portfolio @ {snap.at:%Y-%m-%d %H:%M:%S}  "
                "(simulated / quote-estimated / user-reported records; not on-chain verified)"
            )
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
            "prov",
        ):
            table.add_column(col)
        console.print(
            "[yellow]provenance: SIMULATED / ESTIMATED (quoted amounts) / USER_REPORTED / "
            "UNKNOWN_LEGACY. No record is on-chain verified.[/]"
        )
        for p in rows:
            pnl = p.realized_pnl_eur if p.realized_pnl_eur is not None else p.unrealized_pnl_eur
            table.add_row(
                p.position_id[-8:],
                p.symbol or p.mint[:6],
                str(p.state),
                f"{p.opened_at:%m-%d %H:%M:%S}",
                f"{p.quantity_ui:,.0f}" + ("" if p.units_known else " (units?)"),
                f"€{q_display(p.cost_basis_eur)}",
                f"€{q_display(p.exit_value_eur if p.exit_value_eur is not None else p.current_value_eur)}",
                f"€{q_display(p.peak_value_eur)}",
                f"€{q_display(pnl)} ({p.pnl_pct:+.0%})",
                str(p.exit_reason or ""),
                str(p.provenance),
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
        mix: dict[str, int] = {}
        for entry in state.ledger:
            mix[str(entry.provenance)] = mix.get(str(entry.provenance), 0) + 1
        console.print(
            "[yellow]Every figure is simulated, quote-estimated or user-reported; nothing is "
            "reconciled against the chain.[/] ledger provenance mix: "
            + (", ".join(f"{k} {v}" for k, v in sorted(mix.items())) or "empty")
        )
        table = Table(title="ledger (last 25)")
        for col in ("seq", "at", "kind", "delta", "cash after", "pnl", "prov", "description"):
            table.add_column(col)
        for e in state.ledger[-25:]:
            table.add_row(
                str(e.seq),
                f"{e.at:%m-%d %H:%M:%S}",
                str(e.kind),
                f"{q_display(e.cash_delta_eur):+}",
                f"€{q_display(e.cash_after_eur)}",
                f"{q_display(e.realized_pnl_eur):+}",
                str(e.provenance),
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
def evaluate(
    config: ConfigOpt = None,
    include_truncated: bool = typer.Option(
        False, help="Also count rows finalised early at shutdown (before their horizon elapsed)."
    ),
    min_observations: int = typer.Option(
        5, min=1, help="Ignore rows with fewer market observations than this."
    ),
    session: str | None = typer.Option(None, help="Restrict to one recorded session id."),
) -> None:
    """Hit rates per score bucket from recorded forward outcomes. Measurement, never a forecast."""
    from solana_sniper.strategy.evaluation import MIN_MEANINGFUL_N, MULTIPLES, summarize

    async def go() -> None:
        repo, _ = _open_repo(config)
        await repo.init()
        rows = await repo.outcomes(session_id=session)
        await repo.close()
        report = summarize(
            rows, include_truncated=include_truncated, min_observations=min_observations
        )
        if report.used_rows == 0:
            console.print(
                f"no usable outcomes ({report.total_rows} rows recorded, "
                f"{report.excluded_truncated} truncated, {report.excluded_short} too short). "
                "Run the engine for longer than outcomes.horizon_s and try again."
            )
            return
        horizon = f"{report.horizon_s:.0f}s" if report.horizon_s else "mixed"
        table = Table(
            title=f"forward outcomes: {report.used_rows} tokens, horizon {horizon} "
            f"(passive observation from first sight; excludes slippage, fees, fill risk)"
        )
        table.add_column("group")
        table.add_column("n", justify="right")
        for m in MULTIPLES:
            table.add_column(f">={m:g}x", justify="right")
        table.add_column("median peak", justify="right")
        table.add_column("median end", justify="right")
        table.add_column("median dd", justify="right")
        table.add_column("liq. pulled", justify="right")
        table.add_column("closed pnl", justify="right")
        for b in report.buckets:
            if b.n == 0:
                continue
            cells = [b.name, str(b.n)]
            for m in MULTIPLES:
                lo, hi = b.interval(m)
                cells.append(f"{b.rate(m):.0%} [{lo:.0%}-{hi:.0%}]")
            cells.append(f"{b.median_max_multiple:.2f}x" if b.median_max_multiple else "-")
            cells.append(f"{b.median_final_multiple:.2f}x" if b.median_final_multiple else "-")
            cells.append(f"{b.median_drawdown:.0%}" if b.median_drawdown is not None else "-")
            cells.append(f"{b.rug_rate:.0%}")
            cells.append(
                f"{b.median_closed_pnl_pct:+.0%} (n={b.entered})"
                if b.median_closed_pnl_pct is not None
                else "-"
            )
            style = "" if b.meaningful else "dim"
            table.add_row(*cells, style=style)
        console.print(table)
        console.print(
            f"excluded: {report.excluded_truncated} truncated at shutdown, "
            f"{report.excluded_short} with < {min_observations} observations. "
            f"Rows: {report.simulated_rows} simulated/dry-run, {report.live_rows} live-data."
        )
        console.print(
            f"[bold]Read this carefully:[/bold] groups with n < {MIN_MEANINGFUL_N} are dimmed "
            "because their rates are noise; brackets are 95% Wilson intervals. "
            "Multiples are what a passive observer saw from the first snapshot, not what a "
            "buyer would have realised. Past outcomes do not predict future ones."
        )
        if report.all_simulated:
            console.print(
                "[yellow]Every row here is from a simulated or synthetic run. It says nothing "
                "about real Solana tokens.[/yellow]"
            )
        elif report.mixed_provenance:
            console.print(
                "[yellow]Simulated and live rows are mixed; use --session to separate them.[/yellow]"
            )

    asyncio.run(go())


@app.command()
def health(
    config: ConfigOpt = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the raw heartbeat JSON")
    ] = False,
    quiet_check: Annotated[
        bool, typer.Option("--quiet-check", help="Exit 0 only if fresh and healthy")
    ] = False,
) -> None:
    """Show the running service's heartbeat (written every 5 s to <state>/status.json)."""
    from solana_sniper.app.health import read_status, status_is_fresh
    from solana_sniper.config.paths import STATUS_FILE, state_path

    settings = load_settings(config)
    path = state_path(settings.home, STATUS_FILE)
    status = read_status(path)
    if status is None:
        if not quiet_check:
            console.print(f"[yellow]no heartbeat at {path}[/] (service not started yet?)")
        raise typer.Exit(code=2)
    fresh = status_is_fresh(status)
    healthy = bool(status.get("healthy")) and fresh
    if quiet_check:
        raise typer.Exit(code=0 if healthy else 1)
    if json_output:
        console.print_json(json.dumps(status, default=str))
        raise typer.Exit(code=0 if healthy else 1)
    _print_health(status, fresh, healthy)
    raise typer.Exit(code=0 if healthy else 1)


def _print_health(status: dict[str, object], fresh: bool, healthy: bool) -> None:
    def num(v: object) -> str:
        return f"{v:.0f}s ago" if isinstance(v, int | float) else "never"

    written = status.get("written_at", "?")
    if healthy:
        state, color = "HEALTHY", "green"
    elif status.get("stopped"):
        state, color = "STOPPED", "yellow"
    elif not fresh:
        state, color = "STALE", "red"
    else:
        state, color = "DEGRADED", "yellow"
    table = Table(title=f"heartbeat [{color}]{state}[/] @ {written}")
    table.add_column("item")
    table.add_column("value")
    uptime = status.get("uptime_s")
    table.add_row(
        "process", f"pid {status.get('pid')} on {status.get('hostname')} mode {status.get('mode')}"
    )
    table.add_row("session", str(status.get("session_id")))
    table.add_row("uptime", f"{uptime / 60:.1f} min" if isinstance(uptime, int | float) else "?")
    engine = status.get("engine") or {}
    assert isinstance(engine, dict)
    table.add_row(
        "engine tick",
        f"p50 {engine.get('tick_p50_ms')} ms  p95 {engine.get('tick_p95_ms')} ms  last {num(engine.get('last_tick_age_s'))}",
    )
    connections = status.get("connections") or {}
    assert isinstance(connections, dict)
    for name, conn in connections.items():
        flag = "[green]connected[/]" if conn.get("connected") else "[red]disconnected[/]"
        table.add_row(
            f"connection {name}",
            f"{flag} ({conn.get('kind')}) last activity {num(conn.get('last_activity_age_s'))}",
        )
    md = status.get("market_data") or {}
    assert isinstance(md, dict)
    table.add_row(
        "market data",
        f"{'[green]ok[/]' if md.get('ok') else '[red]stale[/]'}  watched {md.get('watched')}  "
        f"snapshots/min {md.get('snapshots_last_minute')}  last {num(md.get('last_snapshot_age_s'))}  "
        f"new tokens/s {md.get('discovery_rate_per_s')}",
    )
    db = status.get("database") or {}
    assert isinstance(db, dict)
    table.add_row(
        "database",
        f"{'[green]ok[/]' if db.get('ok') else '[red]error[/]'}  last flush {num(db.get('last_flush_age_s'))}  "
        f"failures {db.get('failures')}  dropped {db.get('dropped')}  queued {db.get('queued')}"
        + (f"  [red]{db.get('last_error')}[/]" if db.get("last_error") else ""),
    )
    table.add_row(
        "tokens monitored",
        f"{status.get('tokens_monitored')} (qualified {status.get('qualified')}, pending signals {status.get('pending_signals')})",
    )
    positions = status.get("open_positions") or []
    assert isinstance(positions, list)
    lines = [
        f"{p.get('symbol') or str(p.get('mint', ''))[:8]} cost €{p.get('cost_eur')} value €{p.get('value_eur')} "
        f"pnl {float(p.get('pnl_pct', 0)):+.0%} held {p.get('held_s')}s{' (stale data)' if p.get('data_stale') else ''}"
        for p in positions
    ]
    table.add_row("open positions", "\n".join(lines) or "none")
    table.add_row("last signal", str(status.get("last_signal_at") or "none yet"))
    err = status.get("last_error")
    table.add_row(
        "last error",
        f"{err.get('at')} {err.get('component')}: {err.get('message')}"
        if isinstance(err, dict)
        else "none",
    )
    pf = status.get("portfolio") or {}
    assert isinstance(pf, dict)
    table.add_row(
        "portfolio",
        f"equity €{pf.get('equity_eur')}  cash €{pf.get('cash_eur')}  exposure €{pf.get('open_exposure_eur')}  "
        f"realized €{pf.get('realized_pnl_eur')}  dd {float(pf.get('drawdown_pct', 0)):.1%}  W/L {pf.get('wins')}/{pf.get('losses')}",
    )
    counters = status.get("counters") or {}
    assert isinstance(counters, dict)
    table.add_row("counters", ", ".join(f"{k} {v}" for k, v in counters.items()))
    console.print(table)


@app.command()
def migrate(config: ConfigOpt = None) -> None:
    """Apply pending database schema migrations (safe to run repeatedly)."""
    from solana_sniper.storage.migrations import run_migrations, schema_version

    settings = load_settings(config)
    applied = asyncio.run(run_migrations(settings.storage.database_url))
    version = asyncio.run(schema_version(settings.storage.database_url))
    console.print(
        f"database {settings.storage.database_url}: schema version {version}"
        + (f", applied {applied}" if applied else ", nothing to apply")
    )


@app.command("config-check")
def config_check(config: ConfigOpt = None) -> None:
    """Load and validate configuration without touching the network; exit 1 on problems."""
    try:
        settings = load_settings(config)
    except Exception as exc:
        console.print(f"[red]configuration invalid:[/] {exc}")
        raise typer.Exit(code=1) from None
    problems: list[str] = []
    if settings.risk.starting_bankroll_eur <= 0:
        problems.append("risk.starting_bankroll_eur must be positive")
    if settings.entry.min_score <= 0 or settings.entry.min_score > 100:
        problems.append("entry.min_score must be in (0, 100]")
    if not settings.discovery.sources:
        problems.append("discovery.sources is empty")
    if settings.quotes.prepare_unsigned_transaction and not settings.providers.wallet_public_key:
        problems.append(
            "quotes.prepare_unsigned_transaction needs providers.wallet_public_key (PUBLIC key)"
        )
    console.print(
        f"config {settings.config_path or 'defaults'}  home {settings.home or '(cwd)'}  "
        f"profile {settings.risk.profile}  bankroll €{settings.risk.starting_bankroll_eur}  "
        f"sources {settings.discovery.sources}  quotes {settings.quotes.source}  "
        f"db {safe_url(settings.storage.database_url)}  log {settings.telemetry.log_file}"
    )
    if problems:
        for p in problems:
            console.print(f"[red]- {p}[/]")
        raise typer.Exit(code=1)
    console.print("[green]configuration ok[/]")


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
