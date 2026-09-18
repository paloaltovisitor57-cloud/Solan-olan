"""`solana-sniper smoke-test`: an opt-in, real-network check of every provider this machine will
talk to, without trading. Reports latency, rate-limit/circuit state and what to do about
failures. Unit tests drive it with mocked transports; nothing here runs during normal tests.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import websockets

from solana_sniper.app.bootstrap import build_governor
from solana_sniper.config.settings import Settings
from solana_sniper.discovery.geckoterminal import GeckoTerminalDiscovery
from solana_sniper.domain.clock import SystemClock
from solana_sniper.infra.http import HttpClient, HttpError, ProviderUnavailableError
from solana_sniper.market_data.dexscreener import DexScreenerMarketData
from solana_sniper.portfolio.fx import CoinGeckoFx
from solana_sniper.quotes.jupiter import JupiterQuoteProvider
from solana_sniper.storage.repository import Repository
from solana_sniper.telemetry.metrics import Metrics
from solana_sniper.telemetry.redaction import safe_exception, safe_url
from solana_sniper.token_analysis.solana_rpc import SolanaRpcTokenProvider

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"

WsConnect = Callable[[str], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class SmokeResult:
    name: str
    status: str  # PASS | FAIL | WARN | SKIP
    latency_ms: float | None
    detail: str
    recommendation: str = ""
    provider_state: str = ""


def _remove_db_files(target: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = Path(str(target) + suffix)
        if path.exists():
            path.unlink()


async def _timed(coro: Awaitable[Any]) -> tuple[Any, float]:
    started = time.perf_counter()
    result = await coro
    return result, (time.perf_counter() - started) * 1000.0


def _rec_for(exc: Exception, provider: str) -> str:
    text = safe_exception(exc)
    if "rate limited" in text or isinstance(exc, ProviderUnavailableError) or "429" in text:
        if provider == "solana-rpc":
            return (
                "public RPC throttles per IP; a Helius/QuickNode/Triton RPC URL in "
                "SNIPER_PROVIDERS__SOLANA_RPC_URL removes most of the 429s"
            )
        if provider == "jupiter":
            return "set SNIPER_PROVIDERS__JUPITER_API_KEY for api.jup.ag's higher limits"
        return f"{provider} is rate limiting this IP; the engine backs off automatically"
    if "TransportError" in text or "Connect" in text or "timeout" in text.lower():
        return "check DNS/firewall/VPN; the engine keeps running without this provider"
    return ""


async def run_smoke(
    settings: Settings,
    *,
    timeout_s: float = 10.0,
    http_transport: httpx.AsyncBaseTransport | None = None,
    ws_connect: WsConnect | None = None,
    db_dir: Path | None = None,
) -> list[SmokeResult]:
    out: list[SmokeResult] = []
    p = settings.providers
    metrics = Metrics()
    gov = build_governor(settings, metrics)
    http = HttpClient(timeout_s=timeout_s, metrics=metrics, transport=http_transport, governor=gov)
    clock = SystemClock()

    def state(host_url: str) -> str:
        host = httpx.URL(host_url).host
        return gov.state(host)

    try:
        # --- Solana RPC: getVersion + getAccountInfo(USDC)
        rpc = SolanaRpcTokenProvider(http, p.solana_rpc_url, clock, metrics=metrics)
        try:
            res, ms = await _timed(
                http.post_json(
                    p.solana_rpc_url,
                    json={"jsonrpc": "2.0", "id": 1, "method": "getVersion", "params": []},
                    retries=0,
                    timeout_s=timeout_s,
                )
            )
            version = (
                (res.json.get("result") or {}).get("solana-core", "?")
                if isinstance(res.json, dict)
                else "?"
            )
            auth, ms2 = await _timed(rpc.get_authorities(USDC))
            ok = auth is not None and auth.decimals == 6
            out.append(
                SmokeResult(
                    "solana_rpc",
                    "PASS" if ok else "WARN",
                    ms + ms2,
                    f"{safe_url(p.solana_rpc_url)} solana-core {version}; getAccountInfo(USDC) "
                    f"{'ok' if ok else 'degraded (rate limited or unexpected response)'}",
                    ""
                    if ok
                    else _rec_for(
                        ProviderUnavailableError("solana-rpc", 0, "degraded"), "solana-rpc"
                    ),
                    state(p.solana_rpc_url),
                )
            )
        except (HttpError, ProviderUnavailableError) as exc:
            out.append(
                SmokeResult(
                    "solana_rpc",
                    "FAIL",
                    None,
                    safe_exception(exc),
                    _rec_for(exc, "solana-rpc"),
                    state(p.solana_rpc_url),
                )
            )
        # --- Helius DAS (only if configured)
        if p.helius_api_key:
            try:
                res, ms = await _timed(
                    http.post_json(
                        p.solana_rpc_url,
                        json={
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "getTokenAccounts",
                            "params": [{"mint": USDC, "limit": 1}],
                        },
                        retries=0,
                        timeout_s=timeout_s,
                    )
                )
                ok = isinstance(res.json, dict) and "result" in res.json
                out.append(
                    SmokeResult(
                        "helius_das",
                        "PASS" if ok else "WARN",
                        ms,
                        "getTokenAccounts answered"
                        if ok
                        else "no result (is the RPC URL a Helius endpoint?)",
                        "" if ok else "holder counts need a Helius RPC URL, not just the key",
                        state(p.solana_rpc_url),
                    )
                )
            except (HttpError, ProviderUnavailableError) as exc:
                out.append(
                    SmokeResult(
                        "helius_das",
                        "FAIL",
                        None,
                        safe_exception(exc),
                        _rec_for(exc, "helius"),
                        state(p.solana_rpc_url),
                    )
                )
        else:
            out.append(
                SmokeResult(
                    "helius_das",
                    "SKIP",
                    None,
                    "no Helius key configured (holder counts stay UNKNOWN)",
                    "optional: SNIPER_PROVIDERS__HELIUS_API_KEY",
                )
            )
        # --- GeckoTerminal discovery cycle
        gecko = GeckoTerminalDiscovery(
            http,
            p.geckoterminal_base_url,
            clock,
            max_token_age_s=settings.discovery.max_token_age_s,
        )
        try:
            tokens, ms = await _timed(gecko.poll())
            out.append(
                SmokeResult(
                    "geckoterminal_discovery",
                    "PASS",
                    ms,
                    f"one discovery cycle: {len(tokens)} new pools within max_token_age_s",
                    "",
                    state(p.geckoterminal_base_url),
                )
            )
        except (HttpError, ProviderUnavailableError) as exc:
            out.append(
                SmokeResult(
                    "geckoterminal_discovery",
                    "FAIL",
                    None,
                    safe_exception(exc),
                    _rec_for(exc, "geckoterminal"),
                    state(p.geckoterminal_base_url),
                )
            )
        # --- DexScreener market-data cycle
        dex = DexScreenerMarketData(
            http, p.dexscreener_base_url, clock, settings.market_data.batch_size
        )
        try:
            snaps, ms = await _timed(dex.fetch([WSOL]))
            ok = len(snaps) >= 1
            out.append(
                SmokeResult(
                    "dexscreener_market_data",
                    "PASS" if ok else "WARN",
                    ms,
                    f"one market-data cycle: {len(snaps)} snapshot(s) for WSOL",
                    "" if ok else "empty response; check the DexScreener API shape",
                    state(p.dexscreener_base_url),
                )
            )
        except (HttpError, ProviderUnavailableError) as exc:
            out.append(
                SmokeResult(
                    "dexscreener_market_data",
                    "FAIL",
                    None,
                    safe_exception(exc),
                    _rec_for(exc, "dexscreener"),
                    state(p.dexscreener_base_url),
                )
            )
        # --- Jupiter quote
        try:
            jup = JupiterQuoteProvider(
                http,
                clock,
                base_url=p.jupiter_base_url,
                pro_base_url=p.jupiter_pro_base_url,
                api_key=p.jupiter_api_key.get_secret_value() if p.jupiter_api_key else None,
                timeout_s=timeout_s,
                metrics=metrics,
            )
            q, ms = await _timed(jup.quote(WSOL, USDC, 10_000_000, 50))
            out.append(
                SmokeResult(
                    "jupiter_quote",
                    "PASS",
                    ms,
                    f"0.01 SOL -> {Decimal(q.out_amount_raw) / Decimal(10**6):.4f} USDC "
                    f"via {'/'.join(q.route_labels)}",
                    "",
                    state(p.jupiter_base_url),
                )
            )
        except Exception as exc:
            out.append(
                SmokeResult(
                    "jupiter_quote",
                    "FAIL",
                    None,
                    safe_exception(exc),
                    _rec_for(exc, "jupiter"),
                    state(p.jupiter_base_url),
                )
            )
        # --- CoinGecko FX
        fx = CoinGeckoFx(
            http,
            p.coingecko_base_url,
            fallback_sol_eur=settings.portfolio.sol_eur_fallback,
            refresh_s=0,
        )
        try:
            _, ms = await _timed(fx.refresh())
        except Exception as exc:
            out.append(
                SmokeResult(
                    "coingecko_fx",
                    "FAIL",
                    None,
                    safe_exception(exc),
                    _rec_for(exc, "coingecko"),
                    state(p.coingecko_base_url),
                )
            )
        else:
            out.append(
                SmokeResult(
                    "coingecko_fx",
                    "PASS" if fx.is_live else "WARN",
                    ms,
                    f"SOL/EUR {fx.sol_eur()} "
                    f"{'live' if fx.is_live else '(fallback; live fetch failed)'}",
                    ""
                    if fx.is_live
                    else "paper sessions refuse to start without a live rate "
                    "unless --allow-fallback-fx",
                    state(p.coingecko_base_url),
                )
            )
        # --- PumpPortal WebSocket: connect, subscribe, wait for one message
        connect: WsConnect = ws_connect or (
            lambda url: websockets.connect(url, open_timeout=timeout_s)
        )
        try:
            started = time.perf_counter()
            async with asyncio.timeout(timeout_s + 5):
                conn = await connect(p.pumpportal_ws_url)
                try:
                    await conn.send('{"method": "subscribeNewToken"}')
                    first = await asyncio.wait_for(conn.recv(), timeout_s)
                finally:
                    await conn.close()
            ms = (time.perf_counter() - started) * 1000.0
            out.append(
                SmokeResult(
                    "pumpportal_ws",
                    "PASS",
                    ms,
                    f"connected and received a message ({len(str(first))} bytes)",
                )
            )
        except Exception as exc:
            out.append(
                SmokeResult(
                    "pumpportal_ws",
                    "FAIL",
                    None,
                    f"{safe_url(p.pumpportal_ws_url)}: {safe_exception(exc)}",
                    "check outbound WebSocket access; discovery falls back to "
                    "GeckoTerminal polling",
                )
            )
        # --- Solana WebSocket endpoint (reserved for logsSubscribe; connectivity only)
        try:
            started = time.perf_counter()
            async with asyncio.timeout(timeout_s + 5):
                conn = await connect(p.solana_ws_url)
                await conn.close()
            out.append(
                SmokeResult(
                    "solana_ws",
                    "PASS",
                    (time.perf_counter() - started) * 1000.0,
                    f"connected to {safe_url(p.solana_ws_url)}",
                )
            )
        except Exception as exc:
            out.append(
                SmokeResult(
                    "solana_ws",
                    "WARN",
                    None,
                    f"{safe_url(p.solana_ws_url)}: {safe_exception(exc)}",
                    "not used by paper mode yet; informational",
                )
            )
    finally:
        await http.aclose()
    # --- database write/read in the runtime home
    target = (db_dir or (settings.home / "db" if settings.home else Path("data"))) / "smoke-test.db"
    try:
        repo = Repository(f"sqlite+aiosqlite:///{target}", session_id="smoke")
        started = time.perf_counter()
        await repo.init()
        repo.start()
        await repo.start_session("SMOKE", None)
        await repo.end_session()
        sessions = await repo.list_sessions()
        await repo.close()
        ok = any(s["session_id"] == "smoke" for s in sessions)
        out.append(
            SmokeResult(
                "database",
                "PASS" if ok else "FAIL",
                (time.perf_counter() - started) * 1000.0,
                f"write/read ok at {target}" if ok else "session row not read back",
            )
        )
    except Exception as exc:
        out.append(
            SmokeResult(
                "database",
                "FAIL",
                None,
                safe_exception(exc),
                "check that the runtime home is writable",
            )
        )
    finally:
        await asyncio.to_thread(_remove_db_files, target)
    # --- summary of provider states and rate limiting
    health = gov.health()
    limited = [
        f"{name}={info['state']}" for name, info in health.items() if info["state"] != "HEALTHY"
    ]
    out.append(
        SmokeResult(
            "rate_limiting",
            "WARN" if limited else "PASS",
            None,
            f"{metrics.counters['rate_limited']} rate-limit responses during the smoke test; "
            + (", ".join(limited) if limited else "all providers healthy"),
            "the engine paces requests and degrades checks to UNKNOWN when a provider throttles"
            if limited
            else "",
        )
    )
    return out
