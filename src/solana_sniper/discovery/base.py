"""Token discovery provider interface."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from solana_sniper.domain.models import TokenInfo

EmitToken = Callable[[TokenInfo], Awaitable[None]]


class TokenDiscoveryProvider(Protocol):
    """Runs until cancelled, calling `emit` for every newly seen token. Must handle its own
    reconnects/backoff and never raise for transient failures."""

    name: str

    async def run(self, emit: EmitToken) -> None: ...


class PollingDiscoveryProvider(Protocol):
    """Simpler contract for REST providers: DiscoveryService drives the polling loop."""

    name: str
    poll_interval_s: float

    async def poll(self) -> list[TokenInfo]: ...
