"""Bankroll milestone tracking. Emits an event each time equity crosses a configured level."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from solana_sniper.domain.models import MilestoneEvent


class MilestoneTracker:
    def __init__(self, milestones: list[Decimal], session_id: str = "") -> None:
        self.milestones = sorted(set(milestones))
        self.reached: set[Decimal] = set()
        self.session_id = session_id

    def restore(self, reached: list[Decimal]) -> None:
        self.reached = {m for m in reached if m in self.milestones}

    @property
    def highest_reached(self) -> Decimal | None:
        return max(self.reached) if self.reached else None

    def next_milestone(self, equity: Decimal) -> Decimal | None:
        for m in self.milestones:
            if m > equity:
                return m
        return None

    def update(self, equity: Decimal, at: datetime) -> list[MilestoneEvent]:
        events: list[MilestoneEvent] = []
        for m in self.milestones:
            if equity >= m and m not in self.reached:
                self.reached.add(m)
                events.append(
                    MilestoneEvent(
                        milestone_eur=m,
                        equity_eur=equity,
                        reached_at=at,
                        direction="UP",
                        session_id=self.session_id,
                    )
                )
            elif equity < m and m in self.reached:
                self.reached.discard(m)
                events.append(
                    MilestoneEvent(
                        milestone_eur=m,
                        equity_eur=equity,
                        reached_at=at,
                        direction="DOWN",
                        session_id=self.session_id,
                    )
                )
        return events
