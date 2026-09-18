"""Entry lifecycle regressions from the first live paper run: qualification must be a latched,
auditable state with hysteresis, not a one-tick edge trigger."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from solana_sniper.domain.enums import CandidateState as S
from solana_sniper.domain.models import EntryScore, RoundTripQuote
from solana_sniper.quotes.base import QuoteError
from tests.integration.conftest import Harness


class ScriptedScorer:
    """Wraps the real scorer and overrides the final score with a scripted sequence."""

    def __init__(self, real: Any, scores: list[float]) -> None:
        self._real = real
        self.scores = scores
        self.calls = 0

    def score(self, features: Any, checks: Any, now: Any, round_trip: Any = None) -> EntryScore:
        base = self._real.score(features, checks, now, round_trip)
        value = self.scores[min(self.calls, len(self.scores) - 1)]
        self.calls += 1
        return replace(base, score=value)


class PendingRoundTrip:
    """A round-trip evaluator that never answers until released (quote in flight)."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.calls = 0

    async def evaluate(self, mint: str, spend_sol: Any, token_decimals: int) -> RoundTripQuote:
        self.calls += 1
        await self.release.wait()
        raise QuoteError("released without a quote", retryable=False)


async def _first_qualified(harness: Harness, max_steps: int = 600) -> Any:
    engine = harness.engine
    for _ in range(max_steps):
        await harness.step(0.5)
        for cand in engine.candidates.values():
            if cand.state is S.QUALIFIED:
                return cand
    raise AssertionError(f"no candidate qualified; stats={engine.stats}")


async def test_small_score_wobble_does_not_demote_a_qualified_candidate(harness: Harness) -> None:
    """Analos/BONKCAT: QUALIFIED at 67/71, then 63/60.5 one tick later -> MONITORING while the
    quote was still in flight. Expected: the candidate stays latched for the bounded entry
    validation window."""
    engine = harness.engine
    pending = PendingRoundTrip()
    engine.d = replace(engine.d, round_trip=pending)  # type: ignore[arg-type]
    cand = await _first_qualified(harness)
    assert cand.state is S.QUALIFIED
    assert cand.quote_in_flight or pending.calls >= 0
    min_score = harness.runtime.settings.entry.min_score  # 60 in the synthetic config
    wobble = min_score - 3.0  # 67 -> 63 in the live run's terms: below the threshold, tiny drop
    scripted = ScriptedScorer(engine.d.scorer, [wobble])
    engine.d = replace(engine.d, scorer=scripted)  # type: ignore[arg-type]
    for _ in range(4):  # two seconds of evaluations just under min_score
        await harness.step(0.5)
    history = [(t.target, t.reason) for t in cand.sm.history]
    assert cand.state is S.QUALIFIED, history
    assert not any(t.target is S.MONITORING and "<" in t.reason for t in cand.sm.history[-4:]), (
        history
    )
    pending.release.set()


class FixedRoundTrip:
    """Round-trip evaluator returning a crafted quote (or raising). With `gate` the answer waits
    until the test releases it, so post-quote validation can be arranged deterministically.
    Exit quotes are delegated to the real evaluator (position monitoring keeps working)."""

    def __init__(
        self,
        real: Any,
        *,
        loss_pct: float = 0.05,
        sell_ok: bool = True,
        error: QuoteError | None = None,
        gate: asyncio.Event | None = None,
    ) -> None:
        self._real = real
        self.loss_pct = loss_pct
        self.sell_ok = sell_ok
        self.error = error
        self.gate = gate
        self.calls = 0

    async def evaluate(self, mint: str, spend_sol: Any, token_decimals: int) -> RoundTripQuote:
        from tests.unit.helpers import make_round_trip

        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        max_loss = harness_settings_max_loss()
        viable = self.sell_ok and self.loss_pct <= max_loss
        rt = make_round_trip(
            mint,
            _clock_of_last_harness,
            loss_pct=self.loss_pct,
            sell_ok=self.sell_ok,
            viable=viable,
        )
        reasons = () if viable else (f"round trip loss {self.loss_pct:.0%} > {max_loss:.0%}",)
        return replace(rt, spend_sol=spend_sol, reasons=reasons)

    async def exit_quote(self, mint: str, quantity_ui: Any, token_decimals: int) -> Any:
        return await self._real.exit_quote(mint, quantity_ui, token_decimals)


