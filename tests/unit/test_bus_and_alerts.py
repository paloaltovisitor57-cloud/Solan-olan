from __future__ import annotations

import asyncio
from decimal import Decimal

from solana_sniper.alerts.base import Alert
from solana_sniper.alerts.service import AlertService
from solana_sniper.alerts.terminal import TerminalAlertProvider
from solana_sniper.app.bus import EventBus
from solana_sniper.domain.clock import ManualClock
from solana_sniper.domain.enums import ExitReason, SignalKind, Urgency
from solana_sniper.domain.events import (
    ErrorOccurred,
    Event,
    LogLine,
    MilestoneReached,
    PositionClosed,
    PositionOpened,
    SnapshotObserved,
)
from solana_sniper.domain.models import ErrorRecord, MarketSnapshot, MilestoneEvent
from solana_sniper.portfolio.accounting import PortfolioAccount
from tests.conftest import make_fill


async def test_bus_delivers_and_isolates_errors(clock: ManualClock) -> None:
    bus = EventBus(queue_size=100)
    got: list[Event] = []

    async def good(e: Event) -> None:
        got.append(e)

    async def bad(e: Event) -> None:
        raise RuntimeError("subscriber bug")

    bus.subscribe("good", good)
    bus.subscribe("bad", bad)
    bus.start()
    for i in range(5):
        bus.publish(LogLine(at=clock.now(), level="INFO", message=str(i)))
    await bus.drain()
    assert len(got) == 5 and bus.published == 5
    await bus.stop()


async def test_bus_drops_snapshots_but_keeps_important_events(clock: ManualClock) -> None:
    bus = EventBus(queue_size=3)
    got: list[Event] = []

    async def slow(e: Event) -> None:
        got.append(e)

    bus.subscribe("slow", slow)
    snap = SnapshotObserved(MarketSnapshot(mint="m", observed_at=clock.now(), source="s"))
    for _ in range(5):
        bus.publish(snap)  # queue holds 3, two are dropped
    assert bus.dropped == 2
    important = LogLine(at=clock.now(), level="WARN", message="keep me")
    bus.publish(important)  # evicts a snapshot to make room
    bus.start()
    await bus.drain()
    assert important in got and sum(isinstance(e, SnapshotObserved) for e in got) == 2
    await bus.stop()


async def test_alert_mapping_and_urgency_floor(clock: ManualClock) -> None:
    terminal_alerts: list[Alert] = []
    push_alerts: list[Alert] = []

    class Push:
        name = "push"

        async def send(self, alert: Alert) -> None:
            push_alerts.append(alert)

    svc = AlertService(min_push_urgency=Urgency.HIGH)
    svc.add_terminal(TerminalAlertProvider(sink=terminal_alerts.append))
    svc.add_push(Push())
    account = PortfolioAccount(clock)
    account.deposit(Decimal(50))
    pos = account.open_position(
        make_fill(clock, side=SignalKind.BUY, eur_amount=Decimal(10)),
        symbol="TST",
        entry_price_native=Decimal(1),
    )
    await svc.handle(PositionOpened(pos))
    closed = account.close_position(
        pos.position_id,
        make_fill(clock, side=SignalKind.SELL, eur_amount=Decimal(15)),
        ExitReason.TRAILING_PEAK,
    )
    await svc.handle(PositionClosed(closed))
    await svc.handle(
        MilestoneReached(MilestoneEvent(Decimal(100), Decimal(101), clock.now(), "UP"))
    )
    await svc.handle(ErrorOccurred(ErrorRecord(at=clock.now(), component="x", message="bad")))
    await svc.handle(LogLine(at=clock.now(), level="INFO", message="ignored"))
    assert [a.category for a in terminal_alerts] == ["position", "position", "milestone", "error"]
    assert terminal_alerts[1].title.startswith("CLOSED TST +€4.90")
    assert [a.category for a in push_alerts] == ["milestone"]  # only HIGH+ reaches push
    assert svc.sent == 4


async def test_terminal_provider_prints_without_sink(capsys: object) -> None:
    from rich.console import Console

    console = Console(file=__import__("io").StringIO(), force_terminal=False)
    provider = TerminalAlertProvider(console=console)
    await provider.send(
        Alert(at=ManualClock().now(), title="T", body="B", urgency=Urgency.URGENT, category="x")
    )
    out = console.file.getvalue()  # type: ignore[attr-defined]
    assert "!!! T" in out and "B" in out
    await asyncio.sleep(0)
