"""DexScreener batch market data: GET /tokens/v1/solana/{mint1,mint2,...} (30 per call, 300/min)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from solana_sniper.discovery.parsing import (
    as_decimal,
    as_dict,
    as_int,
    as_list,
    as_str,
    ts_from_ms,
)
from solana_sniper.domain.clock import Clock
from solana_sniper.domain.enums import Venue
from solana_sniper.domain.models import MarketSnapshot, TokenInfo
from solana_sniper.infra.http import HttpClient, HttpError
from solana_sniper.telemetry.logging import get_logger

log = get_logger(__name__)

_DEX_TO_VENUE = {
    "raydium": Venue.RAYDIUM,
    "pumpfun": Venue.PUMP_FUN,
    "pumpswap": Venue.PUMP_SWAP,
    "meteora": Venue.METEORA,
    "orca": Venue.ORCA,
}


@dataclass(frozen=True, slots=True)
class PairView:
    token: TokenInfo
    snapshot: MarketSnapshot


def parse_pair(item: Any, now: datetime, latency_ms: float | None = None) -> PairView | None:
    pair = as_dict(item)
    if pair is None or pair.get("chainId") != "solana":
        return None
    base = as_dict(pair.get("baseToken")) or {}
    quote = as_dict(pair.get("quoteToken")) or {}
    mint = as_str(base.get("address"))
    if mint is None:
        return None
    liq = as_dict(pair.get("liquidity")) or {}
    txns = as_dict(pair.get("txns")) or {}
    m5 = as_dict(txns.get("m5")) or {}
    h1 = as_dict(txns.get("h1")) or {}
    vol = as_dict(pair.get("volume")) or {}
    created = ts_from_ms(pair.get("pairCreatedAt"))
    dex_id = as_str(pair.get("dexId")) or "unknown"
    venue = _DEX_TO_VENUE.get(dex_id, Venue.UNKNOWN)
    token = TokenInfo(
        mint=mint,
        symbol=as_str(base.get("symbol")),
        name=as_str(base.get("name")),
        pool_created_at=created,
        first_liquidity_at=created,
        venue=venue,
        pool_address=as_str(pair.get("pairAddress")),
        quote_mint=as_str(quote.get("address")),
        source="dexscreener",
        discovered_at=now,
    )
    snap = MarketSnapshot(
        mint=mint,
        observed_at=now,
        source="dexscreener",
        price_native=as_decimal(pair.get("priceNative")),
        price_usd=as_decimal(pair.get("priceUsd")),
        liquidity_usd=as_decimal(liq.get("usd")),
        liquidity_native=as_decimal(liq.get("quote")),
        market_cap_usd=as_decimal(pair.get("marketCap")),
        fdv_usd=as_decimal(pair.get("fdv")),
        volume_5m_usd=as_decimal(vol.get("m5")),
        volume_1h_usd=as_decimal(vol.get("h1")),
        buys_5m=as_int(m5.get("buys")),
        sells_5m=as_int(m5.get("sells")),
        buys_1h=as_int(h1.get("buys")),
        sells_1h=as_int(h1.get("sells")),
        pool_address=as_str(pair.get("pairAddress")),
        venue=venue,
        pair_created_at=created,
        provider_latency_ms=latency_ms,
    )
    return PairView(token=token, snapshot=snap)


def best_pairs(items: list[Any], now: datetime, latency_ms: float | None = None) -> list[PairView]:
    """One PairView per mint: the pair with the deepest liquidity."""
    best: dict[str, PairView] = {}
    for item in items:
        view = parse_pair(item, now, latency_ms)
        if view is None:
            continue
        cur = best.get(view.token.mint)
        if cur is None or (view.snapshot.liquidity_usd or 0) > (cur.snapshot.liquidity_usd or 0):
            best[view.token.mint] = view
    return list(best.values())


class DexScreenerMarketData:
    name = "dexscreener"

    def __init__(self, http: HttpClient, base_url: str, clock: Clock, batch_size: int = 30) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._clock = clock
        self.batch_size = min(30, max(1, batch_size))
        http.set_rate_limit("api.dexscreener.com", rate_per_s=4.5, burst=10)

    def supports(self, token: TokenInfo) -> bool:
        return True

    async def fetch_pairs(self, mints: Sequence[str]) -> list[PairView]:
        views: list[PairView] = []
        for i in range(0, len(mints), self.batch_size):
            chunk = list(mints[i : i + self.batch_size])
            try:
                res = await self._http.get_json(f"{self._base}/tokens/v1/solana/{','.join(chunk)}")
            except HttpError as exc:
                log.warning("dexscreener_fetch_failed", count=len(chunk), error=str(exc))
                continue
            views.extend(best_pairs(as_list(res.json), self._clock.now(), res.latency_ms))
        return views

    async def fetch(self, mints: Sequence[str]) -> list[MarketSnapshot]:
        return [v.snapshot for v in await self.fetch_pairs(mints)]
