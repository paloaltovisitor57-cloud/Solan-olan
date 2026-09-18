"""Forward outcome tracking: what actually happened to every candidate the engine saw.

This is the honest feedback loop. For each token the tracker records the price when it was
first observed and when it qualified, then keeps following the market for `horizon_s` and
finalises: peak multiple, time to peak, worst drawdown after the peak, whether liquidity was
pulled, and what the engine did (best score, qualified, signalled, entered, closed PnL).
`solana-sniper evaluate` turns the accumulated rows into hit rates per score bucket.

It measures; it does not predict. A large multiple in this table is a description of the past
with survivorship removed (rejected tokens are followed too), not a forecast. Nothing here
changes what the engine signals.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from solana_sniper.config.settings import OutcomesConfig
from solana_sniper.domain.models import MarketSnapshot


@dataclass(slots=True)
class OutcomeState:
    mint: str
    symbol: str | None
    source: str
    first_seen_at: datetime
    first_price: Decimal
    first_liquidity_usd: Decimal | None
    horizon_end: datetime
    qualified_at: datetime | None = None
    qualified_price: Decimal | None = None
    best_score: float | None = None
    signalled: bool = False
    entered: bool = False
    closed_pnl_pct: float | None = None
    exit_reason: str | None = None
    max_price: Decimal = Decimal(0)
    max_price_at: datetime | None = None
    min_price_after_peak: Decimal | None = None
    last_price: Decimal = Decimal(0)
    max_liquidity_usd: Decimal | None = None
    min_liquidity_after_max: Decimal | None = None
    observations: int = 0
    last_observed_at: datetime | None = None
    reject_reason: str | None = None
    simulated: bool = False
    post_qualified_max: Decimal = field(default=Decimal(0))


@dataclass(frozen=True, slots=True)
class Outcome:
    """Finalised forward record of one token. Every multiple is relative to the FIRST observed
    price (or the qualification price for `qualified_multiple`), i.e. what a passive observer
    could have seen, not what a trade would have realised after slippage and fees."""

    mint: str
    symbol: str | None
    source: str
    first_seen_at: datetime
    finalized_at: datetime
    horizon_s: float  # configured horizon; the observed window is finalized_at - first_seen_at
    observations: int
    first_price: Decimal
    max_multiple: float  # peak / first price within the horizon
    time_to_peak_s: float | None
    max_drawdown_from_peak: float  # (peak - lowest after peak) / peak
    final_multiple: float  # last observed / first
    qualified: bool
    qualified_multiple: float | None  # peak after qualification / qualification price
    best_score: float | None
    signalled: bool
    entered: bool
    closed_pnl_pct: float | None
    exit_reason: str | None
    liquidity_collapsed: bool
    reject_reason: str | None
    simulated: bool
    truncated: bool = False  # finalised early (shutdown) before the horizon elapsed

    def reached(self, multiple: float) -> bool:
        return self.max_multiple >= multiple

    @property
    def observed_window_s(self) -> float:
        return (self.finalized_at - self.first_seen_at).total_seconds()


class OutcomeTracker:
    """Follows tokens after the engine has made its decision and records what happened."""

    def __init__(self, config: OutcomesConfig, *, simulated: bool) -> None:
        self._cfg = config
        self._simulated = simulated
        self._states: dict[str, OutcomeState] = {}
        # One window per mint per process: a token is measured from its first sight, never
        # re-started after its horizon while it is still a candidate. Bounded LRU of mints.
        self._done: OrderedDict[str, None] = OrderedDict()
        self._done_limit = max(1000, config.max_followed * 50)
        self.finalized_count = 0
        self.skipped_full = 0

    @property
    def enabled(self) -> bool:
        return self._cfg.enabled

    def following(self) -> list[str]:
        return list(self._states)

    def is_following(self, mint: str) -> bool:
        return mint in self._states

    def __len__(self) -> int:
        return len(self._states)

    # ------------------------------------------------------------- lifecycle
    def start(
        self, mint: str, symbol: str | None, source: str, snap: MarketSnapshot, now: datetime
    ) -> bool:
        """Begin following `mint` from `snap`. Returns False when disabled, already followed or
        already measured, priceless, or at capacity (the cap bounds memory and data load)."""
        if not self._cfg.enabled or mint in self._states or mint in self._done:
            return False
        if snap.price_native is None or snap.price_native <= 0:
            return False
        if len(self._states) >= self._cfg.max_followed:
            self.skipped_full += 1
            return False
        price = snap.price_native
        self._states[mint] = OutcomeState(
            mint=mint,
            symbol=symbol,
            source=source,
            first_seen_at=now,
            first_price=price,
            first_liquidity_usd=snap.liquidity_usd,
            horizon_end=now + timedelta(seconds=self._cfg.horizon_s),
            max_price=price,
            max_price_at=now,
            min_price_after_peak=price,
            last_price=price,
            max_liquidity_usd=snap.liquidity_usd,
            min_liquidity_after_max=snap.liquidity_usd,
            observations=1,
            last_observed_at=now,
            simulated=self._simulated,
        )
        return True

    def observe(self, snap: MarketSnapshot) -> None:
        st = self._states.get(snap.mint)
        if st is None or snap.price_native is None or snap.price_native <= 0:
            return
        if snap.observed_at > st.horizon_end:
            return
        st.observations += 1
        st.last_observed_at = snap.observed_at
        price = snap.price_native
        st.last_price = price
        if price > st.max_price:
            st.max_price = price
            st.max_price_at = snap.observed_at
            st.min_price_after_peak = price
        elif st.min_price_after_peak is None or price < st.min_price_after_peak:
            st.min_price_after_peak = price
        if st.qualified_price is not None and price > st.post_qualified_max:
            st.post_qualified_max = price
        liq = snap.liquidity_usd
        if liq is not None:
            if st.max_liquidity_usd is None or liq > st.max_liquidity_usd:
                st.max_liquidity_usd = liq
                st.min_liquidity_after_max = liq
            elif st.min_liquidity_after_max is None or liq < st.min_liquidity_after_max:
                st.min_liquidity_after_max = liq

    def note_score(self, mint: str, score: float) -> None:
        st = self._states.get(mint)
        if st is not None and (st.best_score is None or score > st.best_score):
            st.best_score = score

    def note_qualified(self, mint: str, price: Decimal | None, now: datetime) -> None:
        st = self._states.get(mint)
        if st is None or st.qualified_at is not None or price is None or price <= 0:
            return
        st.qualified_at = now
        st.qualified_price = price
        st.post_qualified_max = price

    def note_signal(self, mint: str) -> None:
        st = self._states.get(mint)
        if st is not None:
            st.signalled = True

    def note_entered(self, mint: str) -> None:
        st = self._states.get(mint)
        if st is not None:
            st.entered = True

    def note_closed(self, mint: str, pnl_pct: float, reason: str | None) -> None:
        st = self._states.get(mint)
        if st is not None:
            st.closed_pnl_pct = pnl_pct
            st.exit_reason = reason

    def note_rejected(self, mint: str, reason: str) -> None:
        st = self._states.get(mint)
        if st is not None and st.reject_reason is None:
            st.reject_reason = reason[:120]

    # -------------------------------------------------------------- finalise
    def _finalize(self, st: OutcomeState, now: datetime, *, truncated: bool) -> Outcome:
        first = st.first_price
        max_multiple = float(st.max_price / first) if first > 0 else 1.0
        after_peak = (
            st.min_price_after_peak if st.min_price_after_peak is not None else st.max_price
        )
        drawdown = float((st.max_price - after_peak) / st.max_price) if st.max_price > 0 else 0.0
        final_multiple = float(st.last_price / first) if first > 0 else 1.0
        qualified_multiple = (
            float(st.post_qualified_max / st.qualified_price)
            if st.qualified_price is not None and st.qualified_price > 0
            else None
        )
        collapsed = False
        if st.max_liquidity_usd and st.min_liquidity_after_max is not None:
            drop = float((st.max_liquidity_usd - st.min_liquidity_after_max) / st.max_liquidity_usd)
            collapsed = drop >= self._cfg.rug_liquidity_drop_pct
        time_to_peak = (
            (st.max_price_at - st.first_seen_at).total_seconds() if st.max_price_at else None
        )
        return Outcome(
            mint=st.mint,
            symbol=st.symbol,
            source=st.source,
            first_seen_at=st.first_seen_at,
            finalized_at=now,
            horizon_s=self._cfg.horizon_s,
            observations=st.observations,
            first_price=first,
            max_multiple=max_multiple,
            time_to_peak_s=time_to_peak,
            max_drawdown_from_peak=drawdown,
            final_multiple=final_multiple,
            qualified=st.qualified_at is not None,
            qualified_multiple=qualified_multiple,
            best_score=st.best_score,
            signalled=st.signalled,
            entered=st.entered,
            closed_pnl_pct=st.closed_pnl_pct,
            exit_reason=st.exit_reason,
            liquidity_collapsed=collapsed,
            reject_reason=st.reject_reason,
            simulated=st.simulated,
            truncated=truncated,
        )

    def finalize_due(self, now: datetime) -> list[Outcome]:
        """Finalise every token whose horizon has passed, or whose data went silent for longer
        than `silence_timeout_s` (the pool died or the provider dropped it)."""
        done: list[Outcome] = []
        for mint, st in list(self._states.items()):
            silent = (
                st.last_observed_at is not None
                and (now - st.last_observed_at).total_seconds() > self._cfg.silence_timeout_s
            )
            if now >= st.horizon_end or silent:
                done.append(self._finalize(st, now, truncated=False))
                del self._states[mint]
                self._mark_done(mint)
                self.finalized_count += 1
        return done

    def _mark_done(self, mint: str) -> None:
        self._done[mint] = None
        self._done.move_to_end(mint)
        while len(self._done) > self._done_limit:
            self._done.popitem(last=False)

    def finalize_all(self, now: datetime) -> list[Outcome]:
        """Finalise everything still in flight (shutdown). Rows whose horizon had not elapsed are
        marked `truncated` so `evaluate` can exclude them."""
        done = [
            self._finalize(st, now, truncated=now < st.horizon_end) for st in self._states.values()
        ]
        for mint in self._states:
            self._mark_done(mint)
        self.finalized_count += len(done)
        self._states.clear()
        return done
