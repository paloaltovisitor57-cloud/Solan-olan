"""Execution interface.

The engine itself never signs or broadcasts; only the autonomous adapter (`execution/autonomous.py`,
through the wallet package) does, and only in autonomous mode. An ExecutionInterface turns a
signal into a *pending order*
that a human confirms (or a dry-run auto-confirms). Confirming yields a Fill that the portfolio
records. A future adapter (e.g. wallet-connected) implements this same interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from solana_sniper.domain.enums import DecisionKind, DecisionSource, SignalKind, SignalStatus
from solana_sniper.domain.models import BuySignal, Fill, ManualDecision, SellSignal


@dataclass(slots=True)
class PendingOrder:
    ref: int  # short reference used by the CLI: `b 1`, `s 2`
    order_id: str
    kind: SignalKind
    mint: str
    symbol: str | None
    created_at: datetime
    expires_at: datetime
    buy: BuySignal | None = None
    sell: SellSignal | None = None
    status: SignalStatus = SignalStatus.PENDING

    @property
    def signal_id(self) -> str:
        return self.buy.signal_id if self.buy else (self.sell.signal_id if self.sell else "")

    @property
    def position_id(self) -> str | None:
        return self.sell.position_id if self.sell else None


@dataclass(frozen=True, slots=True)
class Resolution:
    order: PendingOrder
    decision: ManualDecision
    fill: Fill | None


@dataclass(frozen=True, slots=True)
class FillOverride:
    """Amounts a human reports after executing in their own wallet (UI token units, as the wallet
    shows them). A reported signature is stored verbatim and never treated as verification."""

    sol_amount: Decimal | None = None
    token_amount_ui: Decimal | None = None
    reported_tx_signature: str | None = None


class ExecutionInterface(Protocol):
    name: str
    simulated: bool

    async def submit_buy(self, signal: BuySignal) -> PendingOrder: ...

    async def submit_sell(self, signal: SellSignal) -> PendingOrder: ...

    def pending(self) -> list[PendingOrder]: ...

    def find(self, kind: SignalKind, ref: int) -> PendingOrder | None: ...

    async def decide(
        self,
        order: PendingOrder,
        kind: DecisionKind,
        source: DecisionSource,
        *,
        override: FillOverride | None = None,
        note: str = "",
    ) -> Resolution: ...

    async def expire_stale(self, now: datetime) -> list[Resolution]: ...
