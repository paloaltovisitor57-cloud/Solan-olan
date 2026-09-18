"""SQLite failure handling: a failing write must not stop the engine or poison the batch."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from solana_sniper.domain.models import ErrorRecord
from solana_sniper.storage.repository import Repository
from solana_sniper.telemetry.metrics import Metrics


async def test_bad_op_is_isolated_and_counted() -> None:
    repo = Repository(
        "sqlite+aiosqlite:///:memory:",
        session_id="s",
        batch_size=10,
        flush_interval_s=0.01,
        metrics=Metrics(),
    )
    await repo.init()

    async def bad(session: AsyncSession) -> None:
        raise SQLAlchemyError("disk I/O error")

    repo.persist(bad)
    repo.save_error(ErrorRecord(at=datetime.now(tz=UTC), component="t", message="kept"))
    await repo.flush()
    assert repo.failures >= 1  # the batch failed and was retried
    assert repo.dropped == 1  # only the bad op was dropped on the per-op retry
    assert repo.last_flush_error is not None
    counts = await repo.counts()
    assert counts["errors"] == 1  # the good op in the same batch still landed
    # a clean flush afterwards clears the error indicator
    repo.save_error(ErrorRecord(at=datetime.now(tz=UTC), component="t", message="again"))
    await repo.flush()
    assert repo.last_flush_error is None and repo.last_flush_at is not None
    assert (await repo.counts())["errors"] == 2
    await repo.close()


async def test_persist_now_propagates_nothing_but_records_failure() -> None:
    repo = Repository("sqlite+aiosqlite:///:memory:", session_id="s")
    await repo.init()

    async def bad(session: AsyncSession) -> None:
        raise SQLAlchemyError("locked")

    await repo.persist_now(bad)  # never raises into the engine hot path
    assert repo.failures >= 1 and repo.dropped == 1
    await repo.close()


async def test_writer_commits_a_batch_it_already_took_off_the_queue_when_closed(
    tmp_path: Path,
) -> None:
    """Regression: ops the background writer had dequeued but not yet committed were lost when
    close() cancelled the writer mid-batch (flush only drains the queue)."""
    url = f"sqlite+aiosqlite:///{tmp_path}/writer.db"
    repo = Repository(url, session_id="w1", flush_interval_s=0.5)
    await repo.init()
    repo.start()
    now = datetime.now(tz=UTC)
    for i in range(3):
        repo.save_error(ErrorRecord(at=now, component="t", message=f"m{i}", detail=""))
    await asyncio.sleep(0.05)  # the writer has dequeued the ops and is still collecting the batch
    assert repo._queue.empty()
    await repo.close()
    check = Repository(url, session_id="w2")
    await check.init()
    try:
        assert (await check.counts())["errors"] == 3
    finally:
        await check.close()


async def test_flush_waits_for_the_writers_in_flight_batch(tmp_path: Path) -> None:
    """flush() must return only once everything enqueued before the call is committed, including
    a batch the writer is currently holding."""
    url = f"sqlite+aiosqlite:///{tmp_path}/flush.db"
    repo = Repository(url, session_id="f1", flush_interval_s=0.5)
    await repo.init()
    repo.start()
    now = datetime.now(tz=UTC)
    for i in range(5):
        repo.save_error(ErrorRecord(at=now, component="t", message=f"m{i}", detail=""))
    await asyncio.sleep(0.05)
    await repo.flush()
    assert (await repo.counts())["errors"] == 5
    await repo.close()
