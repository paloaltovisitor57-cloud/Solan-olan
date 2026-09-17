"""PumpPortal WebSocket client (wss://pumpportal.fun/api/data). No API key required.

One connection serves both new-token discovery (`subscribeNewToken`) and per-token trade
streams (`subscribeTokenTrade`), as PumpPortal asks clients to use a single socket.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from solana_sniper.discovery.base import EmitToken
from solana_sniper.discovery.parsing import as_decimal, as_dict, as_str
from solana_sniper.domain.clock import Clock
from solana_sniper.domain.enums import Venue
from solana_sniper.domain.models import TokenInfo, TradeEvent
from solana_sniper.infra.websocket import ReconnectingWebSocket
from solana_sniper.telemetry.logging import get_logger
from solana_sniper.telemetry.metrics import Metrics

log = get_logger(__name__)

PUMP_INITIAL_VIRTUAL_SOL = Decimal("30")
PUMP_TOKEN_DECIMALS = 6

TradeHandler = Callable[[TradeEvent], Awaitable[None]]


def parse_new_token(msg: dict[str, Any], now: datetime) -> TokenInfo | None:
    if msg.get("txType") != "create":
        return None
    mint = as_str(msg.get("mint"))
    if mint is None:
        return None
    pool = as_str(msg.get("pool")) or "pump"
    return TokenInfo(
        mint=mint,
        symbol=as_str(msg.get("symbol")),
        name=as_str(msg.get("name")),
        decimals=PUMP_TOKEN_DECIMALS,
        created_at=now,
        pool_created_at=now,
        first_liquidity_at=now,
        venue=Venue.PUMP_FUN if pool == "pump" else Venue.PUMP_SWAP,
        pool_address=as_str(msg.get("bondingCurveKey")),
        quote_mint="So11111111111111111111111111111111111111112",
        source="pumpportal",
        discovered_at=now,
        metadata_uri=as_str(msg.get("uri")),
    )


def parse_trade(msg: dict[str, Any], now: datetime) -> TradeEvent | None:
    tx_type = msg.get("txType")
    if tx_type not in ("buy", "sell"):
        return None
    mint = as_str(msg.get("mint"))
    sol_amount = as_decimal(msg.get("solAmount"))
    token_amount = as_decimal(msg.get("tokenAmount"))
    if mint is None or sol_amount is None or token_amount is None or token_amount <= 0:
        return None
    v_sol = as_decimal(msg.get("vSolInBondingCurve"))
    v_tok = as_decimal(msg.get("vTokensInBondingCurve"))
    price = None
    if v_sol is not None and v_tok is not None and v_tok > 0:
        price = v_sol / v_tok
    elif sol_amount > 0:
        price = sol_amount / token_amount
    return TradeEvent(
        mint=mint,
        observed_at=now,
        source="pumpportal",
        is_buy=tx_type == "buy",
        sol_amount=sol_amount,
        token_amount=token_amount,
        trader=as_str(msg.get("traderPublicKey")),
        signature=as_str(msg.get("signature")),
        price_native=price,
        market_cap_sol=as_decimal(msg.get("marketCapSol")),
        pool_sol_reserves=v_sol,
        pool_token_reserves=v_tok,
    )


class PumpPortalClient:
    """Owns the socket; fans out create/trade messages to discovery and market-data consumers."""

    name = "pumpportal"
    kind = "websocket"

    def __init__(
        self,
        url: str,
        clock: Clock,
        *,
        metrics: Metrics | None = None,
        min_backoff_s: float = 1.0,
        max_backoff_s: float = 30.0,
        ws_factory: Callable[..., ReconnectingWebSocket] | None = None,
    ) -> None:
        self._clock = clock
        self._metrics = metrics
        self._token_handlers: list[EmitToken] = []
        self._trade_handlers: list[TradeHandler] = []
        self._subscribed: set[str] = set()
        self._want_new_tokens = False
        self._pending_ops: asyncio.Queue[tuple[str, list[str]]] = asyncio.Queue()
        factory = ws_factory or ReconnectingWebSocket
        self._ws = factory(
            url,
            name="pumpportal",
            on_connect=self._resubscribe,
            min_backoff_s=min_backoff_s,
            max_backoff_s=max_backoff_s,
            metrics=metrics,
        )
        self._task: asyncio.Task[None] | None = None
        self.messages_seen = 0
        self._last_message_at: datetime | None = None

    def is_connected(self) -> bool:
        return self._ws.connected.is_set()

    def last_activity(self) -> datetime | None:
        return self._last_message_at

    def on_new_token(self, handler: EmitToken) -> None:
        self._token_handlers.append(handler)
        self._want_new_tokens = True

    def on_trade(self, handler: TradeHandler) -> None:
        self._trade_handlers.append(handler)

    async def subscribe_trades(self, mints: list[str]) -> None:
        new = [m for m in mints if m not in self._subscribed]
        if not new:
            return
        self._subscribed.update(new)
        await self._send({"method": "subscribeTokenTrade", "keys": new})

    async def unsubscribe_trades(self, mints: list[str]) -> None:
        gone = [m for m in mints if m in self._subscribed]
        if not gone:
            return
        self._subscribed.difference_update(gone)
        await self._send({"method": "unsubscribeTokenTrade", "keys": gone})

    async def _send(self, payload: dict[str, Any]) -> None:
        if not self._ws.connected.is_set():
            return  # _resubscribe will replay the full subscription set on connect
        try:
            await self._ws.send_json(payload)
        except Exception as exc:
            log.warning("pumpportal_send_failed", error=str(exc))

    async def _resubscribe(self, ws: ReconnectingWebSocket) -> None:
        if self._want_new_tokens:
            await ws.send_json({"method": "subscribeNewToken"})
        if self._subscribed:
            await ws.send_json({"method": "subscribeTokenTrade", "keys": sorted(self._subscribed)})

    async def run(self) -> None:
        async for raw in self._ws.messages():
            msg = as_dict(raw)
            if msg is None:
                continue
            self.messages_seen += 1
            self._last_message_at = datetime.now(tz=UTC)
            await self.dispatch(msg)

    async def dispatch(self, msg: dict[str, Any]) -> None:
        now = self._clock.now()
        if "errors" in msg or ("message" in msg and "mint" not in msg):
            log.debug("pumpportal_info", payload=str(msg)[:200])
            return
        tx_type = msg.get("txType")
        if tx_type == "create":
            token = parse_new_token(msg, now)
            if token is None:
                return
            for handler in self._token_handlers:
                await handler(token)
        elif tx_type in ("buy", "sell"):
            trade = parse_trade(msg, now)
            if trade is None or trade.mint not in self._subscribed:
                return
            for th in self._trade_handlers:
                await th(trade)


class PumpPortalDiscovery:
    name = "pumpportal"

    def __init__(self, client: PumpPortalClient) -> None:
        self._client = client

    async def run(self, emit: EmitToken) -> None:
        self._client.on_new_token(emit)
        # The shared client is run by the engine; discovery just registers and waits.
        await asyncio.Event().wait()
