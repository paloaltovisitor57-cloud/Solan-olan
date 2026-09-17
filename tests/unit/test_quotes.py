from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from solana_sniper.config.settings import QuotesConfig
from solana_sniper.domain.clock import ManualClock
from solana_sniper.infra.http import HttpClient
from solana_sniper.market_data.synthetic import WSOL, SyntheticQuoteProvider, SyntheticWorld
from solana_sniper.quotes.base import QuoteError
from solana_sniper.quotes.jupiter import JupiterQuoteProvider, parse_quote
from solana_sniper.quotes.round_trip import RoundTripEvaluator
from solana_sniper.telemetry.metrics import Metrics

NOW = datetime(2026, 3, 1, tzinfo=UTC)

JUP_QUOTE = {
    "inputMint": WSOL,
    "inAmount": "100000000",
    "outputMint": "MintX",
    "outAmount": "123456789000",
    "otherAmountThreshold": "119753460000",
    "swapMode": "ExactIn",
    "slippageBps": 300,
    "priceImpactPct": "0.0123",
    "routePlan": [
        {
            "swapInfo": {
                "ammKey": "a",
                "label": "Raydium",
                "inputMint": WSOL,
                "outputMint": "MintX",
                "inAmount": "100000000",
                "outAmount": "123456789000",
                "feeAmount": "250000",
                "feeMint": WSOL,
            },
            "percent": 100,
        }
    ],
    "contextSlot": 1,
    "timeTaken": 0.01,
}


def test_parse_jupiter_quote() -> None:
    q = parse_quote(JUP_QUOTE, 12.0, NOW)
    assert q.out_amount_raw == 123456789000
    assert q.other_amount_threshold_raw == 119753460000
    assert q.price_impact_pct == pytest.approx(1.23)
    assert q.route_labels == ("Raydium",)
    assert q.fee_lamports == 250000
    assert q.raw["swapMode"] == "ExactIn"
    with pytest.raises(QuoteError):
        parse_quote({"error": "Could not find any route"}, 1, NOW)
    with pytest.raises(QuoteError) as exc:
        parse_quote({"error": "Rate limit exceeded"}, 1, NOW)
    assert exc.value.retryable
    with pytest.raises(QuoteError):
        parse_quote({"inAmount": "1"}, 1, NOW)
    with pytest.raises(QuoteError):
        parse_quote(["list"], 1, NOW)


async def test_jupiter_provider_http_paths(clock: ManualClock) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/quote"):
            if request.url.params.get("outputMint") == "NoRoute":
                return httpx.Response(400, json={"error": "no route"})
            return httpx.Response(200, json=JUP_QUOTE)
        if request.url.path.endswith("/swap"):
            body = request.read()
            assert b"userPublicKey" in body and b"secret" not in body
            return httpx.Response(200, json={"swapTransaction": "AQID", "lastValidBlockHeight": 1})
        return httpx.Response(404)

    http = HttpClient(transport=httpx.MockTransport(handler))
    metrics = Metrics()
    provider = JupiterQuoteProvider(
        http,
        clock,
        base_url="https://lite-api.jup.ag",
        pro_base_url="https://api.jup.ag",
        api_key="k",
        metrics=metrics,
    )
    q = await provider.quote(WSOL, "MintX", 100_000_000, 300)
    assert calls[0].headers["x-api-key"] == "k"
    assert calls[0].url.host == "api.jup.ag"
    assert q.out_amount_raw == 123456789000
    assert metrics.counters["quotes"] == 1
    with pytest.raises(QuoteError) as exc:
        await provider.quote(WSOL, "NoRoute", 100_000_000, 300)
    assert not exc.value.retryable
    with pytest.raises(QuoteError):
        await provider.quote(WSOL, "MintX", 0, 300)
    unsigned = await provider.prepare_unsigned_swap(q, "PublicKey111")
    assert unsigned == "AQID"
    await http.aclose()


async def test_round_trip_with_synthetic(clock: ManualClock) -> None:
    world = SyntheticWorld(clock, seed=11)
    sim = world.launch(clock.now(), archetype="runner")
    provider = SyntheticQuoteProvider(world, clock)
    evaluator = RoundTripEvaluator(provider, QuotesConfig(), clock, Metrics())
    rt = await evaluator.evaluate(sim.token.mint, Decimal("0.1"), 6)
    assert rt.viable, rt.reasons
    assert rt.sell is not None and rt.immediate_exit_sol is not None
    assert 0 < (rt.round_trip_loss_pct or 0) < 0.1
    assert rt.expected_tokens_ui > 0
    assert rt.total_fee_lamports > 0
    exit_q = await evaluator.exit_quote(sim.token.mint, rt.expected_tokens_ui, 6)
    assert exit_q.out_amount_raw == rt.sell.out_amount_raw
    assert evaluator.is_fresh(exit_q)
    clock.advance(9)
    assert not evaluator.is_fresh(exit_q)


async def test_round_trip_huge_size_not_viable(clock: ManualClock) -> None:
    world = SyntheticWorld(clock, seed=12)
    sim = world.launch(clock.now(), archetype="runner")
    evaluator = RoundTripEvaluator(SyntheticQuoteProvider(world, clock), QuotesConfig(), clock)
    rt = await evaluator.evaluate(sim.token.mint, Decimal("40"), 6)  # bigger than the pool
    assert not rt.viable
    assert rt.round_trip_loss_pct is not None and rt.round_trip_loss_pct > 0.25
    assert any("round trip loss" in r for r in rt.reasons)


async def test_round_trip_sell_failure(clock: ManualClock) -> None:
    world = SyntheticWorld(clock, seed=13)
    sim = world.launch(clock.now(), archetype="runner")
    provider = SyntheticQuoteProvider(world, clock, fail_every=2)  # 2nd call (the sell) fails
    metrics = Metrics()
    rt = await RoundTripEvaluator(provider, QuotesConfig(), clock, metrics).evaluate(
        sim.token.mint, Decimal("0.1"), 6
    )
    assert not rt.viable and rt.sell is None and rt.immediate_exit_sol is None
    assert any("sell quote failed" in r for r in rt.reasons)
    assert metrics.counters["quote_failures"] == 1
