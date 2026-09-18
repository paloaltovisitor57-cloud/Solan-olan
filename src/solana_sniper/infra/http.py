"""Async HTTP client with per-host token-bucket rate limiting, retries and latency metrics."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace
from typing import Any

import httpx

from solana_sniper.infra.backoff import Backoff
from solana_sniper.infra.governor import ProviderGovernor, ProviderUnavailableError
from solana_sniper.telemetry.logging import get_logger
from solana_sniper.telemetry.metrics import Metrics
from solana_sniper.telemetry.redaction import safe_url, scrub_text

log = get_logger(__name__)

__all__ = [
    "HttpClient",
    "HttpError",
    "HttpResult",
    "MalformedResponseError",
    "ProviderUnavailableError",
    "RateLimitedError",
    "TokenBucket",
]


class HttpError(Exception):
    """Transport/HTTP failure. The message never contains query strings, userinfo, headers or
    response bodies: only the method, a sanitised endpoint, the status and the error type."""

    def __init__(
        self,
        message: str,
        status: int | None = None,
        retryable: bool = False,
        endpoint: str | None = None,
    ) -> None:
        super().__init__(scrub_text(message))
        self.status = status
        self.retryable = retryable
        self.endpoint = endpoint


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
        governor: ProviderGovernor | None = None,
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
        self._metrics = metrics
        self.governor = governor or ProviderGovernor(metrics=metrics)
        self._hinted: set[str] = set()

    def set_rate_limit(self, host: str, rate_per_s: float, burst: int) -> None:
        """Provider-supplied pacing hint. Configured policies (registered by the bootstrap
        before providers are built) take precedence; the hint applies only to unknown hosts."""
        if self.governor.has_policy(host):
            return
        policy = self.governor.policy_for(host)
        self.governor.set_policy(host, replace(policy, rate_per_s=rate_per_s, burst=burst))

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
        endpoint = safe_url(url)
        gov = self.governor
        backoff = Backoff(minimum=0.5, maximum=8.0)
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            if attempt:
                gov.on_retry(host)
            delay: float | None = None
            async with gov.slot(host):  # may raise ProviderUnavailableError without sending
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
                except httpx.TransportError as exc:  # timeouts, network, proxy, protocol errors
                    last_error = HttpError(
                        f"{method} {endpoint}: {type(exc).__name__}: {scrub_text(str(exc))}",
                        retryable=True,
                        endpoint=endpoint,
                    )
                    if self._metrics:
                        self._metrics.inc("provider_errors")
                    gov.on_failure(host, type(exc).__name__)
                    if attempt < retries:
                        delay = backoff.next_delay()
                    else:
                        raise last_error from exc
                else:
                    latency_ms = (time.perf_counter() - started) * 1000.0
                    if self._metrics:
                        self._metrics.observe("provider_latency", latency_ms)
                    if response.status_code == 429:
                        retry_after = _parse_retry_after(response.headers.get("retry-after"))
                        cooldown = gov.on_rate_limited(host, retry_after)
                        last_error = RateLimitedError(
                            f"{endpoint} rate limited", retry_after_s=cooldown
                        )
                        # The governor waits out a short cooldown on the next slot; a longer
                        # one is reported to the caller so optional work degrades to UNKNOWN.
                        if attempt < retries and cooldown <= gov.policy_for(host).fast_fail_wait_s:
                            delay = 0.0
                        else:
                            raise last_error
                    elif response.status_code >= 500:
                        last_error = HttpError(
                            f"{method} {endpoint}: HTTP {response.status_code}",
                            status=response.status_code,
                            retryable=True,
                            endpoint=endpoint,
                        )
                        if self._metrics:
                            self._metrics.inc("provider_errors")
                        gov.on_failure(host, f"HTTP {response.status_code}")
                        if attempt < retries:
                            delay = backoff.next_delay()
                        else:
                            raise last_error
                    elif response.status_code >= 400:
                        # Bodies are never included: providers may echo the request (and key).
                        # A client error is ours, not the provider's health.
                        raise HttpError(
                            f"{method} {endpoint}: HTTP {response.status_code}",
                            status=response.status_code,
                            endpoint=endpoint,
                        )
                    else:
                        try:
                            payload = response.json()
                        except ValueError as exc:
                            gov.on_failure(host, "invalid JSON")
                            raise MalformedResponseError(
                                f"{method} {endpoint}: invalid JSON", endpoint=endpoint
                            ) from exc
                        gov.on_success(host)
                        return HttpResult(
                            status=response.status_code,
                            json=payload,
                            latency_ms=latency_ms,
                            headers=dict(response.headers),
                        )
            # sleep outside the slot so a backing-off request does not hold a concurrency permit
            if delay:
                await asyncio.sleep(delay)
        raise last_error or HttpError("unreachable")


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, min(60.0, float(value)))
    except ValueError:
        return None
