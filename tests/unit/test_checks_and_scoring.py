from __future__ import annotations

from decimal import Decimal

from solana_sniper.config.settings import EntryConfig, FiltersConfig
from solana_sniper.domain.clock import ManualClock
from solana_sniper.domain.enums import CheckVerdict
from solana_sniper.domain.models import HolderDistribution, MarketSnapshot, TokenAuthorities
from solana_sniper.features.engine import FeatureEngine
from solana_sniper.filters.checks import TokenChecker
from solana_sniper.market_data.tracker import TokenTrack, TokenTracker
from solana_sniper.strategy.gate import EntryGate
from solana_sniper.strategy.scoring import EntryScorer
from tests.unit.helpers import clean_authorities, feed_path, make_round_trip, make_token


def good_track(clock: ManualClock, *, age_s: float = 120.0) -> TokenTrack:
    tracker = TokenTracker()
    tracker.track(make_token(age_s=age_s, clock=clock))
    prices = [str(Decimal("1.00") * (Decimal("1.03") ** i)) for i in range(13)]
    track = feed_path(
        tracker,
        clock,
        "MintTest",
        prices,
        liquidity=[str(15000 + 800 * i) for i in range(13)],
        unique=[10 + 2 * i for i in range(13)],
        trades_per_step=4,
    )
    track.authorities = clean_authorities()
    track.holders = HolderDistribution(
        holder_count=50, top10_pct=0.2, largest_pct=0.05, observed_at=clock.now()
    )
    return track


def verdicts(report: object) -> dict[str, CheckVerdict]:
    return {r.name: r.verdict for r in report.results}  # type: ignore[attr-defined]


def test_clean_token_passes_all_checks(clock: ManualClock) -> None:
    track = good_track(clock)
    features = FeatureEngine(20).compute(track, clock.now())
    checker = TokenChecker(FiltersConfig(min_token_age_s=10))
    rt = make_round_trip("MintTest", clock)
    report = checker.evaluate(track, features, clock.now(), rt)
    assert report.verdict is CheckVerdict.PASS, report.summary()
    assert not report.is_fatal
    assert checker.qualifies(report) == (True, "ok")
    assert report.summary() == "all checks passed"


def test_fatal_authority_and_liquidity_drop(clock: ManualClock) -> None:
    track = good_track(clock)
    track.authorities = TokenAuthorities(
        mint_authority="X",
        freeze_authority="Y",
        decimals=6,
        supply_raw=1,
        transfer_fee_bps=500,
        has_transfer_hook=True,
        non_transferable=True,
        permanent_delegate="D",
    )
    features = FeatureEngine(20).compute(track, clock.now())
    report = TokenChecker(FiltersConfig()).evaluate(track, features, clock.now())
    names = {r.name for r in report.fatal_rejections}
    assert {
        "mint_authority",
        "freeze_authority",
        "transfer_fee",
        "transfer_hook",
        "transferable",
        "permanent_delegate",
    } <= names
    # liquidity pulled
    track2 = good_track(clock)
    clock.advance(5)
    track2.add_snapshot(
        MarketSnapshot(
            mint="MintTest",
            observed_at=clock.now(),
            source="test",
            price_native=Decimal("1"),
            liquidity_usd=Decimal("500"),
        )
    )
    features2 = FeatureEngine(20).compute(track2, clock.now())
    report2 = TokenChecker(FiltersConfig()).evaluate(track2, features2, clock.now())
    assert any(r.name == "liquidity_stability" and r.fatal for r in report2.fatal_rejections)
    assert report2.is_fatal


def test_non_fatal_rejects_and_unknown_policy(clock: ManualClock) -> None:
    track = good_track(clock, age_s=5)  # ~65s old after the feed advances the clock
    track.authorities = None
    track.holders = None
    features = FeatureEngine(20).compute(track, clock.now())
    checker = TokenChecker(FiltersConfig(min_token_age_s=100, unknown_policy="reject"))
    report = checker.evaluate(track, features, clock.now())
    v = verdicts(report)
    assert v["token_age"] is CheckVerdict.REJECT and not report.is_fatal
    assert v["authorities"] is CheckVerdict.UNKNOWN
    ok, why = checker.qualifies(report)
    assert not ok and "too young" in why
    # only unknowns left -> policy decides
    track_ok = good_track(clock)
    track_ok.authorities = None
    f_ok = FeatureEngine(20).compute(track_ok, clock.now())
    strict = TokenChecker(FiltersConfig(unknown_policy="reject", min_token_age_s=10))
    lenient = TokenChecker(
        FiltersConfig(unknown_policy="allow", min_token_age_s=10, max_unknown_checks=1)
    )
    zero_tolerance = TokenChecker(
        FiltersConfig(unknown_policy="allow", min_token_age_s=10, max_unknown_checks=0)
    )
    assert strict.qualifies(strict.evaluate(track_ok, f_ok, clock.now()))[0] is False
    assert lenient.qualifies(lenient.evaluate(track_ok, f_ok, clock.now()))[0] is True
    assert (
        zero_tolerance.qualifies(zero_tolerance.evaluate(track_ok, f_ok, clock.now()))[0] is False
    )


