"""Shared builders for tests: synthetic tracks with controllable price/liquidity paths."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal

from solana_sniper.domain.clock import ManualClock
from solana_sniper.domain.enums import Venue
from solana_sniper.domain.models import (
    MarketSnapshot,
    RoundTripQuote,
    SwapQuote,
    TokenAuthorities,
    TokenInfo,
    TradeEvent,
    new_id,
)
from solana_sniper.market_data.tracker import TokenTrack, TokenTracker


def make_token(
    mint: str = "MintTest", age_s: float = 60.0, clock: ManualClock | None = None
) -> TokenInfo:
    created = (clock.now() - timedelta(seconds=age_s)) if clock else None
    return TokenInfo(
        mint=mint,
        symbol="TST",
        name="Test",
        decimals=6,
        created_at=created,
        pool_created_at=created,
        venue=Venue.RAYDIUM,
        pool_address="pool",
        quote_mint="So11111111111111111111111111111111111111112",
        source="test",
    )


def clean_authorities() -> TokenAuthorities:
    return TokenAuthorities(
        mint_authority=None, freeze_authority=None, decimals=6, supply_raw=10**15
    )


def feed_path(
    tracker: TokenTracker,
    clock: ManualClock,
    mint: str,
    prices: Sequence[str],
    *,
    step_s: float = 5.0,
    liquidity: Sequence[str] | None = None,
    buys_5m: int = 20,
    sells_5m: int = 8,
    volume_5m: str = "3000",
    unique: Sequence[int] | None = None,
    trades_per_step: int = 0,
) -> TokenTrack:
    """Advance the clock and feed one snapshot per price; returns the track."""
    track = tracker.get(mint)
    assert track is not None
    for i, p in enumerate(prices):
        if i > 0:
            clock.advance(step_s)
        liq = Decimal(liquidity[i]) if liquidity else Decimal("20000")
        price = Decimal(p)
        snap = MarketSnapshot(
            mint=mint,
            observed_at=clock.now(),
            source="test",
            price_native=price,
            price_usd=price * Decimal(160),
            liquidity_usd=liq,
            volume_5m_usd=Decimal(volume_5m),
            buys_5m=buys_5m,
            sells_5m=sells_5m,
            unique_traders=unique[i] if unique else None,
            top10_holder_pct=0.2,
            largest_holder_pct=0.05,
            spread_bps=200,
        )
        tracker.add_snapshot(snap)
        for j in range(trades_per_step):
            tracker.add_trade(
                TradeEvent(
                    mint=mint,
                    observed_at=clock.now(),
                    source="test",
                    is_buy=(j % 4 != 3),
                    sol_amount=Decimal("0.2"),
                    token_amount=Decimal(1000),
                    trader=f"t{i}_{j}",
                    signature=f"sig{i}_{j}",
                    price_native=price,
                )
            )
    return track


def make_round_trip(
    mint: str,
    clock: ManualClock,
    *,
    loss_pct: float = 0.05,
    entry_bps: int = 150,
    exit_bps: int = 200,
    sell_ok: bool = True,
    viable: bool = True,
) -> RoundTripQuote:
    spend = Decimal("0.1")
    buy = SwapQuote(
        quote_id=new_id("q"),
        provider="test",
        input_mint="So11111111111111111111111111111111111111112",
        output_mint=mint,
        in_amount_raw=100_000_000,
        out_amount_raw=1_000_000_000,
        other_amount_threshold_raw=970_000_000,
        slippage_bps=300,
        price_impact_pct=entry_bps / 100,
        route_labels=("test",),
        fee_lamports=5000,
        quoted_at=clock.now(),
        latency_ms=10,
    )
    sell = None
    exit_sol = None
    if sell_ok:
        exit_sol = spend * (1 - Decimal(str(loss_pct)))
        sell = SwapQuote(
            quote_id=new_id("q"),
            provider="test",
            input_mint=mint,
            output_mint="So11111111111111111111111111111111111111112",
            in_amount_raw=1_000_000_000,
            out_amount_raw=int(exit_sol * 10**9),
            other_amount_threshold_raw=0,
            slippage_bps=300,
            price_impact_pct=exit_bps / 100,
            route_labels=("test",),
            fee_lamports=5000,
            quoted_at=clock.now(),
            latency_ms=10,
        )
    return RoundTripQuote(
        mint=mint,
        quoted_at=clock.now(),
        spend_sol=spend,
        buy=buy,
        sell=sell,
        expected_tokens_ui=Decimal("1000"),
        entry_slippage_bps=entry_bps,
        entry_price_impact_pct=entry_bps / 100,
        immediate_exit_sol=exit_sol,
        exit_slippage_bps=exit_bps if sell_ok else None,
        exit_price_impact_pct=exit_bps / 100 if sell_ok else None,
        round_trip_loss_pct=loss_pct if sell_ok else None,
        total_fee_lamports=10000,
        viable=viable and sell_ok,
        reasons=() if (viable and sell_ok) else ("no route",),
    )
