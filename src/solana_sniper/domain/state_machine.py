"""Explicit candidate lifecycle state machine with guarded transitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from solana_sniper.domain.enums import (
    ENTRY_PENDING_STATES,
    POSITION_STATES,
    TERMINAL_STATES,
    CandidateState,
)

S = CandidateState

ALLOWED_TRANSITIONS: dict[CandidateState, frozenset[CandidateState]] = {
    S.DISCOVERED: frozenset({S.MONITORING, S.REJECTED, S.EXPIRED}),
    S.MONITORING: frozenset({S.QUALIFIED, S.REJECTED, S.EXPIRED, S.DATA_STALE}),
    S.QUALIFIED: frozenset({S.BUY_SIGNAL, S.MONITORING, S.REJECTED, S.EXPIRED, S.DATA_STALE}),
    S.BUY_SIGNAL: frozenset(
        {S.AWAITING_CONFIRMATION, S.SIGNAL_CANCELLED, S.DATA_STALE, S.REJECTED}
    ),
    S.AWAITING_CONFIRMATION: frozenset({S.OPEN, S.SIGNAL_CANCELLED, S.DATA_STALE}),
    S.OPEN: frozenset({S.EXIT_SIGNAL}),
    S.EXIT_SIGNAL: frozenset({S.AWAITING_EXIT_CONFIRMATION, S.OPEN}),
    S.AWAITING_EXIT_CONFIRMATION: frozenset({S.CLOSED, S.OPEN}),
    S.DATA_STALE: frozenset({S.MONITORING, S.EXPIRED, S.REJECTED}),
    S.SIGNAL_CANCELLED: frozenset({S.MONITORING, S.EXPIRED, S.REJECTED}),
    S.CLOSED: frozenset(),
    S.REJECTED: frozenset(),
    S.EXPIRED: frozenset(),
}


class InvalidTransitionError(Exception):
    def __init__(self, mint: str, current: CandidateState, target: CandidateState) -> None:
        super().__init__(f"{mint}: {current} -> {target} is not allowed")
        self.mint = mint
        self.current = current
        self.target = target


@dataclass(frozen=True, slots=True)
class Transition:
    at: datetime
    source: CandidateState
    target: CandidateState
    reason: str


@dataclass(slots=True)
class CandidateStateMachine:
    mint: str
    state: CandidateState = CandidateState.DISCOVERED
    history: list[Transition] = field(default_factory=list)
    entry_signal_count: int = 0

    def can(self, target: CandidateState) -> bool:
        return target in ALLOWED_TRANSITIONS[self.state]

    def transition(self, target: CandidateState, at: datetime, reason: str = "") -> Transition:
        if not self.can(target):
            raise InvalidTransitionError(self.mint, self.state, target)
        if target is CandidateState.BUY_SIGNAL:
            # Only one entry signal may be live at a time. Re-entry after a cancel is allowed
            # only through MONITORING -> QUALIFIED -> BUY_SIGNAL, which resets nothing here.
            self.entry_signal_count += 1
        tr = Transition(at=at, source=self.state, target=target, reason=reason)
        self.state = target
        self.history.append(tr)
        return tr

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def has_pending_entry(self) -> bool:
        return self.state in ENTRY_PENDING_STATES

    @property
    def in_position(self) -> bool:
        return self.state in POSITION_STATES

    @property
    def may_generate_entry(self) -> bool:
        """True only from QUALIFIED. Prevents duplicate simultaneous entry signals."""
        return self.state is CandidateState.QUALIFIED
