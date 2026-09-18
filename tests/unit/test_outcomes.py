"""Forward outcome tracking, evaluation summaries, storage round trip and the evaluate command.

Everything here is measurement of recorded rows; nothing asserts or implies a return.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from solana_sniper.cli.main import app
from solana_sniper.config.settings import OutcomesConfig
from solana_sniper.domain.clock import ManualClock
from solana_sniper.domain.enums import MarketDataProvenance
from solana_sniper.domain.models import MarketSnapshot
from solana_sniper.storage.repository import Repository
from solana_sniper.strategy.evaluation import (
    MIN_MEANINGFUL_N,
    summarize,
    wilson_interval,
)
from solana_sniper.strategy.outcomes import Outcome, OutcomeTracker

T0 = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def snap(mint: str, at: datetime, price: str | None, liq: str | None = None) -> MarketSnapshot:
    return MarketSnapshot(
        mint=mint,
        observed_at=at,
        source="test",
        price_native=Decimal(price) if price is not None else None,
        liquidity_usd=Decimal(liq) if liq is not None else None,
    )


def cfg(**over: object) -> OutcomesConfig:
    base: dict[str, object] = {
        "enabled": True,
        "horizon_s": 600.0,
        "silence_timeout_s": 120.0,
        "rug_liquidity_drop_pct": 0.6,
        "max_followed": 100,
    }
    return OutcomesConfig.model_validate({**base, **over})


def make_outcome(**over: object) -> Outcome:
    base = Outcome(
        mint="M",
        symbol="SYM",
        source="test",
        first_seen_at=T0,
        finalized_at=T0 + timedelta(seconds=600),
        horizon_s=600.0,
        observations=20,
        first_price=Decimal("1"),
        max_multiple=1.5,
        time_to_peak_s=30.0,
        max_drawdown_from_peak=0.4,
        final_multiple=0.9,
        qualified=False,
        qualified_multiple=None,
        best_score=50.0,
        signalled=False,
        entered=False,
        closed_pnl_pct=None,
        exit_reason=None,
        liquidity_collapsed=False,
        reject_reason=None,
        simulated=True,
    )
    return replace(base, **over)  # type: ignore[arg-type]


# ----------------------------------------------------------------- tracker


def test_tracker_records_peak_drawdown_final_and_liquidity_collapse() -> None:
    tr = OutcomeTracker(cfg(), simulated=True)
    clock = ManualClock(T0)
    assert tr.start("A", "AAA", "synthetic", snap("A", clock.now(), "1", "10000"), clock.now())
    assert tr.is_following("A") and len(tr) == 1
    clock.advance(10)
    tr.observe(snap("A", clock.now(), "2", "12000"))
    clock.advance(10)
    tr.observe(snap("A", clock.now(), "4", "12000"))
    clock.advance(10)
    tr.observe(snap("A", clock.now(), "1", "3000"))  # 75% liquidity pull after the peak
    assert tr.finalize_due(clock.now()) == []  # horizon not reached, data not silent
    clock.advance(600)
    done = tr.finalize_due(clock.now())
    assert len(done) == 1 and not tr.is_following("A") and tr.finalized_count == 1
    # one window per mint: a measured token is not re-started while the process lives
    assert not tr.start("A", "AAA", "synthetic", snap("A", clock.now(), "1"), clock.now())
    o = done[0]
    assert o.max_multiple == pytest.approx(4.0)
    assert o.time_to_peak_s == pytest.approx(20.0)
    assert o.max_drawdown_from_peak == pytest.approx(0.75)
    assert o.final_multiple == pytest.approx(1.0)
    assert o.observations == 4 and o.liquidity_collapsed and o.simulated and not o.truncated
    assert o.reached(2.0) and o.reached(4.0) and not o.reached(5.0)
    assert o.qualified is False and o.qualified_multiple is None
    assert o.observed_window_s == pytest.approx(630.0)


def test_qualified_multiple_is_relative_to_qualification_price() -> None:
    tr = OutcomeTracker(cfg(), simulated=False)
    clock = ManualClock(T0)
    tr.start("A", None, "s", snap("A", clock.now(), "1"), clock.now())
    tr.note_score("A", 40.0)
    tr.note_score("A", 80.0)
    tr.note_score("A", 60.0)  # best score is kept, not the last one
    clock.advance(5)
    tr.observe(snap("A", clock.now(), "2"))
    tr.note_qualified("A", Decimal("2"), clock.now())
    tr.note_qualified("A", Decimal("9"), clock.now())  # second call is ignored
    clock.advance(5)
    tr.observe(snap("A", clock.now(), "3"))
    tr.note_signal("A")
    tr.note_entered("A")
    tr.note_closed("A", -0.25, "TRAILING")
    tr.note_rejected("A", "first reason")
    tr.note_rejected("A", "second reason")
    (o,) = tr.finalize_all(clock.now())
    assert o.max_multiple == pytest.approx(3.0)
    assert o.qualified and o.qualified_multiple == pytest.approx(1.5)
    assert o.best_score == 80.0 and o.signalled and o.entered
    assert o.closed_pnl_pct == -0.25 and o.exit_reason == "TRAILING"
    assert o.reject_reason == "first reason" and not o.simulated
    assert o.truncated  # horizon had not elapsed at finalize_all


def test_silence_finalizes_early_and_late_snapshots_are_ignored() -> None:
    tr = OutcomeTracker(cfg(horizon_s=600.0, silence_timeout_s=60.0), simulated=True)
    clock = ManualClock(T0)
    tr.start("A", None, "s", snap("A", clock.now(), "1"), clock.now())
    clock.advance(30)
    tr.observe(snap("A", clock.now(), "5"))
    clock.advance(61)
    done = tr.finalize_due(clock.now())
    assert len(done) == 1 and done[0].max_multiple == 5.0 and not done[0].truncated
    # a token whose horizon passed ignores later observations
    tr.start("B", None, "s", snap("B", clock.now(), "1"), clock.now())
    tr.observe(snap("B", clock.now() + timedelta(seconds=601), "100"))
    tr.observe(snap("B", clock.now(), None))  # priceless: ignored
    clock.advance(601)
    (b,) = tr.finalize_due(clock.now())
    assert b.observations == 1 and b.max_multiple == 1.0


def test_capacity_cap_disabled_flag_and_unknown_mints() -> None:
    tr = OutcomeTracker(cfg(max_followed=1), simulated=True)
    clock = ManualClock(T0)
    assert tr.start("A", None, "s", snap("A", clock.now(), "1"), clock.now())
    assert not tr.start("A", None, "s", snap("A", clock.now(), "1"), clock.now())  # duplicate
    assert not tr.start("B", None, "s", snap("B", clock.now(), "1"), clock.now())  # full
    assert tr.skipped_full == 1 and tr.following() == ["A"]
    assert not tr.start("C", None, "s", snap("C", clock.now(), None), clock.now())  # no price
    assert not tr.start("D", None, "s", snap("D", clock.now(), "0"), clock.now())
    # notes about mints that are not followed never raise
    tr.note_score("Z", 1.0)
    tr.note_qualified("Z", Decimal(1), clock.now())
    tr.note_signal("Z")
    tr.note_entered("Z")
    tr.note_closed("Z", 0.0, None)
    tr.note_rejected("Z", "x")
    tr.observe(snap("Z", clock.now(), "1"))
    off = OutcomeTracker(cfg(enabled=False), simulated=True)
    assert not off.enabled and not off.start("A", None, "s", snap("A", T0, "1"), T0)
    assert off.finalize_all(T0) == []


# -------------------------------------------------------------- evaluation


def test_wilson_interval_is_bounded_and_symmetric_at_half() -> None:
    assert wilson_interval(0, 0) == (0.0, 1.0)
    lo, hi = wilson_interval(5, 10)
    assert 0.23 < lo < 0.24 and 0.76 < hi < 0.77
    lo0, hi0 = wilson_interval(0, 10)
    assert lo0 == 0.0 and 0.27 < hi0 < 0.29
    lo1, hi1 = wilson_interval(10, 10)
    assert 0.72 < lo1 < 0.73 and hi1 == 1.0


def test_summarize_groups_excludes_truncated_and_short_rows() -> None:
    rows = [
        make_outcome(mint="a", best_score=95.0, max_multiple=12.0, qualified=True, signalled=True),
        make_outcome(
            mint="b",
            best_score=80.0,
            max_multiple=2.5,
            qualified=True,
            entered=True,
            signalled=True,
            closed_pnl_pct=0.3,
        ),
        make_outcome(
            mint="c",
            best_score=65.0,
            max_multiple=1.1,
            reject_reason="mint authority",
            liquidity_collapsed=True,
        ),
        make_outcome(mint="d", best_score=30.0, max_multiple=6.0),
        make_outcome(mint="e", best_score=None, max_multiple=1.0),
        make_outcome(mint="f", best_score=99.0, max_multiple=50.0, truncated=True),
        make_outcome(mint="g", best_score=99.0, max_multiple=50.0, observations=2),
        make_outcome(mint="h", best_score=50.0, max_multiple=1.0, simulated=False),
    ]
    rep = summarize(rows, min_observations=5)
    assert rep.total_rows == 8 and rep.used_rows == 6
    assert rep.excluded_truncated == 1 and rep.excluded_short == 1
    assert rep.simulated_rows == 5 and rep.live_rows == 1
    assert rep.legacy_rows == 6 and not rep.mixed_provenance  # all rows predate provenance
    assert rep.horizon_s == 600.0
    by = {b.name: b for b in rep.buckets}
    assert by["all followed"].n == 6
    assert by["all followed"].reached[2.0] == 3 and by["all followed"].reached[10.0] == 1
    assert by["all followed"].rate(5.0) == pytest.approx(2 / 6)
    assert by["rejected by checks"].n == 1 and by["rejected by checks"].rug_rate == 1.0
    assert by["never qualified"].n == 3  # d, e, h
    assert by["qualified"].n == 2 and by["BUY signalled"].n == 2
    assert by["entered (position opened)"].n == 1
    assert by["entered (position opened)"].median_closed_pnl_pct == pytest.approx(0.3)
    assert by["unscored"].n == 1
    assert by["score 90+"].n == 1 and by["score 75-89"].n == 1 and by["score 60-74"].n == 1
    assert by["score 40-59"].n == 1 and by["score <40"].n == 1
    assert not by["all followed"].meaningful and MIN_MEANINGFUL_N == 30
    lo, hi = by["all followed"].interval(2.0)
    assert 0.0 <= lo < 0.5 < hi <= 1.0
    # including truncated rows brings f back; the all-simulated flag needs no live rows
    rep2 = summarize(rows[:6], include_truncated=True, min_observations=1)
    assert rep2.used_rows == 6 and rep2.excluded_truncated == 0 and rep2.all_simulated
    assert summarize([]).used_rows == 0 and summarize([]).horizon_s is None
    # market-data provenance is separate from execution provenance
    paper = [
        make_outcome(mint=f"p{i}", market_data=MarketDataProvenance.LIVE, simulated=True)
        for i in range(3)
    ]
    synthetic = [make_outcome(mint="s1", market_data=MarketDataProvenance.SYNTHETIC)]
    rep3 = summarize(paper)
    assert rep3.live_market_rows == 3 and rep3.simulated_rows == 3 and not rep3.all_synthetic
    rep4 = summarize(synthetic)
    assert rep4.all_synthetic and rep4.synthetic_rows == 1
    assert summarize(paper + synthetic).mixed_provenance


# ------------------------------------------------------------------ storage


async def test_outcomes_round_trip_through_sqlite(tmp_path: Path) -> None:
    repo = Repository(f"sqlite+aiosqlite:///{tmp_path}/o.db", session_id="s1")
    await repo.init()
    repo.start()
    a = make_outcome(mint="a", first_price=Decimal("0.000001234"), qualified_multiple=1.25)
    b = make_outcome(mint="b", truncated=True, first_seen_at=T0 + timedelta(seconds=1))
    await repo.save_outcomes_now([a, b])
    rows = await repo.outcomes()
    assert rows == [a, b]
    assert isinstance(rows[0].first_price, Decimal) and rows[0].first_seen_at.tzinfo is not None
    assert (await repo.counts())["outcomes"] == 2
    assert await repo.outcomes(session_id="other") == []
    assert len(await repo.outcomes(session_id="s1")) == 2
    await repo.close()


# ---------------------------------------------------------------------- CLI


def _config_for(tmp_path: Path, db: str) -> Path:
    path = tmp_path / "eval.yaml"
    path.write_text(f"storage:\n  database_url: sqlite+aiosqlite:///{tmp_path}/{db}\n")
    return path


def test_evaluate_command_reports_rates_with_caveats(tmp_path: Path) -> None:
    config = _config_for(tmp_path, "eval.db")

    async def seed() -> None:
        repo = Repository(f"sqlite+aiosqlite:///{tmp_path}/eval.db", session_id="dry-1")
        await repo.init()
        repo.start()
        rows = [
            make_outcome(
                mint=f"m{i}",
                best_score=90.0 + i,
                max_multiple=3.0 if i % 2 else 1.2,
                qualified=True,
                signalled=i < 2,
                entered=i == 0,
                closed_pnl_pct=0.1 if i == 0 else None,
                market_data=MarketDataProvenance.LIVE,
            )
            for i in range(4)
        ]
        rows.append(make_outcome(mint="trunc", truncated=True, max_multiple=99.0))
        await repo.save_outcomes_now(rows)
        await repo.close()

    asyncio.run(seed())
    runner = CliRunner(env={"COLUMNS": "200"})
    res = runner.invoke(app, ["evaluate", "-c", str(config)])
    assert res.exit_code == 0, res.output
    out = res.output
    assert "forward outcomes: 4 tokens" in out and "horizon 600s" in out
    assert "all followed" in out and "score 90+" in out and "entered" in out
    assert "1 truncated at shutdown" in out
    flat = " ".join(out.split())  # prose lines wrap at the terminal width; never assert on that
    assert "Market data: 4 live Solana" in flat and "4 simulated (paper/dry-run)" in flat
    assert "Wilson" in flat and "do not predict" in flat
    assert "live Solana market observations with simulated execution" in flat
    assert "synthetic world" not in flat  # live paper rows are never called synthetic
    assert "99" not in out.split("excluded")[0]  # the truncated 99x row never enters the table
    # a session filter that matches nothing explains itself instead of printing a table
    res2 = runner.invoke(app, ["evaluate", "-c", str(config), "--session", "nope"])
    assert res2.exit_code == 0, res2.output
    assert "no usable outcomes" in res2.output
    res3 = runner.invoke(app, ["evaluate", "-c", str(config), "--include-truncated"])
    assert res3.exit_code == 0 and "forward outcomes: 5 tokens" in res3.output


def test_evaluate_command_on_empty_database(tmp_path: Path) -> None:
    config = _config_for(tmp_path, "empty.db")
    res = CliRunner(env={"COLUMNS": "200"}).invoke(app, ["evaluate", "-c", str(config)])
    assert res.exit_code == 0, res.output
    assert "no usable outcomes (0 rows recorded" in res.output


def test_evaluate_flags_sessions_with_incomplete_data(tmp_path: Path) -> None:
    config = _config_for(tmp_path, "incomplete.db")

    async def seed() -> None:
        repo = Repository(
            f"sqlite+aiosqlite:///{tmp_path}/incomplete.db",
            session_id="holey",
            max_queued_telemetry=1,
        )
        await repo.init()
        repo.start()
        await repo.start_session("PAPER", None)
        from solana_sniper.domain.models import ErrorRecord

        for i in range(3):
            repo.save_error(ErrorRecord(at=T0, component="t", message=str(i)))
        await repo.save_outcomes_now([make_outcome(mint=f"m{i}") for i in range(6)])
        await repo.end_session()
        await repo.close()

    asyncio.run(seed())
    res = CliRunner(env={"COLUMNS": "200"}).invoke(app, ["evaluate", "-c", str(config)])
    assert res.exit_code == 0, res.output
    assert "INCOMPLETE DATA" in res.output and "holey" in res.output
    assert "forward outcomes: 6 tokens" in res.output
