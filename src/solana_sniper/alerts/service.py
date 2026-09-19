"""Turns engine events into alerts and fans them out to providers.

Push providers only receive alerts at or above the configured urgency floor.
"""

from __future__ import annotations

from decimal import Decimal

from solana_sniper.alerts.base import Alert, AlertProvider
from solana_sniper.domain.enums import FillProvenance, Urgency
from solana_sniper.domain.events import (
    AutonomyDisarmed,
    BuySignalCreated,
    ErrorOccurred,
    Event,
    MilestoneReached,
    PositionClosed,
    PositionOpened,
    SellSignalCreated,
    TransactionConfirmed,
    TransactionFailed,
    TransactionSent,
)
from solana_sniper.domain.money import q_display
from solana_sniper.telemetry.logging import get_logger

log = get_logger(__name__)

_ORDER = {Urgency.NORMAL: 0, Urgency.HIGH: 1, Urgency.URGENT: 2}


class AlertService:
    def __init__(self, min_push_urgency: Urgency = Urgency.HIGH) -> None:
        self._terminal: list[AlertProvider] = []
        self._push: list[AlertProvider] = []
        self._min_push = min_push_urgency
        self.sent = 0

    def add_terminal(self, provider: AlertProvider) -> None:
        self._terminal.append(provider)

    def add_push(self, provider: AlertProvider) -> None:
        self._push.append(provider)

    async def send(self, alert: Alert) -> None:
        self.sent += 1
        for p in self._terminal:
            await p.send(alert)
        if _ORDER[alert.urgency] >= _ORDER[self._min_push]:
            for p in self._push:
                await p.send(alert)

    async def handle(self, event: Event) -> None:
        alert = self.to_alert(event)
        if alert is not None:
            await self.send(alert)

    @staticmethod
    def to_alert(event: Event) -> Alert | None:
        if isinstance(event, BuySignalCreated):
            b = event.signal
            exit_val = b.immediate_exit_value_eur
            exit_txt = f"€{q_display(exit_val)}" if exit_val is not None else "?"
            return Alert(
                at=b.created_at,
                title=f"BUY SIGNAL {b.symbol or b.mint[:6]}",
                body=(
                    f"score {b.score.score:.0f} | size €{q_display(b.sizing.recommended_eur)} "
                    f"({b.sizing.recommended_sol:.4f} SOL) | age {b.token_age_s or 0:.0f}s | "
                    f"liq ${b.liquidity_usd or 0:,.0f} | exit now {exit_txt} | "
                    f"{'; '.join(b.score.top_reasons[:3])}"
                ),
                urgency=b.urgency,
                category="buy_signal",
            )
        if isinstance(event, SellSignalCreated):
            s = event.signal
            return Alert(
                at=s.created_at,
                title=f"SELL SIGNAL {s.symbol or s.mint[:6]} [{s.reason}]",
                body=(
                    f"value €{q_display(s.current_value_eur)} vs entry "
                    f"€{q_display(s.entry_value_eur)} (peak €{q_display(s.peak_value_eur)}) "
                    f"pnl {s.pnl_pct:+.0%} | {s.detail}"
                ),
                urgency=s.urgency,
                category="sell_signal",
            )
        if isinstance(event, PositionOpened):
            p = event.position
            return Alert(
                at=p.opened_at,
                title=f"OPENED {p.symbol or p.mint[:6]}",
                body=f"qty {p.quantity_ui:,.0f} cost €{q_display(p.cost_basis_eur)} "
                f"[{p.provenance}, {_verification(p.provenance)}]",
                urgency=Urgency.NORMAL,
                category="position",
            )
        if isinstance(event, PositionClosed):
            p = event.position
            pnl = p.realized_pnl_eur if p.realized_pnl_eur is not None else Decimal(0)
            exit_value = p.exit_value_eur if p.exit_value_eur is not None else Decimal(0)
            sign = "+" if pnl >= 0 else ""
            return Alert(
                at=p.closed_at or p.opened_at,
                title=f"CLOSED {p.symbol or p.mint[:6]} {sign}€{q_display(pnl)}",
                body=f"exit €{q_display(exit_value)} ({p.exit_reason}) pnl {p.pnl_pct:+.0%} "
                f"[{p.provenance}, {_verification(p.provenance)}]",
                urgency=Urgency.NORMAL,
                category="position",
            )
        if isinstance(event, MilestoneReached):
            m = event.milestone
            return Alert(
                at=m.reached_at,
                title=f"MILESTONE {m.direction} €{m.milestone_eur:,.0f}",
                body=f"equity €{q_display(m.equity_eur)}",
                urgency=Urgency.HIGH if m.direction == "UP" else Urgency.NORMAL,
                category="milestone",
            )
        if isinstance(event, ErrorOccurred):
            e = event.error
            return Alert(
                at=e.at,
                title=f"ERROR {e.component}",
                body=e.message,
                urgency=Urgency.NORMAL,
                category="error",
            )
        if isinstance(event, TransactionSent):
            i = event.intent
            return Alert(
                at=i.sent_at or i.updated_at,
                title=f"SENT {i.side} {i.symbol or i.mint[:6]} (real transaction)",
                body=(
                    f"signature {i.signature} | in {i.in_amount_raw} raw | "
                    f"expected out {i.expected_out_raw} raw | slippage {i.slippage_bps}bps | "
                    f"wallet {i.wallet_public_key[:8]}…"
                ),
                urgency=Urgency.HIGH,
                category="autonomy",
            )
        if isinstance(event, TransactionConfirmed):
            i, f = event.intent, event.fill
            return Alert(
                at=f.filled_at,
                title=f"CONFIRMED {i.side} {i.symbol or i.mint[:6]} on chain",
                body=(
                    f"{f.sol_amount} SOL ↔ {f.token_amount_ui:,.4f} tokens | fee €{f.fee_eur} | "
                    f"signature {f.tx_signature} | slot {i.slot}"
                ),
                urgency=Urgency.HIGH,
                category="autonomy",
            )
        if isinstance(event, TransactionFailed):
            i = event.intent
            return Alert(
                at=i.updated_at,
                title=f"{i.status} {i.side} {i.symbol or i.mint[:6]}",
                body=event.reason + (f" | signature {i.signature}" if i.signature else ""),
                urgency=Urgency.URGENT,
                category="autonomy",
            )
        if isinstance(event, AutonomyDisarmed):
            return Alert(
                at=event.at,
                title="AUTONOMY DISARMED",
                body=f"{event.reason}. No new buys until `solana-sniper arm` is run again.",
                urgency=Urgency.URGENT,
                category="autonomy",
            )
        return None


def _verification(provenance: FillProvenance) -> str:
    return (
        "verified on-chain"
        if provenance is FillProvenance.VERIFIED_ONCHAIN
        else "not verified on-chain"
    )
