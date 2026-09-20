"""Synthetic Solana launch simulator.

Drives the *same* engine through discovery → data → features → checks → signals → fills without
network access. Tokens are constant-product pools with archetypes (runner / dud / rug) so every
exit path gets exercised. Used by `--dry-run` with configs/synthetic.yaml and by tests.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from solana_sniper.discovery.base import EmitToken
from solana_sniper.domain.clock import Clock
from solana_sniper.domain.enums import Venue
from solana_sniper.domain.models import (
    HolderDistribution,
    MarketSnapshot,
    SwapQuote,
    TokenAuthorities,
    TokenInfo,
    TradeEvent,
    new_id,
)
from solana_sniper.domain.money import D, raw_to_ui, ui_to_raw
from solana_sniper.market_data.base import EmitSnapshot, EmitTrade
from solana_sniper.quotes.base import QuoteError, SwapBuild

WSOL = "So11111111111111111111111111111111111111112"
TOKEN_DECIMALS = 6
SOL_DECIMALS = 9
POOL_FEE = Decimal("0.01")


@dataclass(slots=True)
class SimToken:
    token: TokenInfo
    archetype: str
    sol_reserve: Decimal
    token_reserve: Decimal
    real_sol: Decimal
    created_at: datetime
    pump_until_s: float
    traders: set[str] = field(default_factory=set)
    rugged: bool = False
    dead: bool = False
    trade_seq: int = 0
    total_buys: int = 0
    total_sells: int = 0
    top_holder_pct: float = 0.08

    @property
    def price(self) -> Decimal:
        return self.sol_reserve / self.token_reserve

    def age(self, now: datetime) -> float:
        return (now - self.created_at).total_seconds()


class SyntheticWorld:
    """Deterministic pseudo-market. `step(now)` advances every live token by one tick."""

    def __init__(
        self,
        clock: Clock,
        *,
        seed: int = 7,
        launch_interval_s: float = 8.0,
        sol_usd: Decimal = Decimal("163"),
        archetype_weights: tuple[float, float, float] = (0.45, 0.35, 0.20),
    ) -> None:
        self._clock = clock
        self._rng = random.Random(seed)
        self.launch_interval_s = launch_interval_s
        self.sol_usd = sol_usd
        self._weights = archetype_weights
        self.tokens: dict[str, SimToken] = {}
        self._next_launch: datetime | None = None
        self._launched = 0
        self._trader_pool = [f"trader{i:04d}" for i in range(400)]

    # --------------------------------------------------------------- launches
    def maybe_launch(self, now: datetime) -> SimToken | None:
        if self._next_launch is None:
            self._next_launch = now
        if now < self._next_launch:
            return None
        self._next_launch = now + timedelta(seconds=self.launch_interval_s)
        return self.launch(now)

    def launch(self, now: datetime, archetype: str | None = None) -> SimToken:
        self._launched += 1
        mint = f"SYN{self._launched:04d}" + "x" * 36
        arche = archetype or self._rng.choices(["runner", "dud", "rug"], self._weights)[0]
        initial_sol = D(self._rng.uniform(2.0, 6.0))
        token = TokenInfo(
            mint=mint,
            symbol=f"SYN{self._launched}",
            name=f"Synthetic {arche} #{self._launched}",
            decimals=TOKEN_DECIMALS,
            created_at=now,
            pool_created_at=now,
            first_liquidity_at=now,
            venue=Venue.SYNTHETIC,
            pool_address=f"pool{self._launched:04d}",
            quote_mint=WSOL,
            source="synthetic",
            discovered_at=now,
        )
        sim = SimToken(
            token=token,
            archetype=arche,
            sol_reserve=Decimal(30) + initial_sol,
            token_reserve=Decimal(1_073_000_000) - initial_sol * Decimal(20_000_000),
            real_sol=initial_sol,
            created_at=now,
            pump_until_s=self._rng.uniform(45, 150) if arche != "dud" else 20.0,
        )
        self.tokens[mint] = sim
        return sim

    # ------------------------------------------------------------------ ticks
    def _apply_trade(
        self, sim: SimToken, is_buy: bool, sol_in: Decimal, now: datetime
    ) -> TradeEvent:
        k = sim.sol_reserve * sim.token_reserve
        sim.trade_seq += 1
        trader = self._rng.choice(self._trader_pool[: 40 + sim.trade_seq // 2])
        if is_buy:
            sol_eff = sol_in * (1 - POOL_FEE)
            new_sol = sim.sol_reserve + sol_eff
            new_tok = k / new_sol
            tokens_out = sim.token_reserve - new_tok
            sim.sol_reserve, sim.token_reserve = new_sol, new_tok
            sim.real_sol += sol_eff
            sim.total_buys += 1
            token_amount = tokens_out
        else:
            # sell: tokens_in sized so that ~sol_in comes out
            tokens_in = (sim.token_reserve * sol_in) / max(
                sim.sol_reserve - sol_in, Decimal("0.001")
            )
            tokens_in = min(tokens_in, sim.token_reserve * Decimal("0.2"))
            new_tok = sim.token_reserve + tokens_in
            new_sol = k / new_tok
            sol_out = (sim.sol_reserve - new_sol) * (1 - POOL_FEE)
            sim.sol_reserve, sim.token_reserve = new_sol, new_tok
            sim.real_sol = max(Decimal(0), sim.real_sol - sol_out)
            sim.total_sells += 1
            token_amount = tokens_in
            sol_in = sol_out
        sim.traders.add(trader)
        return TradeEvent(
            mint=sim.token.mint,
            observed_at=now,
            source="synthetic",
            is_buy=is_buy,
            sol_amount=sol_in,
            token_amount=token_amount,
            trader=trader,
            signature=f"{sim.token.mint[:7]}-{sim.trade_seq}",
            price_native=sim.price,
            market_cap_sol=sim.price * Decimal(1_000_000_000),
            pool_sol_reserves=sim.sol_reserve,
            pool_token_reserves=sim.token_reserve,
        )

    def step_token(self, sim: SimToken, now: datetime) -> list[TradeEvent]:
        if sim.dead:
            return []
        age = sim.age(now)
        trades: list[TradeEvent] = []
        rng = self._rng
        if sim.archetype == "runner":
            if age < sim.pump_until_s:
                n = rng.randint(2, 6)
                buy_p = 0.78
                size = (0.05, 0.9)
            elif age < sim.pump_until_s + 60:
                n = rng.randint(1, 4)
                buy_p = 0.35
                size = (0.05, 1.2)
            else:
                n = rng.randint(0, 1)
                buy_p = 0.4
                size = (0.02, 0.2)
        elif sim.archetype == "rug":
            if age < sim.pump_until_s:
                n = rng.randint(2, 7)
                buy_p = 0.82
                size = (0.05, 1.0)
            elif not sim.rugged:
                sim.rugged = True
                # liquidity pulled: 90% of SOL leaves the pool in one go
                pulled = sim.sol_reserve * Decimal("0.9")
                k = sim.sol_reserve * sim.token_reserve
                sim.sol_reserve -= pulled
                sim.token_reserve = k / sim.sol_reserve
                sim.real_sol = max(Decimal(0), sim.real_sol - pulled)
                n, buy_p, size = 1, 0.0, (0.05, 0.3)
            else:
                n, buy_p, size = rng.randint(0, 1), 0.2, (0.01, 0.1)
        else:  # dud
            n = rng.randint(0, 2) if age < 40 else rng.randint(0, 1)
            buy_p = 0.5
            size = (0.01, 0.15)
        for _ in range(n):
            is_buy = rng.random() < buy_p
            sol = D(round(rng.uniform(*size), 4))
            if not is_buy and sim.sol_reserve - sol < Decimal("0.5"):
                continue
            trades.append(self._apply_trade(sim, is_buy, sol, now))
        if age > 900:
            sim.dead = True
        return trades

    def snapshot(self, sim: SimToken, now: datetime) -> MarketSnapshot:
        price = sim.price
        liq_usd = sim.real_sol * 2 * self.sol_usd
        return MarketSnapshot(
            mint=sim.token.mint,
            observed_at=now,
            source="synthetic",
            price_native=price,
            price_usd=price * self.sol_usd,
            liquidity_usd=liq_usd,
            liquidity_native=sim.real_sol,
            market_cap_usd=price * Decimal(1_000_000_000) * self.sol_usd,
            fdv_usd=price * Decimal(1_000_000_000) * self.sol_usd,
            holder_count=len(sim.traders),
            unique_traders=len(sim.traders),
            top10_holder_pct=min(0.9, sim.top_holder_pct * 3),
            largest_holder_pct=sim.top_holder_pct,
            spread_bps=int(POOL_FEE * 2 * 10_000),
            pool_address=sim.token.pool_address,
            venue=Venue.SYNTHETIC,
            pair_created_at=sim.created_at,
        )

    def step(self, now: datetime) -> tuple[list[TokenInfo], list[TradeEvent], list[MarketSnapshot]]:
        launched: list[TokenInfo] = []
        sim = self.maybe_launch(now)
        if sim is not None:
            launched.append(sim.token)
        trades: list[TradeEvent] = []
        snaps: list[MarketSnapshot] = []
        for tok in list(self.tokens.values()):
            if tok.dead:
                continue
            trades.extend(self.step_token(tok, now))
            snaps.append(self.snapshot(tok, now))
        return launched, trades, snaps

    # ------------------------------------------------------------------ quotes
    def quote(
        self, input_mint: str, output_mint: str, amount_raw: int, slippage_bps: int, now: datetime
    ) -> SwapQuote:
        if amount_raw <= 0:
            raise QuoteError("amount must be positive")
        if input_mint == WSOL:
            sim = self.tokens.get(output_mint)
            if sim is None:
                raise QuoteError(f"unknown output mint {output_mint}")
            sol_in = raw_to_ui(amount_raw, SOL_DECIMALS)
            sol_eff = sol_in * (1 - POOL_FEE)
            k = sim.sol_reserve * sim.token_reserve
            tokens_out = sim.token_reserve - k / (sim.sol_reserve + sol_eff)
            spot = sol_in / sim.price
            impact = float((spot - tokens_out) / spot) * 100 if spot > 0 else 0.0
            out_raw = ui_to_raw(tokens_out, TOKEN_DECIMALS)
        elif output_mint == WSOL:
            sim = self.tokens.get(input_mint)
            if sim is None:
                raise QuoteError(f"unknown input mint {input_mint}")
            tokens_in = raw_to_ui(amount_raw, TOKEN_DECIMALS)
            k = sim.sol_reserve * sim.token_reserve
            sol_out = (sim.sol_reserve - k / (sim.token_reserve + tokens_in)) * (1 - POOL_FEE)
            spot = tokens_in * sim.price
            impact = float((spot - sol_out) / spot) * 100 if spot > 0 else 0.0
            out_raw = ui_to_raw(sol_out, SOL_DECIMALS)
        else:
            raise QuoteError("synthetic quotes must involve WSOL")
        if sim.rugged and sim.real_sol < Decimal("1"):
            raise QuoteError("no route: insufficient liquidity", retryable=False)
        threshold = int(out_raw * (10_000 - slippage_bps) / 10_000)
        return SwapQuote(
            quote_id=new_id("q"),
            provider="synthetic",
            input_mint=input_mint,
            output_mint=output_mint,
            in_amount_raw=amount_raw,
            out_amount_raw=out_raw,
            other_amount_threshold_raw=threshold,
            slippage_bps=slippage_bps,
            price_impact_pct=max(0.0, impact),
            route_labels=("synthetic-cpmm",),
            fee_lamports=5000,
            quoted_at=now,
            latency_ms=1.0,
        )

    def authorities(self, mint: str) -> TokenAuthorities | None:
        if mint not in self.tokens:
            return None
        return TokenAuthorities(
            mint_authority=None,
            freeze_authority=None,
            decimals=TOKEN_DECIMALS,
            supply_raw=1_000_000_000 * 10**TOKEN_DECIMALS,
        )

    def holders(self, mint: str, now: datetime) -> HolderDistribution | None:
        sim = self.tokens.get(mint)
        if sim is None:
            return None
        return HolderDistribution(
            holder_count=len(sim.traders),
            top10_pct=min(0.9, sim.top_holder_pct * 3),
            largest_pct=sim.top_holder_pct,
            largest_is_pool=False,
            observed_at=now,
        )


class SyntheticDiscovery:
    """Streaming discovery: emits every launch produced by the world's ticker."""

    name = "synthetic"

    def __init__(self, world: SyntheticWorld) -> None:
        self._world = world
        self._emit: EmitToken | None = None

    async def run(self, emit: EmitToken) -> None:
        self._emit = emit
        await asyncio.Event().wait()

    async def publish(self, tokens: Sequence[TokenInfo]) -> None:
        if self._emit is None:
            return
        for t in tokens:
            await self._emit(t)


