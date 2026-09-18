"""Smoke-test runner with mocked transports: structure, recommendations, no network."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from solana_sniper.app.smoke import run_smoke
from solana_sniper.config.settings import Settings

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class FakeWs:
    def __init__(self, url: str) -> None:
        self.url = url

    async def send(self, msg: str) -> None:
        return None

    async def recv(self) -> str:
        return json.dumps({"message": "Successfully subscribed"})

    async def close(self) -> None:
        return None


async def fake_connect(url: str) -> FakeWs:
    return FakeWs(url)


def _handler(rate_limit_rpc: bool) -> Any:
    mint = json.loads((FIXTURES / "rpc_mint_account.json").read_text())
    gecko = json.loads((FIXTURES / "geckoterminal_new_pools.json").read_text())
    dex = json.loads((FIXTURES / "dexscreener_tokens.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if "solana.com" in host:
            if rate_limit_rpc:
                return httpx.Response(429, headers={"retry-after": "5"})
            body = json.loads(request.content or b"{}")
            if body.get("method") == "getVersion":
                return httpx.Response(
                    200, json={"jsonrpc": "2.0", "result": {"solana-core": "2.1.0"}}
                )
            return httpx.Response(200, json=mint)
        if "geckoterminal" in host:
            return httpx.Response(200, json=gecko)
        if "dexscreener" in host:
            return httpx.Response(200, json=dex)
        if "jup.ag" in host:
            return httpx.Response(
                200,
                json={
                    "inAmount": "10000000",
                    "outAmount": "1500000",
                    "priceImpactPct": "0.01",
                    "routePlan": [{"swapInfo": {"label": "Orca"}}],
                    "slippageBps": 50,
                    "contextSlot": 1,
                    "timeTaken": 0.01,
                    "otherAmountThreshold": "1490000",
                    "swapMode": "ExactIn",
                    "inputMint": "So11111111111111111111111111111111111111112",
                    "outputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                },
            )
        if "coingecko" in host:
            return httpx.Response(200, json={"solana": {"eur": 92.47, "usd": 100.0}})
        return httpx.Response(404)

    return handler


async def test_smoke_reports_every_provider_with_latency(tmp_path: Path) -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    results = await run_smoke(
        settings,
        timeout_s=2,
        http_transport=httpx.MockTransport(_handler(False)),
        ws_connect=fake_connect,
        db_dir=tmp_path,
    )
    by = {r.name: r for r in results}
    for name in (
        "solana_rpc",
        "helius_das",
        "geckoterminal_discovery",
        "dexscreener_market_data",
        "jupiter_quote",
        "coingecko_fx",
        "pumpportal_ws",
        "solana_ws",
        "database",
        "rate_limiting",
    ):
        assert name in by, name
    assert by["solana_rpc"].status == "PASS" and by["solana_rpc"].latency_ms is not None
    assert by["helius_das"].status == "SKIP"
    assert (
        by["geckoterminal_discovery"].status == "PASS"
        and "discovery cycle" in by["geckoterminal_discovery"].detail
    )
    assert (
        by["dexscreener_market_data"].status == "PASS"
        and "market-data cycle" in by["dexscreener_market_data"].detail
    )
    assert by["coingecko_fx"].status == "PASS" and "92.47" in by["coingecko_fx"].detail
    assert by["pumpportal_ws"].status == "PASS" and by["database"].status == "PASS"
    assert by["rate_limiting"].status == "PASS"
    assert not (tmp_path / "smoke-test.db").exists()  # cleaned up
    assert not any(r.status == "FAIL" for r in results)


async def test_smoke_flags_rate_limited_public_rpc_with_advice(tmp_path: Path) -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    results = await run_smoke(
        settings,
        timeout_s=2,
        http_transport=httpx.MockTransport(_handler(True)),
        ws_connect=fake_connect,
        db_dir=tmp_path,
    )
    by = {r.name: r for r in results}
    assert by["solana_rpc"].status in ("FAIL", "WARN")
    assert "Helius" in by["solana_rpc"].recommendation
    assert by["solana_rpc"].provider_state == "RATE_LIMITED"
    assert (
        by["rate_limiting"].status == "WARN"
        and "solana-rpc=RATE_LIMITED" in by["rate_limiting"].detail
    )
