"""SQLAlchemy ORM tables. JSON payload columns keep full fidelity for replay/audit."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON}


class SessionRow(Base):
    __tablename__ = "sessions"
    session_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mode: Mapped[str] = mapped_column(String(16))
    config_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")


class SessionIntegrityRow(Base):
    """Write-integrity counters of a session: how many rows the background writer dropped or
    failed to commit, per record kind. `complete` is False when any research data is missing."""

    __tablename__ = "session_integrity"
    session_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    dropped_total: Mapped[int] = mapped_column(Integer, default=0)
    failed_total: Mapped[int] = mapped_column(Integer, default=0)
    dropped_by_kind: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    failed_by_kind: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    complete: Mapped[bool] = mapped_column(Boolean, default=True)
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)


class PaperSessionRow(Base):
    """Metadata of a paper-trading experiment: the requested bankroll, the SOL/EUR rate captured
    at session start and where it came from. The original bankroll is never redefined later."""

    __tablename__ = "paper_sessions"
    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    requested: Mapped[str] = mapped_column(String(32))
    bankroll_sol: Mapped[str] = mapped_column(String(40))
    bankroll_eur: Mapped[str] = mapped_column(String(40))
    sol_eur_start: Mapped[str] = mapped_column(String(40))
    fx_source: Mapped[str] = mapped_column(String(16))
    fx_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    config_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    database_url: Mapped[str] = mapped_column(String(512))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class TokenRow(Base):
    __tablename__ = "tokens"
    mint: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(40), index=True)
    symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decimals: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pool_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    venue: Mapped[str] = mapped_column(String(32))
    pool_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quote_mint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(32))
    discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    final_state: Mapped[str | None] = mapped_column(String(32), nullable=True)


class ObservationRow(Base):
    __tablename__ = "observations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(40))
    mint: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    __table_args__ = (Index("ix_obs_session_time", "session_id", "observed_at"),)


class TradeRow(Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(40))
    mint: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    __table_args__ = (Index("ix_trade_session_time", "session_id", "observed_at"),)


class FeatureRow(Base):
    __tablename__ = "features"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(40))
    mint: Mapped[str] = mapped_column(String(64))
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    __table_args__ = (Index("ix_feat_session_mint", "session_id", "mint"),)


class CheckRow(Base):
    __tablename__ = "check_results"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(40))
    mint: Mapped[str] = mapped_column(String(64))
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    verdict: Mapped[str] = mapped_column(String(16))
    fatal: Mapped[bool] = mapped_column(Boolean)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class ScoreRow(Base):
    __tablename__ = "scores"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(40))
    mint: Mapped[str] = mapped_column(String(64))
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    score: Mapped[float] = mapped_column(Float)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class SignalRow(Base):
    __tablename__ = "signals"
    signal_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(40), index=True)
    mint: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class DecisionRow(Base):
    __tablename__ = "decisions"
    decision_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(40))
    signal_id: Mapped[str] = mapped_column(String(40), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(16))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    note: Mapped[str] = mapped_column(Text, default="")


class QuoteRow(Base):
    __tablename__ = "quotes"
    quote_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(40))
    mint: Mapped[str] = mapped_column(String(64), index=True)
    quoted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    provider: Mapped[str] = mapped_column(String(32))
    input_mint: Mapped[str] = mapped_column(String(64))
    output_mint: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class PositionRow(Base):
    __tablename__ = "positions"
    position_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(40), index=True)
    mint: Mapped[str] = mapped_column(String(64), index=True)
    state: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LedgerRow(Base):
    __tablename__ = "ledger"
    entry_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, index=True)
    session_id: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(16))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cash_delta_eur: Mapped[str] = mapped_column(String(40))
    cash_after_eur: Mapped[str] = mapped_column(String(40))
    position_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    mint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str] = mapped_column(Text)
    fee_eur: Mapped[str] = mapped_column(String(40))
    slippage_eur: Mapped[str] = mapped_column(String(40))
    realized_pnl_eur: Mapped[str] = mapped_column(String(40))
    reference_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # UNKNOWN_LEGACY for rows written before provenance existed (migration 2); never upgraded
    provenance: Mapped[str | None] = mapped_column(String(24), nullable=True)


class AccountStateRow(Base):
    """Singleton (id=1) summary for fast restore. The ledger remains the source of truth."""

    __tablename__ = "account_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cash_eur: Mapped[str] = mapped_column(String(40))
    peak_equity_eur: Mapped[str] = mapped_column(String(40))
    realized_pnl_eur: Mapped[str] = mapped_column(String(40))
    fees_eur: Mapped[str] = mapped_column(String(40))
    slippage_eur: Mapped[str] = mapped_column(String(40))
    wins: Mapped[int] = mapped_column(Integer)
    losses: Mapped[int] = mapped_column(Integer)
    recent_results: Mapped[dict[str, Any]] = mapped_column(JSON)
    milestones_reached: Mapped[dict[str, Any]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PortfolioSnapshotRow(Base):
    __tablename__ = "portfolio_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(40))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class MilestoneRow(Base):
    __tablename__ = "milestones"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(40))
    milestone_eur: Mapped[str] = mapped_column(String(40))
    equity_eur: Mapped[str] = mapped_column(String(40))
    reached_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    direction: Mapped[str] = mapped_column(String(8))


class ExecutionRecordRow(Base):
    __tablename__ = "execution_records"
    record_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(40))
    signal_id: Mapped[str] = mapped_column(String(40), index=True)
    mint: Mapped[str] = mapped_column(String(64))
    side: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))
    simulated: Mapped[bool] = mapped_column(Boolean)
    unsigned_transaction_b64: Mapped[str | None] = mapped_column(Text, nullable=True)
    instructions: Mapped[str] = mapped_column(Text)
    quote_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")


class FillRow(Base):
    __tablename__ = "fills"
    fill_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(40))
    signal_id: Mapped[str] = mapped_column(String(40), index=True)
    mint: Mapped[str] = mapped_column(String(64))
    side: Mapped[str] = mapped_column(String(8))
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class StateTransitionRow(Base):
    __tablename__ = "state_transitions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(40))
    mint: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(32))
    target: Mapped[str] = mapped_column(String(32))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(Text, default="")


class ErrorRow(Base):
    __tablename__ = "errors"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(40))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    component: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    detail: Mapped[str] = mapped_column(Text, default="")


class EntryAttemptRow(Base):
    """One qualification latch window (see domain.models.EntryAttempt). Audit trail for
    'this token qualified but no BUY signal was generated because ...'."""

    __tablename__ = "entry_attempts"
    attempt_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    mint: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    qualified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    qualified_score: Mapped[float] = mapped_column(Float)
    post_quote_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    final_decision: Mapped[str] = mapped_column(String(24), index=True)
    block_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class OutcomeRow(Base):
    """Forward outcome of an observed token (see strategy/outcomes.py). Measurement only."""

    __tablename__ = "outcomes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(40), index=True)
    mint: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(32))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    finalized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    horizon_s: Mapped[float] = mapped_column(Float)
    observations: Mapped[int] = mapped_column(Integer)
    max_multiple: Mapped[float] = mapped_column(Float)
    qualified_multiple: Mapped[float | None] = mapped_column(Float, nullable=True)
    final_multiple: Mapped[float] = mapped_column(Float)
    max_drawdown_from_peak: Mapped[float] = mapped_column(Float)
    time_to_peak_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    best_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    qualified: Mapped[bool] = mapped_column(Boolean)
    signalled: Mapped[bool] = mapped_column(Boolean)
    entered: Mapped[bool] = mapped_column(Boolean)
    closed_pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    liquidity_collapsed: Mapped[bool] = mapped_column(Boolean)
    reject_reason: Mapped[str | None] = mapped_column(String(160), nullable=True)
    simulated: Mapped[bool] = mapped_column(Boolean)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    # nullable: databases migrated from v3 gain these columns via ALTER TABLE, which cannot add
    # NOT NULL columns; v4 backfills them and new rows always set them
    market_provenance: Mapped[str | None] = mapped_column(String(16), nullable=True)
    execution_provenance: Mapped[str | None] = mapped_column(String(16), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
