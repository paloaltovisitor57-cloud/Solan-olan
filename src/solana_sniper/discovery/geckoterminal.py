"""GeckoTerminal new-pools discovery (free, 30 req/min). Also yields an initial MarketSnapshot."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from solana_sniper.discovery.parsing import (
    as_decimal,
    as_dict,
    as_int,
    as_list,
    as_str,
    ts_from_iso,
)
from solana_sniper.domain.clock import Clock
from solana_sniper.domain.enums import Venue
from solana_sniper.domain.models import MarketSnapshot, TokenInfo
from solana_sniper.infra.http import HttpClient, HttpError
from solana_sniper.telemetry.logging import get_logger

log = get_logger(__name__)

WSOL = "So11111111111111111111111111111111111111112"
KNOWN_QUOTES = {
    WSOL,
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}

_DEX_TO_VENUE = {
    "raydium": Venue.RAYDIUM,
    "raydium-clmm": Venue.RAYDIUM_CLMM,
    "pump-fun": Venue.PUMP_FUN,
    "pumpswap": Venue.PUMP_SWAP,
    "pumpfun-amm": Venue.PUMP_SWAP,
    "meteora": Venue.METEORA,
    "meteora-dlmm": Venue.METEORA,
    "orca": Venue.ORCA,
}


def _token_id_to_mint(value: Any) -> str | None:
    s = as_str(value)
    if s is None:
        return None
    return s.split("_", 1)[1] if "_" in s else s


def parse_pool(item: Any, now: datetime) -> tuple[TokenInfo, MarketSnapshot] | None:
    pool = as_dict(item)
    if pool is None:
        return None
    attrs = as_dict(pool.get("attributes")) or {}
    rel = as_dict(pool.get("relationships")) or {}
    base = (as_dict(rel.get("base_token")) or {}).get("data")
    quote = (as_dict(rel.get("quote_token")) or {}).get("data")
    base_mint = _token_id_to_mint((as_dict(base) or {}).get("id"))
    quote_mint = _token_id_to_mint((as_dict(quote) or {}).get("id"))
    if base_mint is None:
        return None
    price_native = as_decimal(attrs.get("base_token_price_native_currency"))
    price_usd = as_decimal(attrs.get("base_token_price_usd"))
    if base_mint in KNOWN_QUOTES and quote_mint is not None and quote_mint not in KNOWN_QUOTES:
        # pool listed with SOL/USDC as base: the interesting token is the quote side
        base_mint, quote_mint = quote_mint, base_mint
        price_native = as_decimal(attrs.get("quote_token_price_native_currency"))
        price_usd = as_decimal(attrs.get("quote_token_price_usd"))
    dex = (as_dict(rel.get("dex")) or {}).get("data")
    dex_id = as_str((as_dict(dex) or {}).get("id")) or "unknown"
    created = ts_from_iso(attrs.get("pool_created_at"))
    name = as_str(attrs.get("name")) or ""
    symbol = name.split("/")[0].strip() if "/" in name else (name or None)
    txns = as_dict(attrs.get("transactions")) or {}
    m5 = as_dict(txns.get("m5")) or {}
    h1 = as_dict(txns.get("h1")) or {}
    vol = as_dict(attrs.get("volume_usd")) or {}
    token = TokenInfo(
        mint=base_mint,
        symbol=symbol or None,
        name=name or None,
        pool_created_at=created,
        first_liquidity_at=created,
        venue=_DEX_TO_VENUE.get(dex_id, Venue.UNKNOWN),
        pool_address=as_str(attrs.get("address")),
        quote_mint=quote_mint,
        source="geckoterminal",
        discovered_at=now,
    )
    snap = MarketSnapshot(
        mint=base_mint,
        observed_at=now,
        source="geckoterminal",
        price_native=price_native,
        price_usd=price_usd,
        liquidity_usd=as_decimal(attrs.get("reserve_in_usd")),
        market_cap_usd=as_decimal(attrs.get("market_cap_usd")),
        fdv_usd=as_decimal(attrs.get("fdv_usd")),
        volume_5m_usd=as_decimal(vol.get("m5")),
        volume_1h_usd=as_decimal(vol.get("h1")),
        buys_5m=as_int(m5.get("buys")),
        sells_5m=as_int(m5.get("sells")),
        buys_1h=as_int(h1.get("buys")),
        sells_1h=as_int(h1.get("sells")),
        unique_traders=(as_int(m5.get("buyers")) or 0) + (as_int(m5.get("sellers")) or 0) or None,
        pool_address=as_str(attrs.get("address")),
        venue=token.venue,
        pair_created_at=created,
    )
    return token, snap


class GeckoTerminalDiscovery:
    name = "geckoterminal"

    def __init__(
        self,
        http: HttpClient,
        base_url: str,
        clock: Clock,
        *,
        poll_interval_s: float = 5.0,
        max_token_age_s: float = 1800.0,
        pages: int = 1,
    ) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._clock = clock
        self.poll_interval_s = poll_interval_s
        self._max_age = max_token_age_s
        self._pages = max(1, pages)
        self.last_snapshots: dict[str, MarketSnapshot] = {}
        http.set_rate_limit("api.geckoterminal.com", rate_per_s=0.45, burst=5)

    async def poll(self) -> list[TokenInfo]:
        now = self._clock.now()
        found: list[TokenInfo] = []
        for page in range(1, self._pages + 1):
            try:
                res = await self._http.get_json(
                    f"{self._base}/networks/solana/new_pools",
                    params={"page": page},
                    headers={"accept": "application/json;version=20230302"},
                )
            except HttpError as exc:
                log.warning("geckoterminal_poll_failed", error=str(exc))
                break
            data = as_list((as_dict(res.json) or {}).get("data"))
            for item in data:
                parsed = parse_pool(item, now)
                if parsed is None:
                    continue
                token, snap = parsed
                age = token.age_seconds(now)
                if age is not None and age > self._max_age:
                    continue
                self.last_snapshots[token.mint] = snap
                found.append(token)
        return found
