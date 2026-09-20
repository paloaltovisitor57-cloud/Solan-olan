"""`wallet`, `arm`, `disarm`, `kill`, `resume`: the three-step setup of autonomous mode.

    solana-sniper wallet create          prints a fresh address; send SOL to it from any wallet
    solana-sniper arm --max-loss-sol X   records the loss limit and enables autonomy in sniper.env
    solana-sniper run --autonomous       trades from that wallet until kill / disarm / Ctrl+C

The key file is written once, mode 0600, under `<home>/wallet/`; only its *path* goes into
`sniper.env`. Nothing in this module prints, logs or copies the secret, and the user's own
wallet is never involved: funds are sent *to* the generated address like to any other wallet.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from solana_sniper.app import arming
from solana_sniper.config import load_settings
from solana_sniper.config.envfile import set_env_values
from solana_sniper.config.paths import ENV_FILE, wallet_key_path
from solana_sniper.config.settings import Settings
from solana_sniper.telemetry.redaction import safe_exception, safe_url

console = Console()
LAMPORTS = Decimal(10**9)

ConfigOpt = Annotated[
    Path | None,
    typer.Option("--config", "-c", help="YAML config path (default configs/default.yaml)"),
]

wallet_app = typer.Typer(
    help="Dedicated hot wallet for autonomous mode: create, import, show (never prints the key).",
    no_args_is_help=True,
)


# ------------------------------------------------------------------ helpers
def _home(settings: Settings) -> Path:
    return settings.home or Path("data")


def _env_path(settings: Settings) -> Path:
    return _home(settings) / ENV_FILE


def _key_path(settings: Settings) -> Path | None:
    return arming.key_file_path(_home(settings), settings.wallet.key_file)


def _synthetic(settings: Settings) -> bool:
    return settings.is_synthetic or settings.quotes.source == "synthetic"


def _fail(message: str, code: int = 1) -> typer.Exit:
    console.print(f"[red]{message}[/]", highlight=False)
    return typer.Exit(code=code)


def _positive_decimal(raw: str, flag: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise _fail(f"{flag} must be a number, got {raw!r}") from None
    if not value.is_finite() or value <= 0:
        raise _fail(f"{flag} must be positive, got {raw!r}")
    return value


def _sol(lamports: int) -> Decimal:
    return Decimal(lamports) / LAMPORTS


async def fetch_balance_lamports(
    settings: Settings, pubkey: str, *, timeout_s: float = 10.0
) -> int:
    """The wallet's balance from the send RPC. Module level so tests can replace it."""
    from solana_sniper.domain.clock import SystemClock
    from solana_sniper.infra.http import HttpClient
    from solana_sniper.wallet.rpc import SolanaSendClient

    http = HttpClient(timeout_s=timeout_s)
    try:
        url = settings.send_rpc_url()
        client = SolanaSendClient(http, url, SystemClock(), timeout_s=timeout_s)
        return await asyncio.wait_for(client.get_balance(pubkey), timeout_s)
    finally:
        await http.aclose()


def _balance(settings: Settings, pubkey: str) -> tuple[int | None, str]:
    """(lamports, note): the note explains a None balance without leaking credentials."""
    try:
        lamports = asyncio.run(fetch_balance_lamports(settings, pubkey))
    except Exception as exc:
        where = safe_url(settings.send_rpc_url())
        return None, f"unavailable from {where}: {safe_exception(exc)}"
    return lamports, f"{_sol(lamports):.4f} SOL"


def _record_key_file(settings: Settings, path: Path) -> None:
    set_env_values(_env_path(settings), {"SNIPER_WALLET__KEY_FILE": str(path)})


def _print_created(settings: Settings, pubkey: str, path: Path, verb: str) -> None:
    console.print(f"[bold green]hot wallet {verb}[/]")
    console.print(f"address:   [bold]{pubkey}[/]", highlight=False)
    console.print(f"key file:  {path}  (mode 600: only your user can read it)", highlight=False)
    console.print(f"recorded:  SNIPER_WALLET__KEY_FILE in {_env_path(settings)}", highlight=False)
    console.print()
    console.print(
        "next:  send SOL to that address from any wallet (only what you can afford to lose),"
    )
    console.print("       then run   solana-sniper arm --max-loss-sol <amount>")
    console.print(
        "[dim]Back up the key file somewhere safe: whoever holds it holds the funds, and the key "
        "is never displayed.[/]"
    )


