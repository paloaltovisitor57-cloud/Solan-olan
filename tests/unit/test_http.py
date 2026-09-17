from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from solana_sniper.infra.http import (
    HttpClient,
    HttpError,
    MalformedResponseError,
    RateLimitedError,
    TokenBucket,
)
from solana_sniper.telemetry.metrics import Metrics


def make_client(handler: object) -> HttpClient:
    return HttpClient(transport=httpx.MockTransport(handler), metrics=Metrics())  # type: ignore[arg-type]


async def test_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(503, text="down")
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr("solana_sniper.infra.http.asyncio.sleep", _no_sleep)
    client = make_client(handler)
    res = await client.get_json("https://example.com/x", retries=2)
    assert res.json == {"ok": True}
    assert calls == 3
    await client.aclose()


async def test_gives_up_after_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("solana_sniper.infra.http.asyncio.sleep", _no_sleep)
    client = make_client(lambda r: httpx.Response(500))
    with pytest.raises(HttpError) as exc:
        await client.get_json("https://example.com/x", retries=1)
    assert exc.value.retryable and exc.value.status == 500
    await client.aclose()


async def test_rate_limit_honours_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr("solana_sniper.infra.http.asyncio.sleep", fake_sleep)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"retry-after": "2"})
        return httpx.Response(200, json=[])

    client = make_client(handler)
    res = await client.get_json("https://example.com/x", retries=1)
    assert res.json == []
    assert sleeps == [2.0]
    client2 = make_client(lambda r: httpx.Response(429))
    with pytest.raises(RateLimitedError):
        await client2.get_json("https://example.com/x", retries=0)
    await client.aclose()
    await client2.aclose()


async def test_client_errors_do_not_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, text="nope")

    client = make_client(handler)
    with pytest.raises(HttpError) as exc:
        await client.get_json("https://example.com/x", retries=3)
    assert exc.value.status == 404 and not exc.value.retryable
    assert calls == 1
    await client.aclose()


async def test_malformed_json() -> None:
    client = make_client(lambda r: httpx.Response(200, text="<html>"))
    with pytest.raises(MalformedResponseError):
        await client.get_json("https://example.com/x")
    await client.aclose()


async def test_timeout_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("solana_sniper.infra.http.asyncio.sleep", _no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    client = make_client(handler)
    with pytest.raises(HttpError) as exc:
        await client.get_json("https://example.com/x", retries=1)
    assert exc.value.retryable
    await client.aclose()


async def test_token_bucket_throttles() -> None:
    bucket = TokenBucket(rate_per_s=50, burst=2)
    started = time.monotonic()
    for _ in range(4):
        await bucket.acquire()
    elapsed = time.monotonic() - started
    assert elapsed >= 0.03  # two tokens needed refilling at 50/s


_REAL_SLEEP = asyncio.sleep


async def _no_sleep(_: float) -> None:
    await _REAL_SLEEP(0)