_clock_of_last_harness: Any = None
_max_loss_of_last_harness: float = 0.25


def harness_settings_max_loss() -> float:
    return _max_loss_of_last_harness


def _install(harness: Harness, **deps: Any) -> None:
    global _clock_of_last_harness, _max_loss_of_last_harness
    _clock_of_last_harness = harness.clock
    _max_loss_of_last_harness = harness.runtime.settings.quotes.round_trip_max_loss_pct
    harness.engine.d = replace(harness.engine.d, **deps)


async def _attempts(harness: Harness, mint: str | None = None) -> list[Any]:
    await harness.runtime.repo.flush()
    return await harness.runtime.repo.entry_attempts(mint=mint)


async def test_major_score_collapse_abandons_the_latch(harness: Harness) -> None:
    engine = harness.engine
    pending = PendingRoundTrip()
    _install(harness, round_trip=pending)
    cand = await _first_qualified(harness)
    _install(harness, scorer=ScriptedScorer(engine.d.scorer, [40.0]))
    for _ in range(3):
        await harness.step(0.5)
    assert cand.state is S.MONITORING, [(t.target, t.reason) for t in cand.sm.history]
    assert "collapsed" in cand.sm.history[-1].reason
    (attempt,) = await _attempts(harness, cand.mint)
    assert str(attempt.final_decision) == "ABANDONED" and "collapsed" in (
        attempt.block_reason or ""
    )
    assert attempt.completed_at is not None
    pending.release.set()


async def test_fatal_check_while_latched_rejects_immediately(harness: Harness) -> None:
    from solana_sniper.domain.models import TokenAuthorities

    engine = harness.engine
    pending = PendingRoundTrip()
    _install(harness, round_trip=pending)
    cand = await _first_qualified(harness)
    cand.track.authorities = TokenAuthorities(
        mint_authority="StillActive",
        freeze_authority=None,
        decimals=6,
        supply_raw=1,
        transfer_fee_bps=0,
        has_transfer_hook=False,
        non_transferable=False,
        permanent_delegate=None,
    )
    cand.dirty = True
    for _ in range(3):
        await harness.step(0.5)
    assert cand.state is S.REJECTED, [(t.target, t.reason) for t in cand.sm.history]
    (attempt,) = await _attempts(harness, cand.mint)
    assert str(attempt.final_decision) == "HARD_REJECT" and "mint_authority" in (
        attempt.block_reason or ""
    )
    assert engine.stats.signals == 0
    pending.release.set()


async def test_liquidity_collapse_while_latched_rejects(harness: Harness) -> None:
    from decimal import Decimal

    pending = PendingRoundTrip()
    _install(harness, round_trip=pending)
    cand = await _first_qualified(harness)
    sim = harness.world.tokens[cand.mint]
    sim.real_sol = sim.real_sol * Decimal("0.05")  # the pool is drained: liquidity -95%
    sim.sol_reserve = sim.sol_reserve * Decimal("0.05")
    sim.rugged = True
    for _ in range(6):
        await harness.step(0.5)
        if cand.state is S.REJECTED:
            break
    assert cand.state is S.REJECTED, [(t.target, t.reason) for t in cand.sm.history]
    (attempt,) = await _attempts(harness, cand.mint)
    assert str(attempt.final_decision) == "HARD_REJECT"
    assert "liquidity" in (attempt.block_reason or "")
    pending.release.set()


async def test_quote_failure_is_recorded_and_no_signal(harness: Harness) -> None:
    engine = harness.engine
    failing = FixedRoundTrip(
        engine.d.round_trip, error=QuoteError("jupiter: no route", retryable=False)
    )
    _install(harness, round_trip=failing)
    for _ in range(400):
        await harness.step(0.5)
        if failing.calls >= 2:
            break
    assert failing.calls >= 1 and engine.stats.signals == 0
    attempts = [a for a in await _attempts(harness) if str(a.final_decision) == "QUOTE_FAILED"]
    assert attempts, [(str(a.final_decision), a.block_reason) for a in await _attempts(harness)]
    attempt = attempts[0]
    assert attempt.buy_quote_status == "failed" and "no route" in (attempt.quote_error or "")
    assert "quote failed" in (attempt.block_reason or "")
    assert attempt.quote_started_at is not None and attempt.quote_finished_at is not None
    cand = engine.candidates[attempt.mint]
    assert cand.state is not S.QUALIFIED and cand.cooldown_until is not None  # cooled down
    assert (await harness.runtime.repo.counts())["quotes"] == 0  # nothing to persist


