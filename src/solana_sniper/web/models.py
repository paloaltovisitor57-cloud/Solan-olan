"""View models for the web dashboard: plain frozen dataclasses, no Streamlit, no I/O.

Everything the pages render comes through these types, so a widget can never reach a raw row
or a database handle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from solana_sniper.domain.models import EntryAttempt
from solana_sniper.strategy.evaluation import EvaluationReport

SessionKind = Literal["PAPER", "LIVE"]

ALIVE_RUNNING = "RUNNING"
ALIVE_ENDED = "ENDED"
ALIVE_NOT_RUNNING = "NOT RUNNING"


@dataclass(frozen=True, slots=True)
class SessionRef:
    """One discoverable session and the database it lives in."""

    kind: SessionKind
    session_id: str
    db_path: Path
    mode: str  # PAPER | LIVE | DRY_RUN | REPLAY | UNKNOWN
    name: str | None
    started_at: datetime | None
    ended_at: datetime | None
    running: bool
    requested: str | None = None  # paper bankroll as requested ("1 SOL")
    note: str | None = None  # e.g. why the file could not be read

    @property
    def label(self) -> str:
        bits = [self.session_id]
        if self.requested:
            bits.append(self.requested)
        if self.running:
            bits.append("RUNNING")
        elif self.ended_at is not None:
            bits.append("ended")
        if self.note:
            bits.append(self.note)
        return " · ".join(bits)


@dataclass(frozen=True, slots=True)
class Heartbeat:
    """The engine's status.json as far as the dashboard trusts it."""

    path: Path
    status: dict[str, Any]
    fresh: bool  # written recently and not marked stopped
    session_id: str | None
    written_at: datetime | None
    age_s: float | None


@dataclass(frozen=True, slots=True)
class Provenance:
    """What the numbers on screen are: which market data, which execution, and whether real
    transactions were involved (only an AUTONOMOUS session ever signed and broadcast; this page
    itself never does)."""

    mode_label: str  # PAPER | PAPER / TEST | LIVE / SIGNAL MODE | LIVE / AUTONOMOUS | DRY RUN ...
    market_data: str  # LIVE | SYNTHETIC | MIXED | NOT YET OBSERVED | RECORDED
    execution: str  # SIMULATED | MANUAL SIGNAL / ... | BOT-SIGNED, RECONCILED ON-CHAIN
    real_transactions: str  # DISABLED | NOT RECONCILED ON-CHAIN | ENABLED (hot wallet)
    simulated: bool
    tone: Literal["paper", "test", "live", "autonomous", "unknown"]

    def lines(self) -> tuple[str, ...]:
        return (
            self.mode_label,
            f"MARKET DATA: {self.market_data}",
            f"EXECUTION: {self.execution}",
            f"REAL TRANSACTIONS: {self.real_transactions}",
        )


def provenance_for(mode: str, market_data: str) -> Provenance:
    """Derive the header from the recorded run mode and the observed market-data provenance.

    A live-data paper run is never called synthetic: only sessions whose recorded observations
    or outcomes are synthetic get the TEST label."""
    mode_u = (mode or "UNKNOWN").upper()
    synthetic = market_data == "SYNTHETIC"
    if mode_u == "PAPER":
        return Provenance(
            mode_label="PAPER / TEST" if synthetic else "PAPER",
            market_data=market_data,
            execution="SIMULATED",
            real_transactions="DISABLED",
            simulated=True,
            tone="test" if synthetic else "paper",
        )
    if mode_u == "DRY_RUN":
        return Provenance(
            mode_label="DRY RUN / TEST" if synthetic else "DRY RUN",
            market_data=market_data,
            execution="SIMULATED",
            real_transactions="DISABLED",
            simulated=True,
            tone="test" if synthetic else "paper",
        )
    if mode_u == "REPLAY":
        return Provenance(
            mode_label="REPLAY",
            market_data="RECORDED",
            execution="SIMULATED",
            real_transactions="DISABLED",
            simulated=True,
            tone="test",
        )
    if mode_u == "LIVE":
        return Provenance(
            mode_label="LIVE / SIGNAL MODE",
            market_data=market_data,
            execution="MANUAL SIGNAL / ESTIMATED / USER-REPORTED",
            real_transactions="NOT RECONCILED ON-CHAIN",
            simulated=False,
            tone="live",
        )
    if mode_u == "AUTONOMOUS":
        return Provenance(
            mode_label="LIVE / AUTONOMOUS",
            market_data=market_data,
            execution="BOT-SIGNED, RECONCILED ON-CHAIN",
            real_transactions="ENABLED (hot wallet)",
            simulated=False,
            tone="autonomous",
        )
    return Provenance(
        mode_label=f"UNKNOWN MODE ({mode_u})",
        market_data=market_data,
        execution="UNKNOWN",
        real_transactions="UNKNOWN (only an AUTONOMOUS session ever broadcasts)",
        simulated=False,
        tone="unknown",
    )


