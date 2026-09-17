"""`solana-sniper doctor`: verifies config, database, network, providers, credentials, quotes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal

import websockets

from solana_sniper.config.settings import Settings
from solana_sniper.domain.clock import SystemClock
from solana_sniper.infra.http import HttpClient, HttpError
from solana_sniper.portfolio.fx import CoinGeckoFx
from solana_sniper.quotes.base import QuoteError
from solana_sniper.quotes.jupiter import JupiterQuoteProvider
from solana_sniper.storage.repository import Repository
from solana_sniper.token_analysis.solana_rpc import SolanaRpcTokenProvider

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    name: str
    status: str  # PASS | FAIL | SKIP | WARN
    detail: str


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
                f"{settings.storage.database_url} tokens={counts['tokens']} positions={counts['positions']} "
                f"cash={'€' + str(state.cash) if state.has_account else 'no account yet'}",
            )
        )
    except Exception as exc:
        out.append(CheckOutcome("database", "FAIL", str(exc)))
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
                    f"{p.solana_rpc_url} getAccountInfo(USDC) -> {'ok' if ok else 'unreachable or unexpected response'}",
                )
            )
        except Exception as exc:
            out.append(CheckOutcome("solana_rpc", "FAIL", f"{p.solana_rpc_url}: {exc}"))
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
            out.append(CheckOutcome("dexscreener", "FAIL", str(exc)))
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
                out.append(CheckOutcome("geckoterminal", "FAIL", str(exc)))
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
            out.append(CheckOutcome("jupiter_quote", "FAIL", str(exc)))
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
            out.append(CheckOutcome("fx_coingecko", "FAIL", str(exc)))
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
                    CheckOutcome("pumpportal_ws", "PASS", f"connected to {p.pumpportal_ws_url}")
                )
            except Exception as exc:
                out.append(
                    CheckOutcome(
                        "pumpportal_ws",
                        "FAIL",
                        f"{p.pumpportal_ws_url}: {type(exc).__name__}: {exc}",
                    )
                )
    finally:
        await http.aclose()
    return out