async def test_quote_success_with_small_wobble_still_signals(harness: Harness) -> None:
    engine = harness.engine
    gate = asyncio.Event()
    fixed = FixedRoundTrip(engine.d.round_trip, loss_pct=0.05, gate=gate)
    _install(harness, round_trip=fixed)
    min_score = harness.runtime.settings.entry.min_score
    cand = await _first_qualified(harness)
    for _ in range(4):  # the quote is requested and held
        await harness.step(0.5)
        if fixed.calls:
            break
    assert fixed.calls >= 1 and cand.state is S.QUALIFIED
    # from here every evaluation, including the post-quote one, scores just under min_score
    _install(harness, scorer=ScriptedScorer(engine.d.scorer, [min_score - 3.0]))
    await harness.step(0.5)
    assert cand.state is S.QUALIFIED  # within hysteresis: still latched
    gate.set()
    for _ in range(6):
        await harness.step(0.5)
        if engine.stats.signals:
            break
    assert engine.stats.signals >= 1, [(t.target, t.reason) for t in cand.sm.history]
    (attempt,) = await _attempts(harness, cand.mint)
    assert str(attempt.final_decision) == "BUY_SIGNAL" and attempt.signal_id
    assert attempt.post_quote_score is not None and attempt.post_quote_score < min_score
    assert attempt.hysteresis_holds >= 1 and attempt.round_trip_viable is True
    assert attempt.round_trip_loss_pct is not None and attempt.round_trip_loss_pct < 0.25
    counts = await harness.runtime.repo.counts()
    assert counts["quotes"] >= 2  # buy + sell quotes persisted


async def test_quote_success_with_major_degradation_does_not_signal(harness: Harness) -> None:
    engine = harness.engine
    gate = asyncio.Event()
    fixed = FixedRoundTrip(engine.d.round_trip, loss_pct=0.05, gate=gate)
    _install(harness, round_trip=fixed)
    cand = await _first_qualified(harness)
    for _ in range(4):
        await harness.step(0.5)
        if fixed.calls:
            break
    assert fixed.calls >= 1
    _install(harness, scorer=ScriptedScorer(engine.d.scorer, [45.0]))
    gate.set()
    for _ in range(6):
        await harness.step(0.5)
    assert engine.stats.signals == 0 and cand.state is S.MONITORING
    (attempt,) = await _attempts(harness, cand.mint)
    assert str(attempt.final_decision) == "ABANDONED"
    assert "collapsed" in (attempt.block_reason or "")


async def test_bad_round_trip_economics_abandon_the_entry(harness: Harness) -> None:
    engine = harness.engine
    fixed = FixedRoundTrip(engine.d.round_trip, loss_pct=0.40)  # > round_trip_max_loss_pct
    _install(harness, round_trip=fixed)
    for _ in range(400):
        await harness.step(0.5)
        if fixed.calls >= 2:
            break
    assert engine.stats.signals == 0 and fixed.calls >= 1
    attempts = await _attempts(harness)
    economics = [
        a
        for a in attempts
        if str(a.final_decision) == "ABANDONED" and "round trip" in (a.block_reason or "")
    ]
    assert economics, [(str(a.final_decision), a.block_reason) for a in attempts]
    assert all(a.round_trip_viable is False for a in economics)
    assert all(
        a.round_trip_loss_pct is not None and a.round_trip_loss_pct > 0.25 for a in economics
    )
    # the cooldown stops the token from re-qualifying on the very next tick
    mint = economics[0].mint
    cand = engine.candidates[mint]
    assert cand.cooldown_until is not None and cand.state is not S.QUALIFIED