@dataclass(frozen=True, slots=True)
class PaperMeta:
    session_id: str
    name: str | None
    created_at: datetime
    requested: str
    bankroll_sol: Decimal
    bankroll_eur: Decimal
    sol_eur_start: Decimal
    fx_source: str
    fx_at: datetime


@dataclass(frozen=True, slots=True)
class Integrity:
    session_id: str
    updated_at: datetime | None
    complete: bool
    dropped_total: int
    failed_total: int
    dropped_by_kind: dict[str, int]
    failed_by_kind: dict[str, int]
    last_error: str | None


@dataclass(frozen=True, slots=True)
class SessionSummary:
    ref: SessionRef
    mode: str
    provenance: Provenance
    alive: str  # RUNNING | ENDED | NOT RUNNING
    alive_detail: str
    engine_state: str | None  # HEALTHY | DEGRADED | UNHEALTHY | STOPPED (heartbeat)
    started_at: datetime | None
    ended_at: datetime | None
    paper: PaperMeta | None
    starting_equity_eur: Decimal | None
    equity_eur: Decimal | None
    cash_eur: Decimal | None
    open_exposure_eur: Decimal | None
    open_value_eur: Decimal | None
    peak_equity_eur: Decimal | None
    realized_pnl_eur: Decimal | None
    unrealized_pnl_eur: Decimal | None
    fees_eur: Decimal | None
    slippage_eur: Decimal | None
    return_pct: float | None
    drawdown_pct: float | None
    wins: int
    losses: int
    snapshot_at: datetime | None
    open_positions: int
    positions_total: int
    signals: int
    fills: int
    entry_attempts: int
    tokens: int
    errors: int
    outcomes: int
    quotes: int
    latest_attempt: EntryAttempt | None
    integrity: Integrity | None
    last_write_at: datetime | None
    snapshot_count: int


@dataclass(frozen=True, slots=True)
class EquityPoint:
    at: datetime
    equity_eur: float
    cash_eur: float
    open_exposure_eur: float
    open_value_eur: float
    realized_pnl_eur: float
    unrealized_pnl_eur: float
    drawdown_pct: float
    open_positions: int


@dataclass(frozen=True, slots=True)
class EquityHistory:
    points: tuple[EquityPoint, ...]
    total_points: int
    sampled: bool
    max_drawdown_pct: float | None  # from the full history, not the sample


@dataclass(frozen=True, slots=True)
class PositionView:
    position_id: str
    mint: str
    symbol: str | None
    state: str
    opened_at: datetime
    closed_at: datetime | None
    cost_basis_eur: Decimal
    current_value_eur: Decimal
    exit_value_eur: Decimal | None
    pnl_eur: Decimal
    pnl_pct: float
    peak_value_eur: Decimal
    trailing_drawdown_pct: float
    quantity_ui: Decimal
    units: str
    provenance: str
    value_is_executable: bool
    data_stale: bool
    exit_reason: str | None
    realized_pnl_eur: Decimal | None
    entry_signal_id: str | None
    exit_signal_id: str | None
    simulated: bool
    held_s: float
    last_valued_at: datetime | None
    last_quote_at: datetime | None
    verified_onchain: bool = False


