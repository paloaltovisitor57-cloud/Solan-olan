"""Exit engine: adaptive trailing peak, momentum, liquidity, volume, max-loss, timeout, abnormal."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from solana_sniper.config.settings import ExitConfig, TrailingConfig
from solana_sniper.domain.enums import ExitReason, Urgency
from solana_sniper.domain.models import FeatureVector, Position
from solana_sniper.domain.money import fclamp
from solana_sniper.market_data.tracker import TokenTrack


@dataclass(frozen=True, slots=True)
class ExitDecision:
    reason: ExitReason
    urgency: Urgency
    detail: str
    trailing_threshold_pct: float
    trailing_drawdown_pct: float


@dataclass(slots=True)
class MonitorState:
    """Per-position facts the exit engine needs that the Position itself does not carry."""

    peak_velocity_per_min: float = 0.0
    peak_liquidity_usd: Decimal | None = None
    liquidity_window: list[tuple[datetime, Decimal]] = field(default_factory=list)
    last_exit_signal_at: datetime | None = None
    exit_signals: int = 0
    stale_since: datetime | None = None


class AdaptiveTrailing:
    def __init__(self, config: TrailingConfig) -> None:
        self._cfg = config

    def base_threshold(self, peak_multiple: float) -> float:
        chosen = self._cfg.tiers[0].drawdown_pct
        for tier in self._cfg.tiers:
            if peak_multiple >= tier.min_multiple:
                chosen = tier.drawdown_pct
        return chosen

    def threshold(
        self,
        position: Position,
        features: FeatureVector | None,
        now: datetime,
    ) -> tuple[float, list[str]]:
        c = self._cfg
        notes: list[str] = []
        peak_multiple = (
            float(position.peak_value_eur / position.cost_basis_eur)
            if position.cost_basis_eur > 0
            else 1.0
        )
        threshold = self.base_threshold(peak_multiple)
        notes.append(f"base {threshold:.0%} @ {peak_multiple:.2f}x")
        if features is not None:
            vol = features.volatility_60s
            if vol is not None and c.reference_volatility > 0 and vol > c.reference_volatility:
                widen = fclamp(vol / c.reference_volatility, 1.0, c.max_volatility_widen)
                threshold *= widen
                notes.append(f"vol x{widen:.2f}")
            liq = features.liquidity_usd
            if liq is not None and Decimal(str(liq)) < c.low_liquidity_usd:
                threshold *= c.low_liquidity_tighten
                notes.append(f"low liq x{c.low_liquidity_tighten}")
            mom = features.momentum_30s
            if mom is not None and mom < c.negative_momentum_threshold:
                threshold *= c.negative_momentum_tighten
                notes.append(f"neg mom x{c.negative_momentum_tighten}")
        if position.holding_seconds(now) > c.duration_tighten_after_s:
            threshold *= c.duration_tighten
            notes.append(f"duration x{c.duration_tighten}")
        threshold = fclamp(threshold, c.min_drawdown_pct, c.max_drawdown_pct)
        return threshold, notes


class ExitEngine:
    def __init__(self, config: ExitConfig) -> None:
        self._cfg = config
        self.trailing = AdaptiveTrailing(config.trailing)

    def evaluate(
        self,
        position: Position,
        state: MonitorState,
        track: TokenTrack | None,
        features: FeatureVector | None,
        now: datetime,
    ) -> ExitDecision | None:
        c = self._cfg
        holding = position.holding_seconds(now)
        threshold, notes = self.trailing.threshold(position, features, now)
        drawdown = position.trailing_drawdown_pct
        pnl_pct = position.pnl_pct
        peak_multiple = (
            float(position.peak_value_eur / position.cost_basis_eur)
            if position.cost_basis_eur
            else 1.0
        )

        def decision(reason: ExitReason, urgency: Urgency, detail: str) -> ExitDecision:
            return ExitDecision(reason, urgency, detail, threshold, drawdown)

        # 1. data stale: we cannot see the position; that is itself an abnormal condition.
        if features is not None and features.stale:
            if state.stale_since is None:
                state.stale_since = now
            if (now - state.stale_since).total_seconds() >= c.stale_exit_after_s:
                return decision(
                    ExitReason.DATA_STALE,
                    Urgency.HIGH,
                    f"no market data for {features.data_age_s:.0f}s; exit blind or check manually",
                )
        else:
            state.stale_since = None

        # 2. abnormal events
        if features is not None:
            spike = features.momentum_10s
            if spike is not None and spike >= c.abnormal_price_spike_pct:
                return decision(
                    ExitReason.ABNORMAL_EVENT,
                    Urgency.URGENT,
                    f"price spiked {spike:+.0%} in 10s; take it",
                )
        latest = track.latest if track else None
        if (
            latest is not None
            and latest.spread_bps is not None
            and latest.spread_bps >= c.abnormal_spread_bps
        ):
            return decision(
                ExitReason.ABNORMAL_EVENT, Urgency.URGENT, f"spread {latest.spread_bps}bps"
            )

        # 3. liquidity collapse
        if latest is not None and latest.liquidity_usd is not None:
            self._push_liquidity(state, now, latest.liquidity_usd)
            window_peak = max((v for _, v in state.liquidity_window), default=latest.liquidity_usd)
            if window_peak > 0:
                drop = float((window_peak - latest.liquidity_usd) / window_peak)
                if drop >= c.liquidity_collapse_drop_pct:
                    return decision(
                        ExitReason.LIQUIDITY_COLLAPSE,
                        Urgency.URGENT,
                        f"liquidity -{drop:.0%} in {c.liquidity_collapse_window_s:.0f}s "
                        f"(${window_peak:,.0f} → ${latest.liquidity_usd:,.0f})",
                    )

        # 4. max loss
        if pnl_pct <= -c.max_loss_pct:
            return decision(
                ExitReason.MAX_LOSS, Urgency.HIGH, f"pnl {pnl_pct:+.0%} <= -{c.max_loss_pct:.0%}"
            )

        # 5. trailing peak
        if (
            holding >= c.min_holding_s_before_trailing
            and peak_multiple - 1.0 >= c.trailing.activate_after_gain_pct
            and drawdown >= threshold
        ):
            urgency = Urgency.HIGH if peak_multiple >= 2.0 else Urgency.NORMAL
            return decision(
                ExitReason.TRAILING_PEAK,
                urgency,
                f"{drawdown:.0%} below peak (threshold {threshold:.0%}: {', '.join(notes)})",
            )

        # 6. momentum deterioration
        if features is not None:
            mom = features.momentum_30s if c.momentum_window_s <= 30 else features.momentum_60s
            if (
                mom is not None
                and mom <= c.momentum_drop_threshold
                and peak_multiple - 1.0 >= c.momentum_requires_prior_gain_pct
            ):
                return decision(
                    ExitReason.MOMENTUM_DETERIORATION,
                    Urgency.NORMAL,
                    f"momentum {mom:+.0%}/{c.momentum_window_s:.0f}s "
                    f"after {peak_multiple:.2f}x peak",
                )
            # 7. volume collapse
            tv = features.trade_velocity_per_min
            if tv is not None:
                state.peak_velocity_per_min = max(state.peak_velocity_per_min, tv)
                if (
                    holding >= c.volume_collapse_min_holding_s
                    and state.peak_velocity_per_min > 0
                    and tv / state.peak_velocity_per_min <= c.volume_collapse_ratio
                ):
                    return decision(
                        ExitReason.VOLUME_COLLAPSE,
                        Urgency.NORMAL,
                        f"activity {tv:.0f}/min vs peak {state.peak_velocity_per_min:.0f}/min",
                    )

        # 8. timeout
        if holding >= c.max_holding_s:
            return decision(
                ExitReason.TIMEOUT, Urgency.NORMAL, f"held {holding:.0f}s >= {c.max_holding_s:.0f}s"
            )
        return None

    def _push_liquidity(self, state: MonitorState, now: datetime, liquidity: Decimal) -> None:
        state.liquidity_window.append((now, liquidity))
        cutoff = self._cfg.liquidity_collapse_window_s
        state.liquidity_window = [
            (t, v) for t, v in state.liquidity_window if (now - t).total_seconds() <= cutoff
        ]
        if state.peak_liquidity_usd is None or liquidity > state.peak_liquidity_usd:
            state.peak_liquidity_usd = liquidity
