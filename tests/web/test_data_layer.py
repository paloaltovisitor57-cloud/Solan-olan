"""The read-only repository: never writes, never migrates, survives a concurrent writer,
retries SQLITE_BUSY, scrubs secrets and bounds every result set."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from solana_sniper.domain.models import ErrorRecord, PortfolioSnapshot
from solana_sniper.storage.models import Base
from solana_sniper.storage.repository import Repository
from solana_sniper.telemetry.redaction import register_secret, registry
from solana_sniper.web import data as data_module
from solana_sniper.web.data import (
    BUSY_MESSAGE,
    MIGRATION_HINT,
    DashboardRepository,
    DatabaseBusyError,
    DatabaseMissingError,
    SchemaUnsupportedError,
    read_only_uri,
)
from solana_sniper.web.session_discovery import discover_sessions, read_heartbeat
from tests.web.conftest import LIVE_SID, PAPER_LIVE, SECRET, SeededHome
from tests.web.seed import MINT_A, MINT_B, seed_paper


def _ref(seeded: SeededHome, sid: str = PAPER_LIVE):  # type: ignore[no-untyped-def]
    refs = discover_sessions(seeded.home, heartbeat=read_heartbeat(seeded.home))
    return next(r for r in refs if r.session_id == sid)


def _walk_everything(repo: DashboardRepository, ref, hb) -> None:  # type: ignore[no-untyped-def]
    repo.schema()
    repo.summary(ref, hb)
    repo.portfolio_history(50)
    repo.positions(open_only=True)
    repo.positions(open_only=False)
    repo.recent_candidates()
    repo.entry_attempts()
    repo.entry_decision_counts()
    repo.signals()
    repo.fills()
    repo.provider_health(hb)
    repo.engine_health(hb)
    repo.recent_errors()
    repo.recent_transitions()
    repo.events()
    repo.search_tokens("BONK")
    repo.token_detail(MINT_B)
    repo.outcome_summary()


def _fingerprint(db: Path) -> tuple[str, int, int]:
    wal = db.with_name(db.name + "-wal")
    digest = hashlib.sha256(db.read_bytes()).hexdigest()
    wal_size = wal.stat().st_size if wal.exists() else 0
    with sqlite3.connect(read_only_uri(db), uri=True) as c:
        version = int(c.execute("SELECT MAX(version) FROM schema_version").fetchone()[0])
        rows = int(c.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0])
    return digest, wal_size, version * 1000 + rows


def test_dashboard_never_writes_to_the_database(seeded_home: SeededHome) -> None:
    db = seeded_home.paper_live_db
    before = _fingerprint(db)
    ref = _ref(seeded_home)
    hb = read_heartbeat(seeded_home.home)
    repo = DashboardRepository(db, PAPER_LIVE)
    _walk_everything(repo, ref, hb)
    assert _fingerprint(db) == before
    # the connection itself refuses writes even if someone tried
    conn = data_module._connect(db, 1.0)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute(
                "INSERT INTO errors(session_id, at, component, message, detail) "
                "VALUES ('x', '2026-01-01 00:00:00', 'c', 'm', 'd')"
            )
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE should_not_exist (x INTEGER)")
    finally:
        conn.close()
    assert _fingerprint(db) == before


async def _old_schema_db(path: Path) -> None:
    """A database the engine would migrate: schema version 3 without the v4 outcome columns
    and without the v5 execution_intents table."""
    repo = Repository(f"sqlite+aiosqlite:///{path}", session_id="old")
    await repo.init()
    await repo.close()
    with sqlite3.connect(path) as c:
        c.execute("DELETE FROM schema_version WHERE version >= 4")
        c.execute("ALTER TABLE outcomes DROP COLUMN market_provenance")
        c.execute("ALTER TABLE outcomes DROP COLUMN execution_provenance")
        c.execute("DROP TABLE IF EXISTS execution_intents")
        c.commit()


def test_dashboard_never_migrates_an_old_database(tmp_path: Path) -> None:
    old = tmp_path / "old.db"
    asyncio.run(_old_schema_db(old))
    before = _fingerprint(old)
    assert before[2] // 1000 == 3
    repo = DashboardRepository(old, "old")
    status = repo.schema()
    assert not status.supported and status.version == 3
    assert "solana-sniper migrate" in status.message and MIGRATION_HINT in status.message
    with pytest.raises(SchemaUnsupportedError, match="solana-sniper migrate"):
        repo.positions()
    with pytest.raises(SchemaUnsupportedError):
        repo.outcome_summary()
    assert _fingerprint(old) == before  # still version 3, nothing applied
    # a database missing the dashboard's tables (schema_version absent) is reported, not fixed
    bare = tmp_path / "bare.db"
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{bare}")
    Base.metadata.create_all(engine, tables=[Base.metadata.tables["sessions"]])
    engine.dispose()
    bare_repo = DashboardRepository(bare, "x")
    st = bare_repo.schema()
    assert not st.supported and "missing tables" in st.message
    with pytest.raises(SchemaUnsupportedError, match="missing tables"):
        bare_repo.entry_attempts()
    with sqlite3.connect(bare) as c:
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert names == {"sessions"}


def test_missing_database_is_a_clean_error(tmp_path: Path) -> None:
    repo = DashboardRepository(tmp_path / "nope.db", "x")
    with pytest.raises(DatabaseMissingError):
        repo.schema()


def test_sqlite_busy_is_retried_then_reported(
    seeded_home: SeededHome, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_connect = data_module._connect
    calls: list[int] = []

    def flaky(path: Path, timeout: float) -> sqlite3.Connection:
        calls.append(1)
        if len(calls) <= 2:
            raise sqlite3.OperationalError("database is locked")
        return real_connect(path, timeout)

    monkeypatch.setattr(data_module, "_connect", flaky)
    slept: list[float] = []
    repo = DashboardRepository(seeded_home.paper_live_db, PAPER_LIVE, retries=3, sleep=slept.append)
    assert repo.entry_decision_counts() == {"ABANDONED": 1, "BUY_SIGNAL": 1, "QUOTE_FAILED": 1}
    # schema probe: two failures, two short waits, success; then the query itself
    assert len(calls) == 4 and len(slept) == 2

    def always_busy(path: Path, timeout: float) -> sqlite3.Connection:
        raise sqlite3.OperationalError("database table is locked")

    monkeypatch.setattr(data_module, "_connect", always_busy)
    repo2 = DashboardRepository(
        seeded_home.paper_live_db, PAPER_LIVE, retries=2, sleep=slept.append
    )
    with pytest.raises(DatabaseBusyError) as exc:
        repo2.fills()
    assert str(exc.value) == BUSY_MESSAGE == "Database busy — retrying"


def test_concurrent_writer_and_dashboard_reader_do_not_crash(seeded_home: SeededHome) -> None:
    db = seeded_home.paper_live_db
    stop = threading.Event()
    written: list[int] = []
    failures: list[BaseException] = []

    async def writer() -> None:
        repo = Repository(f"sqlite+aiosqlite:///{db}", session_id=PAPER_LIVE, flush_interval_s=0.02)
        await repo.init()
        repo.start()
        try:
            i = 0
            while not stop.is_set():
                i += 1
                at = datetime.now(tz=UTC) + timedelta(seconds=i)
                repo.save_portfolio_snapshot(
                    PortfolioSnapshot(
                        at=at,
                        cash_eur=Decimal("140"),
                        open_exposure_eur=Decimal("12.5"),
                        open_value_eur=Decimal("13"),
                        equity_eur=Decimal("153") + Decimal(i) / 100,
                        peak_equity_eur=Decimal("155"),
                        drawdown_pct=0.0,
                        realized_pnl_eur=Decimal("1.7"),
                        unrealized_pnl_eur=Decimal("1.3"),
                        fees_eur=Decimal("0.04"),
                        slippage_eur=Decimal("0.2"),
                        open_positions=1,
                        wins=1,
                        losses=0,
                        session_id=PAPER_LIVE,
                    )
                )
                repo.save_error(
                    ErrorRecord(at=at, component="engine", message=f"e{i}", session_id=PAPER_LIVE)
                )
                repo.save_transition(MINT_A, "OPEN", "OPEN", at, f"tick {i}")
                if i % 20 == 0:
                    await repo.flush()
                    written.append(i)
                await asyncio.sleep(0.005)
            await repo.flush()
            written.append(i)
        finally:
            await repo.close()

    def run_writer() -> None:
        try:
            asyncio.run(writer())
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=run_writer, daemon=True)
    thread.start()
    ref = _ref(seeded_home)
    hb = read_heartbeat(seeded_home.home)
    repo = DashboardRepository(db, PAPER_LIVE, busy_timeout_s=2.0)
    reads = 0
    busy = 0
    deadline = time.monotonic() + 2.5
    while time.monotonic() < deadline:
        try:
            _walk_everything(repo, ref, hb)
            reads += 1
        except DatabaseBusyError:
            busy += 1
    stop.set()
    thread.join(timeout=15)
    assert not thread.is_alive() and not failures, failures
    assert reads >= 3, (reads, busy)
    assert written and written[-1] >= 40
    # the reader saw the writer's rows and the file is intact
    hist = repo.portfolio_history(10_000)
    assert hist.total_points >= 12 + written[-1] - 20
    assert repo.summary(ref, hb).errors >= 2 + written[-1] - 20


def test_secrets_are_scrubbed_from_every_text_the_dashboard_returns(
    seeded_home: SeededHome,
) -> None:
    register_secret(SECRET)
    try:
        ref = _ref(seeded_home)
        hb = read_heartbeat(seeded_home.home)
        repo = DashboardRepository(seeded_home.paper_live_db, PAPER_LIVE)
        errors = repo.recent_errors()
        assert errors and all(SECRET not in e.message and SECRET not in e.detail for e in errors)
        assert any("***" in e.message for e in errors)
        events = repo.events()
        assert all(SECRET not in e.message and SECRET not in e.detail for e in events)
        providers = repo.provider_health(hb)
        gecko = next(p for p in providers if p.name == "geckoterminal")
        assert gecko.last_error is not None and SECRET not in gecko.last_error
        eh = repo.engine_health(hb)
        assert eh.last_error is not None and SECRET not in eh.last_error
        summary = repo.summary(ref, hb)
        assert SECRET not in repr(summary.integrity)
        cands = repo.recent_candidates()
        assert all(SECRET not in c.gate_reason and SECRET not in c.state_reason for c in cands)
        # exceptions surfaced to the page are scrubbed too
        with pytest.raises(DatabaseMissingError) as exc:
            DashboardRepository(seeded_home.home / "db" / f"x-{SECRET}.db", "x").schema()
        assert SECRET not in str(exc.value)
    finally:
        registry.clear()


def test_large_datasets_are_bounded(isolated_runtime_home: Path) -> None:
    sid = "paper-20260918-090000-big"
    db = asyncio.run(seed_paper(isolated_runtime_home, sid, snapshots=3000))

    async def more() -> None:
        repo = Repository(f"sqlite+aiosqlite:///{db}", session_id=sid)
        await repo.init()
        repo.start()
        try:
            base = datetime(2026, 9, 18, 13, 0, tzinfo=UTC)
            for i in range(1500):
                repo.save_transition(
                    f"Mint{i:05d}" + "x" * 30,
                    "DISCOVERED",
                    "MONITORING",
                    base + timedelta(seconds=i),
                    "n",
                )
                repo.save_error(
                    ErrorRecord(at=base + timedelta(seconds=i), component="c", message=f"m{i}")
                )
            await repo.flush()
        finally:
            await repo.close()

    asyncio.run(more())
    repo = DashboardRepository(db, sid)
    hist = repo.portfolio_history(600)
    assert hist.total_points == 3000 and hist.sampled
    assert 2 <= len(hist.points) <= 602
    assert hist.points[0].at < hist.points[-1].at
    assert hist.max_drawdown_pct is not None and hist.max_drawdown_pct > 0
    assert len(repo.recent_transitions(300)) == 300
    assert len(repo.recent_errors(200)) == 200
    assert len(repo.events(limit=250)) == 250
    assert len(repo.recent_candidates(limit=50)) == 50
    assert len(repo.entry_attempts(limit=2)) == 2
    detail = repo.token_detail(MINT_A, limit=4)
    assert len(detail.scores) <= 4 and len(detail.transitions) <= 4


def test_summary_paper_return_uses_the_recorded_bankroll(seeded_home: SeededHome) -> None:
    ref = _ref(seeded_home)
    hb = read_heartbeat(seeded_home.home)
    s = DashboardRepository(seeded_home.paper_live_db, PAPER_LIVE).summary(ref, hb)
    assert s.paper is not None and s.paper.bankroll_eur == Decimal("150")
    assert s.starting_equity_eur == Decimal("150")
    assert s.equity_eur == Decimal("152.75") and s.return_pct == pytest.approx(0.018333, rel=1e-4)
    assert s.alive == "RUNNING" and s.engine_state == "HEALTHY"
    assert s.provenance.lines() == (
        "PAPER",
        "MARKET DATA: LIVE",
        "EXECUTION: SIMULATED",
        "REAL TRANSACTIONS: DISABLED",
    )
    assert s.open_positions == 1 and s.positions_total == 2 and s.fills == 2 and s.signals == 2
    assert s.latest_attempt is not None and s.latest_attempt.symbol == "BONKCAT"
    live_ref = _ref(seeded_home, LIVE_SID)
    live = DashboardRepository(seeded_home.live_db, LIVE_SID).summary(live_ref, hb)
    assert live.alive == "ENDED" and live.paper is None
    assert live.starting_equity_eur == Decimal("150")  # first snapshot of the session
    assert live.provenance.lines()[0] == "LIVE / SIGNAL MODE"
    assert live.provenance.execution == "MANUAL SIGNAL / ESTIMATED / USER-REPORTED"
    assert live.provenance.real_transactions == "NOT RECONCILED ON-CHAIN"
    assert {f.provenance for f in DashboardRepository(seeded_home.live_db, LIVE_SID).fills()} == {
        "ESTIMATED"
    }


def test_read_only_uri_and_wal_compatibility(seeded_home: SeededHome) -> None:
    db = seeded_home.paper_live_db
    assert read_only_uri(db).startswith("file://") and read_only_uri(db).endswith("?mode=ro")
    with sqlite3.connect(db) as c:
        assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    conn = data_module._connect(db, 1.0)
    try:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        assert conn.in_transaction is False
        conn.execute("SELECT COUNT(*) FROM tokens").fetchone()
        assert conn.in_transaction is False  # plain SELECTs leave no transaction open
    finally:
        conn.close()