# ------------------------------------------------------------------- wallet
@wallet_app.command("create")
def wallet_create(config: ConfigOpt = None) -> None:
    """Generate the hot wallet key file (once) and record its path in sniper.env."""
    from solana_sniper.wallet.keys import WalletError, generate

    settings = load_settings(config)
    existing = _key_path(settings)
    if existing is not None and existing.exists():
        raise _fail(
            f"a hot wallet already exists at {existing}; `solana-sniper wallet show` prints its "
            "address. To start over, move that file away yourself first (it may hold funds)."
        )
    path = existing or wallet_key_path(_home(settings))
    try:
        wallet = generate(path)
    except WalletError as exc:
        raise _fail(str(exc)) from None
    _record_key_file(settings, path)
    _print_created(settings, wallet.pubkey, path, "created")


@wallet_app.command("import")
def wallet_import(
    source: Annotated[
        Path, typer.Argument(help="Exported key file: Solana CLI JSON array or base58 secret")
    ],
    config: ConfigOpt = None,
) -> None:
    """Copy an exported key file into the protected location (seed phrases are refused)."""
    from solana_sniper.wallet.keys import WalletError, import_file

    settings = load_settings(config)
    existing = _key_path(settings)
    if existing is not None and existing.exists():
        raise _fail(
            f"a hot wallet already exists at {existing}; move it away yourself before importing"
        )
    path = existing or wallet_key_path(_home(settings))
    try:
        wallet = import_file(source, path)
    except WalletError as exc:
        raise _fail(str(exc)) from None
    _record_key_file(settings, path)
    _print_created(settings, wallet.pubkey, path, f"imported from {source}")
    console.print(
        "[yellow]Treat the original export as compromised once it has been on this machine; "
        "delete it or keep it offline.[/]"
    )


def wallet_report(settings: Settings, *, with_balance: bool) -> dict[str, Any]:
    """Everything `wallet show`, `status --json` and the doctor say about the wallet."""
    from solana_sniper.wallet.keys import WalletError, public_key_of

    home = _home(settings)
    path = _key_path(settings)
    state = arming.read(home)
    pubkey: str | None = None
    problem: str | None = None
    if path is None:
        problem = "no hot wallet: run `solana-sniper wallet create`"
    else:
        try:
            pubkey = public_key_of(path)
        except WalletError as exc:
            problem = str(exc)
    balance: int | None = None
    balance_note: str | None = None
    if pubkey and with_balance and not _synthetic(settings):
        balance, balance_note = _balance(settings, pubkey)
    key_file = str(path) if path is not None else None
    enabled, acknowledged = settings.autonomy.enabled, settings.autonomy.acknowledge_real_money
    return {
        "key_file": key_file,
        "public_key": pubkey,
        "problem": problem,
        "balance_sol": str(_sol(balance)) if balance is not None else None,
        "balance_note": balance_note,
        "arming": state.describe(),
        "armed": state.armed and enabled and acknowledged,
        "kill_switch": state.kill,
        "disarmed_reason": state.disarmed_reason,
        "max_total_loss_sol": str(state.max_total_loss_sol) if state.max_total_loss_sol else None,
        "start_balance_sol": str(_sol(state.start_balance_lamports))
        if state.start_balance_lamports is not None
        else None,
        "start_blockers": arming.start_blockers(
            enabled=enabled, acknowledged=acknowledged, key_file=key_file, state=state
        ),
        "readiness": arming.readiness(
            enabled=enabled, acknowledged=acknowledged, key_file=key_file, state=state
        ),
        "send_rpc": safe_url(settings.send_rpc_url()),
    }


@wallet_app.command("show")
def wallet_show(
    config: ConfigOpt = None,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
    no_balance: Annotated[
        bool, typer.Option("--no-balance", help="Skip the RPC balance lookup")
    ] = False,
) -> None:
    """Address, key-file status, balance and arming state. The key itself is never shown."""
    settings = load_settings(config)
    report = wallet_report(settings, with_balance=not no_balance)
    if json_output:
        print(json.dumps(report))
        raise typer.Exit(code=0 if report["problem"] is None else 1)
    if report["problem"]:
        console.print(f"[red]{report['problem']}[/]", highlight=False)
    else:
        console.print(f"address:     [bold]{report['public_key']}[/]", highlight=False)
    console.print(f"key file:    {report['key_file'] or 'not configured'}", highlight=False)
    if report["balance_note"]:
        console.print(f"balance:     {report['balance_note']}", highlight=False)
    console.print(f"autonomy:    {report['arming']}", highlight=False)
    if report["start_balance_sol"]:
        console.print(f"at arming:   {report['start_balance_sol']} SOL in the wallet")
    for line in report["readiness"]:
        console.print(f"[yellow]- {line}[/]", highlight=False)
    if not report["readiness"] and not report["problem"]:
        console.print("[green]ready:[/] solana-sniper run --autonomous")
    raise typer.Exit(code=0 if report["problem"] is None else 1)


