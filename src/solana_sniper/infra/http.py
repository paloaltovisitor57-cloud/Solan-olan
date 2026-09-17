"""Async HTTP client with per-host token-bucket rate limiting, retries and latency metrics."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from solana_sniper.infra.backoff import Backoff
from solana_sniper.telemetry.logging import get_logger
from solana_sniper.telemetry.metrics import Metrics

log = get_logger(__name__)


class HttpError(Exception):
    def __init__(self, message: str, status: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class RateLimitedError(HttpError):
    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message, status=429, retryable=True)
        self.retry_after_s = retry_after_s


class MalformedResponseError(HttpError):
    pass


class TokenBucket:
    def __init__(self, rate_per_s: float, burst: int) -> None:
        self.rate = rate_per_s
        self.capacity = float(burst)
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = (1 - self._tokens) / self.rate
                await asyncio.sleep(wait)


@dataclass(frozen=True, slots=True)
class HttpResult:
    status: int
    json: Any
    latency_ms: float
    headers: dict[str, str]


class HttpClient:
    """Thin wrapper around httpx.AsyncClient. One instance shared by all providers."""

    def __init__(
        self,
        *,
        timeout_s: float = 8.0,
        max_connections: int = 32,
        metrics: Metrics | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=min(timeout_s, 5.0)),
            limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=16),
            headers={"user-agent": "solana-sniper/0.1 (+https://github.com)"},
            trust_env=True,
            http2=transport is None,
            transport=transport,
            follow_redirects=True,
        )
        self._buckets: dict[str, TokenBucket] = {}
        self._metrics = metrics

    def set_rate_limit(self, host: str, rate_per_s: float, burst: int) -> None:
        self._buckets[host] = TokenBucket(rate_per_s, burst)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        retries: int = 2,
        timeout_s: float | None = None,
    ) -> HttpResult:
        return await self._request(
            "GET", url, params=params, headers=headers, retries=retries, timeout_s=timeout_s
        )

    async def post_json(
        self,
        url: str,
        *,
        json: Any,
        headers: dict[str, str] | None = None,
        retries: int = 1,
        timeout_s: float | None = None,
    ) -> HttpResult:
        return await self._request(
            "POST", url, json=json, headers=headers, retries=retries, timeout_s=timeout_s
        )

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
        retries: int,
        timeout_s: float | None,
    ) -> HttpResult:
        host = httpx.URL(url).host
        bucket = self._buckets.get(host)
        backoff = Backoff(minimum=0.5, maximum=8.0)
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            if bucket is not None:
                await bucket.acquire()
            started = time.perf_counter()
            try:
                response = await self._client.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers=headers,
                    timeout=timeout_s if timeout_s is not None else httpx.USE_CLIENT_DEFAULT,
                )
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                last_error = HttpError(
                    f"{method} {url}: {type(exc).__name__}: {exc}", retryable=True
                )
                if self._metrics:
                    self._metrics.inc("provider_errors")
                if attempt < retries:
                    await asyncio.sleep(backoff.next_delay())
                    continue
                raise last_error from exc
            latency_ms = (time.perf_counter() - started) * 1000.0
            if self._metrics:
                self._metrics.observe("provider_latency", latency_ms)
            if response.status_code == 429:
                if self._metrics:
                    self._metrics.inc("rate_limited")
                retry_after = _parse_retry_after(response.headers.get("retry-after"))
                last_error = RateLimitedError(f"{host} rate limited", retry_after_s=retry_after)
                if attempt < retries:
                    await asyncio.sleep(
                        retry_after if retry_after is not None else backoff.next_delay()
                    )
                    continue
                raise last_error
            if response.status_code >= 500:
                last_error = HttpError(
                    f"{method} {url}: HTTP {response.status_code}",
                    status=response.status_code,
                    retryable=True,
                )
                if self._metrics:
                    self._metrics.inc("provider_errors")
                if attempt < retries:
                    await asyncio.sleep(backoff.next_delay())
                    continue
                raise last_error
            if response.status_code >= 400:
                raise HttpError(
                    f"{method} {url}: HTTP {response.status_code}: {response.text[:200]}",
                    status=response.status_code,
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise MalformedResponseError(f"{method} {url}: invalid JSON") from exc
            return HttpResult(
                status=response.status_code,
                json=payload,
                latency_ms=latency_ms,
                headers=dict(response.headers),
            )
        raise last_error or HttpError("unreachable")


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, min(60.0, float(value)))
    except ValueError:
        return None
