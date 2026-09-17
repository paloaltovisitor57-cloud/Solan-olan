"""Tiny forward-only schema migration runner. `solana-sniper migrate` applies pending steps.

Version 1 is the initial schema (create_all). New tables/columns are added as numbered steps so
`update.sh` can migrate an existing database before restarting the service.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String, select
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from solana_sniper.storage.models import Base

Step = Callable[[AsyncConnection], Awaitable[None]]


class SchemaVersionRow(Base):
    __tablename__ = "schema_version"
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    description: Mapped[str] = mapped_column(String(255))


async def _v1_initial(conn: AsyncConnection) -> None:
    await conn.run_sync(Base.metadata.create_all)


MIGRATIONS: list[tuple[int, str, Step]] = [
    (1, "initial schema", _v1_initial),
]


async def current_version(conn: AsyncConnection) -> int:
    await conn.run_sync(lambda sync: SchemaVersionRow.__table__.create(sync, checkfirst=True))  # type: ignore[attr-defined]
    rows = (await conn.execute(select(SchemaVersionRow.version))).scalars().all()
    return max(rows, default=0)


async def run_migrations(database_url: str) -> list[int]:
    """Apply every migration above the stored version. Returns the versions applied."""
    engine = create_async_engine(database_url, future=True)
    applied: list[int] = []
    try:
        async with engine.begin() as conn:
            version = await current_version(conn)
            for number, description, step in MIGRATIONS:
                if number <= version:
                    continue
                await step(conn)
                await conn.execute(
                    SchemaVersionRow.__table__.insert().values(  # type: ignore[attr-defined]
                        version=number, applied_at=datetime.now(tz=UTC), description=description
                    )
                )
                applied.append(number)
    finally:
        await engine.dispose()
    return applied


async def schema_version(database_url: str) -> int:
    engine = create_async_engine(database_url, future=True)
    try:
        async with engine.begin() as conn:
            return await current_version(conn)
    finally:
        await engine.dispose()
