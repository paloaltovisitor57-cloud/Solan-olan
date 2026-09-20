"""Enumerations shared across the system."""

from __future__ import annotations

from enum import StrEnum


class CandidateState(StrEnum):
    DISCOVERED = "DISCOVERED"
    MONITORING = "MONITORING"
    QUALIFIED = "QUALIFIED"
    BUY_SIGNAL = "BUY_SIGNAL"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    OPEN = "OPEN"
    EXIT_SIGNAL = "EXIT_SIGNAL"
    AWAITING_EXIT_CONFIRMATION = "AWAITING_EXIT_CONFIRMATION"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    DATA_STALE = "DATA_STALE"
    SIGNAL_CANCELLED = "SIGNAL_CANCELLED"


TERMINAL_STATES: frozenset[CandidateState] = frozenset(
    {CandidateState.CLOSED, CandidateState.REJECTED, CandidateState.EXPIRED}
)

POSITION_STATES: frozenset[CandidateState] = frozenset(
    {
        CandidateState.OPEN,
        CandidateState.EXIT_SIGNAL,
        CandidateState.AWAITING_EXIT_CONFIRMATION,
    }
)

ENTRY_PENDING_STATES: frozenset[CandidateState] = frozenset(
    {CandidateState.BUY_SIGNAL, CandidateState.AWAITING_CONFIRMATION}
)


class CheckVerdict(StrEnum):
    PASS = "PASS"
    REJECT = "REJECT"
    UNKNOWN = "UNKNOWN"


