"""Solana JSON-RPC based token facts: mint authorities, supply, extensions, largest holders."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, TypeVar

from solana_sniper.discovery.parsing import as_dict, as_int, as_list, as_str
from solana_sniper.domain.clock import Clock
from solana_sniper.domain.models import HolderDistribution, TokenAuthorities
from solana_sniper.infra.http import (
    HttpClient,
    HttpError,
    MalformedResponseError,
    ProviderUnavailableError,
    RateLimitedError,
)
from solana_sniper.telemetry.logging import get_logger
from solana_sniper.telemetry.metrics import Metrics
from solana_sniper.telemetry.redaction import safe_exception
from solana_sniper.telemetry.throttle import LogThrottle

log = get_logger(__name__)
_throttle = LogThrottle(60.0)
_MISSING: Any = object()
T = TypeVar("T")


class TtlCache[V]:
    """Tiny bounded TTL cache keyed by mint (monotonic seconds)."""

    def __init__(self, ttl_s: float, max_items: int) -> None:
        self._ttl = ttl_s
        self._max = max_items
        self._items: dict[str, tuple[float, V]] = {}

    def get(self, key: str, now: float) -> V | Any:
        item = self._items.get(key)
        if item is None:
            return _MISSING
        at, value = item
        if now - at > self._ttl:
            self._items.pop(key, None)
            return _MISSING
        return value

    def put(self, key: str, value: V, now: float) -> None:
        if len(self._items) >= self._max:
            oldest = min(self._items, key=lambda k: self._items[k][0])
            self._items.pop(oldest, None)
        self._items[key] = (now, value)


TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


def parse_mint_account(payload: Any) -> TokenAuthorities | None:
    result = as_dict((as_dict(payload) or {}).get("result"))
    value = as_dict((result or {}).get("value"))
    if value is None:
        return None
    data = as_dict(value.get("data"))
    parsed = as_dict((data or {}).get("parsed"))
    info = as_dict((parsed or {}).get("info"))
    if parsed is None or info is None or parsed.get("type") != "mint":
        return None
    owner = as_str(value.get("owner"))
    decimals = as_int(info.get("decimals"))
    supply = as_int(info.get("supply"))
    if decimals is None or supply is None:
        return None
    transfer_fee_bps: int | None = None
    has_hook = False
    non_transferable = False
    permanent_delegate: str | None = None
    for ext in as_list(info.get("extensions")):
        e = as_dict(ext) or {}
        kind = as_str(e.get("extension"))
        state = as_dict(e.get("state")) or {}
        if kind == "transferFeeConfig":
            newer = as_dict(state.get("newerTransferFee")) or {}
            transfer_fee_bps = as_int(newer.get("transferFeeBasisPoints"))
        elif kind == "transferHook":
            has_hook = as_str(state.get("programId")) is not None
        elif kind == "nonTransferable":
            non_transferable = True
        elif kind == "permanentDelegate":
            permanent_delegate = as_str(state.get("delegate"))
    return TokenAuthorities(
        mint_authority=as_str(info.get("mintAuthority")),
        freeze_authority=as_str(info.get("freezeAuthority")),
        decimals=decimals,
        supply_raw=supply,
        is_token_2022=owner == TOKEN_2022_PROGRAM,
        transfer_fee_bps=transfer_fee_bps,
        has_transfer_hook=has_hook,
        non_transferable=non_transferable,
        permanent_delegate=permanent_delegate,
    )


def parse_largest_accounts(
    payload: Any, supply_raw: int, pool_addresses: tuple[str, ...], at: datetime
) -> HolderDistribution | None:
    result = as_dict((as_dict(payload) or {}).get("result"))
    accounts = as_list((result or {}).get("value"))
    if not accounts or supply_raw <= 0:
        return None
    amounts: list[tuple[str, int]] = []
    for acc in accounts:
        a = as_dict(acc) or {}
        amt = as_int(a.get("amount"))
        addr = as_str(a.get("address"))
        if amt is not None and addr is not None:
            amounts.append((addr, amt))
    if not amounts:
        return None
    # Pool/bonding-curve token accounts are not "holders"; exclude them when known.
    pool_set = set(pool_addresses)
    non_pool = [(a, v) for a, v in amounts if a not in pool_set]
    largest_is_pool = amounts[0][0] in pool_set
    if not non_pool:
        return HolderDistribution(None, 0.0, 0.0, largest_is_pool, at)
    top10 = sum(v for _, v in non_pool[:10]) / supply_raw
    largest = non_pool[0][1] / supply_raw
    return HolderDistribution(
        holder_count=None,
        top10_pct=min(1.0, top10),
        largest_pct=min(1.0, largest),
        largest_is_pool=largest_is_pool,
        observed_at=at,
    )


class SolanaRpcTokenProvider:
    """Implements both TokenMetadataProvider and LiquidityProvider over JSON-RPC.

    With a Helius URL, holder counts come from the DAS getTokenAccounts method.
    """

    name = "solana-rpc"

    def __init__(
        self,
        http: HttpClient,
        rpc_url: str,
        clock: Clock,
        *,
        helius_api_key: str | None = None,
        max_holder_pages: int = 3,
        metrics: Metrics | None = None,
        cache_ttl_s: float = 120.0,
        holders_ttl_s: float = 45.0,
    ) -> None:
        self._http = http
        self._url = rpc_url
        self._clock = clock
        self._helius_key = helius_api_key
        self._max_pages = max_holder_pages
        self._supply_cache: dict[str, int] = {}
        self._req_id = 0
        self._metrics = metrics
        # Short-lived caches and single-flight: the engine asks for the same mint from several
        # places (metadata, holders, re-checks); one RPC call answers all of them.
        self._auth_cache: TtlCache[TokenAuthorities | None] = TtlCache(cache_ttl_s, 2000)
        self._holders_cache: TtlCache[HolderDistribution | None] = TtlCache(holders_ttl_s, 2000)
        self._inflight: dict[str, asyncio.Future[Any]] = {}
        self.degraded_calls = 0

    async def _rpc(self, method: str, params: list[Any]) -> Any:
        self._req_id += 1
        payload = {"jsonrpc": "2.0", "id": self._req_id, "method": method, "params": params}
        res = await self._http.post_json(self._url, json=payload)
        body = as_dict(res.json)
        if body is None:
            raise MalformedResponseError(f"{method}: non-object response")
        if "error" in body:
            raise HttpError(f"{method}: rpc error {body['error']}")
        return body

    def _degraded(self, what: str, mint: str, exc: Exception) -> None:
        """An optional enrichment could not be fetched: the check stays UNKNOWN. The governor
        already logs a rate-limit summary, so this is a debug line plus a counter."""
        self.degraded_calls += 1
        if self._metrics:
            self._metrics.inc("checks_degraded")
        if isinstance(exc, RateLimitedError | ProviderUnavailableError):
            log.debug(f"rpc_{what}_degraded", mint=mint, error=safe_exception(exc))
        else:
            decision = _throttle.hit(f"rpc:{what}")
            if decision.log:
                log.warning(
                    f"rpc_{what}_failed",
                    mint=mint,
                    error=safe_exception(exc),
                    suppressed_since_last=decision.suppressed,
                )

    async def _single_flight(self, key: str, fetch: Callable[[], Awaitable[T]]) -> T:
        existing = self._inflight.get(key)
        if existing is not None:
            result: T = await asyncio.shield(existing)
            return result
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._inflight[key] = fut
        try:
            value = await fetch()
        except BaseException as exc:
            if not fut.done():
                fut.set_exception(exc)
            raise
        else:
            if not fut.done():
                fut.set_result(value)
            return value
        finally:
            self._inflight.pop(key, None)
            if not fut.done():
                fut.cancel()

    async def get_authorities(self, mint: str) -> TokenAuthorities | None:
        cached = self._auth_cache.get(mint, self._clock.monotonic())
        if cached is not _MISSING:
            return cached

        async def fetch() -> TokenAuthorities | None:
            try:
                body = await self._rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
            except (HttpError, ProviderUnavailableError) as exc:
                self._degraded("get_account_info", mint, exc)
                return None
            auth = parse_mint_account(body)
            if auth is not None:
                self._supply_cache[mint] = auth.supply_raw
            self._auth_cache.put(mint, auth, self._clock.monotonic())
            return auth

        return await self._single_flight(f"auth:{mint}", fetch)

    async def get_holder_distribution(
        self, mint: str, pool_addresses: tuple[str, ...]
    ) -> HolderDistribution | None:
        cached = self._holders_cache.get(mint, self._clock.monotonic())
        if cached is not _MISSING:
            return cached

        async def fetch() -> HolderDistribution | None:
            supply = self._supply_cache.get(mint)
            if supply is None:
                auth = await self.get_authorities(mint)
                if auth is None:
                    return None
                supply = auth.supply_raw
            try:
                body = await self._rpc("getTokenLargestAccounts", [mint])
            except (HttpError, ProviderUnavailableError) as exc:
                self._degraded("largest_accounts", mint, exc)
                return None
            dist = parse_largest_accounts(body, supply, pool_addresses, self._clock.now())
            if dist is None:
                return None
            holder_count = await self._holder_count(mint) if self._helius_key else None
            result = HolderDistribution(
                holder_count=holder_count,
                top10_pct=dist.top10_pct,
                largest_pct=dist.largest_pct,
                largest_is_pool=dist.largest_is_pool,
                observed_at=dist.observed_at,
            )
            self._holders_cache.put(mint, result, self._clock.monotonic())
            return result

        return await self._single_flight(f"holders:{mint}", fetch)

    async def _holder_count(self, mint: str) -> int | None:
        """Helius DAS getTokenAccounts, capped at max_holder_pages*1000 accounts."""
        total = 0
        cursor: str | None = None
        for _ in range(self._max_pages):
            params: dict[str, Any] = {"mint": mint, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            try:
                body = await self._rpc("getTokenAccounts", [params])
            except (HttpError, ProviderUnavailableError) as exc:
                self._degraded("helius_token_accounts", mint, exc)
                return None
            result = as_dict(body.get("result")) or {}
            accounts = as_list(result.get("token_accounts"))
            total += sum(1 for a in accounts if (as_int((as_dict(a) or {}).get("amount")) or 0) > 0)
            cursor = as_str(result.get("cursor"))
            if not cursor or len(accounts) < 1000:
                break
        return total
