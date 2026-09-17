"""Dashboard rendering and fast-confirm command handling against a live synthetic engine."""

from __future__ import annotations

from decimal import Decimal
from io import StringIO

from rich.console import Console

from solana_sniper.alerts.base import Alert
from solana_sniper.cli.commands import CommandHandler, StdinReader
from solana_sniper.cli.dashboard import Dashboard
from solana_sniper.domain.enums import CandidateState as S
from solana_sniper.domain.enums import SignalKind, Urgency
from tests.integration.conftest import Harness


async def test_dashboard_renders_and_commands_confirm(harness: Harness) -> None:
    engine = harness.engine
    # disable auto confirmation so a human command is required
    ex = engine.d.execution
    ex._auto_buys = False  # type: ignore[attr-defined]
    ex._auto_sells = False  # type: ignore[attr-defined]
    said: list[str] = []
    quit_called: list[bool] = []
    handler = CommandHandler(engine, lambda: quit_called.append(True), said.append)
    console = Console(file=StringIO(), width=160, height=70, force_terminal=False)
    dashboard = Dashboard(engine, harness.runtime.settings.dashboard, console)
    dashboard.push_alert(
        Alert(at=harness.clock.now(), title="T", body="B", urgency=Urgency.URGENT, category="x")
    )
    dashboard.push_message("hello")
    for _ in range(400):
        await harness.step(0.5)
        if ex.pending():
            break
    pending = ex.pending()
    assert pending and pending[0].kind is SignalKind.BUY
    ref = pending[0].ref
    console.print(dashboard.render())
    out = console.file.getvalue()  # type: ignore[attr-defined]
    assert (
        "PENDING CONFIRMATIONS" in out
        and f"[b {ref}]" in out
        and "BANKROLL" in out
        and "hello" in out
    )
    # bad input never crashes
    await handler.handle("")
    await handler.handle("zzz")
    await handler.handle("b notanumber")
    await handler.handle("b 99")
    assert any("unknown command" in s for s in said) and any(
        "no pending BUY #99" in s for s in said
    )
    await handler.handle("c")
    await handler.handle("p")
    await handler.handle("h")
    # confirm with reported actual amounts
    await handler.handle(f"b {ref} 0.05 100000 5sig")
    assert any(f"BUY #{ref} confirmed" in s for s in said), said
    pos = harness.runtime.account.open_positions[0]
    assert pos.quantity == Decimal("100000") and pos.entry_sol == Decimal("0.05")
    cand = engine.candidates[pos.mint]
    assert cand.state is S.OPEN
    # drive until a sell signal appears, then ignore it and confirm the next one
    for _ in range(1200):
        await harness.step(0.5)
        if any(o.kind is SignalKind.SELL for o in ex.pending()):
            break
    sells = [o for o in ex.pending() if o.kind is SignalKind.SELL]
    assert sells, f"no sell signal; state={cand.state}"
    console.print(dashboard.render())
    assert f"[s {sells[0].ref}]" in console.file.getvalue()  # type: ignore[attr-defined]
    await handler.handle(f"i {sells[0].ref}")
    assert cand.state is S.OPEN and pos.is_open
    for _ in range(1200):
        await harness.step(0.5)
        if any(o.kind is SignalKind.SELL for o in ex.pending()):
            break
    sells = [o for o in ex.pending() if o.kind is SignalKind.SELL]
    assert sells
    await handler.handle(f"s {sells[0].ref}")
    assert pos.state is S.CLOSED and any("confirmed" in s for s in said[-2:])
    await handler.handle("q")
    assert quit_called
    reader = StdinReader()
    assert reader.queue.empty()


async def test_reject_buy_cools_down(harness: Harness) -> None:
    engine = harness.engine
    ex = engine.d.execution
    ex._auto_buys = False  # type: ignore[attr-defined]
    said: list[str] = []
    handler = CommandHandler(engine, lambda: None, said.append)
    for _ in range(400):
        await harness.step(0.5)
        if ex.pending():
            break
    order = ex.pending()[0]
    cand = engine.candidates[order.mint]
    await handler.handle(f"r {order.ref}")
    assert cand.state is S.SIGNAL_CANCELLED and cand.cooldown_until is not None
    assert not harness.runtime.account.open_positions
    assert engine.d.metrics.counters["signals_rejected"] == 1
    # after the cooldown the candidate may re-qualify but never with a duplicate live signal
    for _ in range(300):
        await harness.step(0.5)
        buys = [o.mint for o in ex.pending() if o.kind is SignalKind.BUY]
        assert len(buys) == len(set(buys))
