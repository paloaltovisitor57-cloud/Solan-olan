"""Core domain models.

Immutable value objects are frozen dataclasses; mutable aggregates (Position) are plain.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from solana_sniper.domain.enums import (
    CandidateState,
    CheckVerdict,
    DecisionKind,
    DecisionSource,
    EntryDecision,
    ExitReason,
    FillProvenance,
    LedgerEntryKind,
    SignalKind,
    SignalStatus,
    TokenUnits,
    Urgency,
    Venue,
)
from solana_sniper.domain.money import ZERO, check_decimals, raw_to_ui


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Tokens, pools, market data
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TokenInfo:
    """What we know about a token at discovery time. Missing fields are None, never guessed."""

    mint: str
    symbol: str | None = None
    name: str | None = None
    decimals: int | None = None
    created_at: datetime | None = None
    pool_created_at: datetime | None = None
    first_liquidity_at: datetime | None = None
    venue: Venue = Venue.UNKNOWN
    pool_address: str | None = None
    quote_mint: str | None = None
    source: str = "unknown"
    discovered_at: datetime | None = None
    metadata_uri: str | None = None

    def age_seconds(self, now: datetime) -> float | None:
        anchor = self.pool_created_at or self.created_at or self.first_liquidity_at
        if anchor is None:
            return None
        return max(0.0, (now - anchor).total_seconds())


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """One observation of a token's market state. Every field is optional except identity/time."""

    mint: str
    observed_at: datetime
    source: str
    price_native: Decimal | None = None  # quote asset (usually SOL) per token
    price_usd: Decimal | None = None
    liquidity_usd: Decimal | None = None
    liquidity_native: Decimal | None = None  # SOL side of pool
    market_cap_usd: Decimal | None = None
    fdv_usd: Decimal | None = None
    volume_5m_usd: Decimal | None = None
    buy_volume_5m_usd: Decimal | None = None
    sell_volume_5m_usd: Decimal | None = None
    buys_5m: int | None = None
    sells_5m: int | None = None
    volume_1h_usd: Decimal | None = None
    buys_1h: int | None = None
    sells_1h: int | None = None
    holder_count: int | None = None
    unique_traders: int | None = None
    top10_holder_pct: float | None = None
    largest_holder_pct: float | None = None
    spread_bps: int | None = None
    pool_address: str | None = None
    venue: Venue = Venue.UNKNOWN
    pair_created_at: datetime | None = None
    provider_latency_ms: float | None = None

    @property
    def total_txns_5m(self) -> int | None:
        if self.buys_5m is None and self.sells_5m is None:
            return None
        return (self.buys_5m or 0) + (self.sells_5m or 0)


@dataclass(frozen=True, slots=True)
class TradeEvent:
    """A single on-chain trade observed from a stream."""

    mint: str
    observed_at: datetime
    source: str
    is_buy: bool
    sol_amount: Decimal
    token_amount: Decimal
    trader: str | None = None
    signature: str | None = None
    price_native: Decimal | None = None
    market_cap_sol: Decimal | None = None
    pool_sol_reserves: Decimal | None = None
    pool_token_reserves: Decimal | None = None


@dataclass(frozen=True, slots=True)
class TokenAuthorities:
    mint_authority: str | None
    freeze_authority: str | None
    decimals: int
    supply_raw: int
    is_token_2022: bool = False
    transfer_fee_bps: int | None = None
    has_transfer_hook: bool = False
    non_transferable: bool = False
    permanent_delegate: str | None = None


@dataclass(frozen=True, slots=True)
class HolderDistribution:
    holder_count: int | None
    top10_pct: float | None
    largest_pct: float | None
    largest_is_pool: bool | None = None
    observed_at: datetime | None = None