class SignalKind(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class SignalStatus(StrEnum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class Urgency(StrEnum):
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    URGENT = "URGENT"


class ExitReason(StrEnum):
    TRAILING_PEAK = "TRAILING_PEAK"
    MOMENTUM_DETERIORATION = "MOMENTUM_DETERIORATION"
    LIQUIDITY_COLLAPSE = "LIQUIDITY_COLLAPSE"
    VOLUME_COLLAPSE = "VOLUME_COLLAPSE"
    MAX_LOSS = "MAX_LOSS"
    TIMEOUT = "TIMEOUT"
    ABNORMAL_EVENT = "ABNORMAL_EVENT"
    DATA_STALE = "DATA_STALE"
    MANUAL = "MANUAL"


class RiskProfileName(StrEnum):
    NORMAL = "NORMAL"
    AGGRESSIVE = "AGGRESSIVE"
    EXTREME = "EXTREME"


class DecisionKind(StrEnum):
    CONFIRM = "CONFIRM"
    REJECT = "REJECT"
    IGNORE = "IGNORE"
    EXPIRE = "EXPIRE"


class DecisionSource(StrEnum):
    HUMAN = "HUMAN"
    DRY_RUN = "DRY_RUN"
    SYSTEM = "SYSTEM"
    AUTONOMOUS = "AUTONOMOUS"  # the bot itself, in autonomous mode (hot wallet)


class FillProvenance(StrEnum):
    """How a recorded fill came to exist.

    SIMULATED        dry-run/paper auto-confirmation; no transaction ever existed
    ESTIMATED        human confirmed, amounts taken from the quote (not from a wallet or chain)
    USER_REPORTED    human confirmed and typed the amounts / signature they saw in their wallet
    VERIFIED_ONCHAIN autonomous mode: the bot signed and broadcast the swap and the amounts were
                     read back from the confirmed transaction (the only provenance that is
                     reconciled against the chain; set only by the on-chain reconciler)
    UNKNOWN_LEGACY   record written before provenance existed; migration flags it, never upgrades it
    """

    SIMULATED = "SIMULATED"
    ESTIMATED = "ESTIMATED"
    USER_REPORTED = "USER_REPORTED"
    VERIFIED_ONCHAIN = "VERIFIED_ONCHAIN"
    UNKNOWN_LEGACY = "UNKNOWN_LEGACY"


class TokenUnits(StrEnum):
    """Which unit a recorded token quantity is in. UI = human units (10^-decimals)."""

    UI = "UI"
    UNKNOWN_LEGACY = "UNKNOWN_LEGACY"


PROVENANCE_RANK = {
    FillProvenance.SIMULATED: 0,
    FillProvenance.UNKNOWN_LEGACY: 1,
    FillProvenance.ESTIMATED: 2,
    FillProvenance.USER_REPORTED: 3,
    FillProvenance.VERIFIED_ONCHAIN: 4,
}


def weakest_provenance(*items: FillProvenance) -> FillProvenance:
    """The least trustworthy of several provenances (a position is only as verified as its
    weakest fill)."""
    return min(items, key=lambda p: PROVENANCE_RANK[p]) if items else FillProvenance.UNKNOWN_LEGACY


class LedgerEntryKind(StrEnum):
    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"
    BUY = "BUY"
    SELL = "SELL"
    FEE = "FEE"
    ADJUSTMENT = "ADJUSTMENT"


class Venue(StrEnum):
    PUMP_FUN = "pump.fun"
    PUMP_SWAP = "pumpswap"
    RAYDIUM = "raydium"
    RAYDIUM_CLMM = "raydium-clmm"
    METEORA = "meteora"
    ORCA = "orca"
    JUPITER = "jupiter"
    SYNTHETIC = "synthetic"
    UNKNOWN = "unknown"


class MarketDataProvenance(StrEnum):
    """Where the observed prices/liquidity came from."""

    LIVE = "LIVE"  # real Solana discovery and market data
    SYNTHETIC = "SYNTHETIC"  # the offline synthetic world
    UNKNOWN_LEGACY = "UNKNOWN_LEGACY"  # recorded before the distinction existed


class ExecutionProvenance(StrEnum):
    """How fills were produced."""

    SIMULATED = "SIMULATED"  # paper/dry-run: simulated confirmations and fills
    MANUAL_SIGNAL = "MANUAL_SIGNAL"  # live signal mode: a human confirms every fill
    AUTONOMOUS = "AUTONOMOUS"  # autonomous mode: bot-signed swaps, fills reconciled on-chain


class EntryDecision(StrEnum):
    """How an entry attempt (one qualification latch window) ended."""

    PENDING = "PENDING"  # still latched
    BUY_SIGNAL = "BUY_SIGNAL"  # a BUY signal was generated
    ABANDONED = "ABANDONED"  # strategy/economics no longer acceptable (score collapse, round trip)
    EXPIRED = "EXPIRED"  # the latch window ended without a signal (quote/decimals/sizing pending)
    HARD_REJECT = "HARD_REJECT"  # fatal safety check while latched
    QUOTE_FAILED = "QUOTE_FAILED"  # no usable quote after the bounded retries
    SIZING_ZERO = "SIZING_ZERO"  # risk engine sized the position to zero
    STALE = "STALE"  # market data became unusable
    CANCELLED = "CANCELLED"  # engine stopped or candidate retired while latched


class RunMode(StrEnum):
    LIVE = "LIVE"  # live signal mode: a human executes and confirms every trade
    DRY_RUN = "DRY_RUN"
    PAPER = "PAPER"  # live data, simulated fills, isolated per-session database
    REPLAY = "REPLAY"
    AUTONOMOUS = "AUTONOMOUS"  # live data, the bot signs and broadcasts from its hot wallet


class IntentStatus(StrEnum):
    """Lifecycle of one autonomous execution intent (durable before anything is sent)."""

    PREPARED = "PREPARED"  # rails passed, quote in progress, nothing built yet
    BUILT = "BUILT"  # transaction built and signed; signature known; not yet sent
    SENT = "SENT"  # broadcast acknowledged (or possibly received); awaiting confirmation
    CONFIRMED = "CONFIRMED"  # confirmed on chain and reconciled into a fill
    FAILED = "FAILED"  # rejected by preflight/the network, or the transaction errored on chain
    EXPIRED = "EXPIRED"  # blockhash expired before confirmation; the transaction cannot land
    ABANDONED = "ABANDONED"  # rails or quote drift stopped it before anything was sent
