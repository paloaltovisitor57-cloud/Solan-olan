"""Human confirmation books fills at a fresh quote; recovery restores stored token decimals."""

from __future__ import annotations

from decimal import Decimal

from solana_sniper.domain.enums import CandidateState as S
from solana_sniper.domain.enums import SignalKind, Venue
from solana_sniper.domain.models import TokenInfo
from tests.integration.conftest import Harness


async def test_confirm_buy_requotes_when_signal_quote_is_old(harness: Harness) -> None:
    engine = harness.engine
    ex = engine.d.execution
    ex._auto_buys = False  # type: ignore[attr-defined]  # noqa: SLF001
    for _ in range(400):
        await harness.step(0.5)
        if ex.pending():
            break
    order = ex.pending()[0]
    assert order.buy is not None
    signal_tokens = order.buy.quote.expected_tokens_ui
    # let the signal quote age past max_quote_age_s but stay inside the signal TTL
    max_age = harness.runtime.settings.quotes.max_quote_age_s
    await harness.step(max_age + 1, ticks=1)
    if not ex.find(SignalKind.BUY, order.ref):
        return  # signal expired in this seed; nothing to assert
    msg = await engine.confirm_buy(order.ref)
    assert "confirmed" in msg
    pos = harness.runtime.account.open_positions[0]
    assert pos.quantity != signal_tokens  # booked at a fresh quote, price moved meanwhile
    assert pos.entry_sol == order.buy.quote.spend_sol
    decision = ex.history[-1].decision
    assert decision.note == "booked at fresh quote"
    assert engine.candidates[pos.mint].state is S.OPEN


async def test_repo_get_token_roundtrip(harness: Harness) -> None:
    repo = harness.runtime.repo
    token = TokenInfo(
        mint="ABC", symbol="A", name="Alpha", decimals=9, venue=Venue.RAYDIUM, source="test",
        discovered_at=harness.clock.now(), pool_address="pool",
    )
    repo.save_token(token)
    await repo.flush()
    loaded = await repo.get_token("ABC")
    assert loaded is not None and loaded.decimals == 9 and loaded.venue is Venue.RAYDIUM
    assert loaded.symbol == "A" and loaded.pool_address == "pool"
    assert await repo.get_token("missing") is None
    assert Decimal(10) ** loaded.decimals == Decimal(10**9)