# ---------------------------------------------------------------------------
# Features, checks, scores
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeatureVector:
    mint: str
    computed_at: datetime
    observation_count: int
    token_age_s: float | None
    price_native: float | None
    liquidity_usd: float | None
    momentum_10s: float | None
    momentum_30s: float | None
    momentum_60s: float | None
    momentum_180s: float | None
    acceleration: float | None
    liquidity_growth_60s: float | None
    liquidity_acceleration: float | None
    volume_acceleration: float | None
    trade_velocity_per_min: float | None
    buy_velocity_per_min: float | None
    sell_velocity_per_min: float | None
    buy_sell_imbalance: float | None  # (buys - sells)/(buys+sells) in [-1, 1]
    buy_ratio: float | None  # buys/(buys+sells) in [0, 1]
    unique_trader_growth: float | None
    holder_growth: float | None
    drawdown_from_peak: float | None
    seconds_since_peak: float | None
    market_depth_usd: float | None
    estimated_slippage_bps: int | None
    estimated_exit_slippage_bps: int | None
    volatility_60s: float | None
    data_age_s: float
    stale: bool
    acceleration_raw: float | None = None  # unsmoothed m10 - prev10 (diagnostics)

    def as_dict(self) -> dict[str, float | int | bool | str | None]:
        return {
            "mint": self.mint,
            "computed_at": self.computed_at.isoformat(),
            "observation_count": self.observation_count,
            "token_age_s": self.token_age_s,
            "price_native": self.price_native,
            "liquidity_usd": self.liquidity_usd,
            "momentum_10s": self.momentum_10s,
            "momentum_30s": self.momentum_30s,
            "momentum_60s": self.momentum_60s,
            "momentum_180s": self.momentum_180s,
            "acceleration": self.acceleration,
            "acceleration_raw": self.acceleration_raw,
            "liquidity_growth_60s": self.liquidity_growth_60s,
            "liquidity_acceleration": self.liquidity_acceleration,
            "volume_acceleration": self.volume_acceleration,
            "trade_velocity_per_min": self.trade_velocity_per_min,
            "buy_velocity_per_min": self.buy_velocity_per_min,
            "sell_velocity_per_min": self.sell_velocity_per_min,
            "buy_sell_imbalance": self.buy_sell_imbalance,
            "buy_ratio": self.buy_ratio,
            "unique_trader_growth": self.unique_trader_growth,
            "holder_growth": self.holder_growth,
            "drawdown_from_peak": self.drawdown_from_peak,
            "seconds_since_peak": self.seconds_since_peak,
            "market_depth_usd": self.market_depth_usd,
            "estimated_slippage_bps": self.estimated_slippage_bps,
            "estimated_exit_slippage_bps": self.estimated_exit_slippage_bps,
            "volatility_60s": self.volatility_60s,
            "data_age_s": self.data_age_s,
            "stale": self.stale,
        }


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    verdict: CheckVerdict
    reason: str
    observed_at: datetime
    value: str | None = None
    fatal: bool = False  # a fatal REJECT ends the candidate; non-fatal means "not (yet) qualified"


@dataclass(frozen=True, slots=True)
class CheckReport:
    mint: str
    evaluated_at: datetime
    results: tuple[CheckResult, ...]

    @property
    def verdict(self) -> CheckVerdict:
        verdicts = {r.verdict for r in self.results}
        if CheckVerdict.REJECT in verdicts:
            return CheckVerdict.REJECT
        if CheckVerdict.UNKNOWN in verdicts:
            return CheckVerdict.UNKNOWN
        return CheckVerdict.PASS

    @property
    def rejections(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if r.verdict is CheckVerdict.REJECT)

    @property
    def fatal_rejections(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if r.verdict is CheckVerdict.REJECT and r.fatal)

    @property
    def is_fatal(self) -> bool:
        return bool(self.fatal_rejections)

    def summary(self, limit: int = 4) -> str:
        parts = [f"{r.name}:{r.reason}" for r in self.rejections[:limit]]
        parts += [f"{r.name}:unknown" for r in self.unknowns[: max(0, limit - len(parts))]]
        return ", ".join(parts) if parts else "all checks passed"

    @property
    def unknowns(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if r.verdict is CheckVerdict.UNKNOWN)


@dataclass(frozen=True, slots=True)
class ScoreComponent:
    name: str
    raw: float | None
    normalized: float  # 0..1
    weight: float
    contribution: float  # normalized*weight (already scaled to points)
    note: str = ""


