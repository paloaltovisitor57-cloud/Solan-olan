from __future__ import annotations

import pytest

from solana_sniper.domain.clock import ManualClock
from solana_sniper.domain.enums import CandidateState as S
from solana_sniper.domain.state_machine import (
    ALLOWED_TRANSITIONS,
    CandidateStateMachine,
    InvalidTransitionError,
)


def test_happy_path(clock: ManualClock) -> None:
    sm = CandidateStateMachine("m")
    path = [
        S.MONITORING,
        S.QUALIFIED,
        S.BUY_SIGNAL,
        S.AWAITING_CONFIRMATION,
        S.OPEN,
        S.EXIT_SIGNAL,
        S.AWAITING_EXIT_CONFIRMATION,
        S.CLOSED,
    ]
    for target in path:
        sm.transition(target, clock.now(), "test")
    assert sm.state is S.CLOSED
    assert sm.is_terminal
    assert len(sm.history) == len(path)
    assert sm.entry_signal_count == 1


def test_invalid_transition_raises(clock: ManualClock) -> None:
    sm = CandidateStateMachine("m")
    with pytest.raises(InvalidTransitionError):
        sm.transition(S.OPEN, clock.now())
    assert sm.state is S.DISCOVERED


def test_terminal_states_have_no_exits() -> None:
    for state in (S.CLOSED, S.REJECTED, S.EXPIRED):
        assert ALLOWED_TRANSITIONS[state] == frozenset()


def test_no_duplicate_entry_signal(clock: ManualClock) -> None:
    sm = CandidateStateMachine("m")
    sm.transition(S.MONITORING, clock.now())
    sm.transition(S.QUALIFIED, clock.now())
    assert sm.may_generate_entry
    sm.transition(S.BUY_SIGNAL, clock.now())
    assert not sm.may_generate_entry
    assert sm.has_pending_entry
    with pytest.raises(InvalidTransitionError):
        sm.transition(S.BUY_SIGNAL, clock.now())
    sm.transition(S.AWAITING_CONFIRMATION, clock.now())
    with pytest.raises(InvalidTransitionError):
        sm.transition(S.BUY_SIGNAL, clock.now())


def test_cancel_and_requalify(clock: ManualClock) -> None:
    sm = CandidateStateMachine("m")
    for t in (S.MONITORING, S.QUALIFIED, S.BUY_SIGNAL, S.SIGNAL_CANCELLED):
        sm.transition(t, clock.now())
    sm.transition(S.MONITORING, clock.now())
    sm.transition(S.QUALIFIED, clock.now())
    sm.transition(S.BUY_SIGNAL, clock.now())
    assert sm.entry_signal_count == 2


def test_stale_recovery(clock: ManualClock) -> None:
    sm = CandidateStateMachine("m")
    sm.transition(S.MONITORING, clock.now())
    sm.transition(S.DATA_STALE, clock.now())
    sm.transition(S.MONITORING, clock.now())
    assert sm.state is S.MONITORING


def test_open_position_cannot_go_stale_or_reject(clock: ManualClock) -> None:
    sm = CandidateStateMachine("m")
    for t in (S.MONITORING, S.QUALIFIED, S.BUY_SIGNAL, S.AWAITING_CONFIRMATION, S.OPEN):
        sm.transition(t, clock.now())
    for bad in (S.DATA_STALE, S.REJECTED, S.EXPIRED, S.CLOSED, S.MONITORING):
        assert not sm.can(bad)
    assert sm.in_position


def test_every_state_in_table() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(S)
