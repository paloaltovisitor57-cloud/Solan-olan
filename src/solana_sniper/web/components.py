"""Streamlit building blocks: theme, provenance header, metric cards, badges, tables.

Every widget here is display-only. There is deliberately no button, form, input or upload that
could act on the engine; the only interactive controls in the app are the session selector,
the page selector, filters and the token search box.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pandas as pd
import streamlit as st

from solana_sniper.telemetry.redaction import scrub_text
from solana_sniper.web.models import (
    ALIVE_ENDED,
    ALIVE_RUNNING,
    Integrity,
    Provenance,
)

DECISION_COLORS: dict[str, str] = {
    "BUY_SIGNAL": "green",
    "PENDING": "orange",
    "EXPIRED": "orange",
    "ABANDONED": "red",
    "HARD_REJECT": "red",
    "QUOTE_FAILED": "red",
    "SIZING_ZERO": "red",
    "STALE": "red",
    "CANCELLED": "gray",
}
STATE_COLORS: dict[str, str] = {
    "DISCOVERED": "gray",
    "MONITORING": "blue",
    "QUALIFIED": "green",
    "BUY_SIGNAL": "green",
    "AWAITING_CONFIRMATION": "orange",
    "OPEN": "green",
    "EXIT_SIGNAL": "orange",
    "AWAITING_EXIT_CONFIRMATION": "orange",
    "CLOSED": "gray",
    "REJECTED": "red",
    "EXPIRED": "gray",
    "DATA_STALE": "orange",
    "SIGNAL_CANCELLED": "gray",
}
PROVIDER_COLORS: dict[str, str] = {
    "HEALTHY": "green",
    "RATE_LIMITED": "orange",
    "DEGRADED": "orange",
    "DOWN": "red",
}
ENGINE_COLORS: dict[str, str] = {
    "HEALTHY": "green",
    "DEGRADED": "orange",
    "UNHEALTHY": "red",
    "STOPPED": "gray",
}

THEME_CSS = """
<style>
:root { --ink: #d7dde5; --muted: #7d8794; --line: #1f262e; --card: #10151b; }
html, body, [data-testid="stAppViewContainer"] { background: #0b0f14; }
[data-testid="stHeader"] { background: rgba(11,15,20,0.9); }
[data-testid="stMetric"] {
  background: var(--card); border: 1px solid var(--line); border-radius: 6px; padding: 10px 12px;
}
[data-testid="stMetricLabel"] { color: var(--muted); font-size: 0.72rem; letter-spacing: .04em;
  text-transform: uppercase; }
[data-testid="stMetricValue"] { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 1.25rem; }
[data-testid="stMetricDelta"] { font-size: 0.8rem; }
.sniper-banner { border: 1px solid var(--line); border-left: 4px solid #5ec4a6; border-radius: 6px;
  background: var(--card); padding: 10px 14px; margin-bottom: 8px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.8rem; }
.sniper-banner.test { border-left-color: #e0b04f; }
.sniper-banner.live { border-left-color: #e06c6c; }
.sniper-banner.autonomous { border-left-color: #ff3b3b; border-color: #5a1f1f; }
.sniper-banner.unknown { border-left-color: #7d8794; }
.sniper-banner .row { display: flex; flex-wrap: wrap; gap: 6px 14px; align-items: center; }
.sniper-banner .k { color: var(--muted); }
.sniper-banner .v { color: var(--ink); font-weight: 600; }
.sniper-banner .disabled { color: #e06c6c; font-weight: 700; }
.sniper-banner .enabled { color: #ff3b3b; font-weight: 700; }
.sniper-title { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.95rem;
  letter-spacing: .06em; color: var(--muted); text-transform: uppercase; margin: 0 0 4px 0; }
.sniper-kv { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.8rem;
  color: var(--ink); line-height: 1.5; }
.sniper-kv .k { color: var(--muted); }
div[data-testid="stDataFrame"] { border: 1px solid var(--line); border-radius: 6px; }
@media (max-width: 640px) {
  [data-testid="stMetricValue"] { font-size: 1.05rem; }
  .block-container { padding-left: 16px; padding-right: 16px; }
}
</style>
"""


def inject_theme() -> None:
    st.markdown(THEME_CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------- formatting


def money(value: Decimal | float | None, *, signed: bool = False) -> str:
    if value is None:
        return "—"
    v = float(value)
    if signed:
        return f"{'+' if v >= 0 else '-'}€{abs(v):,.2f}"
    return f"€{v:,.2f}"


def pct(value: float | None, *, signed: bool = True, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value:+.{digits}%}" if signed else f"{value:.{digits}%}"


def num(value: float | int | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    if isinstance(value, int):
        return f"{value:,}"
    return f"{value:,.{digits}f}"


def usd(value: float | None) -> str:
    if value is None:
        return "—"
    if abs(value) >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if abs(value) >= 1_000:
        return f"${value / 1_000:.1f}k"
    return f"${value:,.0f}"


def age(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    s = max(0, int(seconds))
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{s // 60}m {s % 60:02d}s"
    if s < 172800:
        return f"{s // 3600}h {(s % 3600) // 60:02d}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


def when(dt: datetime | None, *, seconds: bool = True) -> str:
    if dt is None:
        return "—"
    dt = dt.astimezone(UTC)
    return dt.strftime("%H:%M:%S" if seconds else "%Y-%m-%d %H:%M")


def since(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return age((datetime.now(tz=UTC) - dt.astimezone(UTC)).total_seconds()) + " ago"


def short(mint: str, n: int = 6) -> str:
    return mint if len(mint) <= 2 * n + 1 else f"{mint[:n]}…{mint[-4:]}"


def safe(text: str | None) -> str:
    return scrub_text(text) if text else ""


# ------------------------------------------------------------------- widgets


def badge(label: str, color: str = "gray") -> str:
    """Markdown badge directive (Streamlit renders `:color-badge[...]`)."""
    return f":{color}-badge[{label}]"


def decision_badge(decision: str) -> str:
    return badge(decision, DECISION_COLORS.get(decision, "gray"))


def state_badge(state: str) -> str:
    return badge(state, STATE_COLORS.get(state, "gray"))


def provider_badge(state: str) -> str:
    return badge(state, PROVIDER_COLORS.get(state, "gray"))


def engine_badge(state: str | None) -> str:
    return badge(state or "NO HEARTBEAT", ENGINE_COLORS.get(state or "", "gray"))


def alive_badge(alive: str) -> str:
    color = {ALIVE_RUNNING: "green", ALIVE_ENDED: "gray"}.get(alive, "orange")
    return badge(alive, color)


def provenance_header(
    prov: Provenance, *, session_id: str, alive: str, alive_detail: str, engine_state: str | None
) -> None:
    """The banner every page starts with: mode, market data, execution, real transactions."""
    tone = prov.tone
    real_class = "enabled" if tone == "autonomous" else "disabled"
    rows = [
        f'<span class="v">{prov.mode_label}</span>'
        f'<span class="k">session</span> <span class="v">{session_id}</span>'
        f'<span class="k">status</span> <span class="v">{alive}</span> '
        f'<span class="k">{alive_detail}</span>'
        + (
            f'<span class="k">engine</span> <span class="v">{engine_state}</span>'
            if engine_state
            else ""
        ),
        f'<span class="k">MARKET DATA:</span> <span class="v">{prov.market_data}</span>'
        f'<span class="k">EXECUTION:</span> <span class="v">{prov.execution}</span>'
        f'<span class="k">REAL TRANSACTIONS:</span> '
        f'<span class="{real_class}">{prov.real_transactions}</span>',
    ]
    html = f'<div class="sniper-banner {tone}">' + "".join(
        f'<div class="row">{r}</div>' for r in rows
    )
    if tone == "autonomous":
        html += (
            '<div class="row"><span class="k">This session signs and broadcasts real swaps from '
            "its dedicated hot wallet. Every fill marked verified was read back from the confirmed "
            "transaction; open values are quote estimates. This page only reads.</span></div>"
        )
    elif not prov.simulated:
        html += (
            '<div class="row"><span class="k">Fills are what a human confirmed at quoted or '
            "user-typed amounts; nothing here was signed, broadcast or reconciled on-chain by "
            "this software.</span></div>"
        )
    html += "</div>"
    st.markdown(html, unsafe_allow_html=True)


def metric_grid(items: Sequence[tuple[str, str, str | None]], *, columns: int = 4) -> None:
    """Metric cards laid out `columns` per row on desktop; Streamlit stacks them on phones."""
    for start in range(0, len(items), columns):
        chunk = items[start : start + columns]
        cols = st.columns(len(chunk))
        for col, (label, value, delta) in zip(cols, chunk, strict=True):
            with col:
                st.metric(label, value, delta=delta, delta_color="normal" if delta else "off")


def section(title: str) -> None:
    st.markdown(f'<div class="sniper-title">{title}</div>', unsafe_allow_html=True)


def kv_block(pairs: Iterable[tuple[str, str]]) -> None:
    html = "".join(
        f'<div><span class="k">{k}</span>&nbsp; {v}</div>' for k, v in pairs if v is not None
    )
    st.markdown(f'<div class="sniper-kv">{html}</div>', unsafe_allow_html=True)


def empty_state(message: str) -> None:
    st.caption(message)


def busy_notice() -> None:
    st.warning("Database busy — retrying", icon="⏳")


def integrity_banner(integrity: Integrity | None) -> None:
    if integrity is None or integrity.complete:
        return
    dropped = ", ".join(f"{k}={v}" for k, v in sorted(integrity.dropped_by_kind.items()))
    st.error(
        "DATA INTEGRITY COMPROMISED — the storage writer dropped "
        f"{integrity.dropped_total} rows ({dropped or 'unknown kinds'}) and failed "
        f"{integrity.failed_total}. Figures on this page are computed from incomplete records "
        "and must not be read as a measurement."
    )


def table(
    rows: Sequence[dict[str, Any]],
    *,
    height: int | None = None,
    column_config: dict[str, Any] | None = None,
    order: Sequence[str] | None = None,
) -> None:
    if not rows:
        empty_state("nothing recorded yet")
        return
    frame = pd.DataFrame(rows)
    if order:
        frame = frame[[c for c in order if c in frame.columns]]
    st.dataframe(
        frame,
        width="stretch",
        height="auto" if height is None else height,
        hide_index=True,
        column_config=column_config,
    )


def stacked_rows(rows: Sequence[dict[str, Any]], *, limit: int = 8) -> None:
    """Phone-friendly alternative to a wide table: one compact line per row."""
    if not rows:
        empty_state("nothing recorded yet")
        return
    for r in rows[:limit]:
        st.markdown(
            "  ".join(f"**{k}** {v}" for k, v in r.items() if v not in (None, "")),
            unsafe_allow_html=False,
        )
