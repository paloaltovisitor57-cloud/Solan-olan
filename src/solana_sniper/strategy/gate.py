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
    hard_reasons: tuple[str, ...] = ()  # data/checks/velocity: never bridged by hysteresis
    soft_reasons: tuple[str, ...] = ()  # score/momentum thresholds: bridged while latched


class EntryGate:
    def __init__(self, config: EntryConfig, checker: TokenChecker) -> None:
        self._cfg = config
        self._checker = checker

    def decide(
        self, features: FeatureVector, checks: CheckReport, score: EntryScore
    ) -> GateDecision:
        hard: list[str] = []
        soft: list[str] = []
        if checks.is_fatal:
            fatal = tuple(f"{r.name}: {r.reason}" for r in checks.fatal_rejections)
            return GateDecision(False, True, fatal, fatal, ())
        if features.stale:
            hard.append("data stale")
        if features.observation_count < self._cfg.min_observations:
            hard.append(f"only {features.observation_count} observations")
        ok, why = self._checker.qualifies(checks)
        if not ok:
            hard.append(why)
        if score.score < self._cfg.min_score:
            soft.append(f"score {score.score:.1f} < {self._cfg.min_score:.0f}")
        if features.momentum_30s is not None and features.momentum_30s < self._cfg.min_momentum_30s:
            soft.append(f"momentum30 {features.momentum_30s:+.1%}")
        if features.momentum_60s is not None and features.momentum_60s < self._cfg.min_momentum_60s:
            soft.append(f"momentum60 {features.momentum_60s:+.1%}")
        tv = features.trade_velocity_per_min
        if tv is None or tv < self._cfg.min_trade_velocity_per_min:
            shown = f"{tv:.0f}" if tv is not None else "n/a"
            hard.append(f"velocity {shown} < {self._cfg.min_trade_velocity_per_min}")
        reasons = tuple(hard + soft)
        return GateDecision(not reasons, False, reasons, tuple(hard), tuple(soft))

    @property
    def exit_score(self) -> float:
        """Score below which a latched candidate is abandoned (min_score - hysteresis)."""
        return max(0.0, self._cfg.min_score - self._cfg.qualification_hysteresis)

    def latched(
        self, features: FeatureVector, checks: CheckReport, score: EntryScore
    ) -> tuple[str, str]:
        """Verdict for a candidate that already qualified: ("continue", ""),
        ("abandon", reason) or ("fatal", reason). Hysteresis bridges the score threshold and the
        momentum thresholds only; hard blocks, a score collapse below exit_score and a severe
        momentum reversal end the attempt."""
        decision = self.decide(features, checks, score)
        if decision.fatal:
            return "fatal", "; ".join(decision.reasons)
        if decision.hard_reasons:
            return "abandon", "; ".join(decision.hard_reasons)
        if score.score < self.exit_score:
            return (
                "abandon",
                f"score {score.score:.1f} collapsed below {self.exit_score:.0f} "
                f"(min {self._cfg.min_score:.0f} - hysteresis "
                f"{self._cfg.qualification_hysteresis:.0f})",
            )
        m60 = features.momentum_60s
        if m60 is not None and m60 < self._cfg.severe_momentum_60s:
            return "abandon", f"momentum collapse: 60s {m60:+.1%}"
        return "continue", ""
