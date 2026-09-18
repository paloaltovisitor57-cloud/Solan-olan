"""The LIVE composition (PumpPortal WS + GeckoTerminal + DexScreener + Solana RPC + Jupiter +
CoinGecko) driven end to end with mocked transports, in dry-run mode on the real clock.

This is the closest offline proof that the live provider path produces a BUY signal, a simulated
fill, executable position monitoring and an exit, since external hosts are unreachable here.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from solana_sniper.app.bootstrap import build_runtime
from solana_sniper.config import load_settings
from solana_sniper.domain.enums import CandidateState as S
from solana_sniper.domain.enums import RunMode

MINT = "LivePipeMint111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"
SOL_USD = Decimal("160")


class FakePool:
    """Constant-product pump.fun-style curve driving prices, trades, DexScreener and Jupiter."""

    def __init__(self) -> None:
        self.sol = Decimal("34")  # 30 virtual + 4 real
        self.tokens = Decimal("1000000000")
        self.started = time.monotonic()
        self.buys = 0
        self.sells = 0
        self.volume_usd = Decimal(0)
        self.traders: set[str] = set()
        self.seq = 0

    @property
    def price(self) -> Decimal:
        return self.sol / self.tokens

    def trade(self) -> dict[str, Any]:
        elapsed = time.monotonic() - self.started
        # 45 s pump with ~80 % buys (enough history for momentum features), then a dump:
        # a trailing / momentum / liquidity exit must follow
        is_buy = (self.seq % 5 != 0) if elapsed < 45 else (self.seq % 4 == 0)
        sol_amt = Decimal("0.3") if is_buy else Decimal("0.25")
        k = self.sol * self.tokens
        self.seq += 1
        if is_buy:
            self.sol += sol_amt
            token_amt = self.tokens - k / self.sol
            self.tokens = k / self.sol
            self.buys += 1
        else:
            token_amt = self.tokens * (Decimal("0.02") if elapsed < 45 else Decimal("0.05"))
            self.tokens += token_amt
            sol_amt = self.sol - k / self.tokens
            self.sol = k / self.tokens
            self.sells += 1
        trader = f"trader{self.seq % 40:03d}"
        self.traders.add(trader)
        self.volume_usd += sol_amt * SOL_USD
        return {
            "signature": f"sig{self.seq}",
            "mint": MINT,
            "traderPublicKey": trader,
            "txType": "buy" if is_buy else "sell",
            "tokenAmount": float(token_amt),
            "solAmount": float(sol_amt),
            "newTokenBalance": 1.0,
            "bondingCurveKey": "Curve111",
            "vTokensInBondingCurve": float(self.tokens),
            "vSolInBondingCurve": float(self.sol),
            "marketCapSol": float(self.price * Decimal(1_000_000_000)),
            "pool": "pump",
        }

    def quote(self, input_mint: str, amount_raw: int) -> dict[str, Any]:
        k = self.sol * self.tokens
        if input_mint == WSOL:
            sol_in = Decimal(amount_raw) / Decimal(10**9)
            out = self.tokens - k / (self.sol + sol_in)
            out_raw = int(out * Decimal(10**6))
            impact = float(sol_in / (self.sol + sol_in))
            out_mint = MINT
        else:
            tok_in = Decimal(amount_raw) / Decimal(10**6)
            out = self.sol - k / (self.tokens + tok_in)
            out_raw = int(out * Decimal(10**9))
            impact = float(tok_in / (self.tokens + tok_in))
            out_mint = WSOL
        return {
            "inputMint": input_mint,
            "inAmount": str(amount_raw),
            "outputMint": out_mint,
            "outAmount": str(out_raw),
            "otherAmountThreshold": str(int(out_raw * 0.97)),
            "swapMode": "ExactIn",
            "slippageBps": 300,
            "priceImpactPct": str(impact),
            "routePlan": [
                {
                    "swapInfo": {"label": "Pump.fun", "feeAmount": "1000", "feeMint": WSOL},
                    "percent": 100,
                }
            ],
        }

    def dexscreener_pair(self, created_ms: int) -> dict[str, Any]:
        real_sol = max(Decimal(0), self.sol - 30)
        return {
            "chainId": "solana",
            "dexId": "pumpfun",
            "pairAddress": "Curve111",
            "baseToken": {"address": MINT, "name": "Live Pipe", "symbol": "LIVE"},
            "quoteToken": {"address": WSOL, "symbol": "SOL"},
            "priceNative": str(self.price),
            "priceUsd": str(self.price * SOL_USD),
            "txns": {
                "m5": {"buys": self.buys, "sells": self.sells},
                "h1": {"buys": self.buys, "sells": self.sells},
            },
            "volume": {"m5": float(self.volume_usd), "h1": float(self.volume_usd)},
            "liquidity": {"usd": float(real_sol * 2 * SOL_USD), "quote": float(real_sol)},
            "fdv": float(self.price * Decimal(1_000_000_000) * SOL_USD),
            "marketCap": float(self.price * Decimal(1_000_000_000) * SOL_USD),
            "pairCreatedAt": created_ms,
        }


class FakeWsConn:
    """PumpPortal stand-in: emits the create message, then a trade every 150 ms."""

    def __init__(self, pool: FakePool) -> None:
        self.pool = pool
        self.sent: list[dict[str, Any]] = []
        self.subscribed = False

    async def send(self, data: str) -> None:
        msg = json.loads(data)
        self.sent.append(msg)
        if msg.get("method") == "subscribeTokenTrade" and MINT in msg.get("keys", []):
            self.subscribed = True

    async def close(self) -> None:
        return None

    def __aiter__(self) -> AsyncIterator[str]:
        return self._gen()

    async def _gen(self) -> AsyncIterator[str]:
        yield json.dumps({"message": "Successfully subscribed to token creation events."})
        yield json.dumps(
            {
                "signature": "create1",
                "mint": MINT,
                "traderPublicKey": "dev",
                "txType": "create",
                "initialBuy": 40000000.0,
                "solAmount": 4.0,
                "bondingCurveKey": "Curve111",
                "vTokensInBondingCurve": float(self.pool.tokens),
                "vSolInBondingCurve": float(self.pool.sol),
                "marketCapSol": 34.0,
                "name": "Live Pipe",
                "symbol": "LIVE",
                "uri": "ipfs://x",
                "pool": "pump",
            }
        )
        while True:
            await asyncio.sleep(0.15)
            if self.subscribed:
                yield json.dumps(self.pool.trade())


def make_http_handler(pool: FakePool, created_ms: int, counters: dict[str, int]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        path = url.path
        if url.host == "api.dexscreener.com":
            counters["dexscreener"] += 1
            if MINT in path:
                return httpx.Response(200, json=[pool.dexscreener_pair(created_ms)])
            return httpx.Response(200, json=[])
        if url.host == "api.geckoterminal.com":
            counters["gecko"] += 1
            return httpx.Response(200, json={"data": []})
        if url.host == "api.mainnet-beta.solana.com":
            body = json.loads(request.read())
            counters["rpc"] += 1
            if body["method"] == "getAccountInfo":
                return httpx.Response(
                    200, json=json.loads((Path("tests/fixtures/rpc_mint_account.json")).read_text())
                )
            if body["method"] == "getTokenLargestAccounts":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {
                            "value": [
                                {"address": "Curve111", "amount": "800000000000000"},
                                {"address": "h1", "amount": "40000000000000"},
                                {"address": "h2", "amount": "20000000000000"},
                            ]
                        },
                    },
                )
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": None})
        if url.host == "lite-api.jup.ag":
            counters["jupiter"] += 1
            return httpx.Response(
                200, json=pool.quote(url.params["inputMint"], int(url.params["amount"]))
            )
        if url.host == "api.coingecko.com":
            counters["fx"] += 1
            return httpx.Response(200, json={"solana": {"eur": 150.0, "usd": 160.0}})
        return httpx.Response(404, text=f"unmocked {url}")

    return handler


@pytest.mark.timeout(240)
async def test_live_composition_with_mocked_providers(tmp_path: Path) -> None:
    settings = load_settings(Path("configs/default.yaml"))
    settings.storage.database_url = f"sqlite+aiosqlite:///{tmp_path}/live.db"
    settings.telemetry.log_file = None
    settings.dashboard.enabled = False
    settings.market_data.poll_interval_s = 0.3
    settings.market_data.stale_after_s = 5
    settings.discovery.poll_interval_s = 0.5
    settings.filters.min_token_age_s = 2
    settings.filters.min_liquidity_usd = Decimal("500")
    settings.filters.min_volume_5m_usd = Decimal("100")
    settings.filters.min_buys_5m = 3
    settings.filters.min_unique_traders = 3
    settings.entry.min_score = 55
    settings.entry.min_observations = 3
    settings.entry.min_trade_velocity_per_min = 3
    settings.quotes.refresh_interval_s = 0.5
    settings.dry_run.confirm_delay_s = 0.5
    settings.exit.min_holding_s_before_trailing = 1
    settings.exit.trailing.tiers[0].drawdown_pct = 0.15
    settings.exit.momentum_drop_threshold = -0.08
    settings.portfolio.fx_refresh_s = 1000
    settings.portfolio.snapshot_interval_s = 2
    pool = FakePool()
    counters = {"dexscreener": 0, "gecko": 0, "rpc": 0, "jupiter": 0, "fx": 0}
    conn = FakeWsConn(pool)

    async def ws_connect(url: str) -> Any:
        assert url.startswith("wss://pumpportal.fun")
        return conn

    runtime = build_runtime(
        settings,
        mode=RunMode.DRY_RUN,
        session_id="live-mocked",
        quiet_alerts=True,
        http_transport=httpx.MockTransport(
            make_http_handler(pool, int(time.time() * 1000), counters)
        ),
        ws_connect=ws_connect,
    )
    assert runtime.discovery.provider_names == ["pumpportal", "geckoterminal"]
    assert set(runtime.market.provider_names) == {"dexscreener", "pumpportal"}
    assert runtime.quote_provider is not None and runtime.quote_provider.name == "jupiter"
    await runtime.start()
    engine = runtime.engine
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and not runtime.account.open_positions:
            await asyncio.sleep(0.5)
        assert runtime.account.open_positions, (
            f"no position; stats={engine.stats} cands={[(c.symbol, c.state, c.gate_reasons) for c in engine.candidates.values()]} "
            f"counters={counters} recent={engine.stats.recent[-8:]}"
        )
        pos = runtime.account.open_positions[0]
        cand = engine.candidates[MINT]
        assert cand.state is S.OPEN and cand.signal is not None
        sig = cand.signal
        assert sig.quote.buy.provider == "jupiter" and sig.quote.sell is not None
        assert sig.quote.buy.route_labels == ("Pump.fun",)
        assert cand.track.authorities is not None and cand.track.authorities.mint_authority is None
        assert cand.track.holders is not None and cand.track.holders.largest_is_pool
        assert sig.sol_eur == Decimal("150.0")  # CoinGecko FX was used
        assert counters["rpc"] >= 2 and counters["jupiter"] >= 2 and counters["dexscreener"] >= 1
        assert conn.subscribed and any(m.get("method") == "subscribeNewToken" for m in conn.sent)
        # executable valuation from Jupiter exit quotes
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not pos.value_is_executable:
            await asyncio.sleep(0.3)
        assert pos.value_is_executable
        # the scripted dump produces an exit and the dry-run confirms it
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and pos.state is not S.CLOSED:
            await asyncio.sleep(0.5)
        assert pos.state is S.CLOSED, (
            f"state={cand.state} pos={pos} recent={engine.stats.recent[-6:]}"
        )
        assert pos.exit_reason is not None and engine.stats.exits >= 1
        assert runtime.account.cash > 0 and len(runtime.account.ledger) >= 3
        assert runtime.health is not None
        snap = runtime.health.snapshot()
        assert snap["connections"]["pumpportal"]["connected"]
        assert snap["connections"]["market-data"]["connected"]
        assert snap["counters"]["confirmed"] >= 1
    finally:
        await runtime.stop(reason="test")
