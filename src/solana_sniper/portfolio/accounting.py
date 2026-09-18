"""Portfolio accounting with an append-only ledger. Every cash movement is a LedgerEntry.

Invariants enforced here (and tested):
* cash can never go negative (InsufficientCashError raised before any mutation)
* a position can never be closed twice (PositionAlreadyClosedError)
* equity = cash + executable value of open positions
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from solana_sniper.domain.clock import Clock
from solana_sniper.domain.enums import (
    CandidateState,
    ExitReason,
    FillProvenance,
    LedgerEntryKind,
    SignalKind,
    TokenUnits,
    weakest_provenance,
)
from solana_sniper.domain.models import (
    Fill,
    LedgerEntry,
    PortfolioSnapshot,
    Position,
    new_id,
)
from solana_sniper.domain.money import ZERO, q_eur


class InsufficientCashError(Exception):
    pass


class PositionAlreadyClosedError(Exception):
    pass


class UnknownPositionError(KeyError):
    pass


@dataclass(frozen=True, slots=True)
class RecentPerformance:
    window: int
    results: tuple[Decimal, ...]
    wins: int
    losses: int
    consecutive_losses: int
    consecutive_wins: int
    expectancy_eur: Decimal
    win_rate: float


class PortfolioAccount:
    def __init__(
        self,
        clock: Clock,
        session_id: str = "",
        streak_window: int = 10,
    ) -> None:
        self._clock = clock
        self.session_id = session_id
        self.cash: Decimal = ZERO
        self.positions: dict[str, Position] = {}
        self.ledger: list[LedgerEntry] = []
        self._seq = 0
        self.realized_pnl: Decimal = ZERO
        self.fees_total: Decimal = ZERO
        self.slippage_total: Decimal = ZERO
        self.peak_equity: Decimal = ZERO
        self.wins = 0
        self.losses = 0
        self._recent: deque[Decimal] = deque(maxlen=max(1, streak_window))

    # ------------------------------------------------------------------ ledger
    def _append(
        self,
        kind: LedgerEntryKind,
        cash_delta: Decimal,
        description: str,
        *,
        at: datetime | None = None,
        position_id: str | None = None,
        mint: str | None = None,
        fee: Decimal = ZERO,
        slippage: Decimal = ZERO,
        realized: Decimal = ZERO,
        reference_id: str | None = None,
        provenance: FillProvenance = FillProvenance.UNKNOWN_LEGACY,
    ) -> LedgerEntry:
        new_cash = q_eur(self.cash + cash_delta)
        if new_cash < 0:
            raise InsufficientCashError(
                f"{description}: cash {self.cash} + delta {cash_delta} would be negative"
            )
        self._seq += 1
        self.cash = new_cash
        entry = LedgerEntry(
            entry_id=new_id("led"),
            seq=self._seq,
            kind=kind,
            at=at or self._clock.now(),
            cash_delta_eur=q_eur(cash_delta),
            cash_after_eur=new_cash,
            position_id=position_id,
            mint=mint,
            description=description,
            fee_eur=q_eur(fee),
            slippage_eur=q_eur(slippage),
            realized_pnl_eur=q_eur(realized),
            reference_id=reference_id,
            provenance=provenance,
        )
        self.ledger.append(entry)
        return entry

    def deposit(self, amount: Decimal, description: str = "deposit") -> LedgerEntry:
        if amount <= 0:
            raise ValueError("deposit must be positive")
        entry = self._append(
            LedgerEntryKind.DEPOSIT, amount, description, provenance=FillProvenance.USER_REPORTED
        )
        self._update_peak()
        return entry

    # --------------------------------------------------------------- positions
    def open_position(
        self,
        fill: Fill,
        *,
        symbol: str | None,
        entry_price_native: Decimal,
        entry_signal_id: str | None = None,
    ) -> Position:
        if fill.side is not SignalKind.BUY:
            raise ValueError("open_position requires a BUY fill")
        if fill.token_amount_ui <= 0 or fill.token_amount_raw <= 0:
            raise ValueError("fill token amount must be positive")
        total_cost = q_eur(fill.eur_amount + fill.fee_eur)
        if total_cost > self.cash:
            raise InsufficientCashError(
                f"buy {fill.mint}: cost {total_cost} exceeds available cash {self.cash}"
            )
        position = Position(
            position_id=new_id("pos"),
            mint=fill.mint,
            symbol=symbol,
            opened_at=fill.filled_at,
            entry_price_native=entry_price_native,
            entry_sol_eur=fill.sol_eur,
            quantity_ui=fill.token_amount_ui,
            quantity_raw=fill.token_amount_raw,
            token_decimals=fill.token_decimals,
            units=TokenUnits.UI,
            provenance=fill.provenance,
            cost_basis_eur=total_cost,
            entry_sol=fill.sol_amount,
            entry_fee_eur=q_eur(fill.fee_eur),
            entry_slippage_eur=q_eur(fill.slippage_cost_eur),
            peak_value_eur=q_eur(fill.eur_amount),
            peak_price_native=entry_price_native,
            peak_at=fill.filled_at,
            current_value_eur=q_eur(fill.eur_amount),
            current_price_native=entry_price_native,
            last_valued_at=fill.filled_at,
            value_is_executable=True,
            simulated=fill.simulated,
            session_id=self.session_id,
            entry_signal_id=entry_signal_id,
        )
        self._append(
            LedgerEntryKind.BUY,
            -total_cost,
            f"BUY {symbol or fill.mint[:8]} qty={fill.token_amount_ui} for {total_cost} EUR",
            at=fill.filled_at,
            position_id=position.position_id,
            mint=fill.mint,
            fee=fill.fee_eur,
            slippage=fill.slippage_cost_eur,
            reference_id=fill.fill_id,
            provenance=fill.provenance,
        )
        self.fees_total = q_eur(self.fees_total + fill.fee_eur)
        self.slippage_total = q_eur(self.slippage_total + fill.slippage_cost_eur)
        self.positions[position.position_id] = position
        self._update_peak()
        return position

    def mark_position(
        self,
        position_id: str,
        *,
        value_eur: Decimal,
        price_native: Decimal,
        at: datetime,
        executable: bool,
    ) -> Position:
        pos = self._get_open(position_id)
        if value_eur < 0:
            raise ValueError("position value cannot be negative")
        pos.current_value_eur = q_eur(value_eur)
        pos.current_price_native = price_native
        pos.last_valued_at = at
        pos.value_is_executable = executable
        if executable:
            pos.last_quote_at = at
        if pos.current_value_eur > pos.peak_value_eur:
            pos.peak_value_eur = pos.current_value_eur
            pos.peak_price_native = price_native
            pos.peak_at = at
        self._update_peak()
        return pos

    def close_position(self, position_id: str, fill: Fill, reason: ExitReason) -> Position:
        pos = self._get_open(position_id)
        if fill.side is not SignalKind.SELL:
            raise ValueError("close_position requires a SELL fill")
        proceeds = q_eur(fill.eur_amount - fill.fee_eur)
        if proceeds < 0:
            proceeds = ZERO
        realized = q_eur(proceeds - pos.cost_basis_eur)
        self._append(
            LedgerEntryKind.SELL,
            proceeds,
            f"SELL {pos.symbol or pos.mint[:8]} qty={fill.token_amount_ui} "
            f"for {proceeds} EUR ({reason})",
            at=fill.filled_at,
            position_id=pos.position_id,
            mint=pos.mint,
            fee=fill.fee_eur,
            slippage=fill.slippage_cost_eur,
            realized=realized,
            reference_id=fill.fill_id,
            provenance=fill.provenance,
        )
        pos.provenance = weakest_provenance(pos.provenance, fill.provenance)
        pos.state = CandidateState.CLOSED
        pos.closed_at = fill.filled_at
        pos.exit_value_eur = proceeds
        pos.exit_fee_eur = q_eur(fill.fee_eur)
        pos.exit_slippage_eur = q_eur(fill.slippage_cost_eur)
        pos.exit_reason = reason
        pos.realized_pnl_eur = realized
        pos.current_value_eur = ZERO
        pos.exit_signal_id = fill.signal_id
        self.realized_pnl = q_eur(self.realized_pnl + realized)
        self.fees_total = q_eur(self.fees_total + fill.fee_eur)
        self.slippage_total = q_eur(self.slippage_total + fill.slippage_cost_eur)
        if realized > 0:
            self.wins += 1
        else:
            self.losses += 1
        self._recent.append(realized)
        self._update_peak()
        return pos

    def _get_open(self, position_id: str) -> Position:
        pos = self.positions.get(position_id)
        if pos is None:
            raise UnknownPositionError(position_id)
        if not pos.is_open:
            raise PositionAlreadyClosedError(position_id)
        return pos

    # ------------------------------------------------------------------ views
    @property
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.is_open]

    @property
    def open_exposure(self) -> Decimal:
        return q_eur(sum((p.cost_basis_eur for p in self.open_positions), ZERO))

    @property
    def open_value(self) -> Decimal:
        return q_eur(sum((p.current_value_eur for p in self.open_positions), ZERO))

    @property
    def equity(self) -> Decimal:
        return q_eur(self.cash + self.open_value)

    @property
    def unrealized_pnl(self) -> Decimal:
        return q_eur(sum((p.unrealized_pnl_eur for p in self.open_positions), ZERO))

    @property
    def drawdown_pct(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        dd = (self.peak_equity - self.equity) / self.peak_equity
        return max(0.0, float(dd))

    def _update_peak(self) -> None:
        eq = self.equity
        if eq > self.peak_equity:
            self.peak_equity = eq

    def recent_performance(self) -> RecentPerformance:
        results = tuple(self._recent)
        wins = sum(1 for r in results if r > 0)
        losses = len(results) - wins
        consecutive_losses = 0
        for r in reversed(results):
            if r <= 0:
                consecutive_losses += 1
            else:
                break
        consecutive_wins = 0
        for r in reversed(results):
            if r > 0:
                consecutive_wins += 1
            else:
                break
        expectancy = q_eur(sum(results, ZERO) / len(results)) if results else ZERO
        win_rate = wins / len(results) if results else 0.0
        return RecentPerformance(
            window=self._recent.maxlen or 0,
            results=results,
            wins=wins,
            losses=losses,
            consecutive_losses=consecutive_losses,
            consecutive_wins=consecutive_wins,
            expectancy_eur=expectancy,
            win_rate=win_rate,
        )

    def snapshot(self, at: datetime | None = None) -> PortfolioSnapshot:
        return PortfolioSnapshot(
            at=at or self._clock.now(),
            cash_eur=self.cash,
            open_exposure_eur=self.open_exposure,
            open_value_eur=self.open_value,
            equity_eur=self.equity,
            peak_equity_eur=self.peak_equity,
            drawdown_pct=self.drawdown_pct,
            realized_pnl_eur=self.realized_pnl,
            unrealized_pnl_eur=self.unrealized_pnl,
            fees_eur=self.fees_total,
            slippage_eur=self.slippage_total,
            open_positions=len(self.open_positions),
            wins=self.wins,
            losses=self.losses,
            session_id=self.session_id,
        )

    # --------------------------------------------------------------- recovery
    def restore(
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
    ) -> None:
        """Rebuild in-memory state from persisted records (restart recovery)."""
        if cash < 0:
            raise InsufficientCashError(
                "persisted cash is negative; refusing to restore corrupt state"
            )
        self.cash = q_eur(cash)
        self.ledger = list(ledger)
        self._seq = max((e.seq for e in ledger), default=0)
        self.positions = {p.position_id: p for p in positions}
        self.peak_equity = q_eur(peak_equity)
        self.realized_pnl = q_eur(realized_pnl)
        self.fees_total = q_eur(fees)
        self.slippage_total = q_eur(slippage)
        self.wins = wins
        self.losses = losses
        self._recent.clear()
        for r in recent_results[-(self._recent.maxlen or 10) :]:
            self._recent.append(r)
        self._update_peak()