@dataclass(frozen=True, slots=True)
class EntryScore:
    mint: str
    scored_at: datetime
    score: float  # 0..100
    components: tuple[ScoreComponent, ...]
    reasons: tuple[str, ...]
    penalties: tuple[str, ...]

    @property
    def top_reasons(self) -> tuple[str, ...]:
        return self.reasons[:5]


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SwapQuote:
    """A quote for swapping in_amount (raw units of input mint) into output mint."""

    quote_id: str
    provider: str
    input_mint: str
    output_mint: str
    in_amount_raw: int
    out_amount_raw: int
    other_amount_threshold_raw: int
    slippage_bps: int
    price_impact_pct: float
    route_labels: tuple[str, ...]
    fee_lamports: int
    quoted_at: datetime
    latency_ms: float
    raw: dict[str, object] = field(default_factory=dict, compare=False, repr=False)

    def is_fresh(self, now: datetime, max_age_s: float) -> bool:
        return (now - self.quoted_at).total_seconds() <= max_age_s


@dataclass(frozen=True, slots=True)
class RoundTripQuote:
    """BUY quote plus an estimate of immediately SELLING what we would receive."""

    mint: str
    quoted_at: datetime
    spend_sol: Decimal
    buy: SwapQuote
    sell: SwapQuote | None
    expected_tokens_ui: Decimal
    entry_slippage_bps: int
    entry_price_impact_pct: float
    immediate_exit_sol: Decimal | None
    exit_slippage_bps: int | None
    exit_price_impact_pct: float | None
    round_trip_loss_pct: float | None  # (spend - exit)/spend, positive = loss
    total_fee_lamports: int
    viable: bool
    reasons: tuple[str, ...]


# ---------------------------------------------------------------------------
# Signals, decisions, positions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PositionSizing:
    recommended_eur: Decimal
    recommended_sol: Decimal
    fraction_of_equity: Decimal
    equity_eur: Decimal
    available_cash_eur: Decimal
    caps_applied: tuple[str, ...]
    multipliers: dict[str, float]
    profile: str
    tier: str


@dataclass(frozen=True, slots=True)
class BuySignal:
    signal_id: str
    mint: str
    symbol: str | None
    created_at: datetime
    expires_at: datetime
    score: EntryScore
    features: FeatureVector
    checks: CheckReport
    sizing: PositionSizing
    quote: RoundTripQuote
    token_age_s: float | None
    liquidity_usd: Decimal | None
    price_native: Decimal | None
    sol_eur: Decimal
    token_decimals: int = 6
    kind: SignalKind = SignalKind.BUY
    urgency: Urgency = Urgency.NORMAL
    session_id: str = ""

    @property
    def expected_tokens(self) -> Decimal:
        return self.quote.expected_tokens_ui

    @property
    def immediate_exit_value_eur(self) -> Decimal | None:
        if self.quote.immediate_exit_sol is None:
            return None
        return self.quote.immediate_exit_sol * self.sol_eur


@dataclass(frozen=True, slots=True)
class SellSignal:
    signal_id: str
    position_id: str
    mint: str
    symbol: str | None
    created_at: datetime
    expires_at: datetime
    reason: ExitReason
    urgency: Urgency
    detail: str
    current_value_eur: Decimal
    entry_value_eur: Decimal
    peak_value_eur: Decimal
    pnl_eur: Decimal
    pnl_pct: float
    trailing_drawdown_pct: float
    trailing_threshold_pct: float
    exit_quote: SwapQuote | None
    estimated_sell_output_sol: Decimal | None
    sol_eur: Decimal
    quantity_ui: Decimal = ZERO
    token_decimals: int = 6
    kind: SignalKind = SignalKind.SELL
    session_id: str = ""


@dataclass(frozen=True, slots=True)
class ManualDecision:
    decision_id: str
    signal_id: str
    kind: DecisionKind
    source: DecisionSource
    decided_at: datetime
    note: str = ""


