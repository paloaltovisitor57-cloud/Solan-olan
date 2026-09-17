"""Async SQLite repository with a batched background writer.

Hot-path callers enqueue small write operations; the writer flushes them in batches. Critical
records (fills, positions, ledger, account state) go through `persist_now` which awaits commit
so an open position always survives a crash that happens right after it was opened.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from solana_sniper.domain.enums import Venue
from solana_sniper.domain.models import (
    BuySignal,
    CheckReport,
    EntryScore,
    ErrorRecord,
    ExecutionRecord,
    FeatureVector,
    Fill,
    LedgerEntry,
    ManualDecision,
    MarketSnapshot,
    MilestoneEvent,
    PortfolioSnapshot,
    Position,
    SellSignal,
    SwapQuote,
    TokenInfo,
    TradeEvent,
)
from solana_sniper.storage.models import (
    AccountStateRow,
    Base,
    CheckRow,
    DecisionRow,
    ErrorRow,
    ExecutionRecordRow,
    FeatureRow,
    FillRow,
    LedgerRow,
    MilestoneRow,
    ObservationRow,
    PortfolioSnapshotRow,
    PositionRow,
    QuoteRow,
    ScoreRow,
    SessionRow,
    SignalRow,
    StateTransitionRow,
    TokenRow,
    TradeRow,
)
from solana_sniper.storage.serialization import dataclass_from_dict, to_jsonable
from solana_sniper.telemetry.logging import get_logger
from solana_sniper.telemetry.metrics import Metrics

log = get_logger(__name__)

WriteOp = Callable[[AsyncSession], Awaitable[None]]


class RecoveredState:
    def __init__(
        self,
        *,
        cash: Decimal,
        ledger: list[LedgerEntry],
        positions: list[Position],
        peak_equity: Decimal,
        realized_pnl: Decimal,
        fees: Decimal,
        slippage: Decimal,
        wins: int,
        losses: int,
        recent_results: list[Decimal],
        milestones_reached: list[Decimal],
        has_account: bool,
    ) -> None:
        self.cash = cash
        self.ledger = ledger
        self.positions = positions
        self.peak_equity = peak_equity
        self.realized_pnl = realized_pnl
        self.fees = fees
        self.slippage = slippage
        self.wins = wins
        self.losses = losses
        self.recent_results = recent_results
        self.milestones_reached = milestones_reached
        self.has_account = has_account


class Repository:
    def __init__(
        self,
        database_url: str,
        *,
        session_id: str,
        batch_size: int = 200,
        flush_interval_s: float = 0.5,
        metrics: Metrics | None = None,
    ) -> None:
        if database_url.startswith("sqlite+aiosqlite:///") and ":memory:" not in database_url:
            path = database_url.removeprefix("sqlite+aiosqlite:///")
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._engine: AsyncEngine = create_async_engine(database_url, future=True)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)
        self.session_id = session_id
        self._queue: asyncio.Queue[WriteOp] = asyncio.Queue(maxsize=20_000)
        self._batch = batch_size
        self._interval = flush_interval_s
        self._metrics = metrics
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self.dropped = 0
        self.failures = 0
        self.last_flush_at: datetime | None = None
        self.last_flush_error: str | None = None

    # ------------------------------------------------------------ lifecycle
    async def init(self) -> None:
        async with self._engine.begin() as conn:
            if self._engine.dialect.name == "sqlite":
                await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
                await conn.exec_driver_sql("PRAGMA synchronous=NORMAL")
            await conn.run_sync(Base.metadata.create_all)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._writer(), name="storage-writer")

    async def close(self) -> None:
        self._stopping = True
        if self._task is not None:
            await self.flush()
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self._engine.dispose()

    async def flush(self) -> None:
        ops: list[WriteOp] = []
        while not self._queue.empty():
            ops.append(self._queue.get_nowait())
        if ops:
            await self._run_batch(ops)

    async def _writer(self) -> None:
        while True:
            ops: list[WriteOp] = []
            try:
                first = await asyncio.wait_for(self._queue.get(), timeout=self._interval)
                ops.append(first)
            except TimeoutError:
                continue
            deadline = time.monotonic() + self._interval
            while len(ops) < self._batch and time.monotonic() < deadline:
                try:
                    ops.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0.01)
            await self._run_batch(ops)

    async def _run_batch(self, ops: Sequence[WriteOp]) -> None:
        started = time.perf_counter()
        for attempt in range(2):
            try:
                async with self._sessions() as session:
                    for op in ops:
                        await op(session)
                    await session.commit()
                if self._metrics:
                    self._metrics.observe("storage_flush", (time.perf_counter() - started) * 1000)
                self.last_flush_at = datetime.now(tz=UTC)
                self.last_flush_error = None
                return
            except SQLAlchemyError as exc:
                self.failures += 1
                self.last_flush_error = str(exc)[:200]
                log.error("storage_batch_failed", attempt=attempt, ops=len(ops), error=str(exc))
                await asyncio.sleep(0.2)
        # second failure: try ops individually so one bad row does not poison the batch
        for op in ops:
            try:
                async with self._sessions() as session:
                    await op(session)
                    await session.commit()
            except SQLAlchemyError as exc:
                self.dropped += 1
                log.error("storage_op_dropped", error=str(exc))

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    def persist(self, op: WriteOp) -> None:
        try:
            self._queue.put_nowait(op)
        except asyncio.QueueFull:
            self.dropped += 1

    async def persist_now(self, op: WriteOp) -> None:
        await self._run_batch([op])

    # ------------------------------------------------------------ write ops
    async def start_session(self, mode: str, config_path: str | None) -> None:
        async def op(s: AsyncSession) -> None:
            s.add(
                SessionRow(
                    session_id=self.session_id,
                    started_at=datetime.now(tz=UTC),
                    mode=mode,
                    config_path=config_path,
                )
            )

        await self.persist_now(op)

    async def end_session(self) -> None:
        async def op(s: AsyncSession) -> None:
            await s.execute(
                update(SessionRow)
                .where(SessionRow.session_id == self.session_id)
                .values(ended_at=datetime.now(tz=UTC))
            )

        await self.persist_now(op)

    def save_token(self, token: TokenInfo) -> None:
        async def op(s: AsyncSession) -> None:
            await s.merge(
                TokenRow(
                    mint=token.mint,
                    session_id=self.session_id,
                    symbol=token.symbol,
                    name=token.name,
                    decimals=token.decimals,
                    created_at=token.created_at,
                    pool_created_at=token.pool_created_at,
                    venue=str(token.venue),
                    pool_address=token.pool_address,
                    quote_mint=token.quote_mint,
                    source=token.source,
                    discovered_at=token.discovered_at,
                )
            )

        self.persist(op)

    def save_token_state(self, mint: str, state: str) -> None:
        async def op(s: AsyncSession) -> None:
            await s.execute(update(TokenRow).where(TokenRow.mint == mint).values(final_state=state))

        self.persist(op)

    def save_observation(self, snap: MarketSnapshot) -> None:
        payload = to_jsonable(snap)

        async def op(s: AsyncSession) -> None:
            s.add(
                ObservationRow(
                    session_id=self.session_id,
                    mint=snap.mint,
                    observed_at=snap.observed_at,
                    source=snap.source,
                    payload=payload,
                )
            )

        self.persist(op)

    def save_trade(self, trade: TradeEvent) -> None:
        payload = to_jsonable(trade)

        async def op(s: AsyncSession) -> None:
            s.add(
                TradeRow(
                    session_id=self.session_id,
                    mint=trade.mint,
                    observed_at=trade.observed_at,
                    payload=payload,
                )
            )

        self.persist(op)

    def save_features(self, f: FeatureVector) -> None:
        payload = f.as_dict()

        async def op(s: AsyncSession) -> None:
            s.add(
                FeatureRow(
                    session_id=self.session_id,
                    mint=f.mint,
                    computed_at=f.computed_at,
                    payload=payload,
                )
            )

        self.persist(op)

    def save_checks(self, report: CheckReport) -> None:
        payload = to_jsonable(report)
        verdict = str(report.verdict)
        fatal = report.is_fatal

        async def op(s: AsyncSession) -> None:
            s.add(
                CheckRow(
                    session_id=self.session_id,
                    mint=report.mint,
                    evaluated_at=report.evaluated_at,
                    verdict=verdict,
                    fatal=fatal,
                    payload=payload,
                )
            )

        self.persist(op)

    def save_score(self, score: EntryScore) -> None:
        payload = to_jsonable(score)

        async def op(s: AsyncSession) -> None:
            s.add(
                ScoreRow(
                    session_id=self.session_id,
                    mint=score.mint,
                    scored_at=score.scored_at,
                    score=score.score,
                    payload=payload,
                )
            )

        self.persist(op)

    def save_signal(self, signal: BuySignal | SellSignal, status: str) -> None:
        payload = to_jsonable(signal)

        async def op(s: AsyncSession) -> None:
            await s.merge(
                SignalRow(
                    signal_id=signal.signal_id,
                    session_id=self.session_id,
                    mint=signal.mint,
                    kind=str(signal.kind),
                    created_at=signal.created_at,
                    expires_at=signal.expires_at,
                    status=status,
                    payload=payload,
                )
            )

        self.persist(op)

    def update_signal_status(self, signal_id: str, status: str) -> None:
        async def op(s: AsyncSession) -> None:
            await s.execute(
                update(SignalRow).where(SignalRow.signal_id == signal_id).values(status=status)
            )

        self.persist(op)

    def save_decision(self, d: ManualDecision) -> None:
        async def op(s: AsyncSession) -> None:
            await s.merge(
                DecisionRow(
                    decision_id=d.decision_id,
                    session_id=self.session_id,
                    signal_id=d.signal_id,
                    kind=str(d.kind),
                    source=str(d.source),
                    decided_at=d.decided_at,
                    note=d.note,
                )
            )

        self.persist(op)

    def save_quote(self, q: SwapQuote, mint: str) -> None:
        payload = to_jsonable(q)
        payload.pop("raw", None)

        async def op(s: AsyncSession) -> None:
            await s.merge(
                QuoteRow(
                    quote_id=q.quote_id,
                    session_id=self.session_id,
                    mint=mint,
                    quoted_at=q.quoted_at,
                    provider=q.provider,
                    input_mint=q.input_mint,
                    output_mint=q.output_mint,
                    payload=payload,
                )
            )

        self.persist(op)

    def _position_op(self, p: Position) -> WriteOp:
        payload = to_jsonable(p)
        state = str(p.state)

        async def op(s: AsyncSession) -> None:
            await s.merge(
                PositionRow(
                    position_id=p.position_id,
                    session_id=p.session_id or self.session_id,
                    mint=p.mint,
                    state=state,
                    payload=payload,
                    updated_at=datetime.now(tz=UTC),
                )
            )

        return op

    def save_position(self, p: Position) -> None:
        self.persist(self._position_op(p))

    async def save_position_now(self, p: Position) -> None:
        await self.persist_now(self._position_op(p))

    def _ledger_op(self, e: LedgerEntry) -> WriteOp:
        async def op(s: AsyncSession) -> None:
            await s.merge(
                LedgerRow(
                    entry_id=e.entry_id,
                    seq=e.seq,
                    session_id=self.session_id,
                    kind=str(e.kind),
                    at=e.at,
                    cash_delta_eur=str(e.cash_delta_eur),
                    cash_after_eur=str(e.cash_after_eur),
                    position_id=e.position_id,
                    mint=e.mint,
                    description=e.description,
                    fee_eur=str(e.fee_eur),
                    slippage_eur=str(e.slippage_eur),
                    realized_pnl_eur=str(e.realized_pnl_eur),
                    reference_id=e.reference_id,
                )
            )

        return op

    async def save_ledger_now(self, entries: Sequence[LedgerEntry]) -> None:
        for e in entries:
            await self.persist_now(self._ledger_op(e))

    async def save_account_state_now(
        self,
        *,
        cash: Decimal,
        peak_equity: Decimal,
        realized_pnl: Decimal,
        fees: Decimal,
        slippage: Decimal,
        wins: int,
        losses: int,
        recent_results: Sequence[Decimal],
        milestones_reached: Sequence[Decimal],
    ) -> None:
        async def op(s: AsyncSession) -> None:
            await s.merge(
                AccountStateRow(
                    id=1,
                    cash_eur=str(cash),
                    peak_equity_eur=str(peak_equity),
                    realized_pnl_eur=str(realized_pnl),
                    fees_eur=str(fees),
                    slippage_eur=str(slippage),
                    wins=wins,
                    losses=losses,
                    recent_results={"values": [str(r) for r in recent_results]},
                    milestones_reached={"values": [str(m) for m in milestones_reached]},
                    updated_at=datetime.now(tz=UTC),
                )
            )

        await self.persist_now(op)

    async def save_fill_now(self, fill: Fill) -> None:
        payload = to_jsonable(fill)

        async def op(s: AsyncSession) -> None:
            await s.merge(
                FillRow(
                    fill_id=fill.fill_id,
                    session_id=self.session_id,
                    signal_id=fill.signal_id,
                    mint=fill.mint,
                    side=str(fill.side),
                    filled_at=fill.filled_at,
                    payload=payload,
                )
            )

        await self.persist_now(op)

    def save_portfolio_snapshot(self, snap: PortfolioSnapshot) -> None:
        payload = to_jsonable(snap)

        async def op(s: AsyncSession) -> None:
            s.add(PortfolioSnapshotRow(session_id=self.session_id, at=snap.at, payload=payload))

        self.persist(op)

    def save_milestone(self, m: MilestoneEvent) -> None:
        async def op(s: AsyncSession) -> None:
            s.add(
                MilestoneRow(
                    session_id=self.session_id,
                    milestone_eur=str(m.milestone_eur),
                    equity_eur=str(m.equity_eur),
                    reached_at=m.reached_at,
                    direction=m.direction,
                )
            )

        self.persist(op)

    def save_execution_record(self, r: ExecutionRecord) -> None:
        async def op(s: AsyncSession) -> None:
            await s.merge(
                ExecutionRecordRow(
                    record_id=r.record_id,
                    session_id=self.session_id,
                    signal_id=r.signal_id,
                    mint=r.mint,
                    side=str(r.side),
                    created_at=r.created_at,
                    status=str(r.status),
                    simulated=r.simulated,
                    unsigned_transaction_b64=r.unsigned_transaction_b64,
                    instructions=r.instructions,
                    quote_id=r.quote_id,
                    note=r.note,
                )
            )

        self.persist(op)

    def save_transition(
        self, mint: str, source: str, target: str, at: datetime, reason: str
    ) -> None:
        async def op(s: AsyncSession) -> None:
            s.add(
                StateTransitionRow(
                    session_id=self.session_id,
                    mint=mint,
                    source=source,
                    target=target,
                    at=at,
                    reason=reason,
                )
            )

        self.persist(op)

    def save_error(self, e: ErrorRecord) -> None:
        async def op(s: AsyncSession) -> None:
            s.add(
                ErrorRow(
                    session_id=self.session_id,
                    at=e.at,
                    component=e.component,
                    message=e.message,
                    detail=e.detail,
                )
            )

        self.persist(op)

    # ------------------------------------------------------------- recovery
    async def load_state(self) -> RecoveredState:
        async with self._sessions() as s:
            acct = (
                await s.execute(select(AccountStateRow).where(AccountStateRow.id == 1))
            ).scalar_one_or_none()
            ledger_rows = (
                (await s.execute(select(LedgerRow).order_by(LedgerRow.seq))).scalars().all()
            )
            pos_rows = (
                (await s.execute(select(PositionRow).where(PositionRow.state != "CLOSED")))
                .scalars()
                .all()
            )
        ledger = [
            LedgerEntry(
                entry_id=r.entry_id,
                seq=r.seq,
                kind=r.kind,  # type: ignore[arg-type]
                at=_aware(r.at),
                cash_delta_eur=Decimal(r.cash_delta_eur),
                cash_after_eur=Decimal(r.cash_after_eur),
                position_id=r.position_id,
                mint=r.mint,
                description=r.description,
                fee_eur=Decimal(r.fee_eur),
                slippage_eur=Decimal(r.slippage_eur),
                realized_pnl_eur=Decimal(r.realized_pnl_eur),
                reference_id=r.reference_id,
            )
            for r in ledger_rows
        ]
        positions = [dataclass_from_dict(Position, r.payload) for r in pos_rows]
        if acct is not None:
            return RecoveredState(
                cash=Decimal(acct.cash_eur),
                ledger=ledger,
                positions=positions,
                peak_equity=Decimal(acct.peak_equity_eur),
                realized_pnl=Decimal(acct.realized_pnl_eur),
                fees=Decimal(acct.fees_eur),
                slippage=Decimal(acct.slippage_eur),
                wins=acct.wins,
                losses=acct.losses,
                recent_results=[Decimal(v) for v in acct.recent_results.get("values", [])],
                milestones_reached=[Decimal(v) for v in acct.milestones_reached.get("values", [])],
                has_account=True,
            )
        # No summary row: rebuild from the ledger (source of truth).
        cash = ledger[-1].cash_after_eur if ledger else Decimal(0)
        realized = sum((e.realized_pnl_eur for e in ledger), Decimal(0))
        fees = sum((e.fee_eur for e in ledger), Decimal(0))
        slippage = sum((e.slippage_eur for e in ledger), Decimal(0))
        sells = [e.realized_pnl_eur for e in ledger if str(e.kind) == "SELL"]
        return RecoveredState(
            cash=cash,
            ledger=ledger,
            positions=positions,
            peak_equity=max((e.cash_after_eur for e in ledger), default=Decimal(0)),
            realized_pnl=realized,
            fees=fees,
            slippage=slippage,
            wins=sum(1 for r in sells if r > 0),
            losses=sum(1 for r in sells if r <= 0),
            recent_results=sells[-10:],
            milestones_reached=[],
            has_account=bool(ledger),
        )

    # -------------------------------------------------------------- queries
    async def latest_portfolio_snapshot(self) -> PortfolioSnapshot | None:
        async with self._sessions() as s:
            row = (
                await s.execute(
                    select(PortfolioSnapshotRow).order_by(PortfolioSnapshotRow.at.desc()).limit(1)
                )
            ).scalar_one_or_none()
        return dataclass_from_dict(PortfolioSnapshot, row.payload) if row else None

    async def positions(self, *, open_only: bool = True) -> list[Position]:
        async with self._sessions() as s:
            stmt = select(PositionRow)
            if open_only:
                stmt = stmt.where(PositionRow.state != "CLOSED")
            rows = (await s.execute(stmt.order_by(PositionRow.updated_at.desc()))).scalars().all()
        return [dataclass_from_dict(Position, r.payload) for r in rows]

    async def recent_scores(self, limit: int = 20) -> list[dict[str, Any]]:
        async with self._sessions() as s:
            rows = (
                (await s.execute(select(ScoreRow).order_by(ScoreRow.scored_at.desc()).limit(limit)))
                .scalars()
                .all()
            )
        return [
            {
                "mint": r.mint,
                "score": r.score,
                "scored_at": _aware(r.scored_at).isoformat(),
                "session_id": r.session_id,
            }
            for r in rows
        ]

    async def token_history(self, mint: str) -> dict[str, Any]:
        async with self._sessions() as s:
            token = (
                await s.execute(select(TokenRow).where(TokenRow.mint == mint))
            ).scalar_one_or_none()
            transitions = (
                (
                    await s.execute(
                        select(StateTransitionRow)
                        .where(StateTransitionRow.mint == mint)
                        .order_by(StateTransitionRow.at)
                    )
                )
                .scalars()
                .all()
            )
            checks = (
                (
                    await s.execute(
                        select(CheckRow)
                        .where(CheckRow.mint == mint)
                        .order_by(CheckRow.evaluated_at.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .all()
            )
            scores = (
                (
                    await s.execute(
                        select(ScoreRow)
                        .where(ScoreRow.mint == mint)
                        .order_by(ScoreRow.scored_at.desc())
                        .limit(5)
                    )
                )
                .scalars()
                .all()
            )
            signals = (
                (await s.execute(select(SignalRow).where(SignalRow.mint == mint))).scalars().all()
            )
            obs_count = len(
                (
                    await s.execute(select(ObservationRow.id).where(ObservationRow.mint == mint))
                ).all()
            )
        return {
            "token": None
            if token is None
            else {
                "mint": token.mint,
                "symbol": token.symbol,
                "name": token.name,
                "venue": token.venue,
                "source": token.source,
                "pool_created_at": _iso(token.pool_created_at),
                "final_state": token.final_state,
            },
            "transitions": [
                {
                    "at": _aware(t.at).isoformat(),
                    "from": t.source,
                    "to": t.target,
                    "reason": t.reason,
                }
                for t in transitions
            ],
            "latest_checks": checks[0].payload if checks else None,
            "scores": [
                {
                    "at": _aware(r.scored_at).isoformat(),
                    "score": r.score,
                    "reasons": r.payload.get("reasons"),
                }
                for r in scores
            ],
            "signals": [
                {
                    "signal_id": r.signal_id,
                    "kind": r.kind,
                    "status": r.status,
                    "created_at": _aware(r.created_at).isoformat(),
                }
                for r in signals
            ],
            "observations": obs_count,
        }

    async def get_token(self, mint: str) -> TokenInfo | None:
        async with self._sessions() as s:
            row = (
                await s.execute(select(TokenRow).where(TokenRow.mint == mint))
            ).scalar_one_or_none()
        if row is None:
            return None
        try:
            venue = Venue(row.venue)
        except ValueError:
            venue = Venue.UNKNOWN
        return TokenInfo(
            mint=row.mint,
            symbol=row.symbol,
            name=row.name,
            decimals=row.decimals,
            created_at=_aware(row.created_at) if row.created_at else None,
            pool_created_at=_aware(row.pool_created_at) if row.pool_created_at else None,
            venue=venue,
            pool_address=row.pool_address,
            quote_mint=row.quote_mint,
            source=row.source,
            discovered_at=_aware(row.discovered_at) if row.discovered_at else None,
        )

    async def list_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        async with self._sessions() as s:
            rows = (
                (
                    await s.execute(
                        select(SessionRow).order_by(SessionRow.started_at.desc()).limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        return [
            {
                "session_id": r.session_id,
                "mode": r.mode,
                "started_at": _aware(r.started_at).isoformat(),
                "ended_at": _iso(r.ended_at),
            }
            for r in rows
        ]

    async def session_stream(self, session_id: str) -> list[tuple[datetime, str, dict[str, Any]]]:
        """All persisted events for a session in time order (tokens/observations/trades/quotes)."""
        async with self._sessions() as s:
            tokens = (
                (await s.execute(select(TokenRow).where(TokenRow.session_id == session_id)))
                .scalars()
                .all()
            )
            obs = (
                (
                    await s.execute(
                        select(ObservationRow).where(ObservationRow.session_id == session_id)
                    )
                )
                .scalars()
                .all()
            )
            trades = (
                (await s.execute(select(TradeRow).where(TradeRow.session_id == session_id)))
                .scalars()
                .all()
            )
            quotes = (
                (await s.execute(select(QuoteRow).where(QuoteRow.session_id == session_id)))
                .scalars()
                .all()
            )
        stream: list[tuple[datetime, str, dict[str, Any]]] = []
        for t in tokens:
            stream.append(
                (
                    _aware(
                        t.discovered_at or t.pool_created_at or t.created_at or datetime.now(tz=UTC)
                    ),
                    "token",
                    {
                        "mint": t.mint,
                        "symbol": t.symbol,
                        "name": t.name,
                        "decimals": t.decimals,
                        "created_at": _iso(t.created_at),
                        "pool_created_at": _iso(t.pool_created_at),
                        "venue": t.venue,
                        "pool_address": t.pool_address,
                        "quote_mint": t.quote_mint,
                        "source": t.source,
                        "discovered_at": _iso(t.discovered_at),
                    },
                )
            )
        stream.extend((_aware(o.observed_at), "observation", o.payload) for o in obs)
        stream.extend((_aware(t.observed_at), "trade", t.payload) for t in trades)
        stream.extend((_aware(q.quoted_at), "quote", {**q.payload, "mint": q.mint}) for q in quotes)
        stream.sort(key=lambda item: item[0])
        return stream

    async def prune_observations(self, older_than: datetime) -> int:
        async with self._sessions() as s:
            res = await s.execute(
                delete(ObservationRow).where(ObservationRow.observed_at < older_than)
            )
            res2 = await s.execute(delete(TradeRow).where(TradeRow.observed_at < older_than))
            await s.commit()
        count = getattr(res, "rowcount", 0) or 0
        count2 = getattr(res2, "rowcount", 0) or 0
        return int(count) + int(count2)

    async def counts(self) -> dict[str, int]:
        async with self._sessions() as s:
            out: dict[str, int] = {}
            for name, model in (
                ("tokens", TokenRow),
                ("observations", ObservationRow),
                ("trades", TradeRow),
                ("signals", SignalRow),
                ("positions", PositionRow),
                ("ledger", LedgerRow),
                ("errors", ErrorRow),
            ):
                out[name] = len((await s.execute(select(model))).scalars().all())
        return out


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _iso(dt: datetime | None) -> str | None:
    return _aware(dt).isoformat() if dt else None
