"""Engine events. Everything observable flows through these so storage/dashboard/alerts decouple."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from solana_sniper.domain.enums import CandidateState
from solana_sniper.domain.models import (
    BuySignal,
    CheckReport,
    EntryScore,
    ErrorRecord,
    ExecutionIntent,
    ExecutionRecord,
    FeatureVector,
    Fill,
    ManualDecision,
    MarketSnapshot,
    MilestoneEvent,
    PortfolioSnapshot,
    Position,
    RoundTripQuote,
    SellSignal,
    SwapQuote,
    TokenInfo,
    TradeEvent,
)


@dataclass(frozen=True, slots=True)
class TokenDiscovered:
    token: TokenInfo
    at: datetime


@dataclass(frozen=True, slots=True)
class SnapshotObserved:
    snapshot: MarketSnapshot


@dataclass(frozen=True, slots=True)
class TradeObserved:
    trade: TradeEvent


@dataclass(frozen=True, slots=True)
class FeaturesComputed:
    features: FeatureVector


@dataclass(frozen=True, slots=True)
class ChecksEvaluated:
    report: CheckReport


@dataclass(frozen=True, slots=True)
class Scored:
    score: EntryScore


@dataclass(frozen=True, slots=True)
class StateChanged:
    mint: str
    source: CandidateState
    target: CandidateState
    at: datetime
    reason: str


@dataclass(frozen=True, slots=True)
class BuySignalCreated:
    signal: BuySignal


@dataclass(frozen=True, slots=True)
class SellSignalCreated:
    signal: SellSignal


@dataclass(frozen=True, slots=True)
class SignalResolved:
    signal_id: str
    mint: str
    status: str
    at: datetime


@dataclass(frozen=True, slots=True)
class DecisionRecorded:
    decision: ManualDecision


@dataclass(frozen=True, slots=True)
class QuoteObtained:
    quote: SwapQuote


@dataclass(frozen=True, slots=True)
class RoundTripEvaluated:
    round_trip: RoundTripQuote


@dataclass(frozen=True, slots=True)
class FillRecorded:
    fill: Fill


@dataclass(frozen=True, slots=True)
class PositionOpened:
    position: Position


@dataclass(frozen=True, slots=True)
class PositionUpdated:
    position: Position


@dataclass(frozen=True, slots=True)
class PositionClosed:
    position: Position


@dataclass(frozen=True, slots=True)
class PortfolioUpdated:
    snapshot: PortfolioSnapshot


@dataclass(frozen=True, slots=True)
class MilestoneReached:
    milestone: MilestoneEvent


@dataclass(frozen=True, slots=True)
class ExecutionPrepared:
    record: ExecutionRecord


@dataclass(frozen=True, slots=True)
class ErrorOccurred:
    error: ErrorRecord


@dataclass(frozen=True, slots=True)
class TransactionSent:
    """Autonomous mode broadcast a signed swap (real money in flight)."""

    intent: ExecutionIntent


@dataclass(frozen=True, slots=True)
class TransactionConfirmed:
    """The swap confirmed on chain and its amounts were read back into `fill`."""

    intent: ExecutionIntent
    fill: Fill


@dataclass(frozen=True, slots=True)
class TransactionFailed:
    """The swap was abandoned, rejected, expired or errored; `reason` is already scrubbed."""

    intent: ExecutionIntent
    reason: str


@dataclass(frozen=True, slots=True)
class AutonomyDisarmed:
    """A safety rail disarmed autonomous trading (no new buys until re-armed)."""

    at: datetime
    reason: str


@dataclass(frozen=True, slots=True)
class LogLine:
    at: datetime
    level: str
    message: str


Event = (
    TokenDiscovered
    | SnapshotObserved
    | TradeObserved
    | FeaturesComputed
    | ChecksEvaluated
    | Scored
    | StateChanged
    | BuySignalCreated
    | SellSignalCreated
    | SignalResolved
    | DecisionRecorded
    | QuoteObtained
    | RoundTripEvaluated
    | FillRecorded
    | PositionOpened
    | PositionUpdated
    | PositionClosed
    | PortfolioUpdated
    | MilestoneReached
    | ExecutionPrepared
    | ErrorOccurred
    | TransactionSent
    | TransactionConfirmed
    | TransactionFailed
    | AutonomyDisarmed
    | LogLine
)
