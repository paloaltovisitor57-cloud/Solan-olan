"""Streamlit entry point: `solana-sniper dashboard-web` runs `streamlit run` on this file.

Read-only by construction: the page has no button, form or input that reaches the engine. It
selects a session, reads its database through `DashboardRepository` and the heartbeat file,
and renders. Nothing here imports the execution, quote, alert or command-file modules.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import streamlit as st

from solana_sniper import __version__
from solana_sniper.config.paths import configured_home
from solana_sniper.domain.models import EntryAttempt
from solana_sniper.strategy.evaluation import MIN_MEANINGFUL_N, MULTIPLES
from solana_sniper.telemetry.redaction import safe_exception
from solana_sniper.web import charts
from solana_sniper.web import components as ui
from solana_sniper.web.data import (
    DashboardError,
    DashboardRepository,
    DatabaseBusyError,
    SchemaUnsupportedError,
)
from solana_sniper.web.models import (
    EVENT_CATEGORIES,
    CandidateView,
    EngineHealth,
    EquityHistory,
    EventItem,
    FillView,
    Heartbeat,
    OutcomeSummary,
    PositionView,
    ProviderHealth,
    SessionRef,
    SessionSummary,
    SignalView,
    TokenDetail,
    TokenHit,
)
from solana_sniper.web.session_discovery import (
    discover_sessions,
    read_heartbeat,
    resolve_selection,
    running_session_id,
)

ENV_PREFIX = "SOLANA_SNIPER_WEB_"  # never SNIPER_*: those are audited by the strict config loader
DEFAULT_REFRESH_S = 3
MIN_REFRESH_S = 2
MAX_REFRESH_S = 30
CACHE_TTL_S = 2.0  # at most one query per two seconds per viewer; never a frozen page
PAGES: tuple[str, ...] = (
    "Overview",
    "Equity",
    "Positions",
    "Candidates",
    "Entry attempts",
    "Signals",
    "Fills",
    "Token",
    "Outcomes",
    "Providers",
    "Engine",
    "Events",
)
DECISIONS: tuple[str, ...] = (
    "BUY_SIGNAL",
    "ABANDONED",
    "EXPIRED",
    "HARD_REJECT",
    "QUOTE_FAILED",
    "SIZING_ZERO",
    "STALE",
    "CANCELLED",
    "PENDING",
)
ACTIVE_STATES = ("QUALIFIED", "BUY_SIGNAL", "AWAITING_CONFIRMATION", "OPEN", "EXIT_SIGNAL")


# ------------------------------------------------------------------ config


@dataclass(frozen=True, slots=True)
class WebConfig:
    home: Path
    paper: str | None
    session: str | None
    refresh_seconds: int
    busy_timeout_s: float

    @classmethod
    def load(cls, argv: list[str] | None = None) -> WebConfig:
        """Environment (set by the CLI launcher) first, then script arguments after `--`."""
        env = os.environ
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--home")
        parser.add_argument("--paper")
        parser.add_argument("--session")
        parser.add_argument("--refresh-seconds", type=float)
        parser.add_argument("--busy-timeout", type=float)
        args, _ = parser.parse_known_args(argv if argv is not None else sys.argv[1:])
        home_raw = args.home or env.get(ENV_PREFIX + "HOME")
        home = Path(home_raw).expanduser() if home_raw else configured_home()
        refresh_raw = args.refresh_seconds or env.get(ENV_PREFIX + "REFRESH")
        try:
            refresh = round(float(refresh_raw)) if refresh_raw else DEFAULT_REFRESH_S
        except ValueError:
            refresh = DEFAULT_REFRESH_S
        busy_raw = args.busy_timeout or env.get(ENV_PREFIX + "BUSY_TIMEOUT")
        try:
            busy = float(busy_raw) if busy_raw else 5.0
        except ValueError:
            busy = 5.0
        return cls(
            home=home,
            paper=args.paper or env.get(ENV_PREFIX + "PAPER") or None,
            session=args.session or env.get(ENV_PREFIX + "SESSION") or None,
            refresh_seconds=min(MAX_REFRESH_S, max(MIN_REFRESH_S, refresh)),
            busy_timeout_s=busy,
        )


# ------------------------------------------------------------------ loaders
# Each loader opens a fresh read-only connection per call; `st.cache_data` bounds the query rate
# per viewer to one per CACHE_TTL_S and the fragment reruns re-query after that, so a RUNNING
# session never shows a frozen page.


def _repo(db_path: str, session_id: str, busy_timeout_s: float) -> DashboardRepository:
    return DashboardRepository(Path(db_path), session_id, busy_timeout_s=busy_timeout_s)


@st.cache_data(ttl=10, show_spinner=False)
def cached_sessions(home: str, running: str | None) -> list[SessionRef]:
    hb = read_heartbeat(Path(home))
    return discover_sessions(Path(home), heartbeat=hb)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def load_summary(
    home: str, db_path: str, session_id: str, busy: float, ref: SessionRef
) -> SessionSummary:
    return _repo(db_path, session_id, busy).summary(ref, read_heartbeat(Path(home)))


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def load_history(db_path: str, session_id: str, busy: float, max_points: int) -> EquityHistory:
    return _repo(db_path, session_id, busy).portfolio_history(max_points)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def load_positions(
    db_path: str, session_id: str, busy: float, open_only: bool
) -> list[PositionView]:
    return _repo(db_path, session_id, busy).positions(open_only=open_only)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def load_candidates(db_path: str, session_id: str, busy: float, limit: int) -> list[CandidateView]:
    return _repo(db_path, session_id, busy).recent_candidates(limit=limit)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def load_attempts(
    db_path: str, session_id: str, busy: float, decision: str | None, limit: int
) -> list[EntryAttempt]:
    return _repo(db_path, session_id, busy).entry_attempts(limit=limit, decision=decision)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def load_decision_counts(db_path: str, session_id: str, busy: float) -> dict[str, int]:
    return _repo(db_path, session_id, busy).entry_decision_counts()


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def load_signals(db_path: str, session_id: str, busy: float, limit: int) -> list[SignalView]:
    return _repo(db_path, session_id, busy).signals(limit=limit)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def load_fills(db_path: str, session_id: str, busy: float, limit: int) -> list[FillView]:
    return _repo(db_path, session_id, busy).fills(limit=limit)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def load_providers(home: str, db_path: str, session_id: str, busy: float) -> list[ProviderHealth]:
    return _repo(db_path, session_id, busy).provider_health(read_heartbeat(Path(home)))


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def load_engine(home: str, db_path: str, session_id: str, busy: float) -> EngineHealth:
    return _repo(db_path, session_id, busy).engine_health(read_heartbeat(Path(home)))


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def load_events(
    db_path: str, session_id: str, busy: float, category: str, limit: int
) -> list[EventItem]:
    return _repo(db_path, session_id, busy).events(category=category, limit=limit)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def load_token(db_path: str, session_id: str, busy: float, mint: str) -> TokenDetail:
    return _repo(db_path, session_id, busy).token_detail(mint)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def load_search(db_path: str, session_id: str, busy: float, query: str) -> list[TokenHit]:
    return _repo(db_path, session_id, busy).search_tokens(query)


@st.cache_data(ttl=CACHE_TTL_S, show_spinner=False)
def load_outcomes(
    db_path: str, session_id: str, busy: float, include_truncated: bool, min_observations: int
) -> OutcomeSummary:
    return _repo(db_path, session_id, busy).outcome_summary(
        include_truncated=include_truncated, min_observations=min_observations
    )


def fetch[T](key: str, loader: Callable[..., T], *args: Any) -> T | None:
    """Run a loader; on SQLITE_BUSY show the retry notice and fall back to the last good value
    of this key; on any other problem show a scrubbed message. Never raises into the page."""
    try:
        value = loader(*args)
    except DatabaseBusyError:
        ui.busy_notice()
        last = st.session_state.get("last:" + key)
        return last if last is not None else None
    except SchemaUnsupportedError as exc:
        st.error(f"Unsupported database schema: {safe_exception(exc)}")
        return None
    except DashboardError as exc:
        st.error(f"Could not read the database: {safe_exception(exc)}")
        return None
    except Exception as exc:
        st.error(f"Unexpected error while reading: {safe_exception(exc)}")
        return None
    st.session_state["last:" + key] = value
    return value


# -------------------------------------------------------------------- pages


def page_overview(ctx: Ctx, summary: SessionSummary) -> None:
    ui.integrity_banner(summary.integrity)
    if summary.paper is not None:
        p = summary.paper
        ui.kv_block(
            [
                (
                    "starting bankroll",
                    f"{p.bankroll_sol:.4f} SOL = {ui.money(p.bankroll_eur)} @ "
                    f"€{p.sol_eur_start:.2f}/SOL ({p.fx_source} rate at start, "
                    f"{ui.when(p.fx_at, seconds=False)} UTC)",
                ),
                ("requested", p.requested + (f"  ·  name {p.name}" if p.name else "")),
            ]
        )
    ui.metric_grid(
        [
            ("Equity", ui.money(summary.equity_eur), None),
            ("Return", ui.pct(summary.return_pct, digits=2), None),
            ("Drawdown", ui.pct(summary.drawdown_pct, signed=False), None),
            ("Cash", ui.money(summary.cash_eur), None),
            ("Exposure", ui.money(summary.open_exposure_eur), None),
            ("Realized P&L", ui.money(summary.realized_pnl_eur, signed=True), None),
            ("Unrealized P&L", ui.money(summary.unrealized_pnl_eur, signed=True), None),
            ("Open positions", str(summary.open_positions), None),
            ("Signals", str(summary.signals), None),
            ("Fills", str(summary.fills), None),
            ("Entry attempts", str(summary.entry_attempts), None),
            ("Tokens seen", str(summary.tokens), None),
        ],
        columns=4,
    )
    st.caption(
        f"portfolio snapshot {ui.since(summary.snapshot_at)}  ·  "
        f"W/L {summary.wins}/{summary.losses}  ·  fees {ui.money(summary.fees_eur)}  ·  "
        f"simulated slippage {ui.money(summary.slippage_eur)}  ·  "
        f"outcomes {summary.outcomes}  ·  errors {summary.errors}"
    )
    left, right = st.columns(2)
    with left:
        ui.section("latest entry attempt")
        a = summary.latest_attempt
        if a is None:
            ui.empty_state("no token has qualified yet")
        else:
            st.markdown(
                f"**{a.symbol or ui.short(a.mint)}** #{a.attempt_number} "
                f"{ui.decision_badge(str(a.final_decision))}  score {a.qualified_score:.1f} "
                f"→ {ui.num(a.post_quote_score)}  ·  {ui.when(a.qualified_at)}"
            )
            st.caption(ui.safe(a.block_reason) or "—")
    with right:
        ui.section("providers")
        providers = fetch(
            "providers",
            load_providers,
            str(ctx.home),
            str(ctx.ref.db_path),
            ctx.ref.session_id,
            ctx.busy,
        )
        if not providers:
            ui.empty_state("no fresh heartbeat for this session; provider state unknown")
        else:
            st.markdown("  ".join(f"{p.name} {ui.provider_badge(p.state)}" for p in providers))
    ui.section("open positions")
    positions = fetch(
        "positions-open", load_positions, str(ctx.ref.db_path), ctx.ref.session_id, ctx.busy, True
    )
    if positions is not None:
        ui.stacked_rows(
            [
                {
                    "sym": p.symbol or ui.short(p.mint),
                    "value": ui.money(p.current_value_eur),
                    "pnl": ui.pct(p.pnl_pct),
                    "held": ui.age(p.held_s),
                    "flags": ui.badge(p.provenance, "gray")
                    + ("" if p.value_is_executable else " " + ui.badge("ESTIMATED VALUE", "orange"))
                    + (" " + ui.badge("STALE", "orange") if p.data_stale else ""),
                }
                for p in positions
            ],
        )
    ui.section("latest events")
    events = fetch(
        "events-overview",
        load_events,
        str(ctx.ref.db_path),
        ctx.ref.session_id,
        ctx.busy,
        "ALL",
        8,
    )
    if events is not None:
        ui.stacked_rows(
            [
                {
                    "at": ui.when(e.at),
                    "cat": e.category,
                    "who": e.subject,
                    "what": ui.safe(e.message),
                }
                for e in events
            ],
            limit=8,
        )


def page_equity(ctx: Ctx, summary: SessionSummary) -> None:
    hist = fetch("history", load_history, str(ctx.ref.db_path), ctx.ref.session_id, ctx.busy, 600)
    if hist is None:
        return
    ui.metric_grid(
        [
            ("Equity", ui.money(summary.equity_eur), None),
            ("Peak equity", ui.money(summary.peak_equity_eur), None),
            ("Drawdown now", ui.pct(summary.drawdown_pct, signed=False), None),
            ("Max drawdown", ui.pct(hist.max_drawdown_pct, signed=False), None),
            ("Cash", ui.money(summary.cash_eur), None),
            ("Exposure", ui.money(summary.open_exposure_eur), None),
            ("Realized", ui.money(summary.realized_pnl_eur, signed=True), None),
            ("Unrealized", ui.money(summary.unrealized_pnl_eur, signed=True), None),
        ]
    )
    if not hist.points:
        ui.empty_state("no portfolio snapshots recorded yet")
        return
    start = float(summary.starting_equity_eur) if summary.starting_equity_eur else None
    ui.section("equity")
    st.plotly_chart(
        charts.equity_figure(hist.points, starting_equity=start), config=charts.PLOT_CONFIG
    )
    ui.section("drawdown")
    st.plotly_chart(charts.drawdown_figure(hist.points), config=charts.PLOT_CONFIG)
    ui.section("realized / unrealized / exposure")
    st.plotly_chart(charts.exposure_figure(hist.points), config=charts.PLOT_CONFIG)
    st.caption(
        f"{hist.total_points} snapshots"
        + (f", downsampled to {len(hist.points)} points" if hist.sampled else "")
        + "; maximum drawdown is computed from the full history."
    )


def _position_rows(items: list[PositionView]) -> list[dict[str, Any]]:
    return [
        {
            "symbol": p.symbol or ui.short(p.mint),
            "state": p.state,
            "opened": ui.when(p.opened_at),
            "closed": ui.when(p.closed_at),
            "cost €": float(p.cost_basis_eur),
            "value €": float(
                p.exit_value_eur if p.exit_value_eur is not None else p.current_value_eur
            ),
            "pnl €": float(p.pnl_eur),
            "pnl %": p.pnl_pct,
            "peak €": float(p.peak_value_eur),
            "trail dd": p.trailing_drawdown_pct,
            "held": ui.age(p.held_s),
            "value basis": "executable quote" if p.value_is_executable else "estimated (price)",
            "provenance": p.provenance,
            "units": p.units,
            "stale": p.data_stale,
            "exit": p.exit_reason or "",
            "mint": p.mint,
        }
        for p in items
    ]


def page_positions(ctx: Ctx, summary: SessionSummary) -> None:
    open_tab, closed_tab = st.tabs(["Open", "Closed"])
    with open_tab:
        items = fetch(
            "positions-open",
            load_positions,
            str(ctx.ref.db_path),
            ctx.ref.session_id,
            ctx.busy,
            True,
        )
        if items is not None:
            ui.table(_position_rows(items))
            for p in items:
                with st.expander(
                    f"{p.symbol or ui.short(p.mint)} · {p.state} · {ui.money(p.current_value_eur)}"
                ):
                    _position_detail(p)
    with closed_tab:
        items = fetch(
            "positions-all",
            load_positions,
            str(ctx.ref.db_path),
            ctx.ref.session_id,
            ctx.busy,
            False,
        )
        if items is not None:
            closed = [p for p in items if p.state == "CLOSED"]
            ui.table(_position_rows(closed))
            for p in closed[:50]:
                with st.expander(
                    f"{p.symbol or ui.short(p.mint)} · {p.exit_reason or 'CLOSED'} · "
                    f"{ui.money(p.realized_pnl_eur, signed=True)}"
                ):
                    _position_detail(p)
    st.caption(
        "Value is executable when it comes from a fresh sell quote, otherwise it is an estimate "
        "from the displayed price. Provenance SIMULATED = paper/dry-run fill; ESTIMATED = human "
        "confirmed at quoted amounts; USER-REPORTED = amounts typed by the user. No position is "
        "verified on-chain."
    )


def _position_detail(p: PositionView) -> None:
    st.markdown(
        f"{ui.state_badge(p.state)} {ui.badge(p.provenance, 'gray')} "
        f"{ui.badge('units ' + p.units, 'gray')} "
        f"{ui.badge('EXECUTABLE VALUE' if p.value_is_executable else 'ESTIMATED VALUE', 'green' if p.value_is_executable else 'orange')} "
        f"{ui.badge('DATA STALE', 'orange') if p.data_stale else ''} "
        f"{ui.badge('VERIFIED ON-CHAIN: NO', 'red')}"
    )
    ui.kv_block(
        [
            ("position", p.position_id),
            ("mint", p.mint),
            ("opened", ui.when(p.opened_at, seconds=True) + " UTC"),
            ("closed", ui.when(p.closed_at) + (" UTC" if p.closed_at else "")),
            ("quantity", f"{p.quantity_ui:,.6f} ({p.units})"),
            ("cost basis", ui.money(p.cost_basis_eur)),
            ("current value", ui.money(p.current_value_eur)),
            ("exit value", ui.money(p.exit_value_eur)),
            ("P&L", f"{ui.money(p.pnl_eur, signed=True)} ({ui.pct(p.pnl_pct)})"),
            ("realized", ui.money(p.realized_pnl_eur, signed=True)),
            (
                "peak value",
                f"{ui.money(p.peak_value_eur)} (trailing dd {ui.pct(p.trailing_drawdown_pct, signed=False)})",
            ),
            ("last valued", ui.since(p.last_valued_at)),
            ("last quote", ui.since(p.last_quote_at)),
            ("entry signal", p.entry_signal_id or "—"),
            ("exit signal", p.exit_signal_id or "—"),
            ("exit reason", p.exit_reason or "—"),
            ("simulated", "yes" if p.simulated else "no"),
        ]
    )


def page_candidates(ctx: Ctx, summary: SessionSummary) -> None:
    items = fetch(
        "candidates", load_candidates, str(ctx.ref.db_path), ctx.ref.session_id, ctx.busy, 200
    )
    if items is None:
        return
    counts: dict[str, int] = {}
    for c in items:
        counts[c.state] = counts.get(c.state, 0) + 1
    if counts:
        st.markdown("  ".join(f"{ui.state_badge(s)} {n}" for s, n in sorted(counts.items())))
    active = [c for c in items if c.state in ACTIVE_STATES]
    if active:
        ui.section("active")
        ui.stacked_rows(
            [
                {
                    "sym": c.symbol or ui.short(c.mint),
                    "state": ui.state_badge(c.state),
                    "score": ui.num(c.score),
                    "liq": ui.usd(c.liquidity_usd),
                    "m60": ui.pct(c.momentum_60s),
                    "age": ui.age(c.age_s),
                }
                for c in active
            ],
            limit=12,
        )
    states = sorted(counts)
    chosen = st.multiselect("states", states, default=states, key="cand_states")
    rows = [
        {
            "symbol": c.symbol or ui.short(c.mint),
            "state": c.state,
            "score": c.score,
            "age": ui.age(c.age_s),
            "liquidity $": c.liquidity_usd,
            "vol 5m $": c.volume_5m_usd,
            "buys/sells 5m": f"{c.buys_5m if c.buys_5m is not None else '—'}/{c.sells_5m if c.sells_5m is not None else '—'}",
            "velocity/min": c.trade_velocity_per_min,
            "mom 10s": c.momentum_10s,
            "mom 60s": c.momentum_60s,
            "accel": c.acceleration,
            "data age s": c.data_age_s,
            "stale": c.stale,
            "checks": c.check_verdict or "",
            "gate reason": c.gate_reason,
            "since": ui.when(c.state_at),
            "source": c.source or "",
            "mint": c.mint,
        }
        for c in items
        if c.state in chosen
    ]
    ui.table(
        rows,
        column_config={
            "score": st.column_config.NumberColumn(format="%.1f"),
            "liquidity $": st.column_config.NumberColumn(format="$%.0f"),
            "vol 5m $": st.column_config.NumberColumn(format="$%.0f"),
            "velocity/min": st.column_config.NumberColumn(format="%.1f"),
            "mom 10s": st.column_config.NumberColumn(format="%.3f"),
            "mom 60s": st.column_config.NumberColumn(format="%.3f"),
            "accel": st.column_config.NumberColumn(format="%.4f"),
            "data age s": st.column_config.NumberColumn(format="%.1f"),
        },
    )
    st.caption(
        f"{len(items)} most recently active tokens; state is the latest recorded transition, "
        "score/features/checks are the latest recorded rows of the session."
    )


def _attempt_expander(a: EntryAttempt) -> None:
    decision = str(a.final_decision)
    title = (
        f"{a.symbol or ui.short(a.mint)} · #{a.attempt_number} · {decision} · "
        f"{ui.when(a.qualified_at)} · score {a.qualified_score:.1f}"
    )
    with st.expander(title, expanded=False):
        st.markdown(
            f"{ui.decision_badge(decision)} "
            f"{ui.badge('decimals ' + a.decimals_status, 'gray')} "
            f"{ui.badge('buy quote ' + a.buy_quote_status, 'green' if a.buy_quote_status == 'ok' else 'orange')} "
            f"{ui.badge('sell quote ' + a.sell_quote_status, 'green' if a.sell_quote_status == 'ok' else 'orange')} "
            + (
                ui.badge(f"hysteresis holds {a.hysteresis_holds}", "blue")
                if a.hysteresis_holds
                else ""
            )
        )
        sizing = (
            f"{ui.money(a.recommended_eur)} ({a.recommended_sol:.4f} SOL)"
            if a.recommended_eur is not None and a.recommended_sol is not None
            else ("not reached" if not a.sizing_attempted else "n/a")
        ) + (f"  caps: {ui.safe(a.sizing_reason)}" if a.sizing_reason else "")
        rt = (
            f"{a.round_trip_loss_pct:.1%} estimated loss"
            f"{'' if a.round_trip_viable else ' (not viable)'}"
            if a.round_trip_loss_pct is not None
            else "n/a"
        )
        band = (
            f"{a.min_score_seen:.1f}–{a.max_score_seen:.1f} over {a.evaluations} evaluations"
            if a.min_score_seen is not None and a.max_score_seen is not None
            else "n/a"
        )
        ui.kv_block(
            [
                ("attempt", a.attempt_id),
                ("mint", a.mint),
                (
                    "qualified",
                    f"{ui.when(a.qualified_at)} UTC  score {a.qualified_score:.1f}  latch until {ui.when(a.latch_until)}",
                ),
                ("checks at qualification", ui.safe(a.qualified_checks) or "—"),
                ("sizing", sizing),
                (
                    "quotes",
                    f"{a.quote_attempts} attempt(s)"
                    + (f"  started {ui.when(a.quote_started_at)}" if a.quote_started_at else "")
                    + (f"  finished {ui.when(a.quote_finished_at)}" if a.quote_finished_at else "")
                    + (f"  ids {', '.join(a.quote_ids)}" if a.quote_ids else ""),
                ),
                ("quote error", ui.safe(a.quote_error) or "—"),
                (
                    "price impact",
                    f"buy {ui.num(a.entry_price_impact_pct, 2)}%  sell {ui.num(a.exit_price_impact_pct, 2)}%",
                ),
                ("round trip", rt),
                ("post-quote score", ui.num(a.post_quote_score)),
                ("score band", band),
                ("decision", decision),
                ("reason", ui.safe(a.block_reason) or "—"),
                ("signal", a.signal_id or "—"),
                ("completed", ui.when(a.completed_at) if a.completed_at else "still open"),
            ]
        )
        if a.qualified_features:
            ui.table(
                [{"feature": k, "value": ui.safe(str(v))} for k, v in a.qualified_features.items()],
                height=240,
            )


def page_attempts(ctx: Ctx, summary: SessionSummary) -> None:
    counts = fetch(
        "decision-counts",
        load_decision_counts,
        str(ctx.ref.db_path),
        ctx.ref.session_id,
        ctx.busy,
    )
    if counts:
        st.markdown(
            "  ".join(f"{ui.decision_badge(d)} {counts[d]}" for d in DECISIONS if d in counts)
        )
    choice = st.selectbox("decision", ("ALL", *DECISIONS), key="attempt_decision")
    items = fetch(
        "attempts",
        load_attempts,
        str(ctx.ref.db_path),
        ctx.ref.session_id,
        ctx.busy,
        None if choice == "ALL" else choice,
        300,
    )
    if items is None:
        return
    if not items:
        ui.empty_state("no entry attempts recorded: no token has reached QUALIFIED yet")
        return
    ui.table(
        [
            {
                "symbol": a.symbol or ui.short(a.mint),
                "#": a.attempt_number,
                "qualified": ui.when(a.qualified_at),
                "score": a.qualified_score,
                "post-quote": a.post_quote_score,
                "decision": str(a.final_decision),
                "reason": ui.safe(a.block_reason),
                "quotes": a.quote_attempts,
                "round trip": a.round_trip_loss_pct,
                "sizing €": float(a.recommended_eur) if a.recommended_eur is not None else None,
                "holds": a.hysteresis_holds,
            }
            for a in items
        ],
        column_config={
            "score": st.column_config.NumberColumn(format="%.1f"),
            "post-quote": st.column_config.NumberColumn(format="%.1f"),
            "round trip": st.column_config.NumberColumn(format="%.1f%%"),
            "sizing €": st.column_config.NumberColumn(format="€%.2f"),
        },
        height=min(420, 40 + 35 * len(items)),
    )
    ui.section("forensic detail")
    for a in items[:100]:
        _attempt_expander(a)


def _signal_rows(items: list[SignalView]) -> list[dict[str, Any]]:
    return [
        {
            "time": ui.when(s.created_at),
            "kind": s.kind,
            "status": s.status,
            "symbol": s.symbol or ui.short(s.mint),
            "score": s.score,
            "size €": float(s.recommended_eur) if s.recommended_eur is not None else None,
            "urgency": s.urgency or "",
            "decision": s.decision or "",
            "decided": ui.when(s.decided_at) if s.decided_at else "",
            "expires": ui.when(s.expires_at),
            "detail": s.detail,
            "signal": s.signal_id,
            "mint": s.mint,
        }
        for s in items
    ]


def page_signals(ctx: Ctx, summary: SessionSummary) -> None:
    st.caption(
        "Display only. Signals are confirmed or rejected in the terminal dashboard; this page "
        "cannot confirm, cancel or act on any of them."
    )
    items = fetch("signals", load_signals, str(ctx.ref.db_path), ctx.ref.session_id, ctx.busy, 200)
    if items is None:
        return
    pending = [s for s in items if s.status == "PENDING"]
    if pending:
        st.markdown(
            "  ".join(
                f"{ui.badge(s.kind + ' PENDING', 'orange')} {s.symbol or ui.short(s.mint)}"
                for s in pending
            )
        )
    ui.table(
        _signal_rows(items),
        column_config={
            "score": st.column_config.NumberColumn(format="%.1f"),
            "size €": st.column_config.NumberColumn(format="€%.2f"),
        },
    )


def _fill_rows(items: list[FillView]) -> list[dict[str, Any]]:
    return [
        {
            "time": ui.when(f.filled_at),
            "side": f.side,
            "symbol": f.symbol or ui.short(f.mint),
            "eur": float(f.eur_amount) if f.eur_amount is not None else None,
            "sol": float(f.sol_amount) if f.sol_amount is not None else None,
            "tokens (ui)": float(f.token_amount_ui) if f.token_amount_ui is not None else None,
            "fee €": float(f.fee_eur) if f.fee_eur is not None else None,
            "slippage €": float(f.slippage_cost_eur) if f.slippage_cost_eur is not None else None,
            "provenance": f.provenance,
            "kind": "simulated" if f.simulated else "estimated / user-reported",
            "units": f.units,
            "reported signature": f.reported_tx_signature or "",
            "verified on-chain": "no",
            "note": f.note,
            "signal": f.signal_id,
            "mint": f.mint,
        }
        for f in items
    ]


def page_fills(ctx: Ctx, summary: SessionSummary) -> None:
    items = fetch("fills", load_fills, str(ctx.ref.db_path), ctx.ref.session_id, ctx.busy, 300)
    if items is None:
        return
    sim = sum(1 for f in items if f.simulated)
    st.markdown(
        f"{ui.badge('SIMULATED', 'gray')} {sim}  "
        f"{ui.badge('ESTIMATED / USER-REPORTED', 'orange')} {len(items) - sim}  "
        f"{ui.badge('VERIFIED ON-CHAIN', 'red')} 0 (never; this software does not reconcile fills)"
    )
    ui.table(
        _fill_rows(items),
        column_config={
            "eur": st.column_config.NumberColumn(format="€%.2f"),
            "sol": st.column_config.NumberColumn(format="%.4f"),
            "fee €": st.column_config.NumberColumn(format="€%.2f"),
            "slippage €": st.column_config.NumberColumn(format="€%.2f"),
        },
    )


def page_token(ctx: Ctx, summary: SessionSummary) -> None:
    query = st.text_input("mint or symbol", key="token_query", placeholder="mint address or symbol")
    if not query.strip():
        ui.empty_state("type a mint address or a symbol to inspect a token")
        return
    hits = fetch(
        "search", load_search, str(ctx.ref.db_path), ctx.ref.session_id, ctx.busy, query.strip()
    )
    if hits is None:
        return
    mint = query.strip()
    if hits:
        labels = {h.mint: f"{h.symbol or '?'} · {ui.short(h.mint)}" for h in hits}
        mint = st.selectbox(
            "match", list(labels), format_func=lambda m: labels[m], key="token_match"
        )
    detail = fetch(
        "token:" + mint, load_token, str(ctx.ref.db_path), ctx.ref.session_id, ctx.busy, mint
    )
    if detail is None:
        return
    if not detail.found and not detail.transitions and not detail.scores:
        ui.empty_state("no token recorded under that mint in this database")
        return
    st.markdown(
        f"**{detail.symbol or '?'}** {detail.name or ''}  "
        f"{ui.state_badge(detail.final_state) if detail.final_state else ''} "
        f"{ui.badge(detail.venue or '?', 'gray')} {ui.badge(detail.source or '?', 'gray')}"
    )
    ui.kv_block(
        [
            ("mint", detail.mint),
            ("decimals", str(detail.decimals) if detail.decimals is not None else "unknown"),
            ("pool", detail.pool_address or "—"),
            (
                "pool created",
                ui.when(detail.pool_created_at, seconds=False) + " UTC"
                if detail.pool_created_at
                else "—",
            ),
            ("discovered", ui.when(detail.discovered_at) + " UTC" if detail.discovered_at else "—"),
            ("first seen in session", detail.first_session_id or "—"),
            (
                "observations",
                f"{detail.observation_count}{'+' if detail.observation_count_capped else ''} in this session",
            ),
            ("checks", f"{detail.check_verdict or '—'} at {ui.when(detail.checks_at)}"),
        ]
    )
    tabs = st.tabs(
        [
            "Timeline",
            "Scores",
            "Features",
            "Checks",
            "Quotes",
            "Attempts",
            "Signals & fills",
            "Outcomes",
            "Price",
        ]
    )
    with tabs[0]:
        ui.table(
            [
                {"time": ui.when(t.at), "from": t.source, "to": t.target, "reason": t.reason}
                for t in detail.transitions
            ]
        )
    with tabs[1]:
        if detail.scores:
            st.plotly_chart(charts.score_figure(detail.scores), config=charts.PLOT_CONFIG)
            last = detail.scores[-1]
            ui.kv_block(
                [
                    ("latest", f"{last.score:.1f} at {ui.when(last.at)}"),
                    ("reasons", ", ".join(ui.safe(r) for r in last.reasons) or "—"),
                    ("penalties", ", ".join(ui.safe(p) for p in last.penalties) or "—"),
                ]
            )
        else:
            ui.empty_state("never scored")
    with tabs[2]:
        if detail.features:
            ui.table(
                [{"feature": k, "value": ui.safe(str(v))} for k, v in detail.features.items()],
                height=420,
            )
            st.caption(f"computed {ui.when(detail.features_at)} UTC")
        else:
            ui.empty_state("no feature vector recorded in this session")
    with tabs[3]:
        ui.table(list(detail.checks))
    with tabs[4]:
        ui.table(
            [
                {
                    "time": ui.when(q.quoted_at),
                    "provider": q.provider,
                    "in": ui.short(q.input_mint),
                    "out": ui.short(q.output_mint),
                    "in raw": q.in_amount_raw,
                    "out raw": q.out_amount_raw,
                    "impact %": q.price_impact_pct,
                    "slippage bps": q.slippage_bps,
                    "route": q.route,
                    "latency ms": q.latency_ms,
                    "id": q.quote_id,
                }
                for q in detail.quotes
            ]
        )
    with tabs[5]:
        if detail.attempts:
            for a in detail.attempts:
                _attempt_expander(a)
        else:
            ui.empty_state("no entry attempt: the token never reached QUALIFIED")
    with tabs[6]:
        ui.section("signals")
        ui.table(_signal_rows(list(detail.signals)))
        ui.section("fills")
        ui.table(_fill_rows(list(detail.fills)))
    with tabs[7]:
        ui.table(
            [
                {
                    "first seen": ui.when(o.first_seen_at),
                    "obs": o.observations,
                    "peak x": o.max_multiple,
                    "end x": o.final_multiple,
                    "dd from peak": o.max_drawdown_from_peak,
                    "to peak s": o.time_to_peak_s,
                    "best score": o.best_score,
                    "qualified": o.qualified,
                    "signalled": o.signalled,
                    "entered": o.entered,
                    "closed pnl": o.closed_pnl_pct,
                    "liq. pulled": o.liquidity_collapsed,
                    "reject": o.reject_reason or "",
                    "truncated": o.truncated,
                    "market": o.market_data,
                    "execution": o.execution,
                }
                for o in detail.outcomes
            ]
        )
    with tabs[8]:
        if detail.observations:
            st.plotly_chart(charts.price_figure(detail.observations), config=charts.PLOT_CONFIG)
            st.caption(f"last {len(detail.observations)} observations of this session")
        else:
            ui.empty_state("no observations in this session")


def page_outcomes(ctx: Ctx, summary: SessionSummary) -> None:
    c1, c2 = st.columns(2)
    with c1:
        include_truncated = st.checkbox("include rows truncated at shutdown", key="oc_trunc")
    with c2:
        min_obs = st.slider("minimum observations per row", 1, 30, 5, key="oc_minobs")
    data = fetch(
        "outcomes",
        load_outcomes,
        str(ctx.ref.db_path),
        ctx.ref.session_id,
        ctx.busy,
        include_truncated,
        min_obs,
    )
    if data is None:
        return
    for integ in data.incomplete:
        st.error(
            f"INCOMPLETE DATA session {integ.session_id}: the storage writer dropped "
            f"{integ.dropped_total} rows ({integ.dropped_by_kind}) and failed "
            f"{integ.failed_total}. Outcome rows themselves are never dropped, but the "
            "observations behind them have holes; treat every rate below as a lower-quality "
            "estimate."
        )
    rep = data.report
    if rep.used_rows == 0:
        ui.empty_state(
            f"no usable outcomes ({rep.total_rows} rows recorded, {rep.excluded_truncated} "
            f"truncated, {rep.excluded_short} too short). Run the engine for longer than "
            "outcomes.horizon_s and check again."
        )
        return
    horizon = f"{rep.horizon_s:.0f}s" if rep.horizon_s else "mixed"
    ui.section(
        f"forward outcomes: {rep.used_rows} tokens, horizon {horizon} (passive observation from "
        "first sight; excludes slippage, fees, fill risk)"
    )
    rows = []
    for b in rep.buckets:
        if b.n == 0:
            continue
        row: dict[str, Any] = {"group": b.name, "n": b.n}
        for m in MULTIPLES:
            lo, hi = b.interval(m)
            row[f">={m:g}x"] = f"{b.rate(m):.0%} [{lo:.0%}–{hi:.0%}]"
        row["median peak"] = f"{b.median_max_multiple:.2f}x" if b.median_max_multiple else "—"
        row["median end"] = f"{b.median_final_multiple:.2f}x" if b.median_final_multiple else "—"
        row["median dd"] = f"{b.median_drawdown:.0%}" if b.median_drawdown is not None else "—"
        row["liq. pulled"] = f"{b.rug_rate:.0%}"
        row["closed pnl"] = (
            f"{b.median_closed_pnl_pct:+.0%} (n={b.entered})"
            if b.median_closed_pnl_pct is not None
            else "—"
        )
        row["meaningful"] = "yes" if b.meaningful else f"no (n < {MIN_MEANINGFUL_N})"
        rows.append(row)
    ui.table(rows)
    st.caption(
        f"excluded: {rep.excluded_truncated} truncated at shutdown, {rep.excluded_short} with "
        f"< {min_obs} observations. Market data: {rep.live_market_rows} live Solana, "
        f"{rep.synthetic_rows} synthetic, {rep.legacy_rows} legacy (unknown). Execution: "
        f"{rep.simulated_rows} simulated (paper/dry-run), {rep.live_rows} manual-signal."
    )
    st.warning(
        f"Read this carefully: groups with n < {MIN_MEANINGFUL_N} are marked not meaningful "
        "because their rates are noise; brackets are 95% Wilson intervals. Multiples are what a "
        "passive observer saw from the first snapshot, not what a buyer would have realised. "
        "Past outcomes do not predict future ones. This page makes no claim of profitability."
    )
    if rep.all_synthetic:
        st.warning(
            "Every row here is from the synthetic world. It says nothing about real Solana tokens."
        )
    elif rep.mixed_provenance:
        st.warning("Live, synthetic and/or legacy rows are mixed in this session.")
    if rep.live_market_rows and rep.simulated_rows:
        st.info(
            "These are live Solana market observations with simulated execution (paper). The "
            "multiples are passive price paths, not executable returns: slippage, fill risk and "
            "liquidity pulls are not in them."
        )
    with st.expander(f"latest {len(data.rows)} outcome rows"):
        ui.table(
            [
                {
                    "symbol": o.symbol or ui.short(o.mint),
                    "first seen": ui.when(o.first_seen_at),
                    "obs": o.observations,
                    "peak x": o.max_multiple,
                    "end x": o.final_multiple,
                    "dd": o.max_drawdown_from_peak,
                    "best score": o.best_score,
                    "qualified": o.qualified,
                    "signalled": o.signalled,
                    "entered": o.entered,
                    "closed pnl": o.closed_pnl_pct,
                    "liq. pulled": o.liquidity_collapsed,
                    "reject": o.reject_reason or "",
                    "truncated": o.truncated,
                    "market": o.market_data,
                    "execution": o.execution,
                }
                for o in data.rows
            ]
        )


def page_providers(ctx: Ctx, summary: SessionSummary) -> None:
    providers = fetch(
        "providers",
        load_providers,
        str(ctx.home),
        str(ctx.ref.db_path),
        ctx.ref.session_id,
        ctx.busy,
    )
    if providers is None:
        return
    if not providers:
        ui.empty_state(
            "Provider health comes from the running engine's heartbeat (status.json). No fresh "
            "heartbeat names this session, so its provider state is unknown; the events page "
            "still lists recorded provider errors."
        )
        return
    for p in providers:
        with st.container(border=True):
            st.markdown(f"**{p.name}** {ui.provider_badge(p.state)}  `{p.host or ''}`")
            ui.metric_grid(
                [
                    ("Requests", str(p.requests), None),
                    ("Rate limited", str(p.rate_limited), None),
                    ("Retries", str(p.retries), None),
                    ("Failures", str(p.failures), None),
                    ("Circuit trips", str(p.circuit_trips), None),
                    ("Cooldown", f"{p.cooldown_s:.0f}s", None),
                    ("In flight / waiting", f"{p.inflight} / {p.waiting}", None),
                    ("Recoveries", str(p.recoveries), None),
                ],
                columns=4,
            )
            if p.last_error:
                st.caption(f"last error: {p.last_error}")


def page_engine(ctx: Ctx, summary: SessionSummary) -> None:
    eh = fetch(
        "engine", load_engine, str(ctx.home), str(ctx.ref.db_path), ctx.ref.session_id, ctx.busy
    )
    if eh is None:
        return
    ui.integrity_banner(eh.integrity)
    if not eh.heartbeat_found:
        st.info(
            "No heartbeat file: the engine has not run in this runtime home, or it was cleaned."
        )
    elif not eh.heartbeat_for_this_session:
        st.info(
            f"The heartbeat belongs to session {eh.heartbeat_session_id} "
            f"(written {ui.since(eh.written_at)}); this session is not the running one."
        )
    else:
        st.markdown(
            f"{ui.engine_badge(eh.state)} {ui.badge('FRESH' if eh.fresh else 'STALE HEARTBEAT', 'green' if eh.fresh else 'orange')} "
            f"heartbeat {ui.since(eh.written_at)}"
            + (f"  ·  stopped: {eh.stop_reason}" if eh.stop_reason else "")
        )
        for p in eh.problems:
            st.error(p)
        for d in eh.degraded:
            st.warning(d)
        ui.metric_grid(
            [
                ("Uptime", ui.age(eh.uptime_s), None),
                ("Last tick", ui.age(eh.last_tick_age_s) + " ago", None),
                ("Tick p50 / p95", f"{ui.num(eh.tick_p50_ms)} / {ui.num(eh.tick_p95_ms)} ms", None),
                ("Evaluations", ui.num(eh.evaluations), None),
                ("Watched tokens", ui.num(eh.watched), None),
                ("Snapshots / min", ui.num(eh.snapshots_last_minute), None),
                ("Last snapshot", ui.age(eh.last_snapshot_age_s) + " ago", None),
                ("Discovery / s", ui.num(eh.discovery_rate_per_s, 2), None),
                ("Storage queued", ui.num(eh.db_queued), None),
                ("Storage dropped", ui.num(eh.db_dropped), None),
                ("Storage failures", ui.num(eh.db_failures), None),
                ("Last flush", ui.age(eh.db_last_flush_age_s) + " ago", None),
            ],
            columns=4,
        )
        ui.kv_block(
            [
                ("pid / host", f"{eh.pid} / {eh.hostname}"),
                ("starts", f"{eh.starts} (previous exit {eh.previous_exit or '?'})"),
                (
                    "monitored / qualified / pending signals",
                    f"{eh.tokens_monitored} / {eh.qualified} / {eh.pending_signals}",
                ),
                ("last signal", ui.since(eh.last_signal_at)),
                ("last error", eh.last_error or "—"),
                (
                    "database",
                    "ok" if eh.db_ok else f"write failures ({eh.db_last_error or 'unknown'})",
                ),
                ("records verified on-chain", "never (no reconciliation exists)"),
            ]
        )
        if eh.db_integrity:
            with st.expander("storage integrity (live counters)"):
                ui.table([{"key": k, "value": str(v)} for k, v in eh.db_integrity.items()])
        if eh.counters:
            with st.expander("engine counters"):
                ui.table([{"counter": k, "value": v} for k, v in eh.counters.items()])
        if eh.connections:
            ui.section("connections")
            st.markdown(
                "  ".join(
                    f"{c.name} {ui.badge('connected' if c.connected else 'disconnected', 'green' if c.connected else 'red')} "
                    f"({c.kind}, activity {ui.age(c.last_activity_age_s)} ago)"
                    for c in eh.connections
                )
            )
    if eh.integrity is not None:
        ui.section("persisted write integrity")
        ui.kv_block(
            [
                ("complete", "yes" if eh.integrity.complete else "NO"),
                ("dropped", f"{eh.integrity.dropped_total} {eh.integrity.dropped_by_kind or ''}"),
                ("failed", f"{eh.integrity.failed_total} {eh.integrity.failed_by_kind or ''}"),
                ("updated", ui.since(eh.integrity.updated_at)),
                ("last error", eh.integrity.last_error or "—"),
            ]
        )


def page_events(ctx: Ctx, summary: SessionSummary) -> None:
    category = st.radio("filter", EVENT_CATEGORIES, horizontal=True, key="event_filter")
    items = fetch(
        "events:" + str(category),
        load_events,
        str(ctx.ref.db_path),
        ctx.ref.session_id,
        ctx.busy,
        str(category),
        300,
    )
    if items is None:
        return
    ui.table(
        [
            {
                "time": ui.when(e.at),
                "category": e.category,
                "source": e.source,
                "subject": e.subject,
                "message": e.message,
                "detail": e.detail,
            }
            for e in items
        ],
        height=min(640, 40 + 35 * max(1, len(items))),
    )


PAGE_RENDERERS: dict[str, Callable[[Ctx, SessionSummary], None]] = {}


@dataclass(frozen=True, slots=True)
class Ctx:
    home: Path
    ref: SessionRef
    busy: float


PAGE_RENDERERS.update(
    {
        "Overview": page_overview,
        "Equity": page_equity,
        "Positions": page_positions,
        "Candidates": page_candidates,
        "Entry attempts": page_attempts,
        "Signals": page_signals,
        "Fills": page_fills,
        "Token": page_token,
        "Outcomes": page_outcomes,
        "Providers": page_providers,
        "Engine": page_engine,
        "Events": page_events,
    }
)


# --------------------------------------------------------------------- main


def _select_session(
    refs: list[SessionRef], cfg: WebConfig, running: str | None
) -> SessionRef | None:
    default = resolve_selection(refs, paper=cfg.paper, session=cfg.session)
    if default is None and (cfg.paper or cfg.session):
        st.sidebar.error(
            f"session {cfg.paper or cfg.session!r} not found under {cfg.home}; "
            "showing the sessions that exist"
        )
        default = resolve_selection(refs)
    if default is None:
        return None
    kinds = [k for k in ("PAPER", "LIVE") if any(r.kind == k for r in refs)]
    st.session_state.setdefault("kind_select", default.kind)
    if st.session_state["kind_select"] not in kinds:
        st.session_state["kind_select"] = kinds[0]
    kind = st.sidebar.radio(
        "mode",
        kinds,
        horizontal=True,
        key="kind_select",
        format_func=lambda k: "PAPER sessions" if k == "PAPER" else "LIVE / DRY-RUN sessions",
    )
    options = [r for r in refs if r.kind == kind]
    ids = [r.session_id for r in options]
    labels = {r.session_id: r.label for r in options}
    key = f"session_select:{kind}"
    if key not in st.session_state or st.session_state[key] not in ids:
        st.session_state[key] = default.session_id if default.session_id in ids else ids[0]
    chosen = st.sidebar.selectbox("session", ids, format_func=lambda s: labels[s], key=key)
    for r in options:
        if r.session_id == chosen:
            return r
    return options[0]


def main() -> None:
    cfg = WebConfig.load()
    st.set_page_config(
        page_title="Solana Sniper",
        page_icon="◆",
        layout="wide",
        initial_sidebar_state="auto",
        menu_items={
            "Get help": None,
            "Report a bug": None,
            "About": "solana-sniper read-only dashboard",
        },
    )
    ui.inject_theme()
    heartbeat: Heartbeat | None = read_heartbeat(cfg.home)
    running = running_session_id(heartbeat)
    with st.sidebar:
        st.markdown("### ◆ SOLANA SNIPER")
        st.caption(f"read-only dashboard v{__version__}")
        st.caption(f"runtime home `{cfg.home}`")
        st.markdown(
            f"{ui.badge('READ-ONLY', 'blue')} {ui.badge('NO KEYS', 'red')} "
            f"{ui.badge('NO SIGNING', 'red')} {ui.badge('NO BROADCAST', 'red')}"
        )
    try:
        refs = cached_sessions(str(cfg.home), running)
    except DashboardError as exc:
        st.error(f"Could not discover sessions: {safe_exception(exc)}")
        return
    if not refs:
        st.info(
            f"No sessions found under {cfg.home}. Start one with "
            "`solana-sniper paper --bankroll-sol 1` (or `solana-sniper run --dry-run`) and this "
            "page will pick it up."
        )
        return
    ref = _select_session(refs, cfg, running)
    if ref is None:
        st.info("No session selected.")
        return
    with st.sidebar:
        auto = st.toggle("auto-refresh", value=True, key="auto_refresh")
        refresh = st.slider(
            "refresh seconds", MIN_REFRESH_S, MAX_REFRESH_S, cfg.refresh_seconds, key="refresh_s"
        )
        st.caption(
            f"{len(refs)} sessions · running: {running or 'none'} · "
            f"heartbeat {ui.since(heartbeat.written_at) if heartbeat else 'absent'}"
        )
    page = st.segmented_control(
        "page",
        PAGES,
        default="Overview",
        selection_mode="single",
        key="page_select",
        label_visibility="collapsed",
    )
    ctx = Ctx(home=cfg.home, ref=ref, busy=cfg.busy_timeout_s)

    def body() -> None:
        summary = fetch(
            "summary:" + ref.session_id,
            load_summary,
            str(cfg.home),
            str(ref.db_path),
            ref.session_id,
            cfg.busy_timeout_s,
            ref,
        )
        if summary is None:
            return
        ui.provenance_header(
            summary.provenance,
            session_id=ref.session_id,
            alive=summary.alive,
            alive_detail=summary.alive_detail,
            engine_state=summary.engine_state,
        )
        renderer = PAGE_RENDERERS.get(str(page or "Overview"), page_overview)
        renderer(ctx, summary)

    run_every = timedelta(seconds=int(refresh)) if auto else None
    st.fragment(run_every=run_every)(body)()


if __name__ == "__main__" or st.runtime.exists():
    main()
