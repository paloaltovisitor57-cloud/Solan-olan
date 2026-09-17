from __future__ import annotations

from decimal import Decimal

from solana_sniper.domain.clock import ManualClock
from solana_sniper.domain.enums import Venue
from solana_sniper.domain.models import MarketSnapshot, TokenInfo, TradeEvent
from solana_sniper.market_data.tracker import TokenTracker


def snap(mint: str, clock: ManualClock, price: str, source: str = "test") -> MarketSnapshot:
    return MarketSnapshot(
        mint=mint, observed_at=clock.now(), source=source, price_native=Decimal(price)
    )


def trade(
    mint: str, clock: ManualClock, is_buy: bool, sol: str, trader: str, sig: str | None
) -> TradeEvent:
    return TradeEvent(
        mint=mint,
        observed_at=clock.now(),
        source="t",
        is_buy=is_buy,
        sol_amount=Decimal(sol),
        token_amount=Decimal(1),
        trader=trader,
        signature=sig,
    )


def test_track_merge_and_dedupe(clock: ManualClock) -> None:
    tracker = TokenTracker(max_tracked=2)
    t1 = TokenInfo(mint="m1", symbol=None, source="pumpportal", venue=Venue.PUMP_FUN)
    track = tracker.track(t1)
    tracker.track(TokenInfo(mint="m1", symbol="SYM", name="Name", source="gecko", pool_address="p"))
    assert track.token.symbol == "SYM" and track.token.source == "pumpportal"
    assert track.token.pool_address == "p"
    assert track.token.venue is Venue.PUMP_FUN
    assert tracker.add_snapshot(snap("m1", clock, "1")) is not None
    assert tracker.add_snapshot(snap("m1", clock, "1")) is None  # duplicate same ts/source/price
    assert track.dropped_duplicates == 1
    clock.advance(1)
    assert tracker.add_snapshot(snap("m1", clock, "2")) is not None
    clock.advance(-0)  # no-op
    older = MarketSnapshot(
        mint="m1", observed_at=track.snapshots[0].observed_at, source="x", price_native=Decimal(5)
    )
    assert tracker.add_snapshot(older) is None  # out of order
    assert track.dropped_out_of_order == 1
    assert track.latest is not None and track.latest.price_native == Decimal(2)
    assert tracker.add_snapshot(snap("unknown", clock, "1")) is None
    tracker.track(TokenInfo(mint="m2"))
    assert tracker.is_full
    tracker.untrack("m2")
    assert "m2" not in tracker


def test_stale_detection(clock: ManualClock) -> None:
    tracker = TokenTracker()
    track = tracker.track(TokenInfo(mint="m"))
    assert track.is_stale(clock.now(), 5)
    tracker.add_snapshot(snap("m", clock, "1"))
    assert not track.is_stale(clock.now(), 5)
    clock.advance(5.1)
    assert track.is_stale(clock.now(), 5)
    assert track.data_age_s(clock.now()) > 5


def test_trade_windows_and_signature_dedupe(clock: ManualClock) -> None:
    tracker = TokenTracker()
    track = tracker.track(TokenInfo(mint="m"))
    assert tracker.add_trade(trade("m", clock, True, "0.5", "a", "s1")) is not None
    assert tracker.add_trade(trade("m", clock, True, "0.5", "a", "s1")) is None  # dup signature
    clock.advance(10)
    tracker.add_trade(trade("m", clock, False, "0.2", "b", "s2"))
    tracker.add_trade(trade("m", clock, True, "0.3", "c", None))
    clock.advance(10)
    w = track.trade_window(clock.now(), 15)
    assert w.buys == 1 and w.sells == 1 and w.unique_traders == 2
    assert w.volume_sol == Decimal("0.5")
    w_all = track.trade_window(clock.now(), 60)
    assert w_all.count == 3 and w_all.buy_volume_sol == Decimal("0.8")
    assert len(track.traders) == 3
    assert track.trader_history[-1][1] == 3
    assert track.snapshot_at_or_before(clock.now()) is None
    tracker.add_snapshot(snap("m", clock, "3"))
    assert track.snapshot_at_or_before(clock.now()) is not None
    assert len(track.snapshots_since(clock.now(), 1)) == 1
