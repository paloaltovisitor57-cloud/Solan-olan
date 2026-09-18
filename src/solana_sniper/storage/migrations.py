"""Tiny forward-only schema migration runner. `solana-sniper migrate` applies pending steps.

Version 1 is the initial schema (create_all). New tables/columns are added as numbered steps so
`update.sh` can migrate an existing database before restarting the service.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String, select, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
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


async def _v2_provenance_and_units(conn: AsyncConnection) -> None:
    """Explicit migration policy for records written before provenance/units existed.

    * ledger rows get a `provenance` column; existing rows are labelled UNKNOWN_LEGACY
    * fill and position JSON payloads without `provenance` / `units` are labelled
      UNKNOWN_LEGACY (fills additionally keep their original `token_amount` under
      `legacy_token_amount` because its unit is ambiguous: buys were UI, sells were raw)
    Nothing is ever labelled verified, and no unit is guessed.
    """
    await conn.run_sync(Base.metadata.create_all)  # picks up new tables/columns on fresh DBs
    existing = await conn.run_sync(
        lambda sync: {c["name"] for c in sa_inspect(sync).get_columns("ledger")}
    )
    if "provenance" not in existing:
        await conn.execute(text("ALTER TABLE ledger ADD COLUMN provenance VARCHAR(24)"))
    await conn.execute(
        text("UPDATE ledger SET provenance = :p WHERE provenance IS NULL"),
        {"p": "UNKNOWN_LEGACY"},
    )
    for table, key in (("fills", "fill_id"), ("positions", "position_id")):
        rows = (await conn.execute(text(f"SELECT {key}, payload FROM {table}"))).all()
        for row_id, raw in rows:
            payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
            changed = False
            if "provenance" not in payload:
                payload["provenance"] = "UNKNOWN_LEGACY"
                changed = True
            if "units" not in payload:
                payload["units"] = "UNKNOWN_LEGACY"
                changed = True
            if table == "fills" and "token_amount" in payload and "token_amount_ui" not in payload:
                payload["legacy_token_amount"] = payload.pop("token_amount")
                payload.setdefault("token_amount_ui", {"__dec__": "0"})
                payload.setdefault("token_amount_raw", 0)
                payload.setdefault("token_decimals", 0)
                changed = True
            if table == "positions" and "quantity" in payload and "quantity_ui" not in payload:
                # positions were always created from UI buy quantities, but the record itself does
                # not say so: keep the number, flag the units as unverified.
                payload["quantity_ui"] = payload.pop("quantity")
                payload.setdefault("quantity_raw", 0)
                payload.setdefault("token_decimals", 0)
                changed = True
            if "tx_signature" in payload:
                payload["reported_tx_signature"] = payload.pop("tx_signature")
                changed = True
            if changed:
                await conn.execute(
                    text(f"UPDATE {table} SET payload = :payload WHERE {key} = :id"),
                    {"payload": json.dumps(payload), "id": row_id},
                )


MIGRATIONS: list[tuple[int, str, Step]] = [
    (1, "initial schema", _v1_initial),
    (
        2,
        "provenance and token units (legacy rows flagged UNKNOWN_LEGACY)",
        _v2_provenance_and_units,
    ),
]


async def current_version(conn: AsyncConnection) -> int:
    await conn.run_sync(lambda sync: SchemaVersionRow.__table__.create(sync, checkfirst=True))  # type: ignore[attr-defined]
    rows = (await conn.execute(select(SchemaVersionRow.version))).scalars().all()
    return max(rows, default=0)


async def run_migrations(database: str | AsyncEngine) -> list[int]:
    """Apply every migration above the stored version. Returns the versions applied."""
    own_engine = isinstance(database, str)
    engine = create_async_engine(database, future=True) if isinstance(database, str) else database
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
        if own_engine:
            await engine.dispose()
    return applied


async def schema_version(database_url: str) -> int:
    engine = create_async_engine(database_url, future=True)
    try:
        async with engine.begin() as conn:
            return await current_version(conn)
    finally:
        await engine.dispose()