@dataclass(frozen=True, slots=True)
class CandidateView:
    mint: str
    symbol: str | None
    name: str | None
    state: str
    state_at: datetime | None
    state_reason: str
    score: float | None
    scored_at: datetime | None
    age_s: float | None
    liquidity_usd: float | None
    volume_5m_usd: float | None
    buys_5m: int | None
    sells_5m: int | None
    trade_velocity_per_min: float | None
    momentum_60s: float | None
    momentum_10s: float | None
    acceleration: float | None
    data_age_s: float | None
    stale: bool | None
    check_verdict: str | None
    gate_reason: str
    source: str | None
    venue: str | None


@dataclass(frozen=True, slots=True)
class SignalView:
    signal_id: str
    kind: str
    status: str
    mint: str
    symbol: str | None
    created_at: datetime
    expires_at: datetime
    urgency: str | None
    score: float | None
    recommended_eur: Decimal | None
    detail: str
    decision: str | None  # CONFIRM by DRY_RUN, REJECT by HUMAN, ...
    decided_at: datetime | None


@dataclass(frozen=True, slots=True)
class FillView:
    fill_id: str
    signal_id: str
    side: str
    mint: str
    symbol: str | None
    filled_at: datetime
    sol_amount: Decimal | None
    token_amount_ui: Decimal | None
    eur_amount: Decimal | None
    fee_eur: Decimal | None
    slippage_cost_eur: Decimal | None
    provenance: str
    simulated: bool
    units: str
    reported_tx_signature: str | None
    note: str
    readable: bool = True
    verified_onchain: bool = False
    tx_signature: str | None = None  # the confirmed signature (autonomous fills only)


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    name: str
    host: str | None
    state: str
    cooldown_s: float
    inflight: int
    waiting: int
    consecutive_failures: int
    circuit_trips: int
    requests: int
    rate_limited: int
    retries: int
    backoffs: int
    fast_fails: int
    failures: int
    recoveries: int
    last_error: str | None


@dataclass(frozen=True, slots=True)
class ConnectionHealth:
    name: str
    kind: str
    connected: bool
    last_activity_age_s: float | None


@dataclass(frozen=True, slots=True)
class AutonomyHealth:
    """The heartbeat's `autonomy` block of an AUTONOMOUS session (all public values)."""

    armed: bool
    state: str
    kill_switch: bool
    disarmed_reason: str | None
    wallet_public_key: str | None
    wallet_sol: str | None
    wallet_checked_at: datetime | None
    spent_today_sol: str | None
    loss_sol: str | None
    max_total_loss_sol: str | None
    caps: dict[str, Any]
    intents_in_flight: int
    sends: int
    confirmed: int
    failed: int
    last_send_at: datetime | None
    last_confirmed_at: datetime | None


@dataclass(frozen=True, slots=True)
class EngineHealth:
    heartbeat_found: bool
    heartbeat_for_this_session: bool
    heartbeat_session_id: str | None
    fresh: bool
    written_at: datetime | None
    age_s: float | None
    state: str | None
    healthy: bool | None
    problems: tuple[str, ...]
    degraded: tuple[str, ...]
    uptime_s: float | None
    pid: int | None
    hostname: str | None
    starts: int | None
    previous_exit: str | None
    last_tick_age_s: float | None
    tick_p50_ms: float | None
    tick_p95_ms: float | None
    evaluations: int | None
    market_ok: bool | None
    watched: int | None
    last_snapshot_age_s: float | None
    snapshots_last_minute: int | None
    discovery_rate_per_s: float | None
    db_ok: bool | None
    db_last_flush_age_s: float | None
    db_last_error: str | None
    db_failures: int | None
    db_dropped: int | None
    db_queued: int | None
    db_integrity: dict[str, Any]
    counters: dict[str, int]
    connections: tuple[ConnectionHealth, ...]
    tokens_monitored: int | None
    qualified: int | None
    pending_signals: int | None
    last_signal_at: datetime | None
    last_error: str | None
    records_verified_onchain: bool
    integrity: Integrity | None  # persisted counters (survive a crash)
    stop_reason: str | None
    autonomy: AutonomyHealth | None = None


