"""Rich terminal dashboard. Reads engine state; never mutates it."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
from decimal import Decimal

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from solana_sniper.alerts.base import Alert
from solana_sniper.app.engine import Engine
from solana_sniper.config.settings import DashboardConfig
from solana_sniper.domain.enums import CandidateState as S
from solana_sniper.domain.enums import SignalKind, Urgency
from solana_sniper.domain.money import q_display

ACTIVE_STATES = {S.MONITORING, S.QUALIFIED, S.BUY_SIGNAL, S.AWAITING_CONFIRMATION, S.DATA_STALE}


def _eur(v: Decimal) -> str:
    return f"€{q_display(v):,}"


class Dashboard:
    def __init__(
        self, engine: Engine, config: DashboardConfig, console: Console | None = None
    ) -> None:
        self._engine = engine
        self._cfg = config
        self._console = console or Console()
        self._alerts: deque[Alert] = deque(maxlen=50)
        self._messages: deque[str] = deque(maxlen=20)
        self._live: Live | None = None

    def push_alert(self, alert: Alert) -> None:
        self._alerts.append(alert)

    def push_message(self, message: str) -> None:
        self._messages.append(f"{self._engine.now():%H:%M:%S} > {message}")

    # ------------------------------------------------------------- rendering
    def render(self) -> Layout:
        layout = Layout()
        header_size = 7
        available = max(10, self._console.height - header_size)
        footer_size = min(self._cfg.event_lines + 2, max(5, available - 12))
        layout.split_column(
            Layout(self._header(), name="header", size=header_size),
            Layout(name="body"),
            Layout(self._footer(), name="footer", size=footer_size),
        )
        layout["body"].split_row(
            Layout(self._candidates(), name="candidates"),
            Layout(name="right"),
        )
        layout["right"].split_column(
            Layout(self._pending(), name="pending", ratio=1),
            Layout(self._positions(), name="positions", ratio=1),
        )
        return layout

    def _header(self) -> Panel:
        e = self._engine
        snap = e.d.account.snapshot(e.now())
        m = e.d.metrics
        mode = f"[bold red]{e.mode}[/]" if e.mode.value == "LIVE" else f"[bold green]{e.mode}[/]"
        line1 = (
            f"BANKROLL {_eur(snap.equity_eur)}  CASH {_eur(snap.cash_eur)}  "
            f"EXPOSURE {_eur(snap.open_exposure_eur)}  EQUITY {_eur(snap.equity_eur)}  "
            f"PEAK {_eur(snap.peak_equity_eur)}  DD {snap.drawdown_pct:.1%}"
        )
        line2 = (
            f"REALIZED {_eur(snap.realized_pnl_eur)}  UNREALIZED {_eur(snap.unrealized_pnl_eur)}  "
            f"FEES {_eur(snap.fees_eur)}  W/L {snap.wins}/{snap.losses}  "
            f"NEXT MILESTONE {_eur(e.d.milestones.next_milestone(snap.equity_eur) or Decimal(0))}"
        )
        tracked = sum(1 for c in e.candidates.values() if not c.sm.is_terminal)
        qualified = sum(1 for c in e.candidates.values() if c.state is S.QUALIFIED)
        pending = e.d.execution.pending()
        line3 = (
            f"NEW TOKENS/s {m.discovery_rate_per_s():.2f}  TRACKED {tracked}  QUALIFIED {qualified}  "
            f"OPEN {len(e.open_positions())}  PENDING {len(pending)}  "
            f"SIGNALS {e.stats.signals}  CONFIRMED {e.stats.confirmed}  EXITS {e.stats.exits}  "
            f"REJECTED {e.stats.rejected}  SOL/EUR {e.d.fx.sol_eur():.2f}{'' if e.d.fx.is_live else '*'}"
        )
        lat = m.latencies
        line4 = (
            f"latency ms  disc→data p50 {lat['discovery_to_first_data'].summary().get('p50_ms', '-')}  "
            f"data→feat {lat['data_to_features'].summary().get('p50_ms', '-')}  "
            f"feat→signal {lat['features_to_signal'].summary().get('p50_ms', '-')}  "
            f"qualified→quote {lat['qualified_to_quote'].summary().get('p50_ms', '-')}  "
            f"quote {lat['quote_latency'].summary().get('p50_ms', '-')}  "
            f"provider {lat['provider_latency'].summary().get('p50_ms', '-')}  "
            f"tick p50/p95 {lat['engine_tick'].summary().get('p50_ms', '-')}/"
            f"{lat['engine_tick'].summary().get('p95_ms', '-')}"
        )
        title = f"solana-sniper {mode} session {e.session_id}"
        return Panel(
            Group(Text(line1), Text(line2), Text(line3), Text(line4, style="dim")), title=title
        )

    def _candidates(self) -> Panel:
        e = self._engine
        table = Table(expand=True, show_edge=False, pad_edge=False)
        for col in ("sym", "age", "state", "score", "liq$", "tx/m", "mom30", "reason"):
            table.add_column(col, no_wrap=True, overflow="ellipsis")
        cands = [c for c in e.candidates.values() if c.state in ACTIVE_STATES]
        cands.sort(key=lambda c: c.score.score if c.score else -1, reverse=True)
        now = e.now()
        for c in cands[: self._cfg.top_candidates]:
            f = c.features
            age = c.track.token.age_seconds(now)
            style = {
                S.QUALIFIED: "bold green",
                S.AWAITING_CONFIRMATION: "bold yellow",
                S.BUY_SIGNAL: "bold yellow",
                S.DATA_STALE: "dim",
            }.get(c.state, "")
            table.add_row(
                Text(c.symbol or c.mint[:6], style=style),
                f"{age:.0f}s" if age is not None else "?",
                Text(str(c.state), style=style),
                f"{c.score.score:.0f}" if c.score else "-",
                f"{f.liquidity_usd:,.0f}" if f and f.liquidity_usd is not None else "-",
                f"{f.trade_velocity_per_min:.0f}"
                if f and f.trade_velocity_per_min is not None
                else "-",
                f"{f.momentum_30s:+.1%}" if f and f.momentum_30s is not None else "-",
                "; ".join(c.gate_reasons[:2])[:60] if c.gate_reasons else c.last_reason[:60],
            )
        return Panel(table, title=f"TOP CANDIDATES ({len(cands)} active)")

    def _pending(self) -> Panel:
        e = self._engine
        lines: list[Text] = []
        for o in e.d.execution.pending():
            if o.kind is SignalKind.BUY and o.buy is not None:
                s = o.buy
                exit_val = s.immediate_exit_value_eur
                lines.append(
                    Text(
                        f"[b {o.ref}] BUY {s.symbol or s.mint[:8]}  score {s.score.score:.0f}  age {s.token_age_s or 0:.0f}s\n"
                        f"      size {_eur(s.sizing.recommended_eur)} ({s.sizing.recommended_sol:.4f} SOL)  "
                        f"tokens ~{s.expected_tokens:,.0f}  liq ${s.liquidity_usd or 0:,.0f}\n"
                        f"      entry slip {s.quote.entry_slippage_bps}bps  exit now "
                        f"{_eur(exit_val) if exit_val is not None else '?'}  "
                        f"rt loss {s.quote.round_trip_loss_pct or 0:.1%}  ttl {(s.expires_at - e.now()).total_seconds():.0f}s\n"
                        f"      mint {s.mint}\n      why: {'; '.join(s.score.top_reasons[:3])}\n"
                        f"      confirm: b {o.ref}   reject: r {o.ref}",
                        style="bold yellow",
                    )
                )
            elif o.sell is not None:
                s2 = o.sell
                style = "bold white on red" if s2.urgency is Urgency.URGENT else "bold magenta"
                est = s2.estimated_sell_output_sol
                lines.append(
                    Text(
                        f"[s {o.ref}] SELL {s2.symbol or s2.mint[:8]}  {s2.reason}  ({s2.urgency})\n"
                        f"      value {_eur(s2.current_value_eur)}  entry {_eur(s2.entry_value_eur)}  "
                        f"peak {_eur(s2.peak_value_eur)}  pnl {_eur(s2.pnl_eur)} ({s2.pnl_pct:+.0%})\n"
                        f"      trailing dd {s2.trailing_drawdown_pct:.0%} / {s2.trailing_threshold_pct:.0%}  "
                        f"est out {f'{est:.4f} SOL' if est is not None else '?'}  ttl {(s2.expires_at - e.now()).total_seconds():.0f}s\n"
                        f"      {s2.detail}\n      confirm: s {o.ref}   ignore: i {o.ref}",
                        style=style,
                    )
                )
        body: Group | Text = Group(*lines) if lines else Text("no pending signals", style="dim")
        return Panel(body, title="PENDING CONFIRMATIONS  (b N / r N / s N / i N / q)")

    def _positions(self) -> Panel:
        e = self._engine
        table = Table(expand=True, show_edge=False, pad_edge=False)
        for col in ("sym", "held", "cost", "value", "peak", "pnl", "dd/thr", "exec", "state"):
            table.add_column(col, no_wrap=True)
        now = e.now()
        for p in e.open_positions():
            cand = e.candidate_for_position(p)
            thr = e.d.monitor.trailing_threshold(p, cand.features if cand else None, now)
            pnl_style = "green" if p.unrealized_pnl_eur >= 0 else "red"
            table.add_row(
                p.symbol or p.mint[:6],
                f"{p.holding_seconds(now):.0f}s",
                _eur(p.cost_basis_eur),
                _eur(p.current_value_eur),
                _eur(p.peak_value_eur),
                Text(f"{_eur(p.unrealized_pnl_eur)} {p.pnl_pct:+.0%}", style=pnl_style),
                f"{p.trailing_drawdown_pct:.0%}/{thr:.0%}",
                "Q" if p.value_is_executable else ("stale" if p.data_stale else "px"),
                str(cand.state) if cand else str(p.state),
            )
        return Panel(table, title=f"OPEN POSITIONS ({len(e.open_positions())})")

    def _footer(self) -> Panel:
        lines: list[Text] = []
        for a in list(self._alerts)[-4:]:
            style = {Urgency.URGENT: "bold white on red", Urgency.HIGH: "bold yellow"}.get(
                a.urgency, "cyan"
            )
            lines.append(Text(f"{a.at:%H:%M:%S} {a.title}: {a.body}"[:220], style=style))
        for m in list(self._messages)[-3:]:
            lines.append(Text(m, style="bold"))
        remaining = max(0, self._cfg.event_lines - len(lines))
        for ev in self._engine.stats.recent[-remaining:]:
            lines.append(Text(ev[:220], style="dim"))
        return Panel(Group(*lines), title="EVENTS")

    async def run(self) -> None:
        interval = 1.0 / max(0.5, self._cfg.refresh_hz)
        with Live(
            self.render(),
            console=self._console,
            screen=False,
            refresh_per_second=self._cfg.refresh_hz,
        ) as live:
            self._live = live
            while True:
                await asyncio.sleep(interval)
                try:
                    live.update(self.render())
                except Exception as exc:
                    self._messages.append(f"dashboard render error: {exc}")


def format_time(dt: datetime) -> str:
    return dt.strftime("%H:%M:%S")