# -------------------------------------------------------------- arm / disarm
def arm(
    max_loss_sol: Annotated[
        str,
        typer.Option(
            "--max-loss-sol",
            help="Stop buying once this much SOL has been lost since arming (required)",
        ),
    ],
    config: ConfigOpt = None,
    max_trade_sol: Annotated[
        str | None, typer.Option("--max-trade-sol", help="Largest single buy in SOL")
    ] = None,
    max_daily_sol: Annotated[
        str | None, typer.Option("--max-daily-sol", help="Most SOL spent on buys per UTC day")
    ] = None,
    max_open_positions: Annotated[
        int | None, typer.Option("--max-open-positions", help="Most positions held at once")
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt")] = False,
) -> None:
    """Enable autonomous trading from the hot wallet with hard caps (writes sniper.env)."""
    from solana_sniper.wallet.keys import WalletError, load

    settings = load_settings(config)
    if _synthetic(settings):
        raise _fail(
            "autonomous mode needs live providers; this configuration is synthetic "
            "(use configs/default.yaml)"
        )
    home = _home(settings)
    a = settings.autonomy
    loss = _positive_decimal(max_loss_sol, "--max-loss-sol")
    trade = (
        _positive_decimal(max_trade_sol, "--max-trade-sol") if max_trade_sol else a.max_trade_sol
    )
    daily = (
        _positive_decimal(max_daily_sol, "--max-daily-sol")
        if max_daily_sol
        else a.max_daily_spend_sol
    )
    open_max = max_open_positions if max_open_positions is not None else a.max_open_positions
    if open_max < 1:
        raise _fail("--max-open-positions must be at least 1")
    path = _key_path(settings)
    if path is None:
        raise _fail("no hot wallet: run `solana-sniper wallet create` first")
    try:
        wallet = load(path)
    except WalletError as exc:
        raise _fail(str(exc)) from None
    balance, note = _balance(settings, wallet.pubkey)
    if balance is None:
        raise _fail(
            f"cannot read the wallet balance ({note}); check SNIPER_PROVIDERS__SOLANA_RPC_URL "
            "or SNIPER_AUTONOMY__SEND_RPC_URL and try again"
        )
    balance_sol = _sol(balance)
    reserve = a.reserve_sol
    if balance_sol <= reserve:
        raise _fail(
            f"wallet {wallet.pubkey} holds {balance_sol:.4f} SOL, which is not more than the "
            f"fee reserve ({reserve} SOL). Send SOL to it first, then arm."
        )
    spendable = balance_sol - reserve
    warnings: list[str] = []
    if loss > balance_sol:
        warnings.append(
            f"the loss limit ({loss} SOL) is more than the wallet holds; the wallet itself is "
            "the effective limit"
        )
    if trade > spendable:
        warnings.append(
            f"max trade {trade} SOL is more than what is spendable ({spendable:.4f} SOL after "
            "the reserve); buys are clamped to what is there"
        )
    if trade > loss:
        warnings.append("a single trade could exceed the loss limit")
    if "mainnet-beta.solana.com" in settings.send_rpc_url():
        warnings.append(
            "sends go to the public RPC endpoint, which rate-limits and drops transactions; "
            "set SNIPER_AUTONOMY__SEND_RPC_URL (or SNIPER_PROVIDERS__SOLANA_RPC_URL) to a "
            "Helius endpoint"
        )
    console.print("[bold red]ARM AUTONOMOUS TRADING[/]")
    console.print(
        f"wallet:        {wallet.pubkey}   balance {balance_sol:.4f} SOL", highlight=False
    )
    console.print(
        f"loss limit:    {loss} SOL   (buys stop once that much is lost; exits "
        f"{'continue' if a.exits_continue_when_disarmed else 'stop too'})",
        highlight=False,
    )
    console.print(
        f"caps:          per trade <= {trade} SOL   per day <= {daily} SOL   "
        f"open positions <= {open_max}   fee reserve {reserve} SOL",
        highlight=False,
    )
    console.print(
        f"quality:       slippage <= {a.max_slippage_bps} bps   price impact <= "
        f"{a.max_price_impact_pct}%   priority fee <= {a.max_priority_fee_lamports} lamports",
        highlight=False,
    )
    for w in warnings:
        console.print(f"[yellow]warning:[/] {w}", highlight=False)
    console.print(
        "[bold]This process will sign and broadcast real transactions from that wallet. "
        "It can lose everything in it. No result is guaranteed.[/]"
    )
    if not yes and not typer.confirm("Arm with these limits?", default=False):
        console.print("not armed")
        raise typer.Exit(code=1)
    set_env_values(
        _env_path(settings),
        {
            "SNIPER_WALLET__KEY_FILE": str(path),
            "SNIPER_AUTONOMY__ENABLED": "true",
            "SNIPER_AUTONOMY__ACKNOWLEDGE_REAL_MONEY": "true",
            "SNIPER_AUTONOMY__MAX_TOTAL_LOSS_SOL": str(loss),
            "SNIPER_AUTONOMY__MAX_TRADE_SOL": str(trade),
            "SNIPER_AUTONOMY__MAX_DAILY_SPEND_SOL": str(daily),
            "SNIPER_AUTONOMY__MAX_OPEN_POSITIONS": str(open_max),
        },
    )
    state = arming.arm(
        home,
        wallet_public_key=wallet.pubkey,
        start_balance_lamports=balance,
        max_total_loss_sol=loss,
        caps={
            "max_trade_sol": str(trade),
            "max_daily_spend_sol": str(daily),
            "max_open_positions": open_max,
            "reserve_sol": str(reserve),
        },
    )
    console.print(f"[bold green]armed[/]: {state.describe()}", highlight=False)
    console.print(f"recorded in {_env_path(settings)} and {home / 'state' / 'armed.json'}")
    if state.kill:
        console.print("[yellow]the KILL switch is still set; run `solana-sniper resume`[/]")
    console.print(
        "start:  solana-sniper run --autonomous   (as a service: SNIPER_SERVICE_MODE=autonomous "
        "in sniper.env, then solana-sniper service restart)"
    )
    console.print(
        "stop:   solana-sniper kill  (buys and sells)   solana-sniper disarm  (buys only)"
    )


def disarm(
    config: ConfigOpt = None,
    reason: Annotated[str, typer.Option("--reason", help="Recorded in disarmed.json")] = (
        "disarmed by operator"
    ),
) -> None:
    """Stop new buys now; a running process keeps managing exits. `arm` re-arms."""
    settings = load_settings(config)
    state = arming.disarm(_home(settings), reason)
    console.print(f"[yellow]disarmed[/]: {state.describe()}", highlight=False)
    console.print("no new buys; exits continue. Re-arm with: solana-sniper arm --max-loss-sol <n>")


def kill(config: ConfigOpt = None) -> None:
    """Emergency stop: no buys and no sells until `solana-sniper resume`."""
    settings = load_settings(config)
    arming.kill(_home(settings))
    console.print(
        "[bold red]KILL switch set[/]: a running autonomous process stops buying and selling "
        "within one tick (open positions stay open, unmanaged). Lift it with: solana-sniper resume"
    )


def resume(config: ConfigOpt = None) -> None:
    """Remove the KILL switch (the arming state decides whether buys resume)."""
    settings = load_settings(config)
    state = arming.resume(_home(settings))
    console.print(f"KILL switch removed: {state.describe()}", highlight=False)
    if state.disarmed_reason is not None:
        console.print("still disarmed: re-arm with solana-sniper arm --max-loss-sol <n>")


def preflight(settings: Settings) -> tuple[str, arming.ArmingState]:
    """What `run --autonomous` checks before building anything: a loadable wallet, autonomy
    enabled and acknowledged in the configuration, and an arming decision on record. A disarmed
    or killed state still starts (exits continue, `resume` can lift KILL)."""
    from solana_sniper.wallet.keys import WalletError, public_key_of

    home = _home(settings)
    state = arming.read(home)
    path = _key_path(settings)
    blockers = arming.start_blockers(
        enabled=settings.autonomy.enabled,
        acknowledged=settings.autonomy.acknowledge_real_money,
        key_file=str(path) if path else None,
        state=state,
    )
    if _synthetic(settings):
        blockers.insert(0, "the configuration is synthetic; autonomous mode needs live providers")
    pubkey: str | None = None
    if path is not None:
        try:
            pubkey = public_key_of(path)
        except WalletError as exc:
            blockers.append(str(exc))
    if blockers or pubkey is None:
        console.print("[bold red]cannot start autonomous mode:[/]")
        for b in blockers:
            console.print(f"  - {b}", highlight=False)
        raise typer.Exit(code=2)
    return pubkey, state


def register(app: typer.Typer) -> None:
    app.add_typer(wallet_app, name="wallet")
    app.command("arm")(arm)
    app.command("disarm")(disarm)
    app.command("kill")(kill)
    app.command("resume")(resume)
