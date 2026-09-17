"""DexScreener discovery: latest token profiles + boosts, enriched with pair creation time.

DexScreener has no "new pairs" endpoint; profiles/boosts surface tokens that are actively
promoted, which is a useful (if biased) secondary discovery channel. Rate limit: 60 req/min.
"""

from __future__ import annotations

from solana_sniper.discovery.parsing import as_dict, as_list, as_str
from solana_sniper.domain.clock import Clock
from solana_sniper.domain.models import TokenInfo
from solana_sniper.infra.http import HttpClient, HttpError
from solana_sniper.market_data.dexscreener import DexScreenerMarketData
from solana_sniper.telemetry.logging import get_logger

log = get_logger(__name__)


class DexScreenerDiscovery:
    name = "dexscreener"

    def __init__(
        self,
        http: HttpClient,
        base_url: str,
        clock: Clock,
        market: DexScreenerMarketData,
        *,
        poll_interval_s: float = 10.0,
        max_token_age_s: float = 1800.0,
    ) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._clock = clock
        self._market = market
        self.poll_interval_s = poll_interval_s
        self._max_age = max_token_age_s
        self._seen: set[str] = set()

    async def _profile_mints(self, path: str) -> list[str]:
        try:
            res = await self._http.get_json(f"{self._base}{path}")
        except HttpError as exc:
            log.warning("dexscreener_profiles_failed", path=path, error=str(exc))
            return []
        mints: list[str] = []
        for item in as_list(res.json):
            d = as_dict(item)
            if d is None or d.get("chainId") != "solana":
                continue
            mint = as_str(d.get("tokenAddress"))
            if mint and mint not in self._seen:
                mints.append(mint)
        return mints

    async def poll(self) -> list[TokenInfo]:
        now = self._clock.now()
        candidates = await self._profile_mints("/token-profiles/latest/v1")
        candidates += [
            m for m in await self._profile_mints("/token-boosts/latest/v1") if m not in candidates
        ]
        if not candidates:
            return []
        self._seen.update(candidates)
        if len(self._seen) > 5000:
            self._seen = set(candidates)
        tokens: list[TokenInfo] = []
        for pair in await self._market.fetch_pairs(candidates):
            token = pair.token
            age = token.age_seconds(now)
            if age is None or age > self._max_age:
                continue
            tokens.append(token)
        return tokens
