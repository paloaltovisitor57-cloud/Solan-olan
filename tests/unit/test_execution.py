from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from solana_sniper.config.settings import EntryConfig, FiltersConfig, QuotesConfig
from solana_sniper.domain.clock import ManualClock
from solana_sniper.domain.enums import (
    DecisionKind,
    DecisionSource,
    ExitReason,
    SignalKind,
    SignalStatus,
    Urgency,
)
from solana_sniper.domain.models import BuySignal, PositionSizing, SellSignal, SwapQuote, new_id
from solana_sniper.execution.base import FillOverride
from solana_sniper.execution.manual import DryRunExecution, ManualExecution, OrderNotPendingError
from solana_sniper.execution.preparer import TransactionPreparer
from solana_sniper.features.engine import FeatureEngine
from solana_sniper.filters.checks import TokenChecker
from solana_sniper.market_data.tracker import TokenTracker
from solana_sniper.strategy.scoring import EntryScorer
from tests.unit.helpers import feed_path, make_round_trip, make_token


def make_buy_signal(clock: ManualClock, mint: str = "MintTest", ttl_s: float = 45) -> BuySignal:
    tracker = TokenTracker()
    tracker.track(make_token(mint=mint, clock=clock))
    track = feed_path(tracker, clock, mint, ["1", "1.1", "1.2", "1.3"])
    f = FeatureEngine(20).compute(track, clock.now())
    checker = TokenChecker(FiltersConfig())
    report = checker.evaluate(track, f, clock.now())
    rt = make_round_trip(mint, clock)
    score = EntryScorer(EntryConfig(), FiltersConfig()).score(f, report, clock.now(), rt)
    sizing = PositionSizing(
        recommended_eur=Decimal("15"),
        recommended_sol=Decimal("0.1"),
        fraction_of_equity=Decimal("0.3"),
        equity_eur=Decimal("50"),
        available_cash_eur=Decimal("50"),
        caps_applied=(),
        multipliers={},
        profile="EXTREME",
        tier="micro",
    )
    return BuySignal(
        signal_id=new_id("sig"),
        mint=mint,
        symbol="TST",
        created_at=clock.now(),
        expires_at=clock.now() + timedelta(seconds=ttl_s),
        score=score,
        features=f,
        checks=report,
        sizing=sizing,
        quote=rt,
        token_age_s=f.token_age_s,
        liquidity_usd=Decimal("20000"),
        price_native=Decimal("1.3"),
        sol_eur=Decimal("150"),
    )


def make_sell_signal(
    clock: ManualClock, position_id: str = "pos_1", ttl_s: float = 30, with_quote: bool = True
) -> SellSignal:
    quote = None
    if with_quote:
        quote = SwapQuote(
            quote_id=new_id("q"),
            provider="t",
            input_mint="MintTest",
            output_mint="sol",
            in_amount_raw=1_000_000_000,
            out_amount_raw=80_000_000,
            other_amount_threshold_raw=0,
            slippage_bps=300,
            price_impact_pct=1.0,
            route_labels=("t",),
            fee_lamports=5000,
            quoted_at=clock.now(),
            latency_ms=1,
        )
    return SellSignal(
        signal_id=new_id("sig"),
        position_id=position_id,
        mint="MintTest",
        symbol="TST",
        created_at=clock.now(),
        expires_at=clock.now() + timedelta(seconds=ttl_s),
        reason=ExitReason.TRAILING_PEAK,
        urgency=Urgency.HIGH,
        detail="test",
        current_value_eur=Decimal("12.5"),
        entry_value_eur=Decimal("15"),
        peak_value_eur=Decimal("20"),
        pnl_eur=Decimal("-2.5"),
        pnl_pct=-0.166,
        trailing_drawdown_pct=0.375,
        trailing_threshold_pct=0.2,
        exit_quote=quote,
        estimated_sell_output_sol=Decimal("0.08") if with_quote else None,
        sol_eur=Decimal("150"),
    )


async def test_manual_confirm_and_reject(clock: ManualClock) -> None:
    ex = ManualExecution(clock, QuotesConfig())
    sig = make_buy_signal(clock)
    order = await ex.submit_buy(sig)
    assert order.ref == 1 and ex.find(SignalKind.BUY, 1) is order
    with pytest.raises(ValueError):
        await ex.submit_buy(sig)  # duplicate pending buy for the same mint
    res = await ex.decide(order, DecisionKind.CONFIRM, DecisionSource.HUMAN)
    assert res.fill is not None and res.decision.kind is DecisionKind.CONFIRM
    assert res.fill.sol_amount == Decimal("0.1") and res.fill.token_amount_ui == Decimal("1000")
    assert res.fill.eur_amount == Decimal("15") and not res.fill.simulated
    assert res.fill.slippage_cost_eur == Decimal("0.1") * Decimal("0.015") * Decimal("150")
    assert order.status is SignalStatus.CONFIRMED and ex.pending() == []
    with pytest.raises(OrderNotPendingError):
        await ex.decide(order, DecisionKind.CONFIRM, DecisionSource.HUMAN)
    sell = await ex.submit_sell(make_sell_signal(clock))
    with pytest.raises(ValueError):
        await ex.submit_sell(make_sell_signal(clock))
    rejected = await ex.decide(sell, DecisionKind.REJECT, DecisionSource.HUMAN, note="not now")
    assert rejected.fill is None and sell.status is SignalStatus.REJECTED
    assert len(ex.history) == 2


