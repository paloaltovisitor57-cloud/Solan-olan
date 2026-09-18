"""Provider governance: 429 handling, backoff, fast-fail, circuit breaking, recovery, pacing,
and log aggregation. All offline with mocked transports and a manual clock."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from solana_sniper.infra.governor import (
    DEGRADED,
    DOWN,
    HEALTHY,
    RATE_LIMITED,
    HostPolicy,
    ProviderGovernor,
    ProviderUnavailableError,
)
from solana_sniper.infra.http import HttpClient, HttpError, RateLimitedError
from solana_sniper.telemetry.metrics import Metrics
from solana_sniper.telemetry.throttle import LogThrottle


class FakeTime:
    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def make(
    handler: Any, policy: HostPolicy | None = None, ft: FakeTime | None = None
) -> tuple[HttpClient, ProviderGovernor, Metrics, FakeTime]:
    ft = ft or FakeTime()
    metrics = Metrics()
    gov = ProviderGovernor(metrics=metrics, now=ft.now, sleep=ft.sleep, rng=lambda: 0.0)
    gov.register("rpc.example", "solana-rpc", policy or HostPolicy(rate_per_s=100, burst=100))
    client = HttpClient(transport=httpx.MockTransport(handler), metrics=metrics, governor=gov)
    return client, gov, metrics, ft


async def test_429_with_retry_after_waits_exactly_that_long_then_recovers() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"retry-after": "2"})
        return httpx.Response(200, json={"ok": True})

    client, gov, metrics, ft = make(handler)
    res = await client.get_json("https://rpc.example/x", retries=2)
    assert res.json == {"ok": True} and calls == 2
    assert ft.sleeps == [2.0]
    assert metrics.counters["rate_limited"] == 1 and metrics.counters["provider_retries"] == 1
    assert gov.state("rpc.example") == HEALTHY and metrics.counters["provider_recoveries"] == 1
    health = gov.health()["solana-rpc"]
    assert health["rate_limited"] == 1 and health["recoveries"] == 1 and health["cooldown_s"] == 0
    await client.aclose()


async def test_repeated_429_backs_off_exponentially_and_fast_fails_during_cooldown() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429)  # no Retry-After

    policy = HostPolicy(
        rate_per_s=100, burst=100, cooldown_min_s=1.0, cooldown_max_s=8.0, fast_fail_wait_s=1.5
    )
    client, gov, metrics, ft = make(handler, policy)
    with pytest.raises(RateLimitedError) as e1:
        await client.get_json("https://rpc.example/x", retries=3)
    # attempt 1 -> 1s cooldown (retried), attempt 2 -> 2s (> fast_fail_wait: reported)
    assert calls == 2 and ft.sleeps == [1.0]
    assert e1.value.retry_after_s == pytest.approx(2.0)
    assert gov.state("rpc.example") == RATE_LIMITED
    # while cooling down nothing is sent at all: the caller fails fast with the remaining time
    with pytest.raises(ProviderUnavailableError) as e2:
        await client.get_json("https://rpc.example/x", retries=0)
    assert calls == 2 and e2.value.retry_after_s == pytest.approx(2.0) and e2.value.retryable
    assert metrics.counters["provider_fast_fails"] == 1
    # after the cooldown the next 429 doubles again, capped at cooldown_max_s
    ft.t += 2.0
    for expected in (4.0, 8.0, 8.0):
        with pytest.raises(RateLimitedError) as e3:
            await client.get_json("https://rpc.example/x", retries=0)
        assert e3.value.retry_after_s == pytest.approx(expected)
        ft.t += expected
    assert metrics.counters["provider_backoffs"] == 5
    await client.aclose()


async def test_recovery_after_cooldown_resets_backoff() -> None:
    responses: list[int] = [429, 429, 200, 429, 200]

    def handler(request: httpx.Request) -> httpx.Response:
        code = responses.pop(0)
        return httpx.Response(code, json={"ok": True} if code == 200 else None)

    policy = HostPolicy(rate_per_s=100, burst=100, cooldown_min_s=1.0, fast_fail_wait_s=0.5)
    client, gov, metrics, ft = make(handler, policy)
    for _ in range(2):
        with pytest.raises(RateLimitedError):
            await client.get_json("https://rpc.example/x", retries=0)
        ft.t += 10
    assert gov.state("rpc.example") == RATE_LIMITED
    await client.get_json("https://rpc.example/x", retries=0)
    assert gov.state("rpc.example") == HEALTHY and metrics.counters["provider_recoveries"] == 1
    # the streak restarted: the next 429 goes back to the minimum cooldown
    with pytest.raises(RateLimitedError) as e:
        await client.get_json("https://rpc.example/x", retries=0)
    assert e.value.retry_after_s == pytest.approx(1.0)
    ft.t += 5
    await client.get_json("https://rpc.example/x", retries=0)
    assert metrics.counters["provider_recoveries"] == 2
    await client.aclose()


async def test_circuit_breaker_opens_after_repeated_failures_and_half_opens() -> None:
    calls = 0
    healthy = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={}) if healthy else httpx.Response(503)

    policy = HostPolicy(rate_per_s=100, burst=100, trip_after=3, open_s=30.0, open_max_s=120.0)
    client, gov, metrics, ft = make(handler, policy)
    for _ in range(3):
        with pytest.raises(HttpError):
            await client.get_json("https://rpc.example/x", retries=0)
    assert gov.state("rpc.example") == DOWN and metrics.counters["provider_circuit_trips"] == 1
    sent = calls
    with pytest.raises(ProviderUnavailableError) as e:
        await client.get_json("https://rpc.example/x", retries=0)
    assert calls == sent and e.value.reason == "circuit open" and e.value.retry_after_s == 30.0
    # half-open after open_s: exactly one probe goes through; a failing probe re-opens longer
    ft.t += 31
    with pytest.raises(HttpError):
        await client.get_json("https://rpc.example/x", retries=0)
    assert calls == sent + 1 and gov.state("rpc.example") == DOWN
    assert gov.cooldown_remaining("rpc.example") == pytest.approx(60.0)
    ft.t += 61
    healthy = True
    await client.get_json("https://rpc.example/x", retries=0)
    assert gov.state("rpc.example") == HEALTHY and gov.health()["solana-rpc"]["circuit_trips"] == 2
    await client.aclose()


async def test_two_transient_failures_mark_degraded_without_tripping() -> None:
    codes = [500, 500, 200]

    def handler(request: httpx.Request) -> httpx.Response:
        code = codes.pop(0)
        return httpx.Response(code, json={} if code == 200 else None)

    client, gov, _metrics, _ft = make(handler, HostPolicy(rate_per_s=100, burst=100, trip_after=5))
    with pytest.raises(HttpError):
        await client.get_json("https://rpc.example/x", retries=1)
    assert gov.state("rpc.example") == DEGRADED
    await client.get_json("https://rpc.example/x", retries=0)
    assert gov.state("rpc.example") == HEALTHY
    await client.aclose()


async def test_concurrency_cap_and_waiting_limit() -> None:
    inflight = 0
    peak = 0
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await release.wait()
        inflight -= 1
        return httpx.Response(200, json={})

    policy = HostPolicy(rate_per_s=1000, burst=1000, max_concurrent=2, max_waiting=3)
    metrics = Metrics()
    gov = ProviderGovernor(metrics=metrics)  # real clock; nothing sleeps here
    gov.register("rpc.example", "solana-rpc", policy)
    client = HttpClient(transport=httpx.MockTransport(handler), metrics=metrics, governor=gov)
    tasks = [
        asyncio.create_task(client.get_json("https://rpc.example/x", retries=0)) for _ in range(5)
    ]
    await asyncio.sleep(0.05)
    assert peak == 2 and gov.health()["solana-rpc"]["waiting"] == 3
    with pytest.raises(ProviderUnavailableError) as e:  # 6th caller: queue full, fail fast
        await client.get_json("https://rpc.example/x", retries=0)
    assert e.value.reason == "too many requests waiting"
    release.set()
    results = await asyncio.gather(*tasks)
    assert len(results) == 5 and peak == 2
    await client.aclose()


async def test_pacing_spaces_requests_by_rate() -> None:
    client, _gov, _metrics, ft = make(
        lambda r: httpx.Response(200, json={}), HostPolicy(rate_per_s=2.0, burst=1)
    )
    for _ in range(4):
        await client.get_json("https://rpc.example/x", retries=0)
    assert ft.sleeps == pytest.approx([0.5, 0.5, 0.5])
    await client.aclose()


async def test_provider_hint_does_not_override_configured_policy() -> None:
    client, gov, _, _ = make(lambda r: httpx.Response(200, json={}))
    client.set_rate_limit("rpc.example", rate_per_s=0.1, burst=1)  # provider default hint
    assert gov.policy_for("rpc.example").rate_per_s == 100  # configured policy wins
    client.set_rate_limit("other.example", rate_per_s=0.1, burst=1)
    assert gov.policy_for("other.example").rate_per_s == 0.1  # unknown host: hint applies
    await client.aclose()


def test_log_throttle_reports_first_then_summarises() -> None:
    t = [0.0]
    th = LogThrottle(window_s=60.0, now=lambda: t[0])
    first = th.hit("rpc:429")
    assert first.log and first.suppressed == 0 and first.total == 1
    for _ in range(9):
        assert not th.hit("rpc:429").log
    t[0] = 61.0
    later = th.hit("rpc:429")
    assert later.log and later.suppressed == 9 and later.total == 11
    assert th.hit("other").log


async def test_rate_limit_summary_is_aggregated_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    client, _gov, metrics, ft = make(
        lambda r: httpx.Response(429),
        HostPolicy(rate_per_s=100, burst=100, fast_fail_wait_s=0.0, cooldown_max_s=2.0),
    )
    import logging

    caplog.set_level(logging.WARNING)
    for _ in range(20):
        with pytest.raises((RateLimitedError, ProviderUnavailableError)):
            await client.get_json("https://rpc.example/x", retries=0)
        ft.t += 2.5  # cooldown (capped at 2 s) expired each time, all inside one window
    lines = [r for r in caplog.records if "provider_rate_limited" in r.getMessage()]
    assert 1 <= len(lines) <= 4, [r.getMessage() for r in lines]
    assert metrics.counters["rate_limited"] == 20