async def test_stale_data_during_entry_is_bounded(harness: Harness) -> None:
    engine = harness.engine
    settings = harness.runtime.settings
    pending = PendingRoundTrip()
    _install(harness, round_trip=pending)
    cand = await _first_qualified(harness)
    for _ in range(4):
        await harness.step(0.5)
        if cand.quote_in_flight:
            break
    assert cand.quote_in_flight
    del harness.world.tokens[cand.mint]  # no more observations for this mint
    stale_after = settings.market_data.stale_after_s
    grace = settings.entry.stale_grace_during_quote_s
    steps_inside = int((stale_after + grace * 0.5) / 0.5)
    for _ in range(steps_inside):
        await harness.step(0.5)
    assert cand.state is S.QUALIFIED, [(t.target, t.reason) for t in cand.sm.history]
    for _ in range(int(grace / 0.5) + 4):
        await harness.step(0.5)
    assert cand.state is S.DATA_STALE, [(t.target, t.reason) for t in cand.sm.history]
    attempts = await _attempts(harness, cand.mint)
    assert str(attempts[-1].final_decision) == "STALE", [
        (str(a.final_decision), a.block_reason) for a in attempts
    ]
    assert engine.stats.signals == 0
    pending.release.set()


async def test_every_terminal_path_has_exactly_one_completed_attempt(harness: Harness) -> None:
    """Run the synthetic world for a while with real quotes: each latch window produces one
    completed record, every record has a terminal decision, and signals map 1:1."""
    engine = harness.engine
    for _ in range(600):
        await harness.step(0.5)
    await engine.finalize_entry_attempts()
    await harness.runtime.repo.flush()
    attempts = await harness.runtime.repo.entry_attempts()
    assert attempts, "no candidate qualified in 300 simulated seconds"
    assert all(str(a.final_decision) != "PENDING" for a in attempts)
    assert all(a.completed_at is not None and a.block_reason for a in attempts)
    buy_attempts = [a for a in attempts if str(a.final_decision) == "BUY_SIGNAL"]
    assert len(buy_attempts) == engine.stats.signals
    assert all(a.signal_id for a in buy_attempts)
    assert len({a.attempt_id for a in attempts}) == len(attempts)
    by_mint: dict[str, list[int]] = {}
    for a in attempts:
        by_mint.setdefault(a.mint, []).append(a.attempt_number)
    for numbers in by_mint.values():
        assert sorted(numbers) == list(range(1, len(numbers) + 1))
    assert (await harness.runtime.repo.counts())["entry_attempts"] == len(attempts)


async def test_decimals_unknown_is_retried_and_audited(harness: Harness) -> None:
    """Gecko-discovered tokens carry no decimals; a rate-limited metadata fetch used to leave them
    unknown forever, so no quote was ever requested. Now the fetch is retried and the attempt says
    exactly what blocked it."""
    from solana_sniper.infra.http import RateLimitedError

    engine = harness.engine

    class Throttled:
        name = "rpc"
        calls = 0

        async def get_authorities(self, mint: str) -> None:
            self.calls += 1
            raise RateLimitedError("rate limited", retry_after_s=1.0)

    throttled = Throttled()
    pending = PendingRoundTrip()
    _install(harness, metadata=throttled, round_trip=pending)
    engine.settings.entry.metadata_retry_s = 1.0
    original_on_token = engine.on_token

    async def without_decimals(token: Any) -> None:  # every discovery arrives without decimals
        await original_on_token(replace(token, decimals=None))

    engine.on_token = without_decimals  # type: ignore[method-assign]
    latch = engine.settings.entry.qualification_latch_s
    expired: list[Any] = []
    for _ in range(600):
        await harness.step(0.5)
        if engine.stats.qualified and throttled.calls >= 3:
            attempts = await _attempts(harness)
            expired = [a for a in attempts if str(a.final_decision) == "EXPIRED"]
            if expired:
                break
    assert throttled.calls >= 3, "metadata fetch was not retried after a rate limit"
    assert expired, [
        (a.decimals_status, a.block_reason, str(a.final_decision)) for a in await _attempts(harness)
    ]
    first = expired[0]
    assert "decimals unknown" in (first.block_reason or "")
    assert first.decimals_status == "unknown" and first.quote_attempts == 0
    assert pending.calls == 0 and engine.stats.signals == 0  # no quote without decimals
    assert (first.completed_at - first.qualified_at).total_seconds() >= latch  # type: ignore[operator]