async def test_override_amounts_and_sell_fill(clock: ManualClock) -> None:
    ex = ManualExecution(clock, QuotesConfig())
    order = await ex.submit_buy(make_buy_signal(clock))
    res = await ex.decide(
        order,
        DecisionKind.CONFIRM,
        DecisionSource.HUMAN,
        override=FillOverride(
            sol_amount=Decimal("0.12"), token_amount_ui=Decimal("900"), reported_tx_signature="5abc"
        ),
    )
    assert (
        res.fill is not None
        and res.fill.sol_amount == Decimal("0.12")
        and res.fill.token_amount_ui == Decimal("900")
    )
    assert res.fill.reported_tx_signature == "5abc" and not res.fill.verified_onchain
    sell = await ex.submit_sell(make_sell_signal(clock))
    res_s = await ex.decide(sell, DecisionKind.CONFIRM, DecisionSource.HUMAN)
    assert res_s.fill is not None and res_s.fill.side is SignalKind.SELL
    assert res_s.fill.sol_amount == Decimal("0.08") and res_s.fill.eur_amount == Decimal("12")
    assert res_s.fill.slippage_cost_eur == Decimal("0.5")  # 12.5 marked vs 12 realised
    sell2 = await ex.submit_sell(make_sell_signal(clock, position_id="pos_2", with_quote=False))
    res_s2 = await ex.decide(
        sell2,
        DecisionKind.CONFIRM,
        DecisionSource.HUMAN,
        override=FillOverride(sol_amount=Decimal("0.05")),
    )
    assert res_s2.fill is not None and res_s2.fill.eur_amount == Decimal("7.5")


async def test_expiry(clock: ManualClock) -> None:
    ex = ManualExecution(clock, QuotesConfig())
    order = await ex.submit_buy(make_buy_signal(clock, ttl_s=10))
    assert await ex.expire_stale(clock.now()) == []
    clock.advance(11)
    expired = await ex.expire_stale(clock.now())
    assert (
        len(expired) == 1
        and expired[0].decision.kind is DecisionKind.EXPIRE
        and expired[0].fill is None
    )
    assert order.status is SignalStatus.EXPIRED
    late = await ex.submit_buy(make_buy_signal(clock, mint="Other", ttl_s=5))
    clock.advance(6)
    res = await ex.decide(late, DecisionKind.CONFIRM, DecisionSource.HUMAN)
    assert res.fill is None and res.decision.kind is DecisionKind.EXPIRE  # too late to confirm


async def test_dry_run_auto_confirms_with_haircut(clock: ManualClock) -> None:
    ex = DryRunExecution(
        clock,
        QuotesConfig(extra_fill_slippage_bps=100),
        confirm_delay_s=2,
        auto_confirm_sells=False,
    )
    buy = await ex.submit_buy(make_buy_signal(clock))
    sell = await ex.submit_sell(make_sell_signal(clock))
    assert await ex.auto_confirm(clock.now()) == []
    clock.advance(2)
    results = await ex.auto_confirm(clock.now())
    assert len(results) == 1 and results[0].order is buy
    fill = results[0].fill
    assert fill is not None and fill.simulated and fill.token_amount_ui == Decimal("990")
    assert fill.eur_amount == Decimal("15")
    assert results[0].decision.source is DecisionSource.DRY_RUN
    assert ex.pending() == [sell]  # sells not auto-confirmed in this configuration
    res = await ex.decide(sell, DecisionKind.CONFIRM, DecisionSource.HUMAN)
    assert res.fill is not None and res.fill.sol_amount == Decimal("0.08") * Decimal("0.99")


class FakeQuoteProvider:
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def quote(
        self, input_mint: str, output_mint: str, amount_raw: int, slippage_bps: int
    ) -> SwapQuote:
        raise NotImplementedError

    async def prepare_unsigned_swap(self, quote: SwapQuote, user_public_key: str) -> str | None:
        self.calls.append((quote.quote_id, user_public_key))
        return "unsigned-base64"


async def test_preparer_modes(clock: ManualClock) -> None:
    provider = FakeQuoteProvider()
    sig = make_buy_signal(clock)
    dry = TransactionPreparer(
        provider, clock, wallet_public_key="Pub", enabled=True, simulated=True, session_id="s"
    )
    rec = await dry.prepare(sig)
    assert rec.unsigned_transaction_b64 is None and "dry-run" in rec.note and rec.simulated
    disabled = TransactionPreparer(
        provider, clock, wallet_public_key=None, enabled=True, simulated=False, session_id="s"
    )
    rec2 = await disabled.prepare(sig)
    assert rec2.unsigned_transaction_b64 is None and "disabled" in rec2.note
    live = TransactionPreparer(
        provider, clock, wallet_public_key="Pub", enabled=True, simulated=False, session_id="s"
    )
    rec3 = await live.prepare(sig)
    assert rec3.unsigned_transaction_b64 == "unsigned-base64" and provider.calls == [
        (sig.quote.buy.quote_id, "Pub")
    ]
    assert "BUY TST" in rec3.instructions and rec3.side is SignalKind.BUY
    rec4 = await live.prepare(make_sell_signal(clock, with_quote=False))
    assert (
        rec4.unsigned_transaction_b64 is None
        and "no quote" in rec4.note
        and rec4.side is SignalKind.SELL
    )
