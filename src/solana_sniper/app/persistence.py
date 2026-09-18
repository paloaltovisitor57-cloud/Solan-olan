"""Bus subscriber that persists every event that has a table."""

from __future__ import annotations

from solana_sniper.domain.enums import SignalStatus
from solana_sniper.domain.events import (
    BuySignalCreated,
    ChecksEvaluated,
    DecisionRecorded,
    ErrorOccurred,
    Event,
    ExecutionPrepared,
    FeaturesComputed,
    MilestoneReached,
    PortfolioUpdated,
    QuoteObtained,
    Scored,
    SellSignalCreated,
    SnapshotObserved,
    StateChanged,
    TokenDiscovered,
    TradeObserved,
)
from solana_sniper.storage.repository import Repository

_KIND_BY_EVENT = {
    "TokenDiscovered": "token",
    "SnapshotObserved": "observation",
    "TradeObserved": "trade",
    "FeaturesComputed": "feature",
    "ChecksEvaluated": "check",
    "Scored": "score",
    "StateChanged": "transition",
    "BuySignalCreated": "signal",
    "SellSignalCreated": "signal",
    "DecisionRecorded": "decision",
    "QuoteObtained": "quote",
    "PortfolioUpdated": "portfolio_snapshot",
    "MilestoneReached": "milestone",
    "ExecutionPrepared": "execution_record",
    "ErrorOccurred": "error",
}


class PersistenceSubscriber:
    def __init__(
        self, repo: Repository, *, store_features: bool = True, store_market: bool = True
    ) -> None:
        self._repo = repo
        self._store_features = store_features
        self._store_market = store_market

    async def handle(self, event: Event) -> None:
        try:
            self._dispatch(event)
        except Exception as exc:  # serialization or builder failure: count it, never hide it
            kind = _KIND_BY_EVENT.get(type(event).__name__, "unknown")
            self._repo.note_dropped(kind, f"{type(exc).__name__}: {exc}"[:200])

    def _dispatch(self, event: Event) -> None:
        r = self._repo
        if isinstance(event, TokenDiscovered):
            r.save_token(event.token)
        elif isinstance(event, SnapshotObserved):
            if self._store_market:
                r.save_observation(event.snapshot)
        elif isinstance(event, TradeObserved):
            if self._store_market:
                r.save_trade(event.trade)
        elif isinstance(event, FeaturesComputed):
            if self._store_features:
                r.save_features(event.features)
        elif isinstance(event, ChecksEvaluated):
            r.save_checks(event.report)
        elif isinstance(event, Scored):
            r.save_score(event.score)
        elif isinstance(event, StateChanged):
            r.save_transition(
                event.mint, str(event.source), str(event.target), event.at, event.reason
            )
        elif isinstance(event, BuySignalCreated | SellSignalCreated):
            r.save_signal(event.signal, str(SignalStatus.PENDING))
        elif isinstance(event, DecisionRecorded):
            r.save_decision(event.decision)
        elif isinstance(event, QuoteObtained):
            q = event.quote
            mint = q.output_mint if q.input_mint.startswith("So1111") else q.input_mint
            r.save_quote(q, mint)
        elif isinstance(event, PortfolioUpdated):
            r.save_portfolio_snapshot(event.snapshot)
        elif isinstance(event, MilestoneReached):
            r.save_milestone(event.milestone)
        elif isinstance(event, ExecutionPrepared):
            r.save_execution_record(event.record)
        elif isinstance(event, ErrorOccurred):
            r.save_error(event.error)
