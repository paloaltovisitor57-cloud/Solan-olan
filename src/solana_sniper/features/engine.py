"""Short-horizon feature computation over a token's rolling buffers. Pure and timestamped."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from decimal import Decimal

from solana_sniper.domain.models import FeatureVector, MarketSnapshot
from solana_sniper.market_data.tracker import TokenTrack


def _f(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _return(track: TokenTrack, now: datetime, from_s: float, to_s: float = 0.0) -> float | None:
    """Price return between (now - from_s) and (now - to_s). None without enough history."""
    start = track.snapshot_at_or_before(now - timedelta(seconds=from_s))
    end = track.snapshot_at_or_before(now - timedelta(seconds=to_s))
    if start is None or end is None or start is end:
        return None
    if start.price_native is None or end.price_native is None or start.price_native <= 0:
        return None
    if (now - start.observed_at).total_seconds() > from_s * 3 + 5:
        return None  # the "start" observation is far older than the window we asked for
    return float((end.price_native - start.price_native) / start.price_native)


def _liquidity_return(
    track: TokenTrack, now: datetime, from_s: float, to_s: float = 0.0
) -> float | None:
    start = track.snapshot_at_or_before(now - timedelta(seconds=from_s))
    end = track.snapshot_at_or_before(now - timedelta(seconds=to_s))
    if start is None or end is None or start is end:
        return None
    if start.liquidity_usd is None or end.liquidity_usd is None or start.liquidity_usd <= 0:
        return None
    if (now - start.observed_at).total_seconds() > from_s * 3 + 5:
        return None
    return float((end.liquidity_usd - start.liquidity_usd) / start.liquidity_usd)


def _growth(history: list[tuple[datetime, int]], now: datetime, window_s: float) -> float | None:
    if len(history) < 2:
        return None
    then_value: int | None = None
    cutoff = now - timedelta(seconds=window_s)
    for ts, value in history:
        if ts <= cutoff:
            then_value = value
        else:
            break
    if then_value is None:
        then_value = history[0][1]
        if (now - history[0][0]).total_seconds() < window_s * 0.5:
            return None
    now_value = history[-1][1]
    return (now_value - then_value) / max(1, then_value)


def _volatility(snaps: list[MarketSnapshot]) -> float | None:
    prices = [
        float(s.price_native) for s in snaps if s.price_native is not None and s.price_native > 0
    ]
    if len(prices) < 4:
        return None
    rets = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
    return math.sqrt(var) * math.sqrt(len(rets))  # scaled to the window


class FeatureEngine:
    def __init__(self, stale_after_s: float, peak_window_s: float = 300.0) -> None:
        self._stale_after = stale_after_s
        self._peak_window = peak_window_s

    def compute(
        self,
        track: TokenTrack,
        now: datetime,
        *,
        entry_slippage_bps: int | None = None,
        exit_slippage_bps: int | None = None,
    ) -> FeatureVector:
        latest = track.latest
        data_age = track.data_age_s(now)
        stale = track.is_stale(now, self._stale_after)
        price = _f(latest.price_native) if latest else None
        liquidity = _f(latest.liquidity_usd) if latest else None

        m10 = _return(track, now, 10)
        m30 = _return(track, now, 30)
        m60 = _return(track, now, 60)
        m180 = _return(track, now, 180)
        prev10 = _return(track, now, 20, 10)
        acceleration = (m10 - prev10) if (m10 is not None and prev10 is not None) else None

        liq_growth = _liquidity_return(track, now, 60)
        liq_recent = _liquidity_return(track, now, 30)
        liq_prev = _liquidity_return(track, now, 60, 30)
        liq_accel = (
            (liq_recent - liq_prev) if (liq_recent is not None and liq_prev is not None) else None
        )

        # trade-derived velocity (streaming providers) with snapshot fallback (REST providers)
        w60 = track.trade_window(now, 60)
        w_prev = track.trade_window(now, 120)
        trade_velocity: float | None
        buy_velocity: float | None
        sell_velocity: float | None
        buys: int | None
        sells: int | None
        vol_accel: float | None
        if track.trades:
            trade_velocity = float(w60.count)
            buy_velocity = float(w60.buys)
            sell_velocity = float(w60.sells)
            buys, sells = w60.buys, w60.sells
            prev_vol = w_prev.volume_sol - w60.volume_sol
            if prev_vol > 0:
                vol_accel = float(w60.volume_sol / prev_vol) - 1.0
            elif w60.volume_sol > 0 and (now - track.trades[0].observed_at).total_seconds() > 60:
                vol_accel = 1.0
            else:
                vol_accel = None
        elif latest is not None and latest.total_txns_5m is not None:
            trade_velocity = latest.total_txns_5m / 5.0
            buy_velocity = (latest.buys_5m or 0) / 5.0
            sell_velocity = (latest.sells_5m or 0) / 5.0
            buys, sells = latest.buys_5m or 0, latest.sells_5m or 0
            vol_accel = self._snapshot_volume_accel(track, now)
        else:
            trade_velocity = buy_velocity = sell_velocity = None
            buys = sells = None
            vol_accel = None
        total = (buys or 0) + (sells or 0)
        imbalance = ((buys or 0) - (sells or 0)) / total if total else None
        buy_ratio = (buys or 0) / total if total else None

        trader_growth = _growth(list(track.trader_history), now, 60)
        if trader_growth is None:
            trader_growth = self._snapshot_field_growth(track, now, "unique_traders", 60)
        holder_growth = _growth(list(track.holder_history), now, 120)

        window_snaps = track.snapshots_since(now, self._peak_window)
        drawdown: float | None = None
        since_peak: float | None = None
        if window_snaps and price is not None:
            peak_snap = max(
                (s for s in window_snaps if s.price_native is not None),
                key=lambda s: s.price_native or Decimal(0),
                default=None,
            )
            if peak_snap is not None and peak_snap.price_native:
                peak = float(peak_snap.price_native)
                drawdown = (peak - price) / peak if peak > 0 else None
                since_peak = (now - peak_snap.observed_at).total_seconds()
        vol60 = _volatility(track.snapshots_since(now, 60))

        return FeatureVector(
            mint=track.mint,
            computed_at=now,
            observation_count=len(track.snapshots),
            token_age_s=track.token.age_seconds(now),
            price_native=price,
            liquidity_usd=liquidity,
            momentum_10s=m10,
            momentum_30s=m30,
            momentum_60s=m60,
            momentum_180s=m180,
            acceleration=acceleration,
            liquidity_growth_60s=liq_growth,
            liquidity_acceleration=liq_accel,
            volume_acceleration=vol_accel,
            trade_velocity_per_min=trade_velocity,
            buy_velocity_per_min=buy_velocity,
            sell_velocity_per_min=sell_velocity,
            buy_sell_imbalance=imbalance,
            buy_ratio=buy_ratio,
            unique_trader_growth=trader_growth,
            holder_growth=holder_growth,
            drawdown_from_peak=drawdown,
            seconds_since_peak=since_peak,
            market_depth_usd=liquidity,
            estimated_slippage_bps=entry_slippage_bps,
            estimated_exit_slippage_bps=exit_slippage_bps,
            volatility_60s=vol60,
            data_age_s=data_age if data_age != float("inf") else 1e9,
            stale=stale,
        )

    @staticmethod
    def _snapshot_volume_accel(track: TokenTrack, now: datetime) -> float | None:
        cur = track.latest
        then = track.snapshot_at_or_before(now - timedelta(seconds=60))
        if cur is None or then is None or cur is then:
            return None
        if cur.volume_5m_usd is None or then.volume_5m_usd is None or then.volume_5m_usd <= 0:
            return None
        return float((cur.volume_5m_usd - then.volume_5m_usd) / then.volume_5m_usd)

    @staticmethod
    def _snapshot_field_growth(
        track: TokenTrack, now: datetime, field: str, window_s: float
    ) -> float | None:
        cur = track.latest
        then = track.snapshot_at_or_before(now - timedelta(seconds=window_s))
        if cur is None or then is None or cur is then:
            return None
        cur_v = getattr(cur, field)
        then_v = getattr(then, field)
        if not isinstance(cur_v, int) or not isinstance(then_v, int):
            return None
        return (cur_v - then_v) / max(1, then_v)
