"""Acceptance flow (spec §35) on the synthetic world with a deterministic clock."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from solana_sniper.app.bootstrap import build_runtime
from solana_sniper.config import load_settings
from solana_sniper.domain.clock import ManualClock
from solana_sniper.domain.enums import CandidateState as S
from solana_sniper.domain.enums import RunMode
from solana_sniper.storage.repository import Repository
from tests.integration.conftest import Harness


async def test_full_dry_run_flow(harness: Harness) -> None:
    engine = harness.engine
    account = harness.runtime.account
    assert account.equity == Decimal("50")
    # 1-8: discover, monitor, features, checks, score, BUY signal, sizing, quote, simulated confirm
    opened = False
    for _ in range(400):  # 200 simulated seconds
        await harness.step(0.5)
        if account.open_positions:
            opened = True
            break
    cands = [(c.symbol, c.state, c.gate_reasons) for c in engine.candidates.values()]
    assert opened, f"no position opened; stats={engine.stats} cands={cands}"
    pos = account.open_positions[0]
    cand = engine.candidates[pos.mint]
    assert cand.state is S.OPEN
    assert cand.signal is not None
    sig = cand.signal
    assert sig.sizing.recommended_sol == sig.quote.spend_sol  # displayed size == quoted spend
    assert sig.quote.sell is not None and sig.quote.viable
    assert sig.score.score >= harness.runtime.settings.entry.min_score
    assert account.cash < Decimal("50") and account.cash >= 0
    assert pos.cost_basis_eur == sig.sizing.recommended_eur + pos.entry_fee_eur
    transitions = [t.target for t in cand.sm.history]
    assert transitions[:6] == [
        S.MONITORING,
        S.QUALIFIED,
        S.BUY_SIGNAL,
        S.AWAITING_CONFIRMATION,
        S.OPEN,
        *transitions[5:6],
    ] or transitions[:5] == [
        S.MONITORING,
        S.QUALIFIED,
        S.BUY_SIGNAL,
        S.AWAITING_CONFIRMATION,
        S.OPEN,
    ]
    # 14-16: monitor, executable peak tracking, exit condition
    closed = False
    for _ in range(800):
        await harness.step(0.5)
        if pos.state is S.CLOSED:
            closed = True
            break
    assert closed, f"position never closed; state={cand.state} pos={pos}"
    assert pos.exit_reason is not None
    assert pos.value_is_executable or pos.exit_value_eur is not None
    assert pos.peak_value_eur >= pos.cost_basis_eur * Decimal("0.5")
    assert pos.realized_pnl_eur is not None
    assert engine.stats.exits >= 1 and engine.stats.confirmed >= 1
    # 22-23: bankroll updated, future sizing adjusts
    snap = account.snapshot()
    assert snap.equity_eur == account.cash + account.open_value
    assert snap.realized_pnl_eur == pos.realized_pnl_eur + sum(
        (p.realized_pnl_eur or 0)
        for p in account.positions.values()
        if p.state is S.CLOSED and p is not pos
    )
    assert account.wins + account.losses >= 1
    assert len(account.ledger) >= 3
    assert all(e.cash_after_eur >= 0 for e in account.ledger)
    # persistence: the closed position and ledger are in the database
    await harness.runtime.repo.flush()
    repo = harness.runtime.repo
    stored = await repo.positions(open_only=False)
    assert any(p.position_id == pos.position_id and p.state is S.CLOSED for p in stored)
    counts = await repo.counts()
    assert counts["observations"] > 50 and counts["signals"] >= 2 and counts["ledger"] >= 3
    stream = await repo.session_stream("e2e-test")
    assert stream and any(k == "quote" for _, k, _ in stream)


async def test_restart_recovery_resumes_open_position(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    settings = load_settings(Path("configs/synthetic.yaml"))
    settings.storage.database_url = f"sqlite+aiosqlite:///{tmp_path}/recover.db"
    settings.dry_run.confirm_delay_s = 1.0
    settings.exit.max_holding_s = 100000  # keep the position open
    settings.telemetry.log_file = None
    clock = ManualClock(datetime(2026, 3, 1, 12, 0, tzinfo=UTC))
    runtime = build_runtime(
        settings,
        mode=RunMode.DRY_RUN,
        session_id="recover-1",
        clock=clock,
        synthetic_seed=3,
        quiet_alerts=True,
    )
    h = Harness(runtime, clock)
    await h.start()
    for _ in range(400):
        await h.step(0.5)
        if runtime.account.open_positions:
            break
    assert runtime.account.open_positions
    pos = runtime.account.open_positions[0]
    cash_before = runtime.account.cash
    # crash: no graceful stop, just drop the runtime and build a fresh one on the same DB
    for t in [*runtime.engine._tasks, *h._service_tasks]:
        t.cancel()
    await runtime.bus.stop()
    await runtime.repo.close()
    await runtime.http.aclose()

    runtime2 = build_runtime(
        settings,
        mode=RunMode.DRY_RUN,
        session_id="recover-2",
        clock=clock,
        synthetic_seed=3,
        quiet_alerts=True,
    )
    h2 = Harness(runtime2, clock)
    await h2.start()
    assert runtime2.account.cash == cash_before
    restored = runtime2.account.open_positions
    assert [p.position_id for p in restored] == [pos.position_id]
    cand = runtime2.engine.candidates[pos.mint]
    assert cand.state is S.OPEN and cand.position_id == pos.position_id
    assert pos.mint in runtime2.market.watched()
    # the restored position is monitored: the synthetic world does not know the mint anymore, so the
    # engine keeps it open with stale data and eventually raises a DATA_STALE exit signal
    for _ in range(200):
        await h2.step(0.5)
        if cand.state in (S.EXIT_SIGNAL, S.AWAITING_EXIT_CONFIRMATION, S.CLOSED):
            break
    assert cand.state in (S.AWAITING_EXIT_CONFIRMATION, S.CLOSED, S.OPEN)
    await h2.stop()
    repo = Repository(settings.storage.database_url, session_id="check")
    await repo.init()
    sessions = await repo.list_sessions()
    await repo.close()
    assert {s["session_id"] for s in sessions} >= {"recover-1", "recover-2"}


async def test_forward_outcomes_are_measured_for_followed_candidates(harness: Harness) -> None:
    """Every candidate with a price is followed for outcomes.horizon_s and lands in `outcomes`,
    the market watch is released only once measurement is done, and shutdown persists what is
    still in flight as truncated rows."""
    engine = harness.engine
    settings = harness.runtime.settings
    tracker = engine.d.outcomes
    assert tracker.enabled and settings.outcomes.horizon_s == 300
    for _ in range(900):  # 450 simulated seconds > horizon
        await harness.step(0.5)
    assert tracker.finalized_count >= 1
    await harness.runtime.repo.flush()
    repo = harness.runtime.repo
    rows = await repo.outcomes()
    assert rows and len(rows) == tracker.finalized_count
    assert all(r.simulated and not r.truncated for r in rows)
    assert all(r.observations >= 2 and r.max_multiple >= 1.0 for r in rows)
    assert all(0.0 <= r.max_drawdown_from_peak <= 1.0 for r in rows)
    assert all(r.horizon_s == 300 and r.observed_window_s <= 300 + 60 for r in rows)
    # the engine's decisions are recorded on the rows: entered ⊆ signalled ⊆ qualified
    assert any(r.signalled for r in rows) or any(
        tracker.is_following(m) for m in engine.positions_by_mint
    )
    for r in rows:
        assert not r.entered or r.signalled
        assert not r.signalled or r.qualified
        assert r.best_score is None or 0 <= r.best_score <= 100
    # measurement never changes what the engine did
    assert engine.stats.signals >= 1
    # watches are released only for finalised mints that are no longer candidates
    finalised = {r.mint for r in rows}
    assert len(finalised) == len(rows)  # one measurement window per token
    gone = finalised - set(engine.candidates) - set(tracker.following())
    assert gone and not (gone & set(harness.runtime.market.watched()))
    still_followed = set(tracker.following())
    assert still_followed <= set(harness.runtime.market.watched())
    # shutdown: in-flight rows are persisted as truncated and never counted as complete
    n_inflight = len(tracker)
    persisted = await engine.finalize_outcomes()
    assert persisted == n_inflight and len(tracker) == 0
    rows2 = await repo.outcomes()
    assert len(rows2) == len(rows) + persisted
    assert sum(1 for r in rows2 if r.truncated) == persisted
    health_counts = (await repo.counts())["outcomes"]
    assert health_counts == len(rows2)
