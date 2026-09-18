"""Reconnecting WebSocket client with exponential backoff and on-connect hooks."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from solana_sniper.infra.backoff import Backoff
from solana_sniper.telemetry.logging import get_logger
from solana_sniper.telemetry.metrics import Metrics
from solana_sniper.telemetry.redaction import safe_exception

log = get_logger(__name__)

OnConnect = Callable[["ReconnectingWebSocket"], Awaitable[None]]


class ReconnectingWebSocket:
    """Yields decoded JSON messages forever; reconnects with backoff on any failure.

    Consumers call `messages()`; `on_connect` re-sends subscriptions after each (re)connect.
    """

    def __init__(
        self,
        url: str,
        *,
        name: str,
        on_connect: OnConnect | None = None,
        min_backoff_s: float = 1.0,
        max_backoff_s: float = 30.0,
        ping_interval_s: float = 20.0,
        metrics: Metrics | None = None,
        connect: Callable[[str], Awaitable[ClientConnection]] | None = None,
    ) -> None:
        self.url = url
        self.name = name
        self._on_connect = on_connect
        self._backoff = Backoff(min_backoff_s, max_backoff_s)
        self._ping_interval = ping_interval_s
        self._metrics = metrics
        self._conn: ClientConnection | None = None
        self._connect = connect or self._default_connect
        self.connected = asyncio.Event()
        self.reconnects = 0

    async def _default_connect(self, url: str) -> ClientConnection:
        return await websockets.connect(
            url,
            ping_interval=self._ping_interval,
            ping_timeout=self._ping_interval,
            max_size=4 * 1024 * 1024,
            open_timeout=10,
        )

    async def send_json(self, payload: dict[str, Any]) -> None:
        if self._conn is None:
            raise ConnectionError(f"{self.name}: not connected")
        await self._conn.send(json.dumps(payload))

    async def messages(self) -> AsyncIterator[Any]:
        first = True
        while True:
            try:
                conn = await self._connect(self.url)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                delay = self._backoff.next_delay()
                log.warning(
                    "ws_connect_failed",
                    ws=self.name,
                    error=safe_exception(exc),
                    retry_in_s=round(delay, 1),
                )
                if self._metrics:
                    self._metrics.inc("provider_errors")
                await asyncio.sleep(delay)
                continue
            self._conn = conn
            self.connected.set()
            if not first:
                self.reconnects += 1
                if self._metrics:
                    self._metrics.inc("ws_reconnects")
            first = False
            log.info("ws_connected", ws=self.name, reconnects=self.reconnects)
            try:
                if self._on_connect is not None:
                    await self._on_connect(self)
                self._backoff.reset()
                async for raw in conn:
                    try:
                        yield json.loads(raw)
                    except (ValueError, TypeError):
                        log.warning("ws_malformed_message", ws=self.name, sample=str(raw)[:120])
                        continue
            except asyncio.CancelledError:
                await self._close_quietly(conn)
                raise
            except Exception as exc:
                log.warning("ws_disconnected", ws=self.name, error=safe_exception(exc))
            finally:
                self.connected.clear()
                self._conn = None
                await self._close_quietly(conn)
            delay = self._backoff.next_delay()
            log.info("ws_reconnecting", ws=self.name, delay_s=round(delay, 1))
            await asyncio.sleep(delay)

    @staticmethod
    async def _close_quietly(conn: ClientConnection) -> None:
        with contextlib.suppress(Exception):
            await conn.close()
