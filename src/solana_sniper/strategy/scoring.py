"""Entry scoring: weighted, normalized components → 0..100 with human-readable reasons."""

from __future__ import annotations

import math
from datetime import datetime

from solana_sniper.config.settings import EntryConfig, FiltersConfig
from solana_sniper.domain.models import (
    CheckReport,
    EntryScore,
    FeatureVector,
    RoundTripQuote,
    ScoreComponent,
)
from solana_sniper.domain.money import fclamp


class EntryScorer:
    def __init__(self, entry: EntryConfig, filters: FiltersConfig) -> None:
        self._cfg = entry
        self._filters = filters

    def score(
        self,
        features: FeatureVector,
        checks: CheckReport | None,
        now: datetime,
        round_trip: RoundTripQuote | None = None,
    ) -> EntryScore:
        w = self._cfg.weights
        f = features
        comps: list[ScoreComponent] = []
        reasons: list[str] = []
        penalties: list[str] = []

        def add(
            name: str, raw: float | None, normalized: float, weight: float, note: str = ""
        ) -> None:
            normalized = fclamp(normalized, 0.0, 1.0)
            comps.append(ScoreComponent(name, raw, normalized, weight, normalized * weight, note))

        # freshness: 1.0 at launch, 0.5 at the half-life
        if f.token_age_s is None:
            add("freshness", None, 0.2, w.freshness, "age unknown")
        else:
            fresh = math.exp(-math.log(2) * f.token_age_s / self._cfg.freshness_half_life_s)
            add("freshness", f.token_age_s, fresh, w.freshness, f"{f.token_age_s:.0f}s old")
            if f.token_age_s < 180:
                reasons.append(f"very recent launch ({f.token_age_s:.0f}s)")

        lg = f.liquidity_growth_60s
        if lg is None:
            add("liquidity_growth", None, 0.25, w.liquidity_growth, "no history")
        else:
            add(
                "liquidity_growth",
                lg,
                lg / self._cfg.liquidity_growth_full_score,
                w.liquidity_growth,
                f"{lg:+.0%}/60s",
            )
            if lg > 0.05:
                reasons.append(f"liquidity growing {lg:+.0%}/60s")
            elif lg < -0.05:
                penalties.append(f"liquidity shrinking {lg:+.0%}/60s")

        tv = f.trade_velocity_per_min
        if tv is None:
            add("trade_velocity", None, 0.0, w.trade_velocity, "no trades")
        else:
            add(
                "trade_velocity",
                tv,
                tv / self._cfg.velocity_full_score_per_min,
                w.trade_velocity,
                f"{tv:.0f} tx/min",
            )
            if tv >= self._cfg.velocity_full_score_per_min * 0.5:
                reasons.append(f"high trade velocity ({tv:.0f}/min)")

        ug = f.unique_trader_growth
        hg = f.holder_growth
        part = ug if ug is not None else hg
        if part is None:
            add("participation_growth", None, 0.25, w.participation_growth, "no history")
        else:
            add(
                "participation_growth",
                part,
                part / 0.5,
                w.participation_growth,
                f"{part:+.0%} traders/60s",
            )
            if part > 3.0:
                reasons.append("unique participation surging")
            elif part > 0.15:
                reasons.append(f"unique participation growing {part:+.0%}")

        m30, m60 = f.momentum_30s, f.momentum_60s
        if m30 is None and m60 is None:
            add("momentum", None, 0.2, w.momentum, "no history")
        else:
            blend = 0.6 * (m30 if m30 is not None else (m60 or 0.0)) + 0.4 * (
                m60 if m60 is not None else (m30 or 0.0)
            )
            add(
                "momentum",
                blend,
                blend / self._cfg.momentum_full_score,
                w.momentum,
                f"{blend:+.1%}",
            )
            if blend > 0.03:
                reasons.append(f"positive momentum {blend:+.1%}")
            elif blend < -0.03:
                penalties.append(f"negative momentum {blend:+.1%}")

        acc = f.acceleration
        if acc is None:
            add("acceleration", None, 0.4, w.acceleration, "no history")
        else:
            add("acceleration", acc, 0.5 + acc / 0.2, w.acceleration, f"{acc:+.1%}")
            if acc > 0.01:
                reasons.append("price accelerating")

        br = f.buy_ratio
        if br is None:
            add("demand_balance", None, 0.3, w.demand_balance, "no flow data")
        else:
            dist = abs(br - self._cfg.ideal_buy_ratio)
            add("demand_balance", br, 1.0 - dist / 0.3, w.demand_balance, f"{br:.0%} buys")
            if dist < 0.1:
                reasons.append(f"healthy demand balance ({br:.0%} buys)")
            elif br > self._filters.max_buy_ratio:
                penalties.append("one-sided flow")

        top10 = None
        if checks is not None:
            for r in checks.results:
                if r.name == "holder_concentration" and r.value is not None:
                    try:
                        top10 = float(r.value)
                    except ValueError:
                        top10 = None
        if top10 is None:
            add("concentration", None, 0.3, w.concentration, "unknown")
        else:
            add(
                "concentration",
                top10,
                1.0 - top10 / self._filters.max_top10_holder_pct,
                w.concentration,
                f"top10 {top10:.0%}",
            )
            if top10 < self._filters.max_top10_holder_pct * 0.6:
                reasons.append(f"acceptable concentration (top10 {top10:.0%})")

        if round_trip is None or round_trip.round_trip_loss_pct is None:
            add("exit_viability", None, 0.0, w.exit_viability, "no round-trip quote")
        else:
            loss = round_trip.round_trip_loss_pct
            add(
                "exit_viability",
                loss,
                1.0 - loss / self._filters.max_round_trip_loss_pct,
                w.exit_viability,
                f"rt loss {loss:.1%}",
            )
            if loss < self._filters.max_round_trip_loss_pct * 0.5:
                reasons.append(f"viable exit (round trip -{loss:.1%})")

        slip = f.estimated_slippage_bps
        if round_trip is not None:
            slip = round_trip.entry_slippage_bps
        if slip is None:
            add("slippage", None, 0.3, w.slippage, "unknown")
        else:
            add(
                "slippage",
                float(slip),
                1.0 - slip / self._filters.max_estimated_slippage_bps,
                w.slippage,
                f"{slip}bps",
            )

        total_weight = w.total() or 1.0
        raw_score = sum(c.contribution for c in comps) / total_weight * 100.0
        multiplier = 1.0
        if f.drawdown_from_peak is not None and f.drawdown_from_peak > 0.25:
            multiplier *= 0.7
            penalties.append(f"{f.drawdown_from_peak:.0%} below local peak")
        if f.stale:
            multiplier = 0.0
            penalties.append("data stale")
        if checks is not None and checks.is_fatal:
            multiplier = 0.0
            penalties.append("fatal check: " + checks.summary(2))
        score = fclamp(raw_score * multiplier, 0.0, 100.0)
        return EntryScore(
            mint=f.mint,
            scored_at=now,
            score=round(score, 2),
            components=tuple(comps),
            reasons=tuple(reasons),
            penalties=tuple(penalties),
        )
