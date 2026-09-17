"""In-memory per-token tracking: rolling snapshot/trade buffers, dedupe, ordering, staleness."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from solana_sniper.domain.models import (
    HolderDistribution,
    MarketSnapshot,
    TokenAuthorities,
    TokenInfo,
    TradeEvent,
)


@dataclass(frozen=True, slots=True)
class TradeWindow:
    seconds: float
    buys: int
    sells: int
    buy_volume_sol: Decimal
    sell_volume_sol: Decimal
    unique_traders: int

    @property
    def count(self) -> int:
        return self.buys + self.sells

    @property
    def volume_sol(self) -> Decimal:
        return self.buy_volume_sol + self.sell_volume_sol


@dataclass(slots=True)
class TokenTrack:
    token: TokenInfo
    snapshots: deque[MarketSnapshot] = field(default_factory=lambda: deque(maxlen=1200))
    trades: deque[TradeEvent] = field(default_factory=lambda: deque(maxlen=3000))
    traders: set[str] = field(default_factory=set)
    seen_signatures: deque[str] = field(default_factory=lambda: deque(maxlen=5000))
    first_data_at: datetime | None = None
    last_data_at: datetime | None = None
    authorities: TokenAuthorities | None = None
    holders: HolderDistribution | None = None
    holder_history: deque[tuple[datetime, int]] = field(default_factory=lambda: deque(maxlen=200))
    trader_history: deque[tuple[datetime, int]] = field(default_factory=lambda: deque(maxlen=600))
    dropped_out_of_order: int = 0
    dropped_duplicates: int = 0
    _sig_set: set[str] = field(default_factory=set)

    @property
    def mint(self) -> str:
        return self.token.mint

    @property
    def latest(self) -> MarketSnapshot | None:
        return self.snapshots[-1] if self.snapshots else None

    def merge_token(self, other: TokenInfo) -> None:
        """Fill unknown fields from a second discovery source without overwriting known ones."""
        t = self.token
        self.token = TokenInfo(
            mint=t.mint,
            symbol=t.symbol or other.symbol,
            name=t.name or other.name,
            decimals=t.decimals if t.decimals is not None else other.decimals,
            created_at=t.created_at or other.created_at,
            pool_created_at=t.pool_created_at or other.pool_created_at,
            first_liquidity_at=t.first_liquidity_at or other.first_liquidity_at,
            venue=t.venue if t.venue.value != "unknown" else other.venue,
            pool_address=t.pool_address or other.pool_address,
            quote_mint=t.quote_mint or other.quote_mint,
            source=t.source,
            discovered_at=t.discovered_at or other.discovered_at,
            metadata_uri=t.metadata_uri or other.metadata_uri,
        )

    def add_snapshot(self, snap: MarketSnapshot) -> bool:
        last = self.latest
        if last is not None and snap.observed_at < last.observed_at:
            self.dropped_out_of_order += 1
            return False
        if (
            last is not None
            and last.source == snap.source
            and last.observed_at == snap.observed_at
            and last.price_native == snap.price_native
        ):
            self.dropped_duplicates += 1
            return False
        self.snapshots.append(snap)
        if self.first_data_at is None:
            self.first_data_at = snap.observed_at
        self.last_data_at = snap.observed_at
        if snap.holder_count is not None:
            self.holder_history.append((snap.observed_at, snap.holder_count))
        return True

    def add_trade(self, trade: TradeEvent) -> bool:
        if trade.signature is not None:
            if trade.signature in self._sig_set:
                self.dropped_duplicates += 1
                return False
            if len(self.seen_signatures) == self.seen_signatures.maxlen:
                oldest = self.seen_signatures[0]
                self._sig_set.discard(oldest)
            self.seen_signatures.append(trade.signature)
            self._sig_set.add(trade.signature)
        self.trades.append(trade)
        if trade.trader:
            self.traders.add(trade.trader)
        self.trader_history.append((trade.observed_at, len(self.traders)))
        if self.first_data_at is None:
            self.first_data_at = trade.observed_at
        if self.last_data_at is None or trade.observed_at > self.last_data_at:
            self.last_data_at = trade.observed_at
        return True

    def data_age_s(self, now: datetime) -> float:
        if self.last_data_at is None:
            return float("inf")
        return max(0.0, (now - self.last_data_at).total_seconds())

    def is_stale(self, now: datetime, stale_after_s: float) -> bool:
        return self.data_age_s(now) > stale_after_s

    def trade_window(self, now: datetime, seconds: float) -> TradeWindow:
        cutoff_s = seconds
        buys = sells = 0
        bvol = Decimal(0)
        svol = Decimal(0)
        traders: set[str] = set()
        for tr in reversed(self.trades):
            if (now - tr.observed_at).total_seconds() > cutoff_s:
                break
            if tr.is_buy:
                buys += 1
                bvol += tr.sol_amount
            else:
                sells += 1
                svol += tr.sol_amount
            if tr.trader:
                traders.add(tr.trader)
        return TradeWindow(
            seconds=seconds,
            buys=buys,
            sells=sells,
            buy_volume_sol=bvol,
            sell_volume_sol=svol,
            unique_traders=len(traders),
        )

    def snapshots_since(self, now: datetime, seconds: float) -> list[MarketSnapshot]:
        out: list[MarketSnapshot] = []
        for snap in reversed(self.snapshots):
            if (now - snap.observed_at).total_seconds() > seconds:
                break
            out.append(snap)
        out.reverse()
        return out

    def snapshot_at_or_before(self, when: datetime) -> MarketSnapshot | None:
        for snap in reversed(self.snapshots):
            if snap.observed_at <= when:
                return snap
        return None


class TokenTracker:
    def __init__(self, max_tracked: int = 150) -> None:
        self._tracks: dict[str, TokenTrack] = {}
        self._max = max_tracked

    def __contains__(self, mint: str) -> bool:
        return mint in self._tracks

    def __len__(self) -> int:
        return len(self._tracks)

    def get(self, mint: str) -> TokenTrack | None:
        return self._tracks.get(mint)

    def track(self, token: TokenInfo) -> TokenTrack:
        existing = self._tracks.get(token.mint)
        if existing is not None:
            existing.merge_token(token)
            return existing
        track = TokenTrack(token=token)
        self._tracks[token.mint] = track
        return track

    def untrack(self, mint: str) -> None:
        self._tracks.pop(mint, None)

    @property
    def is_full(self) -> bool:
        return len(self._tracks) >= self._max

    def mints(self) -> list[str]:
        return list(self._tracks)

    def tracks(self) -> list[TokenTrack]:
        return list(self._tracks.values())

    def add_snapshot(self, snap: MarketSnapshot) -> TokenTrack | None:
        track = self._tracks.get(snap.mint)
        if track is None:
            return None
        return track if track.add_snapshot(snap) else None

    def add_trade(self, trade: TradeEvent) -> TokenTrack | None:
        track = self._tracks.get(trade.mint)
        if track is None:
            return None
        return track if track.add_trade(trade) else None
