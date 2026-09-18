"""solana-sniper command line."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from solana_sniper import __version__
from solana_sniper.app.paper import PaperSession
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
def _root(
    home: Annotated[
        Path | None,
        typer.Option(
            "--home",
            help="Runtime home (db/, logs/, state/, sniper.env). Default: $SNIPER_HOME, else the "
            "platform home the service uses (macOS: ~/Library/Application Support/SolanaSniper).",
            envvar="SNIPER_HOME",
            show_envvar=True,
        ),
    ] = None,
    paper_session: Annotated[
        str | None,
        typer.Option(
            "--paper",
            help="Read a paper session's database (id from `paper --list`) with status, "
            "positions, portfolio, evaluate, inspect, sessions.",
        ),
    ] = None,
) -> None:
    """solana-sniper: live-data paper trading and signal research for new Solana tokens."""
    from solana_sniper.config.loader import set_database_override
    from solana_sniper.config.paths import configured_home, set_home_override

    set_home_override(home)
    set_database_override(None)
    if paper_session is not None:
        from solana_sniper.app.paper import paper_db_path, paper_db_url

        runtime_home = configured_home()
        if not paper_db_path(runtime_home, paper_session).exists():
            console.print(
                f"[red]no paper session {paper_session!r} in {runtime_home / 'db' / 'paper'}[/]"
            )
            raise typer.Exit(code=2)
        set_database_override(paper_db_url(runtime_home, paper_session))


def _context_line(settings: object) -> str:
    from solana_sniper.config.paths import home_source
    from solana_sniper.config.settings import Settings

    assert isinstance(settings, Settings)
    return (
        f"[dim]runtime home {settings.home} ({home_source()})  "
        f"db {safe_url(settings.storage.database_url)}  "
        f"config {settings.config_path or 'defaults'}[/]"
    )


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
    paper: object = None,
) -> None:
    from solana_sniper.app.bootstrap import build_runtime
    from solana_sniper.app.command_file import FileCommandSource
    from solana_sniper.app.paper import PaperSession
    from solana_sniper.app.report import build_session_report
    from solana_sniper.cli.commands import CommandHandler, StdinReader
    from solana_sniper.cli.dashboard import Dashboard
    from solana_sniper.config.paths import COMMANDS_FILE, state_path
    from solana_sniper.config.settings import Settings

    assert isinstance(settings, Settings)
    assert paper is None or isinstance(paper, PaperSession)
    runtime = build_runtime(
        settings,
        mode=mode,
        session_id=paper.session_id if paper is not None else None,
        synthetic_seed=seed,
        quiet_alerts=False,
    )
    runtime.paper = paper
    stop = asyncio.Event()
    dashboard: Dashboard | None = None
    say: Callable[[str], None]
    if use_dashboard:
        dashboard = Dashboard(
            runtime.engine, settings.dashboard, console, paper=paper, runtime=runtime
        )
        runtime.terminal_alerts.set_sink(dashboard.push_alert)
        say = dashboard.push_message
    else:

        def say(msg: str) -> None:
            console.print(f"[bold]> {msg}[/]")

    runtime.install_signal_handlers(stop.set)
    await runtime.start()
    if paper is not None:
        await runtime.repo.save_paper_session(paper)
        _print_paper_banner(paper, runtime)
    else:
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
        reason = "Ctrl+C / signal" if stop.is_set() else "duration elapsed"
        if paper is not None:
            report = build_session_report(runtime, paper, reason=reason)
        await runtime.stop(reason=reason)
        if paper is not None:
            console.print()
            for line in report.lines():
                style = "bold" if line.startswith("SESSION COMPLETE") else ""
                if line.startswith("WARNING"):
                    style = "bold red"
                console.print(line, style=style, highlight=False)
        else:
            snap = runtime.account.snapshot(runtime.clock.now())
            console.print(
                f"[bold]stopped[/] equity=€{q_display(snap.equity_eur)} "
                f"cash=€{q_display(snap.cash_eur)} "
                f"realized=€{q_display(snap.realized_pnl_eur)} "
                f"signals={runtime.engine.stats.signals} "
                f"confirmed={runtime.engine.stats.confirmed} exits={runtime.engine.stats.exits} "
                f"rejected={runtime.engine.stats.rejected}"
            )
            lat = runtime.metrics.latencies
            console.print(
                "latency ms: "
                + "  ".join(
                    f"{k}={v.summary()}" for k, v in lat.items() if v.summary().get("count")
                )
            )


def _print_paper_banner(paper: object, runtime: object) -> None:
    from solana_sniper.app.bootstrap import Runtime
    from solana_sniper.app.paper import PaperSession

    assert isinstance(paper, PaperSession) and isinstance(runtime, Runtime)
    equity = runtime.account.equity
    console.print()
    console.print("[bold green]SOLANA SNIPER — PAPER[/]", highlight=False)
    synthetic = runtime.settings.is_synthetic or runtime.settings.quotes.source == "synthetic"
    console.print(
        f"Mode:              PAPER / {'SYNTHETIC' if synthetic else 'LIVE'} DATA", highlight=False
    )
    console.print(
        f"Market data:       {'SYNTHETIC' if synthetic else 'LIVE'}   Execution: SIMULATED",
        highlight=False,
    )
    console.print("[bold red]Real transactions: DISABLED[/] (no keys, no signing, no broadcast)")
    console.print(f"Session:           {paper.session_id}", highlight=False)
    console.print(f"Database:          {safe_url(paper.database_url)}", highlight=False)
    console.print(
        f"Starting bankroll: {paper.bankroll_sol:.4f} SOL  (requested {paper.requested})",
        highlight=False,
    )
    console.print(
        f"SOL/EUR at start:  €{paper.sol_eur_start:.2f} ({paper.fx_source} rate, "
        f"{paper.fx_at:%Y-%m-%d %H:%M:%S} UTC)",
        highlight=False,
    )
    console.print(f"Starting equity:   €{paper.bankroll_eur:.2f}", highlight=False)
    if equity != paper.bankroll_eur:
        console.print(f"Resumed equity:    €{equity:.2f}", highlight=False)
    console.print("Stop with Ctrl+C; a session summary is printed on exit.", style="dim")
    console.print()


@app.command()
def paper(
    config: ConfigOpt = None,
    bankroll_sol: Annotated[
        str | None,
        typer.Option("--bankroll-sol", help="Starting bankroll in SOL, converted once at start"),
    ] = None,
    bankroll_eur: Annotated[
        str | None, typer.Option("--bankroll-eur", help="Starting bankroll in EUR")
    ] = None,
    name: Annotated[
        str | None, typer.Option("--name", help="Label for this experiment (part of the id)")
    ] = None,
    resume: Annotated[
        str | None,
        typer.Option("--resume", help="Resume an existing paper session id (explicit only)"),
    ] = None,
    allow_fallback_fx: Annotated[
        bool,
        typer.Option(
            "--allow-fallback-fx",
            help="Start even if no live SOL/EUR rate is available (marked as fallback)",
        ),
    ] = False,
    list_sessions: Annotated[
        bool, typer.Option("--list", help="List paper sessions in this runtime home and exit")
    ] = False,
    no_dashboard: Annotated[
        bool, typer.Option("--no-dashboard", help="Plain log output instead of the TUI")
    ] = False,
    duration: Annotated[
        float | None, typer.Option("--duration", help="Stop after N seconds (testing)")
    ] = None,
    seed: Annotated[int, typer.Option("--seed", help="Synthetic world seed")] = 7,
    quiet: Annotated[bool, typer.Option("--quiet", help="No per-event stdout")] = False,
) -> None:
    """Paper trade on LIVE market data with NO real funds: a fresh, isolated session with a
    simulated bankroll (default 1 SOL). Nothing is ever signed or broadcast. Ctrl+C stops it."""
    from solana_sniper.app.paper import (
        BankrollRequest,
        PaperSession,
        PaperSetupError,
        apply_paper_settings,
        make_session_id,
        paper_db_path,
        paper_db_url,
    )

    settings = load_settings(config)
    home = settings.home
    assert home is not None
    if list_sessions:
        asyncio.run(_list_paper_sessions(home))
        return
    if resume is not None and (bankroll_sol is not None or bankroll_eur is not None):
        console.print(
            "[red]--resume continues an existing session with its recorded bankroll; "
            "do not combine it with --bankroll-sol/--bankroll-eur[/]"
        )
        raise typer.Exit(code=2)
    if resume is not None and name is not None:
        console.print("[red]--resume cannot be combined with --name[/]")
        raise typer.Exit(code=2)
    try:
        if resume is not None:
            if not paper_db_path(home, resume).exists():
                raise PaperSetupError(
                    f"no paper session {resume!r} in {home / 'db' / 'paper'} "
                    "(solana-sniper paper --list shows the recorded ones)"
                )
            meta = asyncio.run(_load_paper_meta(paper_db_url(home, resume), resume))
        else:
            request = BankrollRequest(sol=_decimal(bankroll_sol), eur=_decimal(bankroll_eur))
            if bankroll_sol is None and bankroll_eur is None:
                request = BankrollRequest(sol=Decimal(1))
                console.print("[dim]no bankroll given: defaulting to --bankroll-sol 1[/]")
            request.validate()
            now = datetime.now(tz=UTC)
            session_id = make_session_id(request, name, now)
            sol, eur, rate, source = asyncio.run(
                _resolve_paper_bankroll(settings, request, allow_fallback_fx, now)
            )
            meta = PaperSession(
                session_id=session_id,
                name=name,
                created_at=now,
                requested=request.label,
                bankroll_sol=sol,
                bankroll_eur=eur,
                sol_eur_start=rate,
                fx_source=source,
                fx_at=now,
                config_path=str(settings.config_path) if settings.config_path else None,
                database_url=paper_db_url(home, session_id),
            )
    except PaperSetupError as exc:
        console.print(f"[red]cannot start paper session:[/] {exc}")
        raise typer.Exit(code=2) from None
    apply_paper_settings(settings, meta)
    if settings.telemetry.log_file:
        settings.telemetry.log_file = str(home / "logs" / "paper" / f"{meta.session_id}.log")
    use_dashboard = settings.dashboard.enabled and not no_dashboard and not quiet
    configure_logging(
        settings.telemetry.log_level,
        settings.telemetry.log_json,
        settings.telemetry.log_file,
        quiet_console=use_dashboard,
    )
    try:
        asyncio.run(_run(settings, RunMode.PAPER, use_dashboard, duration, seed, quiet, meta))
    finally:
        logging.shutdown()


def _decimal(raw: str | None) -> Decimal | None:
    from solana_sniper.app.paper import PaperSetupError

    if raw is None:
        return None
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        raise PaperSetupError(f"{raw!r} is not a number") from None


async def _resolve_paper_bankroll(
    settings: object, request: object, allow_fallback_fx: bool, now: datetime
) -> tuple[Decimal, Decimal, Decimal, str]:
    from solana_sniper.app.bootstrap import build_fx
    from solana_sniper.app.paper import BankrollRequest, resolve_bankroll
    from solana_sniper.config.settings import Settings
    from solana_sniper.infra.http import HttpClient

    assert isinstance(settings, Settings) and isinstance(request, BankrollRequest)
    http = HttpClient(timeout_s=settings.providers.http_timeout_s)
    try:
        fx = build_fx(settings, http)
        synthetic = settings.is_synthetic or settings.quotes.source == "synthetic"
        sol, eur, rate, source = await resolve_bankroll(
            fx, request, allow_fallback_fx=allow_fallback_fx or synthetic, now=now
        )
        # the synthetic world has no market: its static rate is labelled as such, never "live"
        return sol, eur, rate, ("synthetic" if synthetic else source)
    finally:
        await http.aclose()


async def _load_paper_meta(database_url: str, session_id: str) -> PaperSession:
    from solana_sniper.app.paper import PaperSetupError

    repo = Repository(database_url, session_id=session_id)
    await repo.init()
    try:
        meta = await repo.paper_session(session_id)
    finally:
        await repo.close()
    if meta is None:
        raise PaperSetupError(f"{session_id} has a database but no paper metadata; not resumable")
    return meta


async def _list_paper_sessions(home: Path) -> None:
    from solana_sniper.app.paper import PAPER_DIR

    folder = home / "db" / PAPER_DIR
    files = sorted(folder.glob("paper-*.db")) if folder.exists() else []
    if not files:
        console.print(f"no paper sessions in {folder}")
        return
    table = Table(title=f"paper sessions in {folder}")
    for col in ("session", "requested", "SOL/EUR start", "fx", "equity now", "ended"):
        table.add_column(col)
    for f in files:
        sid = f.stem
        repo = Repository(f"sqlite+aiosqlite:///{f}", session_id="cli")
        try:
            await repo.init()
            meta = await repo.paper_session(sid)
            snap = await repo.latest_portfolio_snapshot()
            sessions = await repo.list_sessions(limit=1)
        finally:
            await repo.close()
        ended = sessions[0]["ended_at"] if sessions else None
        table.add_row(
            sid,
            meta.requested if meta else "?",
            f"€{meta.sol_eur_start:.2f}" if meta else "?",
            meta.fx_source if meta else "?",
            f"€{q_display(snap.equity_eur)}" if snap else "-",
            "running/unknown" if ended is None else str(ended)[:19],
        )
    console.print(table)
    console.print("[dim]resume one with: solana-sniper paper --resume <session>[/]")


@app.command("smoke-test")
def smoke_test(
    config: ConfigOpt = None,
    timeout_s: Annotated[float, typer.Option("--timeout", help="Per-check timeout")] = 10.0,
) -> None:
    """Real-network check of every provider (no trading): latency, rate limiting, advice."""
    from solana_sniper.app.smoke import run_smoke

    settings = load_settings(config)
    console.print(_context_line(settings))
    if settings.is_synthetic:
        console.print("[yellow]synthetic config: nothing to smoke-test on the network[/]")
        raise typer.Exit(code=0)
    results = asyncio.run(run_smoke(settings, timeout_s=timeout_s))
    table = Table(title="smoke test (real network, no trading)")
    for col in ("check", "status", "latency", "provider", "detail", "recommendation"):
        table.add_column(col, overflow="fold")
    colors = {"PASS": "green", "FAIL": "red", "WARN": "yellow", "SKIP": "dim"}
    for r in results:
        table.add_row(
            r.name,
            f"[{colors[r.status]}]{r.status}[/]",
            f"{r.latency_ms:.0f} ms" if r.latency_ms is not None else "-",
            r.provider_state or "-",
            r.detail,
            r.recommendation,
        )
    console.print(table)
    failed = [r.name for r in results if r.status == "FAIL"]
    if failed:
        console.print(f"[red]failed:[/] {', '.join(failed)}")
        raise typer.Exit(code=1)
    console.print("[green]all required checks passed[/]")


service_app = typer.Typer(
    help="Background macOS service (launchd). Thin wrappers around ./start.sh, ./stop.sh, ..."
)
app.add_typer(service_app, name="service")


def _script(name: str, *args: str) -> None:
    from solana_sniper.app.repo_safety import repo_root_from_package

    root = repo_root_from_package() or Path(__file__).resolve().parents[3]
    script = root / name
    if not script.exists():
        console.print(f"[red]{script} not found; run from a git checkout[/]")
        raise typer.Exit(code=1)
    code = subprocess.call([str(script), *args])
    raise typer.Exit(code=code)


@service_app.command("start")
def service_start() -> None:
    """Start (or re-register) the launchd service."""
    _script("start.sh")


@service_app.command("stop")
def service_stop() -> None:
    """Stop the launchd service gracefully."""
    _script("stop.sh")


@service_app.command("restart")
def service_restart() -> None:
    """Restart the launchd service."""
    _script("restart.sh")


@service_app.command("status")
def service_status() -> None:
    """launchd state plus the engine heartbeat."""
    _script("status.sh")


@service_app.command("logs")
def service_logs(
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Keep following")] = False,
    lines: Annotated[int, typer.Option("-n", help="Lines to show")] = 50,
) -> None:
    """Show recent service logs (add --follow to keep watching)."""
    args = ["-n", str(lines)] + (["--follow"] if follow else [])
    _script("logs.sh", *args)


@service_app.command("install")
def service_install() -> None:
    """Run the macOS installer (venv, deps, tests, launchd agent)."""
    _script("install-macos.sh")


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
def status(
    config: ConfigOpt = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Machine-readable output (no wrapping)")
    ] = False,
) -> None:
    """Portfolio + activity summary from the database (works while `run` is active)."""

    async def go() -> None:
        repo, settings = _open_repo(config)
        await repo.init()
        snap = await repo.latest_portfolio_snapshot()
        counts = await repo.counts()
        sessions = await repo.list_sessions(limit=3)
        await repo.close()
        from solana_sniper.config.settings import Settings

        assert isinstance(settings, Settings)
        if json_output:
            payload = {
                "home": str(settings.home),
                "database_url": settings.storage.database_url,
                "config": str(settings.config_path) if settings.config_path else None,
                "counts": counts,
                "sessions": sessions,
                "portfolio": None
                if snap is None
                else {
                    "at": snap.at.isoformat(),
                    "equity_eur": str(snap.equity_eur),
                    "cash_eur": str(snap.cash_eur),
                    "realized_pnl_eur": str(snap.realized_pnl_eur),
                    "open_positions": snap.open_positions,
                    "wins": snap.wins,
                    "losses": snap.losses,
                },
            }
            print(json.dumps(payload, default=str))
            return
        console.print(_context_line(settings))
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
    """Everything recorded about a token mint, starting with its entry attempts."""

    async def go() -> None:
        repo, _ = _open_repo(config)
        await repo.init()
        history = await repo.token_history(token)
        attempts = await repo.entry_attempts(mint=token)
        await repo.close()
        if attempts:
            console.print(f"[bold]entry attempts for {token}[/]")
            for a in attempts:
                for line in _attempt_lines(a):
                    console.print(line, highlight=False)
                console.print()
        else:
            console.print("[dim]no entry attempts recorded (the token never qualified)[/]")
        console.print_json(json.dumps(history, default=str))

    asyncio.run(go())


def _attempt_lines(a: object) -> list[str]:
    from solana_sniper.domain.models import EntryAttempt

    assert isinstance(a, EntryAttempt)
    decision = str(a.final_decision)
    color = {
        "BUY_SIGNAL": "green",
        "PENDING": "yellow",
        "EXPIRED": "yellow",
        "ABANDONED": "red",
        "HARD_REJECT": "red",
        "QUOTE_FAILED": "red",
        "SIZING_ZERO": "red",
        "STALE": "red",
        "CANCELLED": "dim",
    }.get(decision, "")
    quote = a.buy_quote_status
    if a.quote_attempts:
        quote = (
            f"buy {a.buy_quote_status} / sell {a.sell_quote_status} "
            f"({a.quote_attempts} attempt{'s' if a.quote_attempts != 1 else ''})"
        )
    rt = (
        f"{a.round_trip_loss_pct:.1%} estimated loss"
        f"{'' if a.round_trip_viable else ' (not viable)'}"
        if a.round_trip_loss_pct is not None
        else "n/a"
    )
    sizing = (
        f"€{a.recommended_eur:.2f} ({a.recommended_sol:.4f} SOL)"
        if a.recommended_eur is not None and a.recommended_sol is not None
        else ("not reached" if not a.sizing_attempted else "n/a")
    ) + (f"  caps: {a.sizing_reason}" if a.sizing_reason else "")
    post = f"{a.post_quote_score:.1f}" if a.post_quote_score is not None else "n/a"
    band = (
        f"{a.min_score_seen:.1f}-{a.max_score_seen:.1f} over {a.evaluations} evaluations"
        if a.min_score_seen is not None and a.max_score_seen is not None
        else "n/a"
    )
    verdict = f"[{color}]{decision}[/]" if color else decision
    if a.hysteresis_holds and decision == "BUY_SIGNAL":
        verdict += " (continued within hysteresis)"
    lines = [
        f"{a.symbol or a.mint[:8]}  attempt #{a.attempt_number}  {a.attempt_id}",
        f"  QUALIFIED {a.qualified_at:%H:%M:%S}  score {a.qualified_score:.1f}  "
        f"latch until {a.latch_until:%H:%M:%S}",
        f"  decimals: {a.decimals_status}",
        f"  sizing: {sizing}",
        f"  quote: {quote}"
        + (f"  error: {a.quote_error}" if a.quote_error else "")
        + (
            f"  impact buy {a.entry_price_impact_pct:.1f}%"
            if a.entry_price_impact_pct is not None
            else ""
        )
        + (
            f" / sell {a.exit_price_impact_pct:.1f}%" if a.exit_price_impact_pct is not None else ""
        ),
        f"  round trip: {rt}",
        f"  post quote score: {post}   score band: {band}   hysteresis holds: {a.hysteresis_holds}",
        f"  decision: {verdict}",
        f"  reason: {a.block_reason or '-'}",
    ]
    if a.completed_at is not None:
        lines.append(f"  completed {a.completed_at:%H:%M:%S}")
    return lines


@app.command("entry-attempts")
def entry_attempts(
    config: ConfigOpt = None,
    session: Annotated[
        str | None, typer.Option("--session", help="Restrict to one recorded session id")
    ] = None,
    mint: Annotated[str | None, typer.Option("--mint", help="Restrict to one mint")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Most recent N attempts")] = 100,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Full per-attempt detail instead of a table")
    ] = False,
) -> None:
    """Why each qualified token did or did not become a BUY signal (one row per latch window)."""

    async def go() -> None:
        repo, settings = _open_repo(config)
        await repo.init()
        rows = await repo.entry_attempts(session_id=session, mint=mint, limit=100_000)
        await repo.close()
        console.print(_context_line(settings))
        rows = rows[-limit:]
        if not rows:
            console.print("no entry attempts recorded: no candidate reached QUALIFIED yet")
            return
        if verbose:
            for a in rows:
                for line in _attempt_lines(a):
                    console.print(line, highlight=False)
                console.print()
        else:
            table = Table(title=f"entry attempts ({len(rows)})")
            for col in (
                "qualified",
                "symbol",
                "mint",
                "score",
                "decimals",
                "sizing",
                "quote",
                "rt loss",
                "post",
                "decision",
                "reason",
            ):
                table.add_column(col, overflow="fold")
            for a in rows:
                table.add_row(
                    f"{a.qualified_at:%m-%d %H:%M:%S}",
                    a.symbol or "?",
                    f"{a.mint[:6]}…{a.mint[-4:]}",
                    f"{a.qualified_score:.1f}",
                    a.decimals_status,
                    f"€{a.recommended_eur:.2f}" if a.recommended_eur is not None else "-",
                    f"{a.buy_quote_status}/{a.sell_quote_status}"
                    if a.quote_attempts
                    else "not attempted",
                    f"{a.round_trip_loss_pct:.1%}" if a.round_trip_loss_pct is not None else "-",
                    f"{a.post_quote_score:.1f}" if a.post_quote_score is not None else "-",
                    str(a.final_decision),
                    (a.block_reason or "-")[:90],
                )
            console.print(table)
        by_decision: dict[str, int] = {}
        for a in rows:
            by_decision[str(a.final_decision)] = by_decision.get(str(a.final_decision), 0) + 1
        console.print("decisions: " + ", ".join(f"{k}={v}" for k, v in sorted(by_decision.items())))
        console.print(
            "[dim]-v shows the full trail; `inspect <MINT>` shows one token's attempts and history[/]"
        )

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
        incomplete: list[dict[str, object]] = []
        for sid in await repo.outcome_sessions(session_id=session):
            integrity = await repo.session_integrity(sid)
            if integrity is not None and not integrity["complete"]:
                incomplete.append(integrity)
        await repo.close()
        for integrity in incomplete:
            console.print(
                f"[bold red]INCOMPLETE DATA[/] session {integrity['session_id']}: the storage "
                f"writer dropped {integrity['dropped_total']} rows "
                f"({integrity['dropped_by_kind']}) and failed {integrity['failed_total']}. "
                "Outcome rows themselves are never dropped, but the observations behind them "
                "have holes; treat every rate below as a lower-quality estimate."
            )
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
            f"Market data: {report.live_market_rows} live Solana, {report.synthetic_rows} "
            f"synthetic, {report.legacy_rows} legacy (unknown). "
            f"Execution: {report.simulated_rows} simulated (paper/dry-run), "
            f"{report.live_rows} manual-signal."
        )
        console.print(
            f"[bold]Read this carefully:[/bold] groups with n < {MIN_MEANINGFUL_N} are dimmed "
            "because their rates are noise; brackets are 95% Wilson intervals. "
            "Multiples are what a passive observer saw from the first snapshot, not what a "
            "buyer would have realised. Past outcomes do not predict future ones."
        )
        if report.all_synthetic:
            console.print(
                "[yellow]Every row here is from the synthetic world. It says nothing about real "
                "Solana tokens.[/yellow]"
            )
        elif report.mixed_provenance:
            console.print(
                "[yellow]Live, synthetic and/or legacy rows are mixed; use --session to "
                "separate them.[/yellow]"
            )
        if report.live_market_rows and report.simulated_rows:
            console.print(
                "[bold]These are live Solana market observations with simulated execution "
                "(paper). The multiples are passive price paths, not executable returns: "
                "slippage, fill risk and liquidity pulls are not in them.[/bold]"
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
    if status.get("stopped"):
        state, color = "STOPPED", "yellow"
    elif not fresh:
        state, color = "STALE", "red"
    elif healthy:
        state, color = "HEALTHY", "green"
    else:
        state = str(status.get("state") or "DEGRADED")
        color = "red" if state == "UNHEALTHY" else "yellow"
    table = Table(title=f"heartbeat [{color}]{state}[/] @ {written}")
    table.add_column("item")
    table.add_column("value")
    uptime = status.get("uptime_s")
    table.add_row(
        "process", f"pid {status.get('pid')} on {status.get('hostname')} mode {status.get('mode')}"
    )
    table.add_row("session", str(status.get("session_id")))
    table.add_row("uptime", f"{uptime / 60:.1f} min" if isinstance(uptime, int | float) else "?")
    for label, key in (("problems", "problems"), ("degraded", "degraded")):
        items = status.get(key) or []
        if isinstance(items, list) and items:
            table.add_row(label, "[red]" + "; ".join(str(i) for i in items) + "[/]")
    providers = status.get("providers") or {}
    if isinstance(providers, dict) and providers:
        table.add_row(
            "providers",
            "  ".join(
                f"{name}={info.get('state', '?')}"
                + (f"(cooldown {info['cooldown_s']:.0f}s)" if info.get("cooldown_s") else "")
                for name, info in providers.items()
                if isinstance(info, dict)
            ),
        )
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
    integrity = db.get("integrity") or {}
    if isinstance(integrity, dict) and not integrity.get("complete", True):
        by_kind = integrity.get("dropped_by_kind") or {}
        table.add_row(
            "data integrity",
            "[red]INCOMPLETE[/] dropped "
            + ", ".join(f"{k}={v}" for k, v in dict(by_kind).items())
            + " (research data has holes; trades/positions/ledger are never dropped)",
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
def config_check(
    config: ConfigOpt = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Machine-readable output (no wrapping)")
    ] = False,
) -> None:
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
    if json_output:
        print(
            json.dumps(
                {
                    "ok": not problems,
                    "problems": problems,
                    "config": str(settings.config_path) if settings.config_path else None,
                    "home": str(settings.home),
                    "profile": str(settings.risk.profile),
                    "bankroll_eur": str(settings.risk.starting_bankroll_eur),
                    "sources": list(settings.discovery.sources),
                    "quotes": settings.quotes.source,
                    "database_url": settings.storage.database_url,
                    "log_file": settings.telemetry.log_file,
                }
            )
        )
        raise typer.Exit(code=0 if not problems else 1)
    console.print(
        f"config {settings.config_path or 'defaults'}  home {settings.home}  "
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
    console.print(_context_line(settings))
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
