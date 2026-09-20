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
                # A legacy record that said simulated=True was a dry-run fill: that evidence is
                # kept (SIMULATED is the weakest provenance, never verification). Anything else
                # is UNKNOWN_LEGACY because estimated vs user-reported cannot be told apart.
                payload["provenance"] = (
                    "SIMULATED" if payload.get("simulated") else "UNKNOWN_LEGACY"
                )
                payload["legacy_simulated"] = bool(payload.get("simulated"))
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


async def _v3_repair_legacy_simulated(conn: AsyncConnection) -> None:
    """Databases migrated by the first version of v2 hold fills/positions labelled UNKNOWN_LEGACY
    while `simulated` is true, which the Fill model rejects. Restore the simulation evidence:
    provenance SIMULATED, `legacy_simulated` recorded, units untouched, nothing verified."""
    for table, key in (("fills", "fill_id"), ("positions", "position_id")):
        rows = (await conn.execute(text(f"SELECT {key}, payload FROM {table}"))).all()
        for row_id, raw in rows:
            payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
            changed = False
            if "legacy_simulated" not in payload and "provenance" in payload:
                payload["legacy_simulated"] = bool(payload.get("simulated"))
                changed = True
            if payload.get("provenance") == "UNKNOWN_LEGACY" and payload.get("simulated") is True:
                payload["provenance"] = "SIMULATED"
                changed = True
            if changed:
                await conn.execute(
                    text(f"UPDATE {table} SET payload = :payload WHERE {key} = :id"),
                    {"payload": json.dumps(payload), "id": row_id},
                )


async def _v4_outcome_provenance(conn: AsyncConnection) -> None:
    """Split the single `simulated` flag into market-data and execution provenance. Rows written
    before this migration cannot tell live market data from the synthetic world, so their market
    provenance is UNKNOWN_LEGACY; their execution provenance follows the old flag."""
    await conn.run_sync(Base.metadata.create_all)
    cols = {row[1] for row in (await conn.execute(text("PRAGMA table_info(outcomes)"))).all()}
    if "market_provenance" not in cols:
        await conn.execute(text("ALTER TABLE outcomes ADD COLUMN market_provenance VARCHAR(16)"))
    if "execution_provenance" not in cols:
        await conn.execute(text("ALTER TABLE outcomes ADD COLUMN execution_provenance VARCHAR(16)"))
    await conn.execute(
        text(
            "UPDATE outcomes SET market_provenance = 'UNKNOWN_LEGACY' "
            "WHERE market_provenance IS NULL"
        )
    )
    await conn.execute(
        text(
            "UPDATE outcomes SET execution_provenance = CASE WHEN simulated THEN 'SIMULATED' "
            "ELSE 'MANUAL_SIGNAL' END WHERE execution_provenance IS NULL"
        )
    )
    rows = (await conn.execute(text("SELECT id, payload FROM outcomes"))).all()
    for row_id, raw in rows:
        payload = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        if "market_data" in payload and "execution" in payload:
            continue
        payload.setdefault("market_data", "UNKNOWN_LEGACY")
        payload.setdefault(
            "execution", "SIMULATED" if payload.get("simulated") else "MANUAL_SIGNAL"
        )
        await conn.execute(
            text("UPDATE outcomes SET payload = :payload WHERE id = :id"),
            {"payload": json.dumps(payload), "id": row_id},
        )


async def _v5_execution_intents(conn: AsyncConnection) -> None:
    """Autonomous mode: the `execution_intents` table (durable record of every bot-signed swap).
    A new table only; nothing existing is touched."""
    await conn.run_sync(Base.metadata.create_all)


MIGRATIONS: list[tuple[int, str, Step]] = [
    (1, "initial schema", _v1_initial),
    (
        2,
        "provenance and token units (legacy rows flagged UNKNOWN_LEGACY)",
        _v2_provenance_and_units,
    ),
    (
        3,
        "repair legacy simulated records (UNKNOWN_LEGACY + simulated -> SIMULATED)",
        _v3_repair_legacy_simulated,
    ),
    (
        4,
        "outcome provenance: market data (LIVE/SYNTHETIC) separate from execution (SIMULATED)",
        _v4_outcome_provenance,
    ),
    (5, "execution intents for autonomous mode (new table only)", _v5_execution_intents),
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
