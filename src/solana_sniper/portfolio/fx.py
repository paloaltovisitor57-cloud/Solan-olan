"""FX rates: SOL→EUR and USD→EUR. Live via CoinGecko; static fallback keeps the engine running."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Protocol

from solana_sniper.discovery.parsing import as_decimal, as_dict
from solana_sniper.infra.http import HttpClient, HttpError
from solana_sniper.telemetry.logging import get_logger

log = get_logger(__name__)


class FxProvider(Protocol):
    async def refresh(self) -> None: ...

    def sol_eur(self) -> Decimal: ...

    def usd_eur(self) -> Decimal: ...

    def sol_usd_cached(self) -> Decimal | None: ...

    @property
    def is_live(self) -> bool: ...


class StaticFx:
    def __init__(self, sol_eur: Decimal, usd_eur: Decimal = Decimal("0.92")) -> None:
        self._sol_eur = sol_eur
        self._usd_eur = usd_eur

    async def refresh(self) -> None:
        return None

    def sol_eur(self) -> Decimal:
        return self._sol_eur

    def usd_eur(self) -> Decimal:
        return self._usd_eur

    def sol_usd_cached(self) -> Decimal | None:
        return self._sol_eur / self._usd_eur if self._usd_eur else None

    @property
    def is_live(self) -> bool:
        return False


class CoinGeckoFx:
    """GET /simple/price?ids=solana&vs_currencies=eur,usd. Free tier, ~30 req/min."""

    def __init__(
        self,
        http: HttpClient,
        base_url: str,
        *,
        fallback_sol_eur: Decimal,
        refresh_s: float = 60.0,
    ) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._sol_eur = fallback_sol_eur
        self._sol_usd: Decimal | None = None
        self._usd_eur = Decimal("0.92")
        self._refresh_s = refresh_s
        self._last_refresh = 0.0
        self._live = False
        http.set_rate_limit("api.coingecko.com", rate_per_s=0.4, burst=3)

    async def refresh(self) -> None:
        if time.monotonic() - self._last_refresh < self._refresh_s:
            return
        self._last_refresh = time.monotonic()
        try:
            res = await self._http.get_json(
                f"{self._base}/simple/price", params={"ids": "solana", "vs_currencies": "eur,usd"}
            )
        except HttpError as exc:
            log.warning("fx_refresh_failed", error=str(exc), using_sol_eur=str(self._sol_eur))
            return
        sol = as_dict((as_dict(res.json) or {}).get("solana")) or {}
        eur = as_decimal(sol.get("eur"))
        usd = as_decimal(sol.get("usd"))
        if eur is None or eur <= 0:
            log.warning("fx_malformed", payload=str(res.json)[:200])
            return
        self._sol_eur = eur
        if usd is not None and usd > 0:
            self._sol_usd = usd
            self._usd_eur = eur / usd
        self._live = True

    def sol_eur(self) -> Decimal:
        return self._sol_eur

    def usd_eur(self) -> Decimal:
        return self._usd_eur

    def sol_usd_cached(self) -> Decimal | None:
        if self._sol_usd is not None:
            return self._sol_usd
        return self._sol_eur / self._usd_eur if self._usd_eur else None

    @property
    def is_live(self) -> bool:
        return self._live
