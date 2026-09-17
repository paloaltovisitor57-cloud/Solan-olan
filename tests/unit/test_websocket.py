from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from solana_sniper.infra.websocket import ReconnectingWebSocket
from solana_sniper.telemetry.metrics import Metrics


class FakeConn:
    def __init__(self, messages: list[str], fail_after: bool) -> None:
        self._messages = messages
        self._fail_after = fail_after
        self.sent: list[str] = []
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self) -> AsyncIterator[str]:
        return self._gen()

    async def _gen(self) -> AsyncIterator[str]:
        for m in self._messages:
            yield m
        if self._fail_after:
            raise ConnectionResetError("boom")


async def test_reconnects_and_resubscribes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("solana_sniper.infra.websocket.asyncio.sleep", _no_sleep)
    conns = [
        FakeConn([json.dumps({"n": 1}), "not json"], fail_after=True),
        FakeConn([json.dumps({"n": 2})], fail_after=False),
        FakeConn([json.dumps({"n": 3})], fail_after=True),
    ]
    attempts = 0

    async def connect(url: str) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise OSError("connect refused")
        return conns.pop(0)

    subscribed: list[int] = []

    async def on_connect(ws: ReconnectingWebSocket) -> None:
        subscribed.append(1)
        await ws.send_json({"method": "subscribe"})

    metrics = Metrics()
    ws = ReconnectingWebSocket(
        "wss://x", name="t", on_connect=on_connect, metrics=metrics, connect=connect
    )  # type: ignore[arg-type]
    received: list[Any] = []
    async for msg in ws.messages():
        received.append(msg)
        if len(received) == 3:
            break
    assert received == [{"n": 1}, {"n": 2}, {"n": 3}]
    assert len(subscribed) == 3
    assert ws.reconnects == 2
    assert metrics.counters["ws_reconnects"] == 2
    assert metrics.counters["provider_errors"] >= 1


async def test_send_without_connection_raises() -> None:
    ws = ReconnectingWebSocket("wss://x", name="t")
    with pytest.raises(ConnectionError):
        await ws.send_json({})


_REAL_SLEEP = asyncio.sleep


async def _no_sleep(_: float) -> None:
    await _REAL_SLEEP(0)