class SyntheticMarketData:
    name = "synthetic"

    def __init__(self, world: SyntheticWorld) -> None:
        self._world = world
        self._subscribed: set[str] = set()
        self._emit_snapshot: EmitSnapshot | None = None
        self._emit_trade: EmitTrade | None = None

    def supports(self, token: TokenInfo) -> bool:
        return token.source == "synthetic"

    def set_sinks(self, emit_snapshot: EmitSnapshot, emit_trade: EmitTrade) -> None:
        self._emit_snapshot = emit_snapshot
        self._emit_trade = emit_trade

    async def subscribe(self, mints: Sequence[str]) -> None:
        self._subscribed.update(mints)

    async def unsubscribe(self, mints: Sequence[str]) -> None:
        self._subscribed.difference_update(mints)

    async def publish(self, trades: Sequence[TradeEvent], snaps: Sequence[MarketSnapshot]) -> None:
        if self._emit_trade is not None:
            for tr in trades:
                if tr.mint in self._subscribed:
                    await self._emit_trade(tr)
        if self._emit_snapshot is not None:
            for s in snaps:
                if s.mint in self._subscribed:
                    await self._emit_snapshot(s)


class SyntheticQuoteProvider:
    name = "synthetic"

    def __init__(self, world: SyntheticWorld, clock: Clock, *, fail_every: int = 0) -> None:
        self._world = world
        self._clock = clock
        self._fail_every = fail_every
        self._calls = 0

    async def quote(
        self, input_mint: str, output_mint: str, amount_raw: int, slippage_bps: int
    ) -> SwapQuote:
        self._calls += 1
        if self._fail_every and self._calls % self._fail_every == 0:
            raise QuoteError("synthetic transient quote failure", retryable=True)
        return self._world.quote(
            input_mint, output_mint, amount_raw, slippage_bps, self._clock.now()
        )

    async def build_swap(self, quote: SwapQuote, user_public_key: str) -> SwapBuild:
        raise QuoteError("the synthetic world cannot build transactions")

    async def prepare_unsigned_swap(self, quote: SwapQuote, user_public_key: str) -> str | None:
        return None


class SyntheticTokenProvider:
    name = "synthetic"

    def __init__(self, world: SyntheticWorld, clock: Clock) -> None:
        self._world = world
        self._clock = clock

    async def get_authorities(self, mint: str) -> TokenAuthorities | None:
        return self._world.authorities(mint)

    async def get_holder_distribution(
        self, mint: str, pool_addresses: tuple[str, ...]
    ) -> HolderDistribution | None:
        return self._world.holders(mint, self._clock.now())


class SyntheticTicker:
    """Background task: advances the world on the engine clock and pushes to the adapters."""

    def __init__(
        self,
        world: SyntheticWorld,
        clock: Clock,
        discovery: SyntheticDiscovery,
        market: SyntheticMarketData,
        *,
        tick_s: float = 0.5,
    ) -> None:
        self._world = world
        self._clock = clock
        self._discovery = discovery
        self._market = market
        self._tick = tick_s

    async def tick_once(self) -> None:
        now = self._clock.now()
        launched, trades, snaps = self._world.step(now)
        await self._discovery.publish(launched)
        await self._market.publish(trades, snaps)

    async def run(self) -> None:
        while True:
            await self.tick_once()
            await asyncio.sleep(self._tick)
