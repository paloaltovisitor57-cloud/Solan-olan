from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from solana_sniper.domain.clock import ManualClock
from solana_sniper.domain.money import raw_to_ui, sol_to_lamports
from solana_sniper.market_data.synthetic import (
    WSOL,
    SyntheticQuoteProvider,
    SyntheticWorld,
)
from solana_sniper.quotes.base import QuoteError


def test_world_launches_and_trades(clock: ManualClock) -> None:
    world = SyntheticWorld(clock, seed=1, launch_interval_s=5)
    launched, _trades, _snaps = world.step(clock.now())
    assert len(launched) == 1
    mint = launched[0].mint
    clock.advance(1)
    _, _trades, snaps = world.step(clock.now())
    assert snaps[0].mint == mint and snaps[0].price_native is not None
    clock.advance(5)
    launched2, _, _ = world.step(clock.now())
    assert len(launched2) == 1 and launched2[0].mint != mint


async def test_quote_roundtrip_is_lossy_but_sane(clock: ManualClock) -> None:
    world = SyntheticWorld(clock, seed=2)
    sim = world.launch(clock.now(), archetype="runner")
    provider = SyntheticQuoteProvider(world, clock)
    spend = sol_to_lamports(Decimal("0.1"))
    buy = await provider.quote(WSOL, sim.token.mint, spend, 300)
    tokens = buy.out_amount_raw
    assert tokens > 0 and 0 <= buy.price_impact_pct < 5
    sell = await provider.quote(sim.token.mint, WSOL, tokens, 300)
    back = raw_to_ui(sell.out_amount_raw, 9)
    assert Decimal("0.09") < back < Decimal("0.1")  # fee + impact, never more than spent
    with pytest.raises(QuoteError):
        await provider.quote(WSOL, "unknown", spend, 300)
    with pytest.raises(QuoteError):
        await provider.quote("a", "b", spend, 300)
    with pytest.raises(QuoteError):
        await provider.quote(WSOL, sim.token.mint, 0, 300)


async def test_rug_kills_quotes(clock: ManualClock) -> None:
    world = SyntheticWorld(clock, seed=3)
    sim = world.launch(clock.now(), archetype="rug")
    sim.pump_until_s = 1.0
    clock.advance(2)
    world.step_token(sim, clock.now())
    assert sim.rugged
    provider = SyntheticQuoteProvider(world, clock)
    with pytest.raises(QuoteError):
        await provider.quote(WSOL, sim.token.mint, sol_to_lamports(Decimal("0.1")), 300)


async def test_transient_failures(clock: ManualClock) -> None:
    world = SyntheticWorld(clock, seed=4)
    sim = world.launch(clock.now(), archetype="runner")
    provider = SyntheticQuoteProvider(world, clock, fail_every=2)
    await provider.quote(WSOL, sim.token.mint, 10_000_000, 300)
    with pytest.raises(QuoteError) as exc:
        await provider.quote(WSOL, sim.token.mint, 10_000_000, 300)
    assert exc.value.retryable


def test_runner_pumps_then_dumps(clock: ManualClock) -> None:
    world = SyntheticWorld(clock, seed=5)
    sim = world.launch(clock.now(), archetype="runner")
    start = sim.price
    peak = start
    for _ in range(int(sim.pump_until_s)):
        clock.advance(1)
        world.step_token(sim, clock.now())
        peak = max(peak, sim.price)
    assert peak > start * Decimal("1.2")
    clock.set(clock.now() + timedelta(seconds=120))
    for _ in range(60):
        clock.advance(1)
        world.step_token(sim, clock.now())
    assert sim.price < peak
