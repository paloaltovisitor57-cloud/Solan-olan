"""Alert provider interface."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from solana_sniper.domain.enums import Urgency


@dataclass(frozen=True, slots=True)
class Alert:
    at: datetime
    title: str
    body: str
    urgency: Urgency
    category: str  # buy_signal | sell_signal | position | milestone | error | info


class AlertProvider(Protocol):
    name: str

    async def send(self, alert: Alert) -> None: ...