EventCategory = Literal["ALL", "TRADING", "DATA", "PROVIDERS", "ERRORS"]
EVENT_CATEGORIES: tuple[EventCategory, ...] = ("ALL", "TRADING", "DATA", "PROVIDERS", "ERRORS")


@dataclass(frozen=True, slots=True)
class EventItem:
    at: datetime
    category: str  # TRADING | DATA | PROVIDERS | ERRORS
    source: str  # transition | error | fill | signal | milestone | heartbeat
    subject: str  # symbol or mint or component
    message: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ScorePoint:
    at: datetime
    score: float
    reasons: tuple[str, ...]
    penalties: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TransitionView:
    at: datetime
    source: str
    target: str
    reason: str


@dataclass(frozen=True, slots=True)
class QuoteView:
    quote_id: str
    quoted_at: datetime
    provider: str
    input_mint: str
    output_mint: str
    in_amount_raw: int | None
    out_amount_raw: int | None
    price_impact_pct: float | None
    slippage_bps: int | None
    route: str
    latency_ms: float | None


@dataclass(frozen=True, slots=True)
class ObservationPoint:
    at: datetime
    price_native: float | None
    liquidity_usd: float | None
    volume_5m_usd: float | None
    source: str


@dataclass(frozen=True, slots=True)
class OutcomeView:
    mint: str
    symbol: str | None
    first_seen_at: datetime
    finalized_at: datetime
    observations: int
    max_multiple: float
    final_multiple: float
    max_drawdown_from_peak: float
    time_to_peak_s: float | None
    best_score: float | None
    qualified: bool
    signalled: bool
    entered: bool
    closed_pnl_pct: float | None
    exit_reason: str | None
    liquidity_collapsed: bool
    reject_reason: str | None
    truncated: bool
    market_data: str
    execution: str


@dataclass(frozen=True, slots=True)
class TokenDetail:
    mint: str
    found: bool
    symbol: str | None
    name: str | None
    decimals: int | None
    venue: str | None
    source: str | None
    pool_address: str | None
    created_at: datetime | None
    pool_created_at: datetime | None
    discovered_at: datetime | None
    final_state: str | None
    first_session_id: str | None
    transitions: tuple[TransitionView, ...]
    scores: tuple[ScorePoint, ...]
    features: dict[str, Any] | None
    features_at: datetime | None
    checks: tuple[dict[str, Any], ...]
    checks_at: datetime | None
    check_verdict: str | None
    quotes: tuple[QuoteView, ...]
    attempts: tuple[EntryAttempt, ...]
    signals: tuple[SignalView, ...]
    fills: tuple[FillView, ...]
    outcomes: tuple[OutcomeView, ...]
    observations: tuple[ObservationPoint, ...]
    observation_count: int
    observation_count_capped: bool


@dataclass(frozen=True, slots=True)
class TokenHit:
    mint: str
    symbol: str | None
    name: str | None
    final_state: str | None


@dataclass(frozen=True, slots=True)
class OutcomeSummary:
    report: EvaluationReport
    total_rows: int
    incomplete: tuple[Integrity, ...]
    include_truncated: bool
    min_observations: int
    rows: tuple[OutcomeView, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class SchemaStatus:
    version: int
    tables: frozenset[str]
    missing: tuple[str, ...]
    supported: bool
    message: str
