"""Per-provider request governance: pacing, concurrency, cooldowns, circuit breaking, health.

Every outbound HTTP request passes through `ProviderGovernor.slot(host)`. The governor:

* paces requests with a token bucket and caps in-flight requests per host;
* on HTTP 429 puts the host in a cooldown (Retry-After when supplied, otherwise an exponential
  backoff with jitter that grows with consecutive rate limits and shrinks again on success);
* fast-fails callers while a cooldown or an open circuit is longer than they should wait, so an
  optional enrichment degrades to UNKNOWN instead of piling up hundreds of waiting tasks;
* trips a circuit breaker after repeated failures and lets a single probe through when it
  half-opens;
* keeps a health state per provider (HEALTHY / RATE_LIMITED / DEGRADED / DOWN), counters for
  rate limits, retries, backoffs, trips and recoveries, and logs summaries instead of one line
  per rejected request.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from solana_sniper.telemetry.logging import get_logger
from solana_sniper.telemetry.metrics import Metrics
from solana_sniper.telemetry.throttle import LogThrottle

log = get_logger(__name__)

HEALTHY = "HEALTHY"
RATE_LIMITED = "RATE_LIMITED"
DEGRADED = "DEGRADED"
DOWN = "DOWN"


class ProviderUnavailableError(Exception):
    """Raised without sending a request: the provider is cooling down or its circuit is open."""

    def __init__(self, provider: str, retry_after_s: float, reason: str) -> None:
        super().__init__(f"{provider} unavailable ({reason}); retry in {retry_after_s:.1f}s")
        self.provider = provider
        self.retry_after_s = retry_after_s
        self.reason = reason
        self.retryable = True


@dataclass(frozen=True, slots=True)
class HostPolicy:
    rate_per_s: float = 5.0
    burst: int = 10
    max_concurrent: int = 4
    max_waiting: int = 64  # callers queued for a slot beyond this fast-fail
    cooldown_min_s: float = 1.0
    cooldown_max_s: float = 120.0
    fast_fail_wait_s: float = 3.0  # wait at most this long for a cooldown before failing fast
    trip_after: int = 5  # consecutive failures that open the circuit
    open_s: float = 30.0
    open_max_s: float = 300.0


@dataclass(slots=True)
class _Host:
    name: str
    policy: HostPolicy
    tokens: float
    last_refill: float
    sem: asyncio.Semaphore
    waiting: int = 0
    inflight: int = 0
    state: str = HEALTHY
    cooldown_until: float = 0.0
    consecutive_429: int = 0
    consecutive_failures: int = 0
    circuit_open_until: float = 0.0
    circuit_trips: int = 0
    half_open_probe: bool = False
    last_error: str | None = None
    last_success_at: float | None = None
    counters: dict[str, int] = field(
        default_factory=lambda: {
            "requests": 0,
            "rate_limited": 0,
            "retries": 0,
            "backoffs": 0,
            "fast_fails": 0,
            "failures": 0,
            "recoveries": 0,
        }
    )


class ProviderGovernor:
    def __init__(
        self,
        *,
        metrics: Metrics | None = None,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] | None = None,
        default_policy: HostPolicy | None = None,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._metrics = metrics
        self._now = now
        self._sleep_override = sleep
        self._default = default_policy or HostPolicy()
        self._hosts: dict[str, _Host] = {}
        self._names: dict[str, str] = {}  # host -> provider name
        self._policies: dict[str, HostPolicy] = {}  # provider name -> policy
        self._throttle = LogThrottle(60.0, now)
        self._rng = rng

    async def _sleep(self, seconds: float) -> None:
        if self._sleep_override is not None:
            await self._sleep_override(seconds)
        else:
            await asyncio.sleep(seconds)  # resolved at call time so tests can patch asyncio.sleep

    # ------------------------------------------------------------- registry
    def register(self, host: str, name: str, policy: HostPolicy | None = None) -> None:
        """Map a hostname to a provider name and (optionally) its policy."""
        self._names[host] = name
        if policy is not None:
            self._policies[name] = policy
        existing = self._hosts.get(host)
        if existing is not None:
            existing.name = name
            if policy is not None:
                existing.policy = policy
                existing.sem = asyncio.Semaphore(max(1, policy.max_concurrent))

    def set_policy(self, host: str, policy: HostPolicy) -> None:
        self.register(host, self._names.get(host, host), policy)

    def has_policy(self, host: str) -> bool:
        return self._names.get(host, host) in self._policies

    def policy_for(self, host: str) -> HostPolicy:
        return self._policies.get(self._names.get(host, host), self._default)

    def _host(self, host: str) -> _Host:
        st = self._hosts.get(host)
        if st is None:
            name = self._names.get(host, host)
            policy = self._policies.get(name, self._default)
            st = _Host(
                name=name,
                policy=policy,
                tokens=float(policy.burst),
                last_refill=self._now(),
                sem=asyncio.Semaphore(max(1, policy.max_concurrent)),
            )
            self._hosts[host] = st
        return st

    def name_of(self, host: str) -> str:
        return self._names.get(host, host)

    # ----------------------------------------------------------------- slot
    @asynccontextmanager
    async def slot(self, host: str) -> AsyncIterator[None]:
        st = self._host(host)
        await self._admit(st)
        st.inflight += 1
        try:
            yield
        finally:
            st.inflight -= 1
            st.sem.release()

    async def _admit(self, st: _Host) -> None:
        now = self._now()
        # circuit breaker
        if st.circuit_open_until > now:
            self._fast_fail(st, st.circuit_open_until - now, "circuit open")
        if st.state == DOWN and not st.half_open_probe:
            st.half_open_probe = True  # exactly one probe while half-open
        elif st.state == DOWN:
            self._fast_fail(st, st.policy.open_s, "half-open probe in flight")
        # cooldown after rate limiting
        remaining = st.cooldown_until - now
        if remaining > st.policy.fast_fail_wait_s:
            self._fast_fail(st, remaining, "rate-limit cooldown")
        if st.waiting >= st.policy.max_waiting:
            self._fast_fail(st, max(0.5, remaining), "too many requests waiting")
        st.waiting += 1
        try:
            if remaining > 0:
                await self._sleep(remaining)
            await st.sem.acquire()
            await self._pace(st)
        finally:
            st.waiting -= 1
        st.counters["requests"] += 1

    async def _pace(self, st: _Host) -> None:
        while True:
            now = self._now()
            st.tokens = min(
                float(st.policy.burst), st.tokens + (now - st.last_refill) * st.policy.rate_per_s
            )
            st.last_refill = now
            if st.tokens >= 1.0:
                st.tokens -= 1.0
                return
            await self._sleep((1.0 - st.tokens) / max(st.policy.rate_per_s, 1e-6))

    def _fast_fail(self, st: _Host, retry_in: float, reason: str) -> None:
        st.counters["fast_fails"] += 1
        if self._metrics:
            self._metrics.inc("provider_fast_fails")
        raise ProviderUnavailableError(st.name, max(0.0, retry_in), reason)

    # --------------------------------------------------------------- outcomes
    def on_success(self, host: str) -> None:
        st = self._host(host)
        st.last_success_at = self._now()
        st.consecutive_failures = 0
        st.consecutive_429 = 0
        st.half_open_probe = False
        st.circuit_open_until = 0.0
        if st.state != HEALTHY:
            previous = st.state
            st.state = HEALTHY
            st.cooldown_until = 0.0
            st.counters["recoveries"] += 1
            if self._metrics:
                self._metrics.inc("provider_recoveries")
            log.info("provider_recovered", provider=st.name, previous=previous)
            self._throttle.reset(f"{st.name}:429")
            self._throttle.reset(f"{st.name}:fail")

    def on_rate_limited(self, host: str, retry_after_s: float | None) -> float:
        """Record a 429; returns the cooldown applied (seconds)."""
        st = self._host(host)
        st.consecutive_429 += 1
        st.counters["rate_limited"] += 1
        if self._metrics:
            self._metrics.inc("rate_limited")
        p = st.policy
        if retry_after_s is not None:
            cooldown = min(p.cooldown_max_s, max(0.0, retry_after_s))
        else:
            base = min(p.cooldown_max_s, p.cooldown_min_s * (2 ** (st.consecutive_429 - 1)))
            cooldown = min(p.cooldown_max_s, base * (1.0 + 0.25 * self._rng()))
        st.cooldown_until = max(st.cooldown_until, self._now() + cooldown)
        st.counters["backoffs"] += 1
        if self._metrics:
            self._metrics.inc("provider_backoffs")
        if st.state != DOWN:
            st.state = RATE_LIMITED
        st.last_error = "rate limited"
        self._log(
            st,
            f"{st.name}:429",
            "provider_rate_limited",
            cooldown_s=round(cooldown, 1),
            consecutive=st.consecutive_429,
        )
        return cooldown

    def on_failure(self, host: str, error: str) -> None:
        st = self._host(host)
        st.consecutive_failures += 1
        st.counters["failures"] += 1
        st.last_error = error[:160]
        p = st.policy
        if st.half_open_probe or st.consecutive_failures >= p.trip_after:
            st.circuit_trips += 1
            open_for = min(p.open_max_s, p.open_s * (2 ** max(0, st.circuit_trips - 1)))
            st.circuit_open_until = self._now() + open_for
            st.half_open_probe = False
            st.state = DOWN
            if self._metrics:
                self._metrics.inc("provider_circuit_trips")
            log.warning(
                "provider_down",
                provider=st.name,
                consecutive_failures=st.consecutive_failures,
                open_s=round(open_for, 1),
                error=st.last_error,
            )
            return
        if st.state == HEALTHY and st.consecutive_failures >= 2:
            st.state = DEGRADED
        self._log(st, f"{st.name}:fail", "provider_degraded", error=st.last_error)

    def on_retry(self, host: str) -> None:
        st = self._host(host)
        st.counters["retries"] += 1
        if self._metrics:
            self._metrics.inc("provider_retries")

    def _log(self, st: _Host, key: str, event: str, **fields: Any) -> None:
        decision = self._throttle.hit(key)
        if decision.log:
            log.warning(
                event,
                provider=st.name,
                state=st.state,
                occurrences=decision.total,
                suppressed_since_last=decision.suppressed,
                **fields,
            )

    # ----------------------------------------------------------------- health
    def cooldown_remaining(self, host: str) -> float:
        st = self._host(host)
        return max(0.0, max(st.cooldown_until, st.circuit_open_until) - self._now())

    def state(self, host: str) -> str:
        return self._host(host).state

    def health(self) -> dict[str, dict[str, Any]]:
        now = self._now()
        out: dict[str, dict[str, Any]] = {}
        for host, st in self._hosts.items():
            cooldown = max(0.0, max(st.cooldown_until, st.circuit_open_until) - now)
            entry = {
                "host": host,
                "state": st.state,
                "cooldown_s": round(cooldown, 1),
                "inflight": st.inflight,
                "waiting": st.waiting,
                "consecutive_failures": st.consecutive_failures,
                "circuit_trips": st.circuit_trips,
                "last_error": st.last_error,
                **st.counters,
            }
            out[st.name] = entry
        return out

    def summary_line(self) -> str:
        parts = []
        for name, info in self.health().items():
            piece = f"{name}={info['state']}"
            if info["cooldown_s"]:
                piece += f"({info['cooldown_s']:.0f}s)"
            parts.append(piece)
        return "  ".join(parts) if parts else "no provider traffic yet"
