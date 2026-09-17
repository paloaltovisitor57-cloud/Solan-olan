from __future__ import annotations

from decimal import Decimal

import httpx

from solana_sniper.infra.http import HttpClient
from solana_sniper.portfolio.fx import CoinGeckoFx, StaticFx


async def test_coingecko_fx_live_and_fallback() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"solana": {"eur": 150.5, "usd": 163.0}})
        return httpx.Response(500)

    http = HttpClient(transport=httpx.MockTransport(handler))
    fx = CoinGeckoFx(
        http, "https://api.coingecko.com/api/v3", fallback_sol_eur=Decimal("100"), refresh_s=0
    )
    assert fx.sol_eur() == Decimal("100") and not fx.is_live
    await fx.refresh()
    assert fx.sol_eur() == Decimal("150.5") and fx.is_live
    assert fx.sol_usd_cached() == Decimal("163.0")
    assert fx.usd_eur() == Decimal("150.5") / Decimal("163.0")
    await fx.refresh()  # failure keeps the last good value
    assert fx.sol_eur() == Decimal("150.5")
    await http.aclose()


async def test_coingecko_malformed_keeps_fallback() -> None:
    http = HttpClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"solana": {"eur": "x"}}))
    )
    fx = CoinGeckoFx(
        http, "https://api.coingecko.com/api/v3", fallback_sol_eur=Decimal("100"), refresh_s=0
    )
    await fx.refresh()
    assert fx.sol_eur() == Decimal("100") and not fx.is_live
    await http.aclose()


async def test_static_fx() -> None:
    fx = StaticFx(Decimal("150"), Decimal("0.9"))
    await fx.refresh()
    assert fx.sol_usd_cached() == Decimal("150") / Decimal("0.9")
    assert not fx.is_live