@dataclass(frozen=True, slots=True)
class Fill:
    """A confirmed (or simulated) fill.

    Never produced by broadcasting a transaction. `provenance` says where the numbers came from
    and `verified_onchain` is always False in this software: a reported signature string is stored
    as-is and is *not* evidence that the transaction exists or matches these amounts.

    Token quantities carry both representations, derived from each other with `token_decimals`:
    `token_amount_ui` (human units) and `token_amount_raw` (integer base units).
    """

    fill_id: str
    signal_id: str
    mint: str
    side: SignalKind
    filled_at: datetime
    sol_amount: Decimal  # SOL spent (buy) or received (sell)
    token_amount_ui: Decimal  # tokens received (buy) or sold (sell), UI units
    token_amount_raw: int  # same quantity in base units (10^-decimals)
    token_decimals: int
    eur_amount: Decimal
    sol_eur: Decimal
    fee_eur: Decimal
    slippage_cost_eur: Decimal
    provenance: FillProvenance
    simulated: bool
    units: TokenUnits = TokenUnits.UI
    reported_tx_signature: str | None = None  # user-supplied text, unverified
    verified_onchain: bool = False  # reserved; this software never sets it
    note: str = ""

    def __post_init__(self) -> None:
        if self.simulated != (self.provenance is FillProvenance.SIMULATED):
            raise ValueError("Fill.simulated must agree with provenance")
        if self.verified_onchain:
            raise ValueError(
                "Fill.verified_onchain cannot be set: no on-chain reconciliation exists"
            )
        if self.units is TokenUnits.UI:
            check_decimals(self.token_decimals)
            if self.token_amount_ui < 0 or self.token_amount_raw < 0:
                raise ValueError("token amounts cannot be negative")
            if raw_to_ui(self.token_amount_raw, self.token_decimals) != self.token_amount_ui:
                raise ValueError("token_amount_ui and token_amount_raw disagree for token_decimals")

    @property
    def is_verified(self) -> bool:
        return False


@dataclass(slots=True)
class Position:
    position_id: str
    mint: str
    symbol: str | None
    opened_at: datetime
    entry_price_native: Decimal
    entry_sol_eur: Decimal
    quantity_ui: Decimal  # tokens held, UI units
    cost_basis_eur: Decimal
    entry_sol: Decimal
    quantity_raw: int = 0  # same quantity in base units
    token_decimals: int = 0
    units: TokenUnits = TokenUnits.UNKNOWN_LEGACY  # set to UI by the accounting layer
    provenance: FillProvenance = FillProvenance.UNKNOWN_LEGACY  # weakest of entry/exit fills
    entry_fee_eur: Decimal = ZERO
    entry_slippage_eur: Decimal = ZERO
    peak_value_eur: Decimal = ZERO
    peak_price_native: Decimal = ZERO
    peak_at: datetime | None = None
    current_value_eur: Decimal = ZERO
    current_price_native: Decimal = ZERO
    last_valued_at: datetime | None = None
    last_quote_at: datetime | None = None
    value_is_executable: bool = False
    state: CandidateState = CandidateState.OPEN
    closed_at: datetime | None = None
    exit_value_eur: Decimal | None = None
    exit_fee_eur: Decimal = ZERO
    exit_slippage_eur: Decimal = ZERO
    exit_reason: ExitReason | None = None
    realized_pnl_eur: Decimal | None = None
    simulated: bool = False
    session_id: str = ""
    entry_signal_id: str | None = None
    exit_signal_id: str | None = None
    data_stale: bool = False

    @property
    def is_verified(self) -> bool:
        """Always False: no fill recorded by this software is reconciled against the chain."""
        return False

    @property
    def units_known(self) -> bool:
        return self.units is TokenUnits.UI

    @property
    def is_open(self) -> bool:
        return self.state in (
            CandidateState.OPEN,
            CandidateState.EXIT_SIGNAL,
            CandidateState.AWAITING_EXIT_CONFIRMATION,
        )

    @property
    def unrealized_pnl_eur(self) -> Decimal:
        if not self.is_open:
            return ZERO
        return self.current_value_eur - self.cost_basis_eur

    @property
    def pnl_pct(self) -> float:
        if self.cost_basis_eur == 0:
            return 0.0
        ref = self.exit_value_eur if self.exit_value_eur is not None else self.current_value_eur
        return float((ref - self.cost_basis_eur) / self.cost_basis_eur)

    @property
    def profit_multiple(self) -> float:
        if self.cost_basis_eur == 0:
            return 1.0
        return float(self.current_value_eur / self.cost_basis_eur)

    @property
    def trailing_drawdown_pct(self) -> float:
        if self.peak_value_eur <= 0:
            return 0.0
        return float((self.peak_value_eur - self.current_value_eur) / self.peak_value_eur)

    def holding_seconds(self, now: datetime) -> float:
        end = self.closed_at or now
        return (end - self.opened_at).total_seconds()


