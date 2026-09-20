"""Replay a recorded session through the same engine to debug decisions.

Recorded observations/trades are fed in time order on a ManualClock. Quotes come from the
recorded quotes when one exists for the mint within a short window, otherwise from the latest
recorded price with a conservative impact model, so signal logic can still be exercised.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from solana_sniper.app.bootstrap import Runtime, build_runtime
from solana_sniper.config.settings import Settings
from solana_sniper.domain.clock import ManualClock
from solana_sniper.domain.enums import RunMode
from solana_sniper.domain.models import MarketSnapshot, SwapQuote, TokenInfo, TradeEvent, new_id
from solana_sniper.domain.money import raw_to_ui, ui_to_raw
from solana_sniper.market_data.base import EmitSnapshot, EmitTrade
from solana_sniper.market_data.tracker import TokenTracker
from solana_sniper.quotes.base import QuoteError, SwapBuild
from solana_sniper.storage.repository import Repository
from solana_sniper.storage.serialization import dataclass_from_dict

WSOL = "So11111111111111111111111111111111111111112"


class ReplayMarketData:
    name = "replay"

    def __init__(self) -> None:
        self._emit_snapshot: EmitSnapshot | None = None
        self._emit_trade: EmitTrade | None = None
        self.subscribed: set[str] = set()

    def supports(self, token: TokenInfo) -> bool:
        return True

    def set_sinks(self, emit_snapshot: EmitSnapshot, emit_trade: EmitTrade) -> None:
        self._emit_snapshot = emit_snapshot
        self._emit_trade = emit_trade

    async def subscribe(self, mints: Sequence[str]) -> None:
        self.subscribed.update(mints)

    async def unsubscribe(self, mints: Sequence[str]) -> None:
        self.subscribed.difference_update(mints)

    async def snapshot(self, snap: MarketSnapshot) -> None:
        if self._emit_snapshot is not None:
            await self._emit_snapshot(snap)

    async def trade(self, trade: TradeEvent) -> None:
        if self._emit_trade is not None:
            await self._emit_trade(trade)


class ReplayQuoteProvider:
    """Recorded quotes when available; otherwise a price-based estimate with size-based impact."""

    name = "replay"

    def __init__(self, clock: ManualClock, tracker: TokenTracker) -> None:
        self._clock = clock
        self._tracker = tracker
        self._recorded: dict[str, list[SwapQuote]] = {}

    def record(self, mint: str, quote: SwapQuote) -> None:
        self._recorded.setdefault(mint, []).append(quote)

    async def quote(
        self, input_mint: str, output_mint: str, amount_raw: int, slippage_bps: int
    ) -> SwapQuote:
        now = self._clock.now()
        mint = output_mint if input_mint == WSOL else input_mint
        for q in self._recorded.get(mint, []):
            same_dir = q.input_mint == input_mint and q.output_mint == output_mint
            if same_dir and abs((q.quoted_at - now).total_seconds()) <= 10 and q.in_amount_raw > 0:
                scale = Decimal(amount_raw) / Decimal(q.in_amount_raw)
                return SwapQuote(
                    quote_id=new_id("rq"),
                    provider="replay-recorded",
                    input_mint=input_mint,
                    output_mint=output_mint,
                    in_amount_raw=amount_raw,
                    out_amount_raw=int(q.out_amount_raw * scale),
                    other_amount_threshold_raw=int(q.other_amount_threshold_raw * scale),
                    slippage_bps=slippage_bps,
                    price_impact_pct=q.price_impact_pct,
                    route_labels=q.route_labels,
                    fee_lamports=q.fee_lamports,
                    quoted_at=now,
                    latency_ms=0.0,
                )
        track = self._tracker.get(mint)
        latest = track.latest if track else None
        if latest is None or latest.price_native is None or latest.price_native <= 0:
            raise QuoteError("replay: no price for quote")
        decimals = track.token.decimals if track and track.token.decimals is not None else 6
        liq_sol = latest.liquidity_native
        if (
            liq_sol is None
            and latest.liquidity_usd is not None
            and latest.price_usd
            and latest.price_native
        ):
            liq_sol = latest.liquidity_usd / (latest.price_usd / latest.price_native) / 2
        if input_mint == WSOL:
            sol_in = raw_to_ui(amount_raw, 9)
            impact = float(sol_in / liq_sol) if liq_sol and liq_sol > 0 else 0.05
            tokens = sol_in / latest.price_native * Decimal(1 - min(impact, 0.9)) * Decimal("0.99")
            out_raw = ui_to_raw(tokens, decimals)
        else:
            tokens_in = raw_to_ui(amount_raw, decimals)
            sol_val = tokens_in * latest.price_native
            impact = float(sol_val / liq_sol) if liq_sol and liq_sol > 0 else 0.05
            out_raw = ui_to_raw(sol_val * Decimal(1 - min(impact, 0.9)) * Decimal("0.99"), 9)
        return SwapQuote(
            quote_id=new_id("rq"),
            provider="replay-estimated",
            input_mint=input_mint,
            output_mint=output_mint,
            in_amount_raw=amount_raw,
            out_amount_raw=max(0, out_raw),
            other_amount_threshold_raw=max(0, out_raw),
            slippage_bps=slippage_bps,
            price_impact_pct=min(impact, 0.9) * 100,
            route_labels=("replay",),
            fee_lamports=5000,
            quoted_at=now,
            latency_ms=0.0,
        )

    async def build_swap(self, quote: SwapQuote, user_public_key: str) -> SwapBuild:
        raise QuoteError("replay cannot build transactions")

    async def prepare_unsigned_swap(self, quote: SwapQuote, user_public_key: str) -> str | None:
        return None


@dataclass(slots=True)
class ReplaySummary:
    events: int
    tokens: int
    signals: int
    confirmed: int
    exits: int
    final_equity: Decimal
    session_id: str
    log: list[str]


async def replay_session(
    settings: Settings, source_session_id: str, *, speed_log: bool = False
) -> ReplaySummary:
    source = Repository(settings.storage.database_url, session_id="replay-reader")
    await source.init()
    stream = await source.session_stream(source_session_id)
    await source.close()
    if not stream:
        raise ValueError(f"session {source_session_id} has no recorded events")
    start = stream[0][0]
    clock = ManualClock(start)
    replay_settings = settings.model_copy(deep=True)
    replay_settings.storage.database_url = _replay_db_url(settings.storage.database_url)
    replay_settings.discovery.sources = ["synthetic"]  # no live discovery during replay
    replay_settings.market_data.sources = ["synthetic"]
    replay_settings.quotes.source = "synthetic"
    replay_settings.dashboard.enabled = False
    runtime: Runtime = build_runtime(
        replay_settings,
        mode=RunMode.REPLAY,
        session_id=f"replay-{source_session_id}",
        clock=clock,
        quiet_alerts=True,
    )
    # swap in replay adapters (the synthetic ones were registered by the builder; we bypass them)
    md = ReplayMarketData()
    runtime.market.add_streaming(md)
    quotes = ReplayQuoteProvider(clock, runtime.engine.d.tracker)
    runtime.engine.d.round_trip._provider = quotes
    runtime.background = []  # no tickers/pollers: we drive time ourselves
    await runtime.repo.init()
    runtime.repo.start()
    await runtime.repo.start_session("REPLAY", str(settings.config_path or ""))
    await runtime._restore_account()
    runtime.bus.start()
    engine = runtime.engine
    log_lines: list[str] = []
    tokens = 0
    tick_every = timedelta(seconds=0.25)  # same cadence as the live loop
    last_tick = clock.now() - tick_every
    try:
        for at, kind, payload in stream:
            clock.set(max(at, clock.now()))
            if kind == "token":
                tokens += 1
                token = _token_from_row(payload)
                await engine.on_token(token)
            elif kind == "observation":
                await md.snapshot(dataclass_from_dict(MarketSnapshot, payload))
            elif kind == "trade":
                await md.trade(dataclass_from_dict(TradeEvent, payload))
            elif kind == "quote":
                mint = payload.pop("mint", "")
                quote = dataclass_from_dict(SwapQuote, payload)
                quotes.record(mint, quote)
            if clock.now() - last_tick >= tick_every:
                last_tick = clock.now()
                await engine.tick()
                for _ in range(3):  # let spawned quote tasks complete within this step
                    await asyncio.sleep(0)
        for _ in range(20):  # let pending confirmations/exits settle
            clock.advance(2)
            await engine.tick()
            for _ in range(3):
                await asyncio.sleep(0)
    finally:
        log_lines = list(engine.stats.recent)
        await runtime.bus.stop()
        await runtime.repo.end_session()
        await runtime.repo.close()
        await runtime.http.aclose()
    return ReplaySummary(
        events=len(stream),
        tokens=tokens,
        signals=engine.stats.signals,
        confirmed=engine.stats.confirmed,
        exits=engine.stats.exits,
        final_equity=runtime.account.equity,
        session_id=runtime.session_id,
        log=log_lines,
    )


def _replay_db_url(url: str) -> str:
    if url.endswith(".db"):
        return url[:-3] + "-replay.db"
    return url + "-replay"


def _token_from_row(row: dict[str, Any]) -> TokenInfo:
    def dt(v: Any) -> datetime | None:
        if not v:
            return None
        parsed = datetime.fromisoformat(v)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    from solana_sniper.domain.enums import Venue

    try:
        venue = Venue(row.get("venue") or "unknown")
    except ValueError:
        venue = Venue.UNKNOWN
    return TokenInfo(
        mint=row["mint"],
        symbol=row.get("symbol"),
        name=row.get("name"),
        decimals=row.get("decimals"),
        created_at=dt(row.get("created_at")),
        pool_created_at=dt(row.get("pool_created_at")),
        venue=venue,
        pool_address=row.get("pool_address"),
        quote_mint=row.get("quote_mint"),
        source=row.get("source") or "replay",
        discovered_at=dt(row.get("discovered_at")),
    )
