"""Terminal alerts: either printed directly or handed to the dashboard's event feed."""

from __future__ import annotations

from collections.abc import Callable

from rich.console import Console

from solana_sniper.alerts.base import Alert
from solana_sniper.domain.enums import Urgency

STYLE = {Urgency.NORMAL: "cyan", Urgency.HIGH: "bold yellow", Urgency.URGENT: "bold white on red"}


class TerminalAlertProvider:
    name = "terminal"

    def __init__(
        self, console: Console | None = None, sink: Callable[[Alert], None] | None = None
    ) -> None:
        self._console = console or Console(stderr=True)
        self._sink = sink

    def set_sink(self, sink: Callable[[Alert], None] | None) -> None:
        self._sink = sink

    async def send(self, alert: Alert) -> None:
        if self._sink is not None:
            self._sink(alert)
            return
        style = STYLE[alert.urgency]
        prefix = "!!! " if alert.urgency is Urgency.URGENT else ""
        self._console.print(f"[{style}]{prefix}{alert.title}[/] {alert.body}")