# ---------------------------------------------------------------------------
# Portfolio & ledger
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    entry_id: str
    seq: int
    kind: LedgerEntryKind
    at: datetime
    cash_delta_eur: Decimal
    cash_after_eur: Decimal
    position_id: str | None
    mint: str | None
    description: str
    fee_eur: Decimal = ZERO
    slippage_eur: Decimal = ZERO
    realized_pnl_eur: Decimal = ZERO
    reference_id: str | None = None
    provenance: FillProvenance = FillProvenance.UNKNOWN_LEGACY


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    at: datetime
    cash_eur: Decimal
    open_exposure_eur: Decimal  # cost basis of open positions
    open_value_eur: Decimal  # current executable value of open positions
    equity_eur: Decimal
    peak_equity_eur: Decimal
    drawdown_pct: float
    realized_pnl_eur: Decimal
    unrealized_pnl_eur: Decimal
    fees_eur: Decimal
    slippage_eur: Decimal
    open_positions: int
    wins: int
    losses: int
    session_id: str = ""


@dataclass(slots=True)
class EntryAttempt:
    """Durable audit record of one qualification latch window: why a qualified token did or did
    not become a BUY signal. Opened when a candidate reaches QUALIFIED, updated as decimals,
    sizing and quotes arrive, completed with exactly one final decision."""

    attempt_id: str
    session_id: str
    mint: str
    symbol: str | None
    qualified_at: datetime
    qualified_score: float
    latch_until: datetime
    qualified_features: dict[str, float | int | bool | str | None] = field(default_factory=dict)
    qualified_checks: str = ""
    attempt_number: int = 1
    decimals_status: str = "unknown"  # "known:<n>" | "unknown" | "resolved"
    sizing_attempted: bool = False
    recommended_eur: Decimal | None = None
    recommended_sol: Decimal | None = None
    sizing_reason: str | None = None
    quote_attempts: int = 0
    quote_started_at: datetime | None = None
    quote_finished_at: datetime | None = None
    buy_quote_status: str = "not_attempted"  # not_attempted | ok | failed
    sell_quote_status: str = "not_attempted"
    quote_error: str | None = None
    quote_ids: list[str] = field(default_factory=list)
    entry_price_impact_pct: float | None = None
    exit_price_impact_pct: float | None = None
    round_trip_loss_pct: float | None = None
    round_trip_viable: bool | None = None
    post_quote_score: float | None = None
    min_score_seen: float | None = None
    max_score_seen: float | None = None
    evaluations: int = 0
    hysteresis_holds: int = 0  # evaluations that stayed latched with the score under min_score
    final_decision: EntryDecision = EntryDecision.PENDING
    block_reason: str | None = None
    signal_id: str | None = None
    completed_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.final_decision is EntryDecision.PENDING

    def note_score(self, score: float) -> None:
        self.evaluations += 1
        self.min_score_seen = (
            score if self.min_score_seen is None else min(self.min_score_seen, score)
        )
        self.max_score_seen = (
            score if self.max_score_seen is None else max(self.max_score_seen, score)
        )


@dataclass(frozen=True, slots=True)
class MilestoneEvent:
    milestone_eur: Decimal
    equity_eur: Decimal
    reached_at: datetime
    direction: str  # "UP" or "DOWN"
    session_id: str = ""


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    record_id: str
    signal_id: str
    mint: str
    side: SignalKind
    created_at: datetime
    status: SignalStatus
    simulated: bool
    unsigned_transaction_b64: str | None
    instructions: str
    quote_id: str | None
    note: str = ""
    session_id: str = ""


@dataclass(frozen=True, slots=True)
class ErrorRecord:
    at: datetime
    component: str
    message: str
    detail: str = ""
    session_id: str = ""
