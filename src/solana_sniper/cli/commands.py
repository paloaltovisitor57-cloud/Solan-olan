"""Fast manual confirmation: `b 1` confirms BUY #1, `s 2` confirms SELL #2, etc.

Reads stdin in a thread so the asyncio loop never blocks. `kill`, `disarm` and `resume` work in
every mode (they only write the marker files) so `./cmd.sh kill` reaches a headless service.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from pathlib import Path

from solana_sniper.app import arming
from solana_sniper.app.engine import Engine
from solana_sniper.execution.base import FillOverride

HELP = (
    "commands: b N [sol tokens [sig]] confirm buy | r N reject buy | "
    "s N [sol [sig]] confirm sell | i N ignore sell | p positions | c candidates | "
    "kill stop buys+sells | disarm [reason] stop buys | resume lift KILL | a arming state | "
    "q quit | h help"
)


class StdinReader:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._thread = threading.Thread(target=self._pump, name="stdin-reader", daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        assert self._loop is not None
        try:
            for line in sys.stdin:
                self._loop.call_soon_threadsafe(self.queue.put_nowait, line.strip())
        except (ValueError, OSError):
            return


def _parse_decimal(value: str) -> Decimal | None:
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


class CommandHandler:
    def __init__(
        self, engine: Engine, on_quit: Callable[[], None], say: Callable[[str], None]
    ) -> None:
        self._engine = engine
        self._on_quit = on_quit
        self._say = say

    async def handle(self, line: str) -> None:
        parts = line.split()
        if not parts:
            return
        cmd, args = parts[0].lower(), parts[1:]
        try:
            if cmd in ("q", "quit", "exit"):
                self._say("shutting down...")
                self._on_quit()
            elif cmd in ("h", "help", "?"):
                self._say(HELP)
            elif cmd == "b":
                ref = _ref(args)
                if ref is None:
                    self._say(
                        "usage: b N [sol_spent tokens_received_ui [tx_signature]] (recorded as user-reported, unverified)"
                    )
                    return
                override = _buy_override(args[1:])
                self._say(await self._engine.confirm_buy(ref, override))
            elif cmd == "r":
                ref = _ref(args)
                self._say(await self._engine.reject_buy(ref) if ref is not None else "usage: r N")
            elif cmd == "s":
                ref = _ref(args)
                if ref is None:
                    self._say("usage: s N [sol_received [tx_signature]]")
                    return
                override = _sell_override(args[1:])
                self._say(await self._engine.confirm_sell(ref, override))
            elif cmd == "i":
                ref = _ref(args)
                self._say(await self._engine.ignore_sell(ref) if ref is not None else "usage: i N")
            elif cmd == "p":
                for p in self._engine.open_positions():
                    self._say(
                        f"{p.symbol or p.mint[:8]} qty={p.quantity_ui:,.0f} cost=€{p.cost_basis_eur:.2f} "
                        f"value=€{p.current_value_eur:.2f} pnl={p.pnl_pct:+.0%} [{p.provenance}]"
                    )
                if not self._engine.open_positions():
                    self._say("no open positions")
            elif cmd == "c":
                cands = sorted(
                    (c for c in self._engine.candidates.values() if not c.sm.is_terminal),
                    key=lambda c: c.score.score if c.score else -1,
                    reverse=True,
                )
                for c in cands[:10]:
                    self._say(
                        f"{c.symbol or c.mint[:8]} {c.state} score={c.score.score if c.score else 0:.0f} {'; '.join(c.gate_reasons[:2])}"
                    )
                if not cands:
                    self._say("no candidates")
            elif cmd == "kill":
                state = arming.kill(self._home())
                self._say(f"KILL switch set: {state.describe()} (lift with `resume`)")
            elif cmd == "disarm":
                reason = " ".join(args) or "disarmed by operator"
                state = arming.disarm(self._home(), reason)
                self._say(f"{state.describe()}: no new buys, exits continue")
            elif cmd == "resume":
                state = arming.resume(self._home())
                self._say(f"KILL switch removed: {state.describe()}")
            elif cmd == "a":
                self._say(f"autonomy: {arming.read(self._home()).describe()}")
            else:
                self._say(f"unknown command '{cmd}'. {HELP}")
        except Exception as exc:
            self._say(f"command failed: {exc}")

    def _home(self) -> Path:
        return self._engine.settings.home or Path("data")

    async def run(self, reader: StdinReader) -> None:
        while True:
            line = await reader.queue.get()
            await self.handle(line)


def _ref(args: list[str]) -> int | None:
    if not args:
        return None
    try:
        return int(args[0])
    except ValueError:
        return None


def _buy_override(rest: list[str]) -> FillOverride | None:
    if not rest:
        return None
    sol = _parse_decimal(rest[0]) if len(rest) >= 1 else None
    tokens = _parse_decimal(rest[1]) if len(rest) >= 2 else None
    sig = rest[2] if len(rest) >= 3 else None
    return FillOverride(sol_amount=sol, token_amount_ui=tokens, reported_tx_signature=sig)


def _sell_override(rest: list[str]) -> FillOverride | None:
    if not rest:
        return None
    sol = _parse_decimal(rest[0]) if len(rest) >= 1 else None
    sig = rest[1] if len(rest) >= 2 else None
    return FillOverride(sol_amount=sol, reported_tx_signature=sig)