def test_quote_checks(clock: ManualClock) -> None:
    track = good_track(clock)
    features = FeatureEngine(20).compute(track, clock.now())
    checker = TokenChecker(FiltersConfig(min_token_age_s=10))
    bad = checker.evaluate(
        track, features, clock.now(), make_round_trip("MintTest", clock, loss_pct=0.6)
    )
    assert any(r.name == "round_trip" and r.fatal for r in bad.fatal_rejections)
    soft = checker.evaluate(
        track, features, clock.now(), make_round_trip("MintTest", clock, loss_pct=0.3)
    )
    assert verdicts(soft)["round_trip"] is CheckVerdict.REJECT and not soft.is_fatal
    no_sell = checker.evaluate(
        track, features, clock.now(), make_round_trip("MintTest", clock, sell_ok=False)
    )
    assert verdicts(no_sell)["exit_quote"] is CheckVerdict.REJECT
    slippy = checker.evaluate(
        track,
        features,
        clock.now(),
        make_round_trip("MintTest", clock, entry_bps=900, exit_bps=1300),
    )
    v = verdicts(slippy)
    assert v["entry_slippage"] is CheckVerdict.REJECT and v["exit_slippage"] is CheckVerdict.REJECT


def test_score_range_and_ordering(clock: ManualClock) -> None:
    scorer = EntryScorer(EntryConfig(), FiltersConfig())
    checker = TokenChecker(FiltersConfig(min_token_age_s=10))
    good = good_track(clock)
    f_good = FeatureEngine(20).compute(good, clock.now())
    rt = make_round_trip("MintTest", clock)
    report = checker.evaluate(good, f_good, clock.now(), rt)
    s_good = scorer.score(f_good, report, clock.now(), rt)
    assert 0 <= s_good.score <= 100
    assert s_good.score > 60, s_good
    assert any("momentum" in r for r in s_good.reasons)
    assert any("liquidity growing" in r for r in s_good.reasons)
    # dumping token scores lower
    tracker = TokenTracker()
    tracker.track(make_token(age_s=120, clock=clock))
    prices = [str(Decimal("2.00") * (Decimal("0.95") ** i)) for i in range(13)]
    dump = feed_path(
        tracker,
        clock,
        "MintTest",
        prices,
        liquidity=[str(20000 - 700 * i) for i in range(13)],
        trades_per_step=1,
    )
    dump.authorities = clean_authorities()
    f_dump = FeatureEngine(20).compute(dump, clock.now())
    s_dump = scorer.score(f_dump, checker.evaluate(dump, f_dump, clock.now(), rt), clock.now(), rt)
    assert s_dump.score < s_good.score
    assert any("negative momentum" in p for p in s_dump.penalties)
    assert any("below local peak" in p for p in s_dump.penalties)
    # stale -> zero
    clock.advance(60)
    f_stale = FeatureEngine(20).compute(good, clock.now())
    s_stale = scorer.score(f_stale, None, clock.now(), None)
    assert s_stale.score == 0 and "data stale" in s_stale.penalties
    # fatal check -> zero
    good.authorities = TokenAuthorities("X", None, 6, 1)
    clock.advance(-0)
    report_fatal = checker.evaluate(good, f_good, clock.now(), rt)
    assert scorer.score(f_good, report_fatal, clock.now(), rt).score == 0


def test_gate(clock: ManualClock) -> None:
    filters = FiltersConfig(min_token_age_s=10)
    checker = TokenChecker(filters)
    scorer = EntryScorer(EntryConfig(min_score=50), filters)
    gate = EntryGate(EntryConfig(min_score=50, min_observations=4), checker)
    good = good_track(clock)
    f = FeatureEngine(20).compute(good, clock.now())
    rt = make_round_trip("MintTest", clock)
    report = checker.evaluate(good, f, clock.now(), rt)
    score = scorer.score(f, report, clock.now(), rt)
    decision = gate.decide(f, report, score)
    assert decision.qualified, decision.reasons
    strict_gate = EntryGate(
        EntryConfig(min_score=99, min_observations=100, min_trade_velocity_per_min=1000), checker
    )
    d2 = strict_gate.decide(f, report, score)
    assert not d2.qualified and not d2.fatal and len(d2.reasons) == 3
    good.authorities = TokenAuthorities("X", None, 6, 1)
    fatal_report = checker.evaluate(good, f, clock.now(), rt)
    d3 = gate.decide(f, fatal_report, score)
    assert d3.fatal and not d3.qualified
