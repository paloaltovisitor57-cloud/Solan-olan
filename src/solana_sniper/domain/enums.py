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


class FillProvenance(StrEnum):
    """How a recorded fill came to exist. Nothing this software records is on-chain verified.

    SIMULATED        dry-run auto-confirmation; no transaction ever existed
    ESTIMATED        human confirmed, amounts taken from the quote (not from a wallet or chain)
    USER_REPORTED    human confirmed and typed the amounts / signature they saw in their wallet
    VERIFIED_ONCHAIN reserved for a future reconciliation adapter; never set by this software
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


class RunMode(StrEnum):
    LIVE = "LIVE"
    DRY_RUN = "DRY_RUN"
    REPLAY = "REPLAY"
