"""Qualification gate: turns features + checks + score into a QUALIFIED / not-yet decision."""

from __future__ import annotations

from dataclasses import dataclass

from solana_sniper.config.settings import EntryConfig
from solana_sniper.domain.models import CheckReport, EntryScore, FeatureVector
from solana_sniper.filters.checks import TokenChecker


@dataclass(frozen=True, slots=True)
class GateDecision:
    qualified: bool
    fatal: bool
    reasons: tuple[str, ...]


class EntryGate:
    def __init__(self, config: EntryConfig, checker: TokenChecker) -> None:
        self._cfg = config
        self._checker = checker

    def decide(
        self, features: FeatureVector, checks: CheckReport, score: EntryScore
    ) -> GateDecision:
        reasons: list[str] = []
        if checks.is_fatal:
            return GateDecision(
                False, True, tuple(f"{r.name}: {r.reason}" for r in checks.fatal_rejections)
            )
        if features.stale:
            reasons.append("data stale")
        if features.observation_count < self._cfg.min_observations:
            reasons.append(f"only {features.observation_count} observations")
        ok, why = self._checker.qualifies(checks)
        if not ok:
            reasons.append(why)
        if score.score < self._cfg.min_score:
            reasons.append(f"score {score.score:.1f} < {self._cfg.min_score:.0f}")
        if features.momentum_30s is not None and features.momentum_30s < self._cfg.min_momentum_30s:
            reasons.append(f"momentum30 {features.momentum_30s:+.1%}")
        if features.momentum_60s is not None and features.momentum_60s < self._cfg.min_momentum_60s:
            reasons.append(f"momentum60 {features.momentum_60s:+.1%}")
        tv = features.trade_velocity_per_min
        if tv is None or tv < self._cfg.min_trade_velocity_per_min:
            shown = f"{tv:.0f}" if tv is not None else "n/a"
            reasons.append(f"velocity {shown} < {self._cfg.min_trade_velocity_per_min}")
        return GateDecision(not reasons, False, tuple(reasons))
