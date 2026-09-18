from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from solana_sniper.discovery.pumpportal import PumpPortalClient, PumpPortalDiscovery
from solana_sniper.domain.clock import ManualClock
from solana_sniper.domain.models import TokenInfo, TradeEvent


class Conn:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self._messages = messages
        self.sent: list[dict[str, Any]] = []
        self.release = asyncio.Event()

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def close(self) -> None:
        return None

    def __aiter__(self) -> AsyncIterator[str]:
        return self._gen()

    async def _gen(self) -> AsyncIterator[str]:
        for m in self._messages[:2]:
            yield json.dumps(m)
        await self.release.wait()
        for m in self._messages[2:]:
            yield json.dumps(m)
        await asyncio.Event().wait()


CREATE = {
    "txType": "create",
    "mint": "M1",
    "symbol": "A",
    "name": "A",
    "pool": "pump",
    "bondingCurveKey": "c",
}
TRADE = {
    "txType": "buy",
    "mint": "M1",
    "solAmount": 0.1,
    "tokenAmount": 10.0,
    "traderPublicKey": "t",
    "signature": "s1",
    "vSolInBondingCurve": 31.0,
    "vTokensInBondingCurve": 1000.0,
}


async def test_create_before_discovery_runs_is_buffered_and_subscribed(clock: ManualClock) -> None:
    conn = Conn([{"message": "subscribed"}, CREATE, TRADE])

    async def connect(url: str) -> Any:
        return conn

    client = PumpPortalClient("wss://x", clock, ws_connect=connect)
    discovery = PumpPortalDiscovery(client)  # registers before the socket exists
    ws_task = asyncio.create_task(client.run())
    await asyncio.sleep(0.05)  # socket connects and delivers the create before run(emit) is called
    assert {"method": "subscribeNewToken"} in conn.sent
    got: list[TokenInfo] = []

    async def emit(t: TokenInfo) -> None:
        got.append(t)

    disc_task = asyncio.create_task(discovery.run(emit))
    await asyncio.sleep(0.05)
    assert [t.mint for t in got] == ["M1"]  # buffered create flushed
    trades: list[TradeEvent] = []

    async def on_trade(tr: TradeEvent) -> None:
        trades.append(tr)

    client.on_trade(on_trade)
    await client.subscribe_trades(["M1"])
    assert any(
        m.get("method") == "subscribeTokenTrade" and m.get("keys") == ["M1"] for m in conn.sent
    )
    conn.release.set()
    await asyncio.sleep(0.05)
    assert len(trades) == 1 and trades[0].price_native is not None
    await client.unsubscribe_trades(["M1", "unknown"])
    assert conn.sent[-1] == {"method": "unsubscribeTokenTrade", "keys": ["M1"]}
    for t in (ws_task, disc_task):
        t.cancel()
    await asyncio.gather(ws_task, disc_task, return_exceptions=True)


async def test_late_handler_subscribes_on_live_socket(clock: ManualClock) -> None:
    conn = Conn([{"message": "hi"}, {"message": "hi2"}])

    async def connect(url: str) -> Any:
        return conn

    client = PumpPortalClient("wss://x", clock, ws_connect=connect)
    ws_task = asyncio.create_task(client.run())
    await asyncio.sleep(0.05)
    assert conn.sent == []  # nothing wanted yet

    async def emit(t: TokenInfo) -> None:
        return None

    client.on_new_token(emit)  # registered after connect: must subscribe immediately
    await asyncio.sleep(0.05)
    assert conn.sent == [{"method": "subscribeNewToken"}]
    ws_task.cancel()
    await asyncio.gather(ws_task, return_exceptions=True)
