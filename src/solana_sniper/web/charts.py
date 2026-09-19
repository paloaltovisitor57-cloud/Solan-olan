"""Plotly figures for the dashboard: dark, flat, no animation, no gradients."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import plotly.graph_objects as go

from solana_sniper.web.models import EquityPoint, ObservationPoint, ScorePoint

INK = "#d7dde5"
MUTED = "#7d8794"
GRID = "#1f262e"
ACCENT = "#5ec4a6"  # equity / positive
WARN = "#e0b04f"
NEG = "#e06c6c"
CASH = "#7fa7d6"
BG = "rgba(0,0,0,0)"
MONO = "ui-monospace, SFMono-Regular, Menlo, monospace"

PLOT_CONFIG: dict[str, Any] = {"displayModeBar": False, "responsive": True, "staticPlot": False}


def _layout(height: int, *, y_title: str = "", y_format: str | None = None) -> dict[str, Any]:
    return {
        "template": "plotly_dark",
        "paper_bgcolor": BG,
        "plot_bgcolor": BG,
        "height": height,
        "margin": {"l": 8, "r": 8, "t": 8, "b": 8},
        "font": {
            "family": "ui-monospace, SFMono-Regular, Menlo, monospace",
            "color": INK,
            "size": 11,
        },
        "xaxis": {"gridcolor": GRID, "zeroline": False, "showline": False},
        "yaxis": {
            "gridcolor": GRID,
            "zeroline": False,
            "title": y_title,
            "tickformat": y_format,
        },
        "legend": {"orientation": "h", "y": 1.08, "x": 0, "font": {"color": MUTED}},
        "hovermode": "x unified",
        "transition": {"duration": 0},
    }


def downsample[T](points: Sequence[T], max_points: int) -> list[T]:
    """Keep every k-th point plus the last one, so a long session stays a light figure."""
    if max_points <= 0 or len(points) <= max_points:
        return list(points)
    stride = -(-len(points) // max_points)
    out = list(points[::stride])
    if out[-1] is not points[-1]:
        out.append(points[-1])
    return out


def equity_figure(
    points: Sequence[EquityPoint], *, starting_equity: float | None = None, height: int = 260
) -> go.Figure:
    pts = downsample(points, 1500)
    fig = go.Figure()
    x = [p.at for p in pts]
    fig.add_trace(
        go.Scatter(
            x=x,
            y=[p.equity_eur for p in pts],
            name="equity €",
            mode="lines",
            line={"color": ACCENT, "width": 2},
        )
    )
    fig.add_trace(
        go.Scatter(
            x=x,
            y=[p.cash_eur for p in pts],
            name="cash €",
            mode="lines",
            line={"color": CASH, "width": 1, "dash": "dot"},
        )
    )
    fig.add_trace(
        go.Scatter(
            x=x,
            y=[p.open_value_eur for p in pts],
            name="open value €",
            mode="lines",
            line={"color": WARN, "width": 1},
        )
    )
    if starting_equity is not None:
        fig.add_hline(y=starting_equity, line={"color": MUTED, "width": 1, "dash": "dash"})
    fig.update_layout(**_layout(height, y_title="€"))
    return fig


def drawdown_figure(points: Sequence[EquityPoint], *, height: int = 180) -> go.Figure:
    pts = downsample(points, 1500)
    peak = 0.0
    dd: list[float] = []
    for p in pts:
        peak = max(peak, p.equity_eur)
        dd.append(-((peak - p.equity_eur) / peak) if peak > 0 else 0.0)
    fig = go.Figure(
        go.Scatter(
            x=[p.at for p in pts],
            y=dd,
            name="drawdown",
            mode="lines",
            fill="tozeroy",
            line={"color": NEG, "width": 1},
            fillcolor="rgba(224,108,108,0.15)",
        )
    )
    fig.update_layout(**_layout(height, y_format=".1%"))
    return fig


def exposure_figure(points: Sequence[EquityPoint], *, height: int = 180) -> go.Figure:
    pts = downsample(points, 1500)
    fig = go.Figure()
    x = [p.at for p in pts]
    fig.add_trace(
        go.Scatter(
            x=x,
            y=[p.realized_pnl_eur for p in pts],
            name="realized €",
            mode="lines",
            line={"color": ACCENT, "width": 1},
        )
    )
    fig.add_trace(
        go.Scatter(
            x=x,
            y=[p.unrealized_pnl_eur for p in pts],
            name="unrealized €",
            mode="lines",
            line={"color": WARN, "width": 1},
        )
    )
    fig.add_trace(
        go.Scatter(
            x=x,
            y=[p.open_exposure_eur for p in pts],
            name="exposure €",
            mode="lines",
            line={"color": CASH, "width": 1, "dash": "dot"},
        )
    )
    fig.update_layout(**_layout(height, y_title="€"))
    return fig


def score_figure(
    points: Sequence[ScorePoint], *, min_score: float | None = None, height: int = 220
) -> go.Figure:
    pts = downsample(points, 1000)
    fig = go.Figure(
        go.Scatter(
            x=[p.at for p in pts],
            y=[p.score for p in pts],
            name="score",
            mode="lines+markers",
            line={"color": ACCENT, "width": 1.5},
            marker={"size": 4},
        )
    )
    if min_score is not None:
        fig.add_hline(y=min_score, line={"color": MUTED, "width": 1, "dash": "dash"})
    layout = _layout(height)
    layout["yaxis"]["range"] = [0, 100]
    fig.update_layout(**layout)
    return fig


def price_figure(points: Sequence[ObservationPoint], *, height: int = 220) -> go.Figure:
    pts = [p for p in downsample(points, 1000) if p.price_native is not None]
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=[p.at for p in pts],
            y=[p.price_native for p in pts],
            name="price (SOL)",
            mode="lines",
            line={"color": ACCENT, "width": 1.5},
        )
    )
    liq = [p for p in pts if p.liquidity_usd is not None]
    if liq:
        fig.add_trace(
            go.Scatter(
                x=[p.at for p in liq],
                y=[p.liquidity_usd for p in liq],
                name="liquidity $",
                mode="lines",
                yaxis="y2",
                line={"color": CASH, "width": 1, "dash": "dot"},
            )
        )
    layout = _layout(height)
    layout["yaxis2"] = {
        "overlaying": "y",
        "side": "right",
        "gridcolor": GRID,
        "zeroline": False,
        "showgrid": False,
    }
    fig.update_layout(**layout)
    return fig
