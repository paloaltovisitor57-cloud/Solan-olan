"""`solana-sniper doctor`: verifies config, database, network, providers, credentials, quotes."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import websockets

from solana_sniper.app import arming
from solana_sniper.app.repo_safety import check_repo_safety
from solana_sniper.config.paths import WALLET_DIR
from solana_sniper.config.settings import Settings
from solana_sniper.domain.clock import SystemClock
from solana_sniper.infra.http import HttpClient, HttpError
from solana_sniper.portfolio.fx import CoinGeckoFx
from solana_sniper.quotes.base import QuoteError
from solana_sniper.quotes.jupiter import JupiterQuoteProvider
from solana_sniper.storage.repository import Repository
from solana_sniper.telemetry.redaction import safe_exception, safe_url
from solana_sniper.token_analysis.solana_rpc import SolanaRpcTokenProvider
from solana_sniper.wallet.keys import WalletError, public_key_of
from solana_sniper.wallet.rpc import SolanaSendClient

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    name: str
    status: str  # PASS | FAIL | SKIP | WARN
    detail: str


def wallet_checks(settings: Settings) -> tuple[list[CheckOutcome], str | None]:
    """`wallet` and `autonomy` checks; also returns the address for the balance check."""
    out: list[CheckOutcome] = []
    home = settings.home or Path("data")
    path = arming.key_file_path(home, settings.wallet.key_file)
    state = arming.read(home)
    a = settings.autonomy
    pubkey: str | None = None
    if path is None:
        out.append(
            CheckOutcome(
                "wallet",
                "SKIP",
                "no hot wallet (autonomous mode off); `solana-sniper wallet create` makes one",
            )
        )
    else:
        try:
            pubkey = public_key_of(path)
            out.append(CheckOutcome("wallet", "PASS", f"{path} -> {pubkey} (private, mode 600)"))
        except WalletError as exc:
            out.append(CheckOutcome("wallet", "FAIL", str(exc)))
        wallet_dir = home / WALLET_DIR
        if os.name == "posix" and wallet_dir.is_dir() and wallet_dir.stat().st_mode & 0o077:
            out.append(
                CheckOutcome(
                    "wallet_dir",
                    "WARN",
                    f"{wallet_dir} is not private; fix: chmod 700 '{wallet_dir}'",
                )
            )
    key_file = str(path) if path is not None else None
    readiness = arming.readiness(
        enabled=a.enabled, acknowledged=a.acknowledge_real_money, key_file=key_file, state=state
    )
    untouched = not a.enabled and not state.armed_marker and state.disarmed_reason is None
    if path is None and untouched:
        out.append(
            CheckOutcome(
                "autonomy",
                "SKIP",
                "disabled: signal, dry-run and paper modes never sign (wallet create + arm enable it)",
            )
        )
    elif readiness:
        out.append(CheckOutcome("autonomy", "WARN", "; ".join(readiness)))
    else:
        out.append(
            CheckOutcome(
                "autonomy",
                "PASS",
                f"{state.describe()}; per trade <= {a.max_trade_sol} SOL, per day <= "
                f"{a.max_daily_spend_sol} SOL, open positions <= {a.max_open_positions}, "
                f"reserve {a.reserve_sol} SOL, sends via {safe_url(settings.send_rpc_url())}",
            )
        )
    return out, pubkey


async def run_doctor(settings: Settings, *, timeout_s: float = 8.0) -> list[CheckOutcome]:
    out: list[CheckOutcome] = []
    out.append(
        CheckOutcome(
            "config",
            "PASS",
            f"{settings.config_path or 'defaults'} profile={settings.risk.profile} "
            f"bankroll=€{settings.risk.starting_bankroll_eur} sources={settings.discovery.sources}",
        )
    )
    safety = check_repo_safety()
    out.append(CheckOutcome("repo_safety", safety.status, safety.detail))
    out.append(
        CheckOutcome(
            "runtime_home",
            "PASS",
            f"{settings.home}  db={safe_url(settings.storage.database_url)}  "
            f"log={settings.telemetry.log_file}",
        )
    )
    # database
    try:
        repo = Repository(settings.storage.database_url, session_id="doctor")
        await repo.init()
        counts = await repo.counts()
        state = await repo.load_state()
        await repo.close()
        out.append(
            CheckOutcome(
                "database",
                "PASS",
                f"{safe_url(settings.storage.database_url)} tokens={counts['tokens']} positions={counts['positions']} "
                f"cash={'€' + str(state.cash) if state.has_account else 'no account yet'}",
            )
        )
    except Exception as exc:
        out.append(CheckOutcome("database", "FAIL", safe_exception(exc)))
    # credentials
    p = settings.providers
    out.append(
        CheckOutcome(
            "credentials",
            "PASS" if (p.helius_api_key or p.jupiter_api_key) else "WARN",
            f"helius={'set' if p.helius_api_key else 'missing (optional)'} "
            f"jupiter={'set' if p.jupiter_api_key else 'missing (lite-api used)'} "
            f"wallet_public_key={'set' if p.wallet_public_key else 'not set (unsigned tx prep disabled)'}",
        )
    )
    checks, hot_wallet = wallet_checks(settings)
    out.extend(checks)
    if settings.is_synthetic:
        out.append(
            CheckOutcome("network", "SKIP", "synthetic mode: no network providers configured")
        )
        return out
    http = HttpClient(timeout_s=timeout_s)
    clock = SystemClock()
    try:
        # RPC
        try:
            rpc = SolanaRpcTokenProvider(http, p.solana_rpc_url, clock)
            auth = await asyncio.wait_for(rpc.get_authorities(USDC), timeout_s)
            ok = auth is not None and auth.decimals == 6
            out.append(
                CheckOutcome(
                    "solana_rpc",
                    "PASS" if ok else "FAIL",
                    f"{safe_url(p.solana_rpc_url)} getAccountInfo(USDC) -> {'ok' if ok else 'unreachable or unexpected response'}",
                )
            )
        except Exception as exc:
            out.append(
                CheckOutcome(
                    "solana_rpc", "FAIL", f"{safe_url(p.solana_rpc_url)}: {safe_exception(exc)}"
                )
            )
        # hot wallet balance over the send RPC (autonomous mode only)
        if hot_wallet is not None:
            send_url = settings.send_rpc_url()
            try:
                sender = SolanaSendClient(http, send_url, clock, timeout_s=timeout_s)
                lamports = await asyncio.wait_for(sender.get_balance(hot_wallet), timeout_s)
                sol = Decimal(lamports) / Decimal(10**9)
                low = sol <= settings.autonomy.reserve_sol
                out.append(
                    CheckOutcome(
                        "wallet_balance",
                        "WARN" if low else "PASS",
                        f"{sol:.4f} SOL at {safe_url(send_url)}"
                        + (
                            f" (not above the {settings.autonomy.reserve_sol} SOL fee reserve: "
                            "nothing can be bought)"
                            if low
                            else ""
                        ),
                    )
                )
            except Exception as exc:
                out.append(
                    CheckOutcome(
                        "wallet_balance", "FAIL", f"{safe_url(send_url)}: {safe_exception(exc)}"
                    )
                )
        # DexScreener
        try:
            res = await http.get_json(
                f"{p.dexscreener_base_url}/tokens/v1/solana/{WSOL}", retries=0, timeout_s=timeout_s
            )
            out.append(
                CheckOutcome(
                    "dexscreener",
                    "PASS",
                    f"{res.latency_ms:.0f}ms, {len(res.json) if isinstance(res.json, list) else '?'} pairs",
                )
            )
        except HttpError as exc:
            out.append(CheckOutcome("dexscreener", "FAIL", safe_exception(exc)))
        # GeckoTerminal
        if "geckoterminal" in settings.discovery.sources:
            try:
                res = await http.get_json(
                    f"{p.geckoterminal_base_url}/networks/solana/new_pools",
                    params={"page": 1},
                    retries=0,
                    timeout_s=timeout_s,
                )
                n = len(res.json.get("data", [])) if isinstance(res.json, dict) else 0
                out.append(
                    CheckOutcome("geckoterminal", "PASS", f"{res.latency_ms:.0f}ms, {n} new pools")
                )
            except HttpError as exc:
                out.append(CheckOutcome("geckoterminal", "FAIL", safe_exception(exc)))
        # Jupiter quote
        try:
            jup = JupiterQuoteProvider(
                http,
                clock,
                base_url=p.jupiter_base_url,
                pro_base_url=p.jupiter_pro_base_url,
                api_key=p.jupiter_api_key.get_secret_value() if p.jupiter_api_key else None,
                timeout_s=timeout_s,
            )
            q = await jup.quote(WSOL, USDC, 10_000_000, 50)
            out.append(
                CheckOutcome(
                    "jupiter_quote",
                    "PASS",
                    f"0.01 SOL -> {Decimal(q.out_amount_raw) / Decimal(10**6):.4f} USDC via {'/'.join(q.route_labels)} ({q.latency_ms:.0f}ms)",
                )
            )
        except QuoteError as exc:
            out.append(CheckOutcome("jupiter_quote", "FAIL", safe_exception(exc)))
        # FX
        fx = CoinGeckoFx(
            http,
            p.coingecko_base_url,
            fallback_sol_eur=settings.portfolio.sol_eur_fallback,
            refresh_s=0,
        )
        try:
            await fx.refresh()
        except Exception as exc:
            out.append(CheckOutcome("fx_coingecko", "FAIL", safe_exception(exc)))
        out.append(
            CheckOutcome(
                "fx_coingecko",
                "PASS" if fx.is_live else "WARN",
                f"SOL/EUR {fx.sol_eur()} {'live' if fx.is_live else '(fallback; live fetch failed)'}",
            )
        )
        # PumpPortal WS
        if (
            "pumpportal" in settings.discovery.sources
            or "pumpportal" in settings.market_data.sources
        ):
            try:
                async with asyncio.timeout(timeout_s):
                    conn = await websockets.connect(p.pumpportal_ws_url, open_timeout=timeout_s)
                    await conn.close()
                out.append(
                    CheckOutcome(
                        "pumpportal_ws", "PASS", f"connected to {safe_url(p.pumpportal_ws_url)}"
                    )
                )
            except Exception as exc:
                out.append(
                    CheckOutcome(
                        "pumpportal_ws",
                        "FAIL",
                        f"{safe_url(p.pumpportal_ws_url)}: {safe_exception(exc)}",
                    )
                )
    finally:
        await http.aclose()
    return out
