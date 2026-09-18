"""Regressions from the first real-network run: duplicate mints must never be an error, and the
storage layer must never lose research data silently.

Every test here uses a temporary SQLite file and fake data; nothing touches the network.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from solana_sniper.domain.clock import ManualClock
from solana_sniper.domain.enums import SignalKind
from solana_sniper.domain.models import ErrorRecord, MarketSnapshot
from solana_sniper.portfolio.accounting import PortfolioAccount
from solana_sniper.storage.models import TokenRow
from solana_sniper.storage.repository import Repository
from tests.conftest import make_fill
from tests.unit.helpers import make_token


async def _token_rows(repo: Repository) -> list[TokenRow]:
    async with repo._sessions() as s:
        return list((await s.execute(select(TokenRow).order_by(TokenRow.mint))).scalars().all())


def _repo(tmp_path: Path, session_id: str = "s1", **kw: object) -> Repository:
    return Repository(f"sqlite+aiosqlite:///{tmp_path}/idem.db", session_id=session_id, **kw)  # type: ignore[arg-type]


# --------------------------------------------------------------- defect 1


async def test_duplicate_mint_in_one_batch_from_two_providers(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    await repo.init()
    repo.start()
    first = make_token("MintDup")
    second = replace(first, source="geckoterminal", symbol="DUP2", name=None, decimals=None)
    repo.save_token(first)
    repo.save_token(second)
    repo.save_token(first)  # repeated identical event
    await repo.flush()
    rows = await _token_rows(repo)
    assert repo.failures == 0 and repo.dropped == 0 and repo.last_flush_error is None
    assert len(rows) == 1 and rows[0].mint == "MintDup"
    await repo.close()


async def test_concurrent_duplicate_persistence_is_idempotent(tmp_path: Path) -> None:
    """A background batch holding the mint is mid-commit while another write path (a synchronous
    save) persists the same mint: this raised UNIQUE constraint failed on the real machine."""
    repo = _repo(tmp_path, flush_interval_s=0.5)
    await repo.init()
    repo.start()
    token = make_token("MintConc")
    repo.save_token(token)
    for _ in range(400):
        await asyncio.sleep(0.005)
        if repo._inflight is not None and not repo._inflight.done():
            break
    assert repo._inflight is not None and not repo._inflight.done(), "writer batch not in flight"
    await repo.save_token_now(replace(token, source="dexscreener"))
    await repo.flush()
    rows = await _token_rows(repo)
    assert repo.failures == 0, repo.last_flush_error
    assert repo.dropped == 0 and len(rows) == 1
    await repo.close()


async def test_many_concurrent_writers_same_mint(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    await repo.init()
    repo.start()
    token = make_token("MintMany")
    await asyncio.gather(
        *(repo.save_token_now(replace(token, source=f"src{i}")) for i in range(12))
    )
    for i in range(12):
        repo.save_token(replace(token, symbol=f"S{i}"))
    await repo.flush()
    assert repo.failures == 0 and repo.dropped == 0
    assert len(await _token_rows(repo)) == 1
    await repo.close()


async def test_rediscovery_after_restart_keeps_state_and_fills_metadata(tmp_path: Path) -> None:
    repo = _repo(tmp_path, session_id="run-1")
    await repo.init()
    repo.start()
    sparse = replace(make_token("MintRestart"), symbol=None, name=None, decimals=None)
    repo.save_token(sparse)
    repo.save_token_state("MintRestart", "REJECTED")
    await repo.flush()
    await repo.close()
    # restart: a different provider sees the token again, this time with metadata
    repo2 = _repo(tmp_path, session_id="run-2")
    await repo2.init()
    repo2.start()
    richer = replace(make_token("MintRestart"), source="geckoterminal", decimals=9)
    repo2.save_token(richer)
    await repo2.flush()
    (row,) = await _token_rows(repo2)
    assert repo2.failures == 0 and repo2.dropped == 0
    # deterministic semantics: first sight keeps its provenance, later data fills the gaps,
    # and the recorded final state is never wiped by a rediscovery
    assert row.session_id == "run-1" and row.source == "test"
    assert row.symbol == "TST" and row.decimals == 9 and row.name == "Test"
    assert row.final_state == "REJECTED"
    await repo2.close()


async def test_observations_for_duplicate_mint_all_persist(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    await repo.init()
    repo.start()
    token = make_token("MintObs")
    now = datetime.now(tz=UTC)
    for i in range(3):
        repo.save_token(replace(token, source=f"p{i}"))
        repo.save_observation(
            MarketSnapshot(
                mint="MintObs",
                observed_at=now + timedelta(seconds=i),
                source=f"p{i}",
                price_native=Decimal("0.001"),
            )
        )
    await repo.flush()
    counts = await repo.counts()
    assert counts["tokens"] == 1 and counts["observations"] == 3
    assert repo.failures == 0 and repo.dropped == 0
    await repo.close()


async def test_session_start_is_idempotent_for_resume(tmp_path: Path) -> None:
    repo = _repo(tmp_path, session_id="paper-x")
    await repo.init()
    repo.start()
    await repo.start_session("PAPER", "cfg")
    await repo.end_session()
    await repo.start_session("PAPER", "cfg")  # explicit resume of the same session id
    sessions = await repo.list_sessions()
    assert len(sessions) == 1 and sessions[0]["ended_at"] is None
    assert repo.failures == 0
    await repo.close()


# --------------------------------------------------------------- defect 2


async def test_telemetry_overflow_is_counted_by_class_and_degrades(tmp_path: Path) -> None:
    repo = _repo(tmp_path, max_queued_telemetry=2)
    await repo.init()
    assert not repo.degraded
    now = datetime.now(tz=UTC)
    for i in range(5):
        repo.save_error(ErrorRecord(at=now, component="t", message=str(i)))
    assert repo.dropped == 3 and repo.dropped_by_kind == {"error": 3}
    assert repo.degraded and repo.integrity_summary()["dropped_by_kind"] == {"error": 3}
    await repo.close()


async def test_critical_and_important_writes_survive_a_full_queue(
    tmp_path: Path, clock: ManualClock
) -> None:
    repo = _repo(tmp_path, max_queued_telemetry=1)
    await repo.init()
    now = datetime.now(tz=UTC)
    for i in range(10):  # saturate the telemetry budget
        repo.save_error(ErrorRecord(at=now, component="t", message=str(i)))
    assert repo.dropped > 0
    fill = make_fill(clock, side=SignalKind.BUY, mint="MintCrit")
    await repo.save_fill_now(fill)
    account = PortfolioAccount(clock, session_id="s1")
    account.deposit(Decimal("50"), "start")
    pos = account.open_position(fill, symbol="CRIT", entry_price_native=Decimal("0.001"))
    await repo.save_position_now(pos)
    token = make_token("MintCrit")
    repo.save_token(token)  # important: never dropped
    repo.save_token_state("MintCrit", "OPEN")
    await repo.flush()
    counts = await repo.counts()
    assert counts["positions"] == 1 and counts["tokens"] == 1
    assert repo.dropped_by_kind.get("token", 0) == 0
    assert repo.dropped_by_kind.get("fill", 0) == 0 and repo.dropped_by_kind.get("position", 0) == 0
    await repo.close()


async def test_session_integrity_is_recorded_and_readable(tmp_path: Path) -> None:
    repo = _repo(tmp_path, session_id="sess-i", max_queued_telemetry=1)
    await repo.init()
    repo.start()
    await repo.start_session("PAPER", None)
    now = datetime.now(tz=UTC)
    for i in range(4):
        repo.save_error(ErrorRecord(at=now, component="t", message=str(i)))
    await repo.end_session()
    integrity = await repo.session_integrity("sess-i")
    assert integrity is not None and integrity["dropped_total"] == repo.dropped > 0
    assert integrity["dropped_by_kind"]["error"] == repo.dropped and integrity["complete"] is False
    clean = _repo(tmp_path, session_id="sess-clean")
    await clean.init()
    clean.start()
    await clean.start_session("PAPER", None)
    await clean.end_session()
    ok = await clean.session_integrity("sess-clean")
    assert ok is not None and ok["complete"] is True and ok["dropped_total"] == 0
    await clean.close()
    await repo.close()


async def test_persistence_subscriber_counts_broken_events(tmp_path: Path) -> None:
    """A row that cannot be built (bad event payload) or cannot be serialised at commit time is
    counted as a dropped write of its kind instead of vanishing."""
    from solana_sniper.app.persistence import PersistenceSubscriber
    from solana_sniper.domain.events import QuoteObtained, SnapshotObserved

    repo = _repo(tmp_path)
    await repo.init()
    sub = PersistenceSubscriber(repo)

    class Broken:
        mint = "x"

    await sub.handle(QuoteObtained(Broken()))  # type: ignore[arg-type]  # builder raises
    assert repo.dropped_by_kind == {"quote": 1} and repo.degraded
    await sub.handle(SnapshotObserved(Broken()))  # type: ignore[arg-type]  # fails at commit
    await repo.flush()
    assert repo.dropped_by_kind == {"quote": 1, "observation": 1}
    assert repo.failed_by_kind == {"quote": 1, "observation": 1} and repo.dropped == 2
    assert repo.last_flush_error is not None
    await repo.close()
