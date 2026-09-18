"""The live engine: wires discovery → data → features → checks → score → sizing → quotes →
signals → manual confirmation → positions → exits → portfolio. One instance per process.

Everything time-related uses the injected Clock so the same engine runs live, dry-run and replay.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import traceback
from collections import deque
from collections.abc import Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from solana_sniper.app.bus import EventBus
from solana_sniper.config.settings import Settings
from solana_sniper.domain.clock import Clock
from solana_sniper.domain.enums import (
    CandidateState,
    DecisionKind,
    DecisionSource,
    EntryDecision,
    RunMode,
    SignalKind,
    SignalStatus,
    Urgency,
    Venue,
)
from solana_sniper.domain.events import (
    BuySignalCreated,
    ChecksEvaluated,
    DecisionRecorded,
    ErrorOccurred,
    ExecutionPrepared,
    FeaturesComputed,
    FillRecorded,
    LogLine,
    MilestoneReached,
    PortfolioUpdated,
    PositionClosed,
    PositionOpened,
    PositionUpdated,
    QuoteObtained,
    RoundTripEvaluated,
    Scored,
    SellSignalCreated,
    SignalResolved,
    SnapshotObserved,
    StateChanged,
    TokenDiscovered,
    TradeObserved,
)
from solana_sniper.domain.models import (
    BuySignal,
    CheckReport,
    EntryAttempt,
    EntryScore,
    ErrorRecord,
    FeatureVector,
    Fill,
    MarketSnapshot,
    Position,
    PositionSizing,
    RoundTripQuote,
    SellSignal,
    SwapQuote,
    TokenInfo,
    TradeEvent,
    new_id,
)
from solana_sniper.domain.money import ZERO, q_eur
from solana_sniper.domain.state_machine import CandidateStateMachine, InvalidTransitionError
from solana_sniper.execution.base import ExecutionInterface, FillOverride, PendingOrder, Resolution
from solana_sniper.execution.manual import DryRunExecution, OrderNotPendingError
from solana_sniper.execution.preparer import TransactionPreparer
from solana_sniper.features.engine import FeatureEngine
from solana_sniper.filters.checks import TokenChecker
from solana_sniper.infra.http import HttpError, ProviderUnavailableError, RateLimitedError
from solana_sniper.market_data.service import MarketDataService
from solana_sniper.market_data.tracker import TokenTrack, TokenTracker
from solana_sniper.portfolio.accounting import (
    InsufficientCashError,
    PortfolioAccount,
    PositionAlreadyClosedError,
)
from solana_sniper.portfolio.fx import FxProvider
from solana_sniper.positions.monitor import PositionMonitor
from solana_sniper.quotes.base import QuoteError
from solana_sniper.quotes.round_trip import RoundTripEvaluator
from solana_sniper.risk.engine import RiskEngine, SizingInputs
from solana_sniper.risk.milestones import MilestoneTracker
from solana_sniper.storage.repository import Repository
from solana_sniper.strategy.gate import EntryGate
from solana_sniper.strategy.outcomes import OutcomeTracker
from solana_sniper.strategy.scoring import EntryScorer
from solana_sniper.telemetry.logging import get_logger
from solana_sniper.telemetry.metrics import Metrics, PipelineTimer
from solana_sniper.telemetry.redaction import safe_exception, scrub_text
from solana_sniper.telemetry.throttle import LogThrottle
from solana_sniper.token_analysis.base import LiquidityProvider, TokenMetadataProvider

log = get_logger(__name__)

S = CandidateState


@dataclass(slots=True)
class Candidate:
    track: TokenTrack
    sm: CandidateStateMachine
    discovered_at: datetime
    features: FeatureVector | None = None
    checks: CheckReport | None = None
    score: EntryScore | None = None
    gate_reasons: tuple[str, ...] = ()
    last_eval_at: datetime | None = None
    dirty: bool = True
    cooldown_until: datetime | None = None
    signal: BuySignal | None = None
    order: PendingOrder | None = None
    round_trip: RoundTripQuote | None = None
    position_id: str | None = None
    quote_attempts: int = 0
    next_quote_at: datetime | None = None
    quote_in_flight: bool = False
    metadata_requested: bool = False
    holders_fetched_at: datetime | None = None
    stale_since: datetime | None = None
    terminal_at: datetime | None = None
    exit_quote: SwapQuote | None = None
    exit_quote_in_flight: bool = False
    sell_order: PendingOrder | None = None
    last_reason: str = ""
    entry: EntryAttempt | None = None  # open qualification latch window (audit record)
    entry_attempts: int = 0
    metadata_attempts: int = 0
    metadata_next_at: datetime | None = None

    @property
    def mint(self) -> str:
        return self.track.mint

    @property
    def state(self) -> CandidateState:
        return self.sm.state

    @property
    def symbol(self) -> str | None:
        return self.track.token.symbol


@dataclass(frozen=True, slots=True)
class EngineDeps:
    clock: Clock
    bus: EventBus
    repo: Repository
    account: PortfolioAccount
    risk: RiskEngine
    milestones: MilestoneTracker
    features: FeatureEngine
    checker: TokenChecker
    scorer: EntryScorer
    gate: EntryGate
    monitor: PositionMonitor
    round_trip: RoundTripEvaluator
    execution: ExecutionInterface
    preparer: TransactionPreparer
    fx: FxProvider
    metadata: TokenMetadataProvider | None
    liquidity: LiquidityProvider | None
    market: MarketDataService
    tracker: TokenTracker
    outcomes: OutcomeTracker
    metrics: Metrics


@dataclass(slots=True)
class EngineStats:
    discovered: int = 0
    degraded_checks: int = 0
    rejected: int = 0
    qualified: int = 0
    signals: int = 0
    confirmed: int = 0
    cancelled: int = 0
    exits: int = 0
    evaluations: int = 0
    recent: list[str] = field(default_factory=list)
    last_signal_at: datetime | None = None
    last_error: ErrorRecord | None = None


class Engine:
    def __init__(
        self, settings: Settings, deps: EngineDeps, *, mode: RunMode, session_id: str
    ) -> None:
        self.settings = settings
        self.d = deps
        self.mode = mode
        self.session_id = session_id
        self.candidates: dict[str, Candidate] = {}
        self.positions_by_mint: dict[str, str] = {}
        self.stats = EngineStats()
        self.timer = PipelineTimer(deps.metrics)
        self._quote_sem = asyncio.Semaphore(4)
        self._meta_sem = asyncio.Semaphore(4)
        self._tasks: set[asyncio.Task[None]] = set()
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        self._last_snapshot_at: datetime | None = None
        self._last_fx_at: datetime | None = None
        self.started_at: datetime | None = None
        self.last_tick_at: datetime | None = None
        self.last_snapshot_at: datetime | None = None
        self._snapshot_times: deque[datetime] = deque(maxlen=20_000)
        self._enrichment_throttle = LogThrottle(60.0)

    # ------------------------------------------------------------------ utils
    @property
    def clock(self) -> Clock:
        return self.d.clock

    def now(self) -> datetime:
        return self.d.clock.now()

    def _spawn(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _log_event(self, message: str, level: str = "INFO") -> None:
        now = self.now()
        self.stats.recent.append(f"{now:%H:%M:%S} {message}")
        if len(self.stats.recent) > 200:
            del self.stats.recent[:-200]
        self.d.bus.publish(LogLine(at=now, level=level, message=message))

    def _error(self, component: str, exc: BaseException | str, detail: str = "") -> None:
        message = safe_exception(exc) if isinstance(exc, BaseException) else scrub_text(str(exc))
        raw_detail = detail or (traceback.format_exc() if isinstance(exc, BaseException) else "")
        rec = ErrorRecord(
            at=self.now(),
            component=component,
            message=message,
            detail=scrub_text(raw_detail),
            session_id=self.session_id,
        )
        log.error("engine_error", component=component, error=message)
        self.stats.last_error = rec
        self.d.bus.publish(ErrorOccurred(rec))

    def _transition(self, cand: Candidate, target: CandidateState, reason: str) -> bool:
        try:
            tr = cand.sm.transition(target, self.now(), reason)
        except InvalidTransitionError as exc:
            self._error("state_machine", exc)
            return False
        cand.last_reason = reason
        self.d.bus.publish(StateChanged(cand.mint, tr.source, tr.target, tr.at, reason))
        if target in (S.REJECTED, S.EXPIRED, S.CLOSED):
            cand.terminal_at = tr.at
        return True

    # -------------------------------------------------------------- inbound
    async def on_token(self, token: TokenInfo) -> None:
        async with self._lock:
            if token.mint in self.candidates:
                self.candidates[token.mint].track.merge_token(token)
                return
            if self.d.tracker.is_full and not self._evict_one():
                self._log_event(f"tracker full; skipping {token.symbol or token.mint[:8]}", "WARN")
                return
            track = self.d.tracker.track(token)
            cand = Candidate(
                track=track, sm=CandidateStateMachine(token.mint), discovered_at=self.now()
            )
            self.candidates[token.mint] = cand
            self.stats.discovered += 1
            self.timer.mark(token.mint, "discovered")
        self.d.bus.publish(TokenDiscovered(token, self.now()))
        await self.d.market.watch(token)
        self._log_event(
            f"discovered {token.symbol or '?'} {token.mint[:8]} via {token.source} "
            f"age={token.age_seconds(self.now()) or 0:.0f}s"
        )

    def _evict_one(self) -> bool:
        """Drop the least interesting non-position, non-pending candidate to make room."""
        victims = [
            c
            for c in self.candidates.values()
            if c.state in (S.DISCOVERED, S.MONITORING, S.DATA_STALE, S.SIGNAL_CANCELLED)
        ]
        if not victims:
            return False
        victims.sort(key=lambda c: (c.score.score if c.score else -1.0, c.discovered_at))
        victim = victims[0]
        self._retire(victim, S.EXPIRED, "evicted: tracker full")
        return True

    async def on_snapshot(self, snap: MarketSnapshot) -> None:
        # Outcome tracking outlives the candidate: keep measuring after reject/expire/close.
        self.d.outcomes.observe(snap)
        track = self.d.tracker.add_snapshot(snap)
        if track is None:
            return
        self.d.metrics.inc("snapshots")
        self.last_snapshot_at = snap.observed_at
        self._snapshot_times.append(snap.observed_at)
        self.timer.mark(snap.mint, "first_data")
        cand = self.candidates.get(snap.mint)
        if cand is not None:
            cand.dirty = True
            if self.d.outcomes.start(
                snap.mint, cand.symbol, cand.track.token.source, snap, self.now()
            ):
                self.d.metrics.inc("outcomes_followed")
        self.d.bus.publish(SnapshotObserved(snap))

    async def on_trade(self, trade: TradeEvent) -> None:
        track = self.d.tracker.add_trade(trade)
        if track is None:
            return
        self.d.metrics.inc("trades")
        self.timer.mark(trade.mint, "first_data")
        cand = self.candidates.get(trade.mint)
        if cand is not None:
            cand.dirty = True
        self.d.bus.publish(TradeObserved(trade))

    # --------------------------------------------------------------- loops
    async def run(self) -> None:
        self.started_at = self.now()
        await self._restore_positions()
        try:
            while not self._stop.is_set():
                await self.tick()
                await asyncio.sleep(0.25)
        finally:
            for t in list(self._tasks):
                t.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def stop(self) -> None:
        self._stop.set()

    async def tick(self) -> None:
        """One engine iteration. Public so tests and replay can drive it deterministically."""
        now = self.now()
        started = time.perf_counter()
        try:
            await self._refresh_fx(now)
            await self._process_execution(now)
            for cand in list(self.candidates.values()):
                if cand.state in (S.OPEN, S.EXIT_SIGNAL, S.AWAITING_EXIT_CONFIRMATION):
                    await self._monitor_position(cand, now)
                elif not cand.sm.is_terminal:
                    await self._evaluate(cand, now)
            self._housekeeping(now)
            await self._finalize_due_outcomes(now)
            await self._portfolio_snapshot(now)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._error("tick", exc)
        finally:
            self.last_tick_at = now
            self.d.metrics.observe("engine_tick", (time.perf_counter() - started) * 1000.0)
        # Give spawned quote/metadata tasks a chance to run before the next tick.
        await asyncio.sleep(0)

    async def _refresh_fx(self, now: datetime) -> None:
        if (
            self._last_fx_at is None
            or (now - self._last_fx_at).total_seconds() >= self.settings.portfolio.fx_refresh_s
        ):
            self._last_fx_at = now
            try:
                await self.d.fx.refresh()
            except Exception as exc:
                self._error("fx", exc)

    # ------------------------------------------------------------ candidates
    async def _evaluate(self, cand: Candidate, now: datetime) -> None:
        cfg = self.settings
        if cand.state is S.DISCOVERED:
            if cand.track.latest is None and not cand.track.trades:
                if (now - cand.discovered_at).total_seconds() > cfg.market_data.stale_after_s * 3:
                    self._retire(cand, S.EXPIRED, "no market data after discovery")
                return
            self._transition(cand, S.MONITORING, "first market data")
            self._request_metadata(cand)
        if cand.state is S.SIGNAL_CANCELLED:
            if cand.cooldown_until is None or now >= cand.cooldown_until:
                self._transition(cand, S.MONITORING, "cooldown elapsed")
            else:
                return
        if cand.state is S.DATA_STALE:
            if not cand.track.is_stale(now, cfg.market_data.stale_after_s):
                self._transition(cand, S.MONITORING, "data resumed")
                cand.stale_since = None
            elif (
                cand.stale_since
                and (now - cand.stale_since).total_seconds() > cfg.market_data.stale_after_s * 15
            ):
                self._retire(cand, S.EXPIRED, "stale too long")
                return
            else:
                return
        if cand.state in (S.BUY_SIGNAL, S.AWAITING_CONFIRMATION):
            # handled by execution processing; only watch for staleness
            if cand.track.is_stale(now, cfg.market_data.stale_after_s):
                await self._cancel_pending_buy(cand, "data went stale while awaiting confirmation")
            return
        if not cand.dirty and cand.last_eval_at and (now - cand.last_eval_at).total_seconds() < 1.0:
            return
        if cand.last_eval_at and (now - cand.last_eval_at).total_seconds() < 0.5:
            return
        cand.dirty = False
        cand.last_eval_at = now
        self.stats.evaluations += 1

        age = cand.track.token.age_seconds(now)
        if (
            age is not None
            and age > cfg.discovery.max_token_age_s
            and cand.state in (S.MONITORING, S.QUALIFIED)
        ):
            self._retire(cand, S.EXPIRED, f"token age {age:.0f}s exceeds max")
            return
        if cand.track.is_stale(now, cfg.market_data.stale_after_s):
            data_age = cand.track.data_age_s(now)
            if (
                cand.state is S.QUALIFIED
                and cand.quote_in_flight
                and data_age <= cfg.market_data.stale_after_s + cfg.entry.stale_grace_during_quote_s
            ):
                return  # bounded grace: let the in-flight quote finish; no signal on stale data
            if cand.state in (S.MONITORING, S.QUALIFIED):
                cand.stale_since = now
                self.d.metrics.inc("stale_suppressions")
                if cand.entry is not None:
                    self._close_attempt(cand, EntryDecision.STALE, f"no data for {data_age:.0f}s")
                self._transition(
                    cand, S.DATA_STALE, f"no data for {data_age:.0f}s{self._provider_note()}"
                )
            return
        if cand.state in (S.MONITORING, S.QUALIFIED):
            self._request_metadata(cand, urgent=cand.state is S.QUALIFIED)

        features = self.d.features.compute(
            cand.track,
            now,
            entry_slippage_bps=cand.round_trip.entry_slippage_bps if cand.round_trip else None,
            exit_slippage_bps=cand.round_trip.exit_slippage_bps if cand.round_trip else None,
        )
        cand.features = features
        self.timer.mark(cand.mint, "features")
        self.d.bus.publish(FeaturesComputed(features))
        rt = cand.round_trip if (cand.round_trip and self._rt_fresh(cand.round_trip, now)) else None
        checks = self.d.checker.evaluate(cand.track, features, now, rt)
        cand.checks = checks
        self.d.bus.publish(ChecksEvaluated(checks))
        score = self.d.scorer.score(features, checks, now, rt)
        cand.score = score
        self.d.bus.publish(Scored(score))
        self.d.outcomes.note_score(cand.mint, score.score)
        decision = self.d.gate.decide(features, checks, score)
        cand.gate_reasons = decision.reasons
        if decision.fatal:
            self._retire(cand, S.REJECTED, "; ".join(decision.reasons)[:200])
            return
        if cand.cooldown_until and now < cand.cooldown_until:
            return
        if cand.state is S.MONITORING and decision.qualified:
            self._qualify(cand, score, features, checks, now)
        elif cand.state is S.QUALIFIED:
            if cand.entry is not None:
                cand.entry.note_score(score.score)
            verdict, why = self.d.gate.latched(features, checks, score)
            if verdict == "fatal":
                self._retire(cand, S.REJECTED, why[:200])
                return
            if verdict == "abandon":
                self._abandon(cand, EntryDecision.ABANDONED, why, now)
                return
            if not decision.qualified and cand.entry is not None:
                cand.entry.hysteresis_holds += 1
            if cand.entry is not None and now >= cand.entry.latch_until:
                pending = (
                    cand.entry.block_reason
                    or "; ".join(decision.soft_reasons)
                    or (
                        "quote still in flight"
                        if cand.quote_in_flight
                        else "no signal within latch"
                    )
                )
                if decision.qualified and cand.entry_attempts < cfg.entry.max_entry_attempts:
                    # still fully qualified: one more bounded window, recorded separately
                    self._close_attempt(cand, EntryDecision.EXPIRED, f"latch expired: {pending}")
                    self._open_attempt(cand, score, features, checks, now)
                else:
                    self._abandon(
                        cand,
                        EntryDecision.EXPIRED,
                        f"latch expired: {pending}"
                        if decision.qualified is False
                        else f"entry attempts exhausted ({cand.entry_attempts}): {pending}",
                        now,
                    )
                    return
        if cand.state is S.QUALIFIED:
            await self._maybe_signal(cand, features, now)

    # ---------------------------------------------------- qualification latch
    def _qualify(
        self,
        cand: Candidate,
        score: EntryScore,
        features: FeatureVector,
        checks: CheckReport,
        now: datetime,
    ) -> None:
        self.stats.qualified += 1
        self.d.metrics.inc("tokens_qualified")
        self._transition(cand, S.QUALIFIED, f"score {score.score:.0f}")
        self.timer.mark(cand.mint, "qualified")
        latest = cand.track.latest
        self.d.outcomes.note_qualified(cand.mint, latest.price_native if latest else None, now)
        self._refresh_holders(cand, now)
        self._open_attempt(cand, score, features, checks, now)

    def _open_attempt(
        self,
        cand: Candidate,
        score: EntryScore,
        features: FeatureVector,
        checks: CheckReport,
        now: datetime,
    ) -> None:
        cand.entry_attempts += 1
        decimals = self._token_decimals(cand)
        attempt = EntryAttempt(
            attempt_id=new_id("entry"),
            session_id=self.session_id,
            mint=cand.mint,
            symbol=cand.symbol,
            qualified_at=now,
            qualified_score=score.score,
            latch_until=now + timedelta(seconds=self.settings.entry.qualification_latch_s),
            qualified_features=features.as_dict(),
            qualified_checks=checks.summary(),
            attempt_number=cand.entry_attempts,
            decimals_status=f"known:{decimals}" if decimals is not None else "unknown",
            min_score_seen=score.score,
            max_score_seen=score.score,
            evaluations=1,
        )
        cand.entry = attempt
        self.d.metrics.inc("entry_attempts")
        self.d.repo.save_entry_attempt(attempt)
        if decimals is None:
            self._request_metadata(cand, urgent=True)

    def _close_attempt(self, cand: Candidate, decision: EntryDecision, reason: str) -> None:
        attempt = cand.entry
        if attempt is None or not attempt.is_open:
            cand.entry = None
            return
        attempt.final_decision = decision
        attempt.block_reason = reason[:300] if reason else None
        attempt.completed_at = self.now()
        self.d.repo.save_entry_attempt(attempt)
        self.d.metrics.inc(f"entry_{decision.lower()}")
        self._log_event(
            f"entry {decision.lower()} {cand.symbol or cand.mint[:8]}: {reason}"[:220],
            "DEBUG" if decision is EntryDecision.BUY_SIGNAL else "INFO",
        )
        cand.entry = None

    def _abandon(
        self, cand: Candidate, decision: EntryDecision, reason: str, now: datetime
    ) -> None:
        """End the latch without a signal: MONITORING with a quote cooldown so a re-qualification
        needs fresh evidence rather than the next tick."""
        self._close_attempt(cand, decision, reason)
        cand.next_quote_at = now + timedelta(seconds=self.settings.quotes.refresh_interval_s)
        cand.cooldown_until = now + timedelta(seconds=self.settings.entry.cooldown_after_abandon_s)
        if cand.state is S.QUALIFIED:
            self._transition(cand, S.MONITORING, f"{decision.lower()}: {reason}"[:120])

    async def finalize_entry_attempts(self) -> int:
        """Shutdown: every open latch window gets a terminal CANCELLED record."""
        closed = 0
        for cand in list(self.candidates.values()):
            if cand.entry is not None and cand.entry.is_open:
                self._close_attempt(cand, EntryDecision.CANCELLED, "engine stopped")
                closed += 1
        if closed:
            await self.d.repo.flush()
        return closed

    def _provider_note(self) -> str:
        note = getattr(self.d.market, "provider_note", None)
        result = note() if callable(note) else ""
        return str(result)

    def _token_decimals(self, cand: Candidate) -> int | None:
        """Decimals from discovery metadata or the mint account; None until known, never guessed."""
        if cand.track.token.decimals is not None:
            return cand.track.token.decimals
        if cand.track.authorities is not None:
            return cand.track.authorities.decimals
        return None

    def _rt_fresh(self, rt: RoundTripQuote, now: datetime) -> bool:
        return (now - rt.quoted_at).total_seconds() <= self.settings.quotes.max_quote_age_s

    def _retire(self, cand: Candidate, state: CandidateState, reason: str) -> None:
        if cand.sm.is_terminal:
            return
        if cand.entry is not None:
            self._close_attempt(
                cand,
                EntryDecision.HARD_REJECT if state is S.REJECTED else EntryDecision.CANCELLED,
                reason,
            )
        if state is S.REJECTED:
            self.stats.rejected += 1
            self.d.metrics.inc("tokens_rejected")
            self.d.outcomes.note_rejected(cand.mint, reason)
        if self._transition(cand, state, reason):
            self._log_event(f"{state.lower()} {cand.symbol or cand.mint[:8]}: {reason}", "DEBUG")
            self.d.repo.save_token_state(cand.mint, str(state))

    def _request_metadata(self, cand: Candidate, *, urgent: bool = False) -> None:
        """Fetch mint authorities/decimals. A rate-limited or failed fetch is retried with a
        bounded backoff while the candidate lives (the first live run showed one degraded fetch
        left decimals unknown forever, so no quote was ever requested)."""
        if self.d.metadata is None or cand.track.authorities is not None:
            return
        now = self.now()
        if cand.metadata_requested:
            if cand.metadata_attempts >= self.settings.entry.metadata_max_attempts:
                return
            if cand.metadata_next_at is not None and now < cand.metadata_next_at:
                return
        cand.metadata_requested = True
        cand.metadata_attempts += 1
        delay = self.settings.entry.metadata_retry_s * (1 if urgent else cand.metadata_attempts)
        cand.metadata_next_at = now + timedelta(seconds=min(60.0, delay))
        self._spawn(self._fetch_metadata(cand), f"meta-{cand.mint[:6]}")

    async def _fetch_metadata(self, cand: Candidate) -> None:
        assert self.d.metadata is not None
        async with self._meta_sem:
            if cand.sm.is_terminal:
                return  # retired while waiting for a slot: do not spend a request on it
            try:
                auth = await self.d.metadata.get_authorities(cand.mint)
            except (RateLimitedError, ProviderUnavailableError, HttpError) as exc:
                self._enrichment_degraded("metadata", cand, exc)
                return
            except Exception as exc:
                self._error("metadata", exc)
                return
        if auth is not None:
            cand.track.authorities = auth
            cand.dirty = True
            if cand.entry is not None and cand.entry.decimals_status == "unknown":
                cand.entry.decimals_status = f"resolved:{auth.decimals}"
        self._refresh_holders(cand, self.now(), force=True)

    def _enrichment_degraded(self, what: str, cand: Candidate, exc: Exception) -> None:
        """A rate-limited or unavailable provider is an expected transient: the affected checks
        stay UNKNOWN (never PASS), the engine keeps running, and the condition is counted and
        summarised rather than raised as an error per token."""
        self.d.metrics.inc("checks_degraded")
        self.stats.degraded_checks += 1
        decision = self._enrichment_throttle.hit(what)
        if decision.log:
            log.warning(
                "enrichment_degraded",
                what=what,
                mint=cand.mint,
                error=safe_exception(exc),
                occurrences=decision.total,
                suppressed_since_last=decision.suppressed,
            )

    def _refresh_holders(self, cand: Candidate, now: datetime, force: bool = False) -> None:
        if self.d.liquidity is None:
            return
        if (
            not force
            and cand.holders_fetched_at
            and (now - cand.holders_fetched_at).total_seconds() < 60
        ):
            return
        cand.holders_fetched_at = now
        self._spawn(self._fetch_holders(cand), f"holders-{cand.mint[:6]}")

    async def _fetch_holders(self, cand: Candidate) -> None:
        assert self.d.liquidity is not None
        pools = tuple(p for p in (cand.track.token.pool_address,) if p)
        async with self._meta_sem:
            if cand.sm.is_terminal:
                return
            try:
                dist = await self.d.liquidity.get_holder_distribution(cand.mint, pools)
            except (RateLimitedError, ProviderUnavailableError, HttpError) as exc:
                self._enrichment_degraded("holders", cand, exc)
                return
            except Exception as exc:
                self._error("holders", exc)
                return
        if dist is not None:
            cand.track.holders = dist
            if dist.holder_count is not None:
                cand.track.holder_history.append((self.now(), dist.holder_count))
            cand.dirty = True

    # ---------------------------------------------------------------- signals
    async def _maybe_signal(self, cand: Candidate, features: FeatureVector, now: datetime) -> None:
        if cand.quote_in_flight or (cand.next_quote_at and now < cand.next_quote_at):
            return
        if len(self.d.execution.pending()) >= self.settings.entry.max_pending_signals:
            cand.gate_reasons = ("max pending signals reached",)
            return
        attempt = cand.entry
        if self._token_decimals(cand) is None:
            cand.gate_reasons = ("token decimals unknown (waiting for mint metadata)",)
            if attempt is not None and attempt.block_reason is None:
                attempt.decimals_status = "unknown"
                attempt.block_reason = "token decimals unknown (mint metadata not fetched yet)"
                self.d.repo.save_entry_attempt(attempt)  # the trail says what is blocking now
            self._request_metadata(cand, urgent=True)
            cand.next_quote_at = now + timedelta(seconds=1)
            return
        sizing = self._size(cand, features)
        if attempt is not None:
            attempt.sizing_attempted = True
            attempt.recommended_eur = sizing.recommended_eur
            attempt.recommended_sol = sizing.recommended_sol
            attempt.sizing_reason = "; ".join(sizing.caps_applied) or None
            if attempt.decimals_status == "unknown":
                attempt.decimals_status = f"resolved:{self._token_decimals(cand)}"
        if sizing.recommended_eur <= 0:
            reason = "sizing: " + ("; ".join(sizing.caps_applied) or "zero")
            cand.gate_reasons = (reason,)
            self._abandon(cand, EntryDecision.SIZING_ZERO, reason, now)
            return
        cand.quote_in_flight = True
        if attempt is not None:
            attempt.quote_attempts += 1
            attempt.quote_started_at = attempt.quote_started_at or now
            attempt.block_reason = None
            self.d.repo.save_entry_attempt(attempt)
        self._spawn(
            self._quote_and_signal(cand, sizing.recommended_sol, sizing.recommended_eur),
            f"quote-{cand.mint[:6]}",
        )

    def _size(self, cand: Candidate, features: FeatureVector) -> PositionSizing:
        acct = self.d.account
        sol_eur = self.d.fx.sol_eur()
        usd_eur = self.d.fx.usd_eur()
        liq_eur = (
            Decimal(str(features.liquidity_usd)) * usd_eur
            if features.liquidity_usd is not None
            else None
        )
        inputs = SizingInputs(
            equity_eur=acct.equity,
            available_cash_eur=acct.cash,
            open_exposure_eur=acct.open_exposure,
            open_positions=len(acct.open_positions) + self._pending_buy_count(),
            score=cand.score.score if cand.score else 0.0,
            liquidity_eur=liq_eur,
            entry_slippage_bps=cand.round_trip.entry_slippage_bps if cand.round_trip else None,
            exit_slippage_bps=cand.round_trip.exit_slippage_bps if cand.round_trip else None,
            drawdown_pct=acct.drawdown_pct,
            recent=acct.recent_performance(),
            sol_eur=sol_eur,
        )
        return self.d.risk.size(inputs)

    def _pending_buy_count(self) -> int:
        return sum(1 for o in self.d.execution.pending() if o.kind is SignalKind.BUY)

    async def _quote_and_signal(
        self, cand: Candidate, spend_sol: Decimal, size_eur: Decimal
    ) -> None:
        try:
            async with self._quote_sem:
                await self._quote_and_signal_inner(cand, spend_sol, size_eur)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._error("quote", exc)
        finally:
            cand.quote_in_flight = False

    async def _quote_and_signal_inner(
        self, cand: Candidate, spend_sol: Decimal, size_eur: Decimal
    ) -> None:
        cfg = self.settings
        attempt = cand.entry
        decimals = self._token_decimals(cand)
        if decimals is None:
            cand.gate_reasons = ("token decimals unknown (waiting for mint metadata)",)
            return
        try:
            rt = await self.d.round_trip.evaluate(cand.mint, spend_sol, decimals)
        except QuoteError as exc:
            cand.quote_attempts += 1
            self.d.metrics.inc("quote_failures")
            if attempt is not None:
                attempt.quote_finished_at = self.now()
                attempt.buy_quote_status = "failed"
                attempt.quote_error = safe_exception(exc)[:200]
            if exc.retryable and cand.quote_attempts < 3:
                cand.next_quote_at = self.now() + timedelta(seconds=3)
                cand.gate_reasons = (f"quote retry: {exc}",)
                if attempt is not None:
                    attempt.block_reason = f"quote retry {cand.quote_attempts}: {exc}"[:300]
                    self.d.repo.save_entry_attempt(attempt)
                return
            cand.cooldown_until = self.now() + timedelta(seconds=cfg.entry.cooldown_after_cancel_s)
            cand.gate_reasons = (f"quote failed: {exc}",)
            self._abandon(cand, EntryDecision.QUOTE_FAILED, f"quote failed: {exc}", self.now())
            return
        now = self.now()
        cand.quote_attempts = 0
        cand.round_trip = rt
        self.timer.mark(cand.mint, "quote")
        self.d.bus.publish(QuoteObtained(rt.buy))
        if rt.sell is not None:
            self.d.bus.publish(QuoteObtained(rt.sell))
        self.d.bus.publish(RoundTripEvaluated(rt))
        if attempt is not None:
            attempt.quote_finished_at = now
            attempt.buy_quote_status = "ok"
            attempt.sell_quote_status = "ok" if rt.sell is not None else "failed"
            attempt.quote_ids = [*attempt.quote_ids, rt.buy.quote_id]
            if rt.sell is not None:
                attempt.quote_ids.append(rt.sell.quote_id)
            attempt.entry_price_impact_pct = rt.entry_price_impact_pct
            attempt.exit_price_impact_pct = rt.exit_price_impact_pct
            attempt.round_trip_loss_pct = rt.round_trip_loss_pct
            attempt.round_trip_viable = rt.viable
            if rt.sell is None:
                attempt.quote_error = "; ".join(rt.reasons)[:200]
        # If price impact is too high, shrink once and re-quote.
        if (
            rt.buy.price_impact_pct > cfg.risk.hard_limits.max_entry_price_impact_pct
            and spend_sol > Decimal("0.01")
        ):
            ratio = Decimal(
                str(
                    cfg.risk.hard_limits.max_entry_price_impact_pct
                    / max(rt.buy.price_impact_pct, 0.01)
                )
            )
            shrunk = q_eur(spend_sol * min(ratio, Decimal("0.9")))
            if shrunk >= cfg.risk.hard_limits.min_position_eur / self.d.fx.sol_eur():
                self._log_event(
                    f"{cand.symbol}: impact {rt.buy.price_impact_pct:.1f}% too high, "
                    f"re-quoting {shrunk} SOL"
                )
                return await self._quote_and_signal_inner(
                    cand, shrunk, q_eur(shrunk * self.d.fx.sol_eur())
                )
        if cand.state is not S.QUALIFIED:
            return  # the evaluator ended the attempt while the quote was in flight
        # Post-quote validation for an already-qualified candidate: execution viability, data
        # freshness and hard blocks end the attempt; a score wobble inside the hysteresis band
        # does not (the quote is what makes the decision executable).
        if not rt.viable:
            self._abandon(
                cand, EntryDecision.ABANDONED, "round trip: " + "; ".join(rt.reasons), now
            )
            return
        if cand.track.is_stale(now, cfg.market_data.stale_after_s):
            self._abandon(
                cand,
                EntryDecision.STALE,
                f"data {cand.track.data_age_s(now):.0f}s old after quote",
                now,
            )
            return
        features = self.d.features.compute(
            cand.track,
            now,
            entry_slippage_bps=rt.entry_slippage_bps,
            exit_slippage_bps=rt.exit_slippage_bps,
        )
        checks = self.d.checker.evaluate(cand.track, features, now, rt)
        score = self.d.scorer.score(features, checks, now, rt)
        cand.features, cand.checks, cand.score = features, checks, score
        self.d.bus.publish(ChecksEvaluated(checks))
        self.d.bus.publish(Scored(score))
        self.d.outcomes.note_score(cand.mint, score.score)
        if attempt is not None:
            attempt.post_quote_score = score.score
            attempt.note_score(score.score)
        decision = self.d.gate.decide(features, checks, score)
        cand.gate_reasons = decision.reasons
        verdict, why = self.d.gate.latched(features, checks, score)
        if verdict == "fatal":
            self._retire(cand, S.REJECTED, why[:200])
            return
        if verdict == "abandon":
            self._abandon(cand, EntryDecision.ABANDONED, why, now)
            return
        if not decision.qualified and attempt is not None:
            attempt.hysteresis_holds += 1
            self._log_event(
                f"{cand.symbol or cand.mint[:8]}: post-quote score {score.score:.1f} under "
                f"{cfg.entry.min_score:.0f}, continuing within hysteresis",
                "DEBUG",
            )
        sizing = self._size(cand, features)
        if sizing.recommended_eur <= 0:
            reason = "sizing: " + ("; ".join(sizing.caps_applied) or "zero")
            cand.gate_reasons = (reason,)
            self._abandon(cand, EntryDecision.SIZING_ZERO, reason, now)
            return
        # Never spend more than the sized amount (a re-quote may have shrunk it).
        if sizing.recommended_sol < rt.spend_sol * Decimal("0.98"):
            cand.next_quote_at = now
            return await self._quote_and_signal_inner(
                cand, sizing.recommended_sol, sizing.recommended_eur
            )
        sol_eur = self.d.fx.sol_eur()
        if sizing.recommended_sol != rt.spend_sol:
            # The quote is what will actually be spent; the signal must say exactly that.
            fraction = (
                q_eur(rt.spend_sol * sol_eur / sizing.equity_eur) if sizing.equity_eur > 0 else ZERO
            )
            sizing = PositionSizing(
                recommended_eur=q_eur(rt.spend_sol * sol_eur),
                recommended_sol=rt.spend_sol,
                fraction_of_equity=fraction,
                equity_eur=sizing.equity_eur,
                available_cash_eur=sizing.available_cash_eur,
                caps_applied=(*sizing.caps_applied, "quoted_spend"),
                multipliers=sizing.multipliers,
                profile=sizing.profile,
                tier=sizing.tier,
            )
        signal = BuySignal(
            signal_id=new_id("buy"),
            mint=cand.mint,
            symbol=cand.symbol,
            created_at=now,
            expires_at=now + timedelta(seconds=cfg.entry.signal_ttl_s),
            score=score,
            features=features,
            checks=checks,
            sizing=sizing,
            quote=rt,
            token_age_s=features.token_age_s,
            liquidity_usd=Decimal(str(features.liquidity_usd))
            if features.liquidity_usd is not None
            else None,
            price_native=Decimal(str(features.price_native))
            if features.price_native is not None
            else None,
            sol_eur=sol_eur,
            token_decimals=decimals,
            urgency=Urgency.HIGH if score.score >= 85 else Urgency.NORMAL,
            session_id=self.session_id,
        )
        if not cand.sm.may_generate_entry:
            return
        if not self._transition(
            cand, S.BUY_SIGNAL, f"score {score.score:.0f}, size €{sizing.recommended_eur:.2f}"
        ):
            return
        try:
            order = await self.d.execution.submit_buy(signal)
        except ValueError as exc:
            self._error("execution", exc)
            self._transition(cand, S.SIGNAL_CANCELLED, "duplicate pending order")
            cand.cooldown_until = now + timedelta(seconds=cfg.entry.cooldown_after_cancel_s)
            return
        cand.signal, cand.order = signal, order
        self.stats.signals += 1
        self.stats.last_signal_at = now
        self.d.metrics.inc("signals_generated")
        self.d.outcomes.note_signal(cand.mint)
        if attempt is not None:
            attempt.signal_id = signal.signal_id
            self._close_attempt(cand, EntryDecision.BUY_SIGNAL, f"BUY signal #{order.ref}")
        self.timer.mark(cand.mint, "signal")
        self.d.repo.save_signal(signal, str(SignalStatus.PENDING))
        self.d.bus.publish(BuySignalCreated(signal))
        self._transition(cand, S.AWAITING_CONFIRMATION, f"buy #{order.ref}")
        exit_now = f"€{rt.immediate_exit_sol * sol_eur:.2f}" if rt.immediate_exit_sol else "?"
        self._log_event(
            f"BUY SIGNAL #{order.ref} {cand.symbol or cand.mint[:8]} score={score.score:.0f} "
            f"size=€{sizing.recommended_eur:.2f} ({sizing.recommended_sol:.4f} SOL) "
            f"exit_now={exit_now}",
            "WARN",
        )
        record = await self.d.preparer.prepare(signal)
        self.d.repo.save_execution_record(record)
        self.d.bus.publish(ExecutionPrepared(record))

    async def _cancel_pending_buy(self, cand: Candidate, reason: str) -> None:
        if cand.order is not None:
            with contextlib.suppress(OrderNotPendingError):
                await self.d.execution.decide(
                    cand.order, DecisionKind.EXPIRE, DecisionSource.SYSTEM, note=reason
                )
        self._resolve_buy_cancel(cand, reason)

    def _resolve_buy_cancel(self, cand: Candidate, reason: str) -> None:
        if cand.state in (S.BUY_SIGNAL, S.AWAITING_CONFIRMATION):
            self._transition(cand, S.SIGNAL_CANCELLED, reason)
        cand.cooldown_until = self.now() + timedelta(
            seconds=self.settings.entry.cooldown_after_cancel_s
        )
        if cand.signal is not None:
            self.d.repo.update_signal_status(cand.signal.signal_id, str(SignalStatus.CANCELLED))
            self.d.bus.publish(
                SignalResolved(cand.signal.signal_id, cand.mint, "CANCELLED", self.now())
            )
        cand.signal, cand.order = None, None
        self.stats.cancelled += 1

    # -------------------------------------------------------------- execution
    async def _process_execution(self, now: datetime) -> None:
        resolutions: list[Resolution] = []
        resolutions.extend(await self.d.execution.expire_stale(now))
        if isinstance(self.d.execution, DryRunExecution):
            resolutions.extend(await self.d.execution.auto_confirm(now))
        for res in resolutions:
            await self._apply_resolution(res)

    async def _apply_resolution(self, res: Resolution) -> None:
        cand = self.candidates.get(res.order.mint)
        self.d.repo.save_decision(res.decision)
        self.d.bus.publish(DecisionRecorded(res.decision))
        status = {
            DecisionKind.CONFIRM: SignalStatus.CONFIRMED,
            DecisionKind.REJECT: SignalStatus.REJECTED,
            DecisionKind.IGNORE: SignalStatus.REJECTED,
            DecisionKind.EXPIRE: SignalStatus.EXPIRED,
        }[res.decision.kind]
        self.d.repo.update_signal_status(res.order.signal_id, str(status))
        self.d.bus.publish(
            SignalResolved(res.order.signal_id, res.order.mint, str(status), self.now())
        )
        if res.order.kind is SignalKind.BUY:
            if res.fill is None:
                if res.decision.kind is DecisionKind.EXPIRE:
                    self.d.metrics.inc("signals_expired")
                else:
                    self.d.metrics.inc("signals_rejected")
                if cand is not None:
                    self._resolve_buy_cancel(cand, f"buy {status.lower()}: {res.decision.note}")
                label = res.order.symbol or res.order.mint[:8]
                self._log_event(f"buy #{res.order.ref} {label} {status.lower()}")
                return
            await self._open_position(res, cand)
            return
        # SELL
        if res.fill is None:
            if cand is not None and cand.state in (S.EXIT_SIGNAL, S.AWAITING_EXIT_CONFIRMATION):
                self._transition(cand, S.OPEN, f"sell {status.lower()}")
                cand.sell_order = None
            self._log_event(
                f"sell #{res.order.ref} {res.order.symbol or res.order.mint[:8]} {status.lower()}"
            )
            return
        await self._close_position(res, cand)

    async def _open_position(self, res: Resolution, cand: Candidate | None) -> None:
        assert res.fill is not None and res.order.buy is not None
        fill = res.fill
        sig = res.order.buy
        entry_price = (fill.sol_amount / fill.token_amount_ui) if fill.token_amount_ui > 0 else ZERO
        try:
            position = self.d.account.open_position(
                fill,
                symbol=sig.symbol,
                entry_price_native=entry_price,
                entry_signal_id=sig.signal_id,
            )
        except (InsufficientCashError, ValueError) as exc:
            self._error("portfolio", exc)
            if cand is not None:
                self._resolve_buy_cancel(cand, f"fill rejected: {exc}")
            return
        self.positions_by_mint[position.mint] = position.position_id
        self.stats.confirmed += 1
        self.d.metrics.inc("signals_confirmed")
        self.d.metrics.inc("positions_opened")
        await self._persist_fill(fill, position)
        self.d.bus.publish(FillRecorded(fill))
        self.d.bus.publish(PositionOpened(position))
        self.d.outcomes.note_entered(position.mint)
        if cand is not None:
            cand.position_id = position.position_id
            cand.order = None
            if cand.state is S.AWAITING_CONFIRMATION:
                self._transition(
                    cand, S.OPEN, f"filled {fill.token_amount_ui:,.0f} @ {entry_price}"
                )
        self._log_event(
            f"OPEN {sig.symbol or sig.mint[:8]} qty={fill.token_amount_ui:,.0f} "
            f"cost=€{position.cost_basis_eur:.2f} [{fill.provenance}]",
            "WARN",
        )
        await self._after_portfolio_change()

    async def _close_position(self, res: Resolution, cand: Candidate | None) -> None:
        assert res.fill is not None and res.order.sell is not None
        sig = res.order.sell
        try:
            position = self.d.account.close_position(sig.position_id, res.fill, sig.reason)
        except PositionAlreadyClosedError:
            self._error("portfolio", f"position {sig.position_id} already closed")
            return
        except KeyError:
            self._error("portfolio", f"unknown position {sig.position_id}")
            return
        self.positions_by_mint.pop(position.mint, None)
        self.d.monitor.forget(position.position_id)
        self.d.metrics.inc("positions_closed")
        await self._persist_fill(res.fill, position)
        self.d.bus.publish(FillRecorded(res.fill))
        self.d.bus.publish(PositionClosed(position))
        self.d.outcomes.note_closed(position.mint, position.pnl_pct, str(sig.reason))
        if cand is not None:
            cand.sell_order = None
            cand.exit_quote = None
            if cand.state in (S.EXIT_SIGNAL, S.AWAITING_EXIT_CONFIRMATION):
                self._transition(
                    cand, S.CLOSED, f"{sig.reason}: pnl €{position.realized_pnl_eur:.2f}"
                )
            self.d.repo.save_token_state(cand.mint, str(S.CLOSED))
        self._log_event(
            f"CLOSED {sig.symbol or sig.mint[:8]} pnl=€{position.realized_pnl_eur:.2f} "
            f"({position.pnl_pct:+.0%}) reason={sig.reason}",
            "WARN",
        )
        await self._after_portfolio_change()

    async def _persist_fill(self, fill: Fill, position: Position) -> None:
        acct = self.d.account
        try:
            await self.d.repo.save_fill_now(fill)
            await self.d.repo.save_position_now(position)
            await self.d.repo.save_ledger_now(acct.ledger[-1:])
            await self.d.repo.save_account_state_now(
                cash=acct.cash,
                peak_equity=acct.peak_equity,
                realized_pnl=acct.realized_pnl,
                fees=acct.fees_total,
                slippage=acct.slippage_total,
                wins=acct.wins,
                losses=acct.losses,
                recent_results=acct.recent_performance().results,
                milestones_reached=sorted(self.d.milestones.reached),
            )
        except Exception as exc:
            self._error("storage", exc)

    async def _after_portfolio_change(self) -> None:
        now = self.now()
        snap = self.d.account.snapshot(now)
        self.d.bus.publish(PortfolioUpdated(snap))
        for m in self.d.milestones.update(snap.equity_eur, now):
            self.d.bus.publish(MilestoneReached(m))
            self._log_event(
                f"MILESTONE {m.direction} €{m.milestone_eur:,.0f} (equity €{m.equity_eur:.2f})",
                "WARN",
            )
        self._last_snapshot_at = now

    # -------------------------------------------------------------- positions
    async def _monitor_position(self, cand: Candidate, now: datetime) -> None:
        cfg = self.settings
        pid = cand.position_id
        if pid is None:
            return
        position = self.d.account.positions.get(pid)
        if position is None or not position.is_open:
            return
        track = cand.track
        features = self.d.features.compute(track, now) if (track.latest or track.trades) else None
        if features is not None:
            cand.features = features
        # refresh executable exit quote periodically
        need_quote = (
            cand.exit_quote is None
            or (now - cand.exit_quote.quoted_at).total_seconds() >= cfg.quotes.refresh_interval_s
        )
        if need_quote and not cand.exit_quote_in_flight and position.units_known:
            cand.exit_quote_in_flight = True
            self._spawn(self._refresh_exit_quote(cand, position), f"exitq-{cand.mint[:6]}")
        latest = track.latest
        price = latest.price_native if latest else None
        if price is None and track.trades and track.trades[-1].price_native is not None:
            price = track.trades[-1].price_native
        updated = self.d.monitor.value(
            position,
            now=now,
            sol_eur=self.d.fx.sol_eur(),
            exit_quote=cand.exit_quote,
            max_quote_age_s=cfg.quotes.max_quote_age_s,
            price_native=price,
        )
        position.data_stale = track.is_stale(now, cfg.market_data.stale_after_s)
        self.d.bus.publish(PositionUpdated(updated))
        if cand.state is not S.OPEN:
            return
        decision = self.d.monitor.evaluate(position, track, features, now)
        if decision is None:
            return
        sol_eur = self.d.fx.sol_eur()
        exit_quote = (
            cand.exit_quote
            if cand.exit_quote and cand.exit_quote.is_fresh(now, cfg.quotes.max_quote_age_s * 2)
            else None
        )
        est_out = (
            (Decimal(exit_quote.out_amount_raw) / Decimal(1_000_000_000)) if exit_quote else None
        )
        signal = SellSignal(
            signal_id=new_id("sell"),
            position_id=position.position_id,
            mint=cand.mint,
            symbol=cand.symbol,
            created_at=now,
            expires_at=now + timedelta(seconds=cfg.exit.exit_signal_ttl_s),
            reason=decision.reason,
            urgency=decision.urgency,
            detail=decision.detail,
            current_value_eur=position.current_value_eur,
            entry_value_eur=position.cost_basis_eur,
            peak_value_eur=position.peak_value_eur,
            pnl_eur=position.unrealized_pnl_eur,
            pnl_pct=position.pnl_pct,
            trailing_drawdown_pct=decision.trailing_drawdown_pct,
            trailing_threshold_pct=decision.trailing_threshold_pct,
            exit_quote=exit_quote,
            estimated_sell_output_sol=est_out,
            sol_eur=sol_eur,
            quantity_ui=position.quantity_ui,
            token_decimals=position.token_decimals,
            session_id=self.session_id,
        )
        if not self._transition(cand, S.EXIT_SIGNAL, f"{decision.reason}: {decision.detail}"[:160]):
            return
        try:
            order = await self.d.execution.submit_sell(signal)
        except ValueError as exc:
            self._error("execution", exc)
            self._transition(cand, S.OPEN, "duplicate sell order")
            return
        cand.sell_order = order
        self.stats.exits += 1
        self.stats.last_signal_at = now
        self.d.metrics.inc("exit_signals")
        self.d.repo.save_signal(signal, str(SignalStatus.PENDING))
        self.d.bus.publish(SellSignalCreated(signal))
        self._transition(cand, S.AWAITING_EXIT_CONFIRMATION, f"sell #{order.ref}")
        self._log_event(
            f"SELL SIGNAL #{order.ref} {cand.symbol or cand.mint[:8]} [{decision.reason}] "
            f"value=€{position.current_value_eur:.2f} pnl={position.pnl_pct:+.0%} "
            f"{decision.detail}",
            "ERROR" if decision.urgency is Urgency.URGENT else "WARN",
        )
        record = await self.d.preparer.prepare(signal)
        self.d.repo.save_execution_record(record)
        self.d.bus.publish(ExecutionPrepared(record))

    async def _refresh_exit_quote(self, cand: Candidate, position: Position) -> None:
        try:
            async with self._quote_sem:
                quote = await self.d.round_trip.exit_quote(
                    cand.mint, position.quantity_ui, position.token_decimals
                )
            cand.exit_quote = quote
            self.d.bus.publish(QuoteObtained(quote))
        except QuoteError as exc:
            self.d.metrics.inc("quote_failures")
            log.debug("exit_quote_failed", mint=cand.mint, error=safe_exception(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._error("exit_quote", exc)
        finally:
            cand.exit_quote_in_flight = False

    # ------------------------------------------------------------ user input
    async def confirm_buy(self, ref: int, override: FillOverride | None = None) -> str:
        order = self.d.execution.find(SignalKind.BUY, ref)
        if order is None:
            return f"no pending BUY #{ref}"
        cand = self.candidates.get(order.mint)
        if cand is not None and cand.track.is_stale(
            self.now(), self.settings.market_data.stale_after_s
        ):
            await self._cancel_pending_buy(cand, "data stale at confirmation time")
            return f"BUY #{ref} cancelled: market data is stale; not safe to book"
        note = ""
        if override is None and order.buy is not None:
            override, note = await self._fresh_buy_override(cand, order.buy)
        try:
            res = await self.d.execution.decide(
                order, DecisionKind.CONFIRM, DecisionSource.HUMAN, override=override, note=note
            )
        except OrderNotPendingError:
            return f"BUY #{ref} is no longer pending"
        await self._apply_resolution(res)
        return (
            f"BUY #{ref} confirmed"
            if res.fill
            else f"BUY #{ref} {res.decision.kind.lower()}: {res.decision.note}"
        )

    async def reject_buy(self, ref: int) -> str:
        order = self.d.execution.find(SignalKind.BUY, ref)
        if order is None:
            return f"no pending BUY #{ref}"
        res = await self.d.execution.decide(
            order, DecisionKind.REJECT, DecisionSource.HUMAN, note="rejected by user"
        )
        await self._apply_resolution(res)
        return f"BUY #{ref} rejected"

    async def confirm_sell(self, ref: int, override: FillOverride | None = None) -> str:
        order = self.d.execution.find(SignalKind.SELL, ref)
        if order is None:
            return f"no pending SELL #{ref}"
        note = ""
        if override is None and order.sell is not None:
            override, note = await self._fresh_sell_override(order.sell)
        try:
            res = await self.d.execution.decide(
                order, DecisionKind.CONFIRM, DecisionSource.HUMAN, override=override, note=note
            )
        except OrderNotPendingError:
            return f"SELL #{ref} is no longer pending"
        await self._apply_resolution(res)
        return f"SELL #{ref} confirmed" if res.fill else f"SELL #{ref} {res.decision.kind.lower()}"

    async def ignore_sell(self, ref: int) -> str:
        order = self.d.execution.find(SignalKind.SELL, ref)
        if order is None:
            return f"no pending SELL #{ref}"
        res = await self.d.execution.decide(
            order, DecisionKind.IGNORE, DecisionSource.HUMAN, note="ignored by user"
        )
        await self._apply_resolution(res)
        return f"SELL #{ref} ignored; position stays open"

    async def _fresh_buy_override(
        self, cand: Candidate | None, signal: BuySignal
    ) -> tuple[FillOverride | None, str]:
        """A human confirms *now*; book the fill at a fresh quote when the signal's one is old."""
        max_age = self.settings.quotes.max_quote_age_s
        if signal.quote.buy.is_fresh(self.now(), max_age):
            return None, ""
        decimals = signal.token_decimals
        try:
            async with self._quote_sem:
                rt = await self.d.round_trip.evaluate(signal.mint, signal.quote.spend_sol, decimals)
        except QuoteError as exc:
            return None, f"booked at signal quote; re-quote failed: {exc}"
        self.d.bus.publish(QuoteObtained(rt.buy))
        return FillOverride(token_amount_ui=rt.expected_tokens_ui), "booked at fresh quote"

    async def _fresh_sell_override(self, signal: SellSignal) -> tuple[FillOverride | None, str]:
        max_age = self.settings.quotes.max_quote_age_s
        if signal.exit_quote is not None and signal.exit_quote.is_fresh(self.now(), max_age):
            return None, ""
        position = self.d.account.positions.get(signal.position_id)
        if position is None or not position.units_known:
            return None, "" if position is None else "legacy record: units unverified, no re-quote"
        try:
            async with self._quote_sem:
                quote = await self.d.round_trip.exit_quote(
                    signal.mint, position.quantity_ui, position.token_decimals
                )
        except QuoteError as exc:
            return None, f"booked at signal quote; re-quote failed: {exc}"
        self.d.bus.publish(QuoteObtained(quote))
        sol_out = Decimal(quote.out_amount_raw) / Decimal(1_000_000_000)
        return FillOverride(sol_amount=sol_out), "booked at fresh quote"

    # ---------------------------------------------------------- housekeeping
    def _housekeeping(self, now: datetime) -> None:
        cfg = self.settings
        for cand in list(self.candidates.values()):
            if cand.sm.is_terminal:
                keep = cfg.entry.cooldown_after_exit_s if cand.state is S.CLOSED else 60.0
                if cand.terminal_at and (now - cand.terminal_at).total_seconds() > keep:
                    self.candidates.pop(cand.mint, None)
                    self.d.tracker.untrack(cand.mint)
                    self.timer.forget(cand.mint)
                    if not self.d.outcomes.is_following(cand.mint):
                        self._spawn(self.d.market.unwatch(cand.mint), f"unwatch-{cand.mint[:6]}")

    async def _finalize_due_outcomes(self, now: datetime) -> None:
        done = self.d.outcomes.finalize_due(now)
        if not done:
            return
        await self.d.repo.save_outcomes_now(done)  # measurement rows commit immediately
        self.d.metrics.inc("outcomes_finalized", len(done))
        for o in done:
            if o.mint not in self.candidates:
                self._spawn(self.d.market.unwatch(o.mint), f"unwatch-{o.mint[:6]}")

    async def finalize_outcomes(self) -> int:
        """Shutdown: persist every outcome still in flight (marked truncated when the horizon
        had not elapsed) so a short session still leaves measurable rows behind."""
        done = self.d.outcomes.finalize_all(self.now())
        if done:
            await self.d.repo.save_outcomes_now(done)
        return len(done)

    async def _portfolio_snapshot(self, now: datetime) -> None:
        interval = self.settings.portfolio.snapshot_interval_s
        if (
            self._last_snapshot_at is not None
            and (now - self._last_snapshot_at).total_seconds() < interval
        ):
            return
        self._last_snapshot_at = now
        snap = self.d.account.snapshot(now)
        self.d.bus.publish(PortfolioUpdated(snap))
        for m in self.d.milestones.update(snap.equity_eur, now):
            self.d.bus.publish(MilestoneReached(m))
            self._log_event(
                f"MILESTONE {m.direction} €{m.milestone_eur:,.0f} (equity €{m.equity_eur:.2f})",
                "WARN",
            )
        self.d.metrics.gauge("equity_eur", float(snap.equity_eur))
        self.d.metrics.gauge("tracked", len(self.candidates))

    # --------------------------------------------------------------- recovery
    async def _restore_positions(self) -> None:
        """Rebuild candidates for open positions loaded into the account before start."""
        for position in self.d.account.open_positions:
            if position.mint in self.candidates:
                cand = self.candidates[position.mint]
            else:
                stored = await self.d.repo.get_token(position.mint)
                decimals = position.token_decimals if position.units_known else None
                if decimals is None and stored is not None:
                    decimals = stored.decimals
                token = TokenInfo(
                    mint=position.mint,
                    symbol=position.symbol or (stored.symbol if stored else None),
                    name=stored.name if stored else None,
                    decimals=decimals,
                    pool_created_at=stored.pool_created_at if stored else None,
                    venue=stored.venue if stored else Venue.UNKNOWN,
                    pool_address=stored.pool_address if stored else None,
                    quote_mint=stored.quote_mint if stored else None,
                    source="recovered",
                )
                track = self.d.tracker.track(token)
                cand = Candidate(
                    track=track, sm=CandidateStateMachine(position.mint), discovered_at=self.now()
                )
                self.candidates[position.mint] = cand
                await self.d.market.watch(token)
            cand.position_id = position.position_id
            self.positions_by_mint[position.mint] = position.position_id
            for state in (S.MONITORING, S.QUALIFIED, S.BUY_SIGNAL, S.AWAITING_CONFIRMATION, S.OPEN):
                cand.sm.transition(state, self.now(), "restored from storage")
            if position.state in (S.EXIT_SIGNAL, S.AWAITING_EXIT_CONFIRMATION):
                position.state = (
                    S.OPEN
                )  # pending exit signals do not survive restarts; re-evaluate live
            units = "units UI" if position.units_known else "UNITS UNVERIFIED (legacy record)"
            self._log_event(
                f"restored open position {position.symbol or position.mint[:8]} "
                f"cost=€{position.cost_basis_eur:.2f} [{position.provenance}, {units}]",
                "WARN",
            )

    # ------------------------------------------------------------------ view
    def snapshots_last_minute(self, now: datetime) -> int:
        cutoff = now - timedelta(seconds=60)
        return sum(1 for t in self._snapshot_times if t >= cutoff)

    def open_positions(self) -> list[Position]:
        return self.d.account.open_positions

    def candidate_for_position(self, position: Position) -> Candidate | None:
        return self.candidates.get(position.mint)

    def priority_mints(self) -> list[str]:
        prio = [m for m in self.positions_by_mint]
        prio += [
            c.mint
            for c in self.candidates.values()
            if c.state in (S.QUALIFIED, S.BUY_SIGNAL, S.AWAITING_CONFIRMATION)
        ]
        return prio

    def exit_reason_label(self, position: Position) -> str:
        cand = self.candidates.get(position.mint)
        if cand is None or cand.sell_order is None or cand.sell_order.sell is None:
            return ""
        return str(cand.sell_order.sell.reason)
