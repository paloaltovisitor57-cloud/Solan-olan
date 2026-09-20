"""Read-only SQLite access for the web dashboard.

Every call opens a short-lived connection with `mode=ro` and `query_only`, runs plain SELECTs
without an explicit transaction, and closes. Nothing here can write, create a table, or apply a
migration: a database whose schema is older than the engine's is reported as unsupported with
the instruction to run the normal CLI migration, never migrated from the web server.

SQLite concurrency: the engine writes in WAL mode, so readers never block it and it never
blocks them; a transient `SQLITE_BUSY` (checkpoint, recovery) is retried briefly and then
surfaced as `DatabaseBusyError`, which the page renders as "Database busy — retrying".

The repository's own serialisation (`dataclass_from_dict`), the domain models and the
evaluation summariser are reused so the dashboard shows exactly what the CLI shows.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, TypeVar

from solana_sniper.app.paper import PaperSession
from solana_sniper.domain.models import EntryAttempt, Fill, PortfolioSnapshot, Position
from solana_sniper.storage.serialization import dataclass_from_dict, from_jsonable
from solana_sniper.strategy.evaluation import summarize
from solana_sniper.strategy.outcomes import Outcome
from solana_sniper.telemetry.redaction import safe_exception, scrub_text
from solana_sniper.web.models import (
    ALIVE_ENDED,
    ALIVE_NOT_RUNNING,
    ALIVE_RUNNING,
    AutonomyHealth,
    CandidateView,
    ConnectionHealth,
    EngineHealth,
    EquityHistory,
    EquityPoint,
    EventItem,
    FillView,
    Heartbeat,
    Integrity,
    ObservationPoint,
    OutcomeSummary,
    OutcomeView,
    PaperMeta,
    PositionView,
    ProviderHealth,
    QuoteView,
    SchemaStatus,
    ScorePoint,
    SessionRef,
    SessionSummary,
    SignalView,
    TokenDetail,
    TokenHit,
    TransitionView,
    provenance_for,
)

T = TypeVar("T")

# Tables the pages read. A database missing any of them predates the dashboard's schema.
REQUIRED_TABLES: frozenset[str] = frozenset(
    {
        "sessions",
        "session_integrity",
        "paper_sessions",
        "tokens",
        "observations",
        "features",
        "check_results",
        "scores",
        "signals",
        "decisions",
        "quotes",
        "positions",
        "fills",
        "portfolio_snapshots",
        "milestones",
        "state_transitions",
        "errors",
        "entry_attempts",
        "outcomes",
        "schema_version",
    }
)
MIN_SCHEMA_VERSION = 4
MIGRATION_HINT = (
    "run `solana-sniper migrate` (or `./update.sh`) from a terminal; the web dashboard never "
    "migrates a database"
)

BUSY_MESSAGE = "Database busy — retrying"
_BUSY_MARKERS = ("database is locked", "database table is locked", "busy", "locked")
_RETRY_DELAYS_S = (0.05, 0.15, 0.4)

TRADING_STATES = frozenset(
    {
        "QUALIFIED",
        "BUY_SIGNAL",
        "AWAITING_CONFIRMATION",
        "OPEN",
        "EXIT_SIGNAL",
        "AWAITING_EXIT_CONFIRMATION",
        "CLOSED",
        "SIGNAL_CANCELLED",
    }
)
_PROVIDER_COMPONENT_MARKERS = (
    "provider",
    "http",
    "rpc",
    "discovery",
    "market",
    "dexscreener",
    "gecko",
    "jupiter",
    "pump",
    "helius",
    "coingecko",
    "websocket",
    "fx",
)


class DashboardError(Exception):
    """Base for everything the pages show as a clean message (already scrubbed)."""


class DatabaseBusyError(DashboardError):
    """SQLITE_BUSY persisted through the short retry budget."""


class DatabaseMissingError(DashboardError):
    """The database file is not there (a session was deleted, or the home is wrong)."""


class SchemaUnsupportedError(DashboardError):
    """The database predates the schema the dashboard needs; migrate it with the CLI."""


# ------------------------------------------------------------------ connection


def read_only_uri(path: Path) -> str:
    """`file:` URI that opens the database read-only (SQLite refuses every write on it)."""
    return f"{path.resolve().as_uri()}?mode=ro"


def _connect(path: Path, busy_timeout_s: float) -> sqlite3.Connection:
    if not path.exists():
        raise DatabaseMissingError(scrub_text(f"database not found: {path}"))
    conn = sqlite3.connect(
        read_only_uri(path), uri=True, timeout=busy_timeout_s, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {max(0, int(busy_timeout_s * 1000))}")
    conn.execute("PRAGMA query_only = 1")  # belt and braces: even this connection cannot write
    return conn


def _is_busy(exc: sqlite3.Error) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _BUSY_MARKERS)


# ------------------------------------------------------------------ conversions


def _parse_dt(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _json(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str | bytes):
        try:
            loaded = json.loads(value)
        except ValueError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _dec(value: object) -> Decimal | None:
    raw = from_jsonable(value)
    if raw is None:
        return None
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int | float | str):
        try:
            return Decimal(str(raw))
        except (InvalidOperation, ValueError):
            return None
    return None


def _flt(value: object) -> float | None:
    raw = from_jsonable(value)
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, Decimal | int | float | str):
        try:
            out = float(raw)
        except (ValueError, TypeError):
            return None
        return out if math.isfinite(out) else None
    return None


def _int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float | str):
        try:
            return int(float(value))
        except (ValueError, TypeError):
            return None
    return None


def _str(value: object) -> str | None:
    return None if value is None else str(value)


def _dict(value: object) -> dict[str, Any]:
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


def _text(value: object) -> str:
    return "" if value is None else scrub_text(str(value))


def _integrity(row: sqlite3.Row | None) -> Integrity | None:
    if row is None:
        return None
    return Integrity(
        session_id=str(row["session_id"]),
        updated_at=_parse_dt(row["updated_at"]),
        complete=bool(row["complete"]),
        dropped_total=int(row["dropped_total"] or 0),
        failed_total=int(row["failed_total"] or 0),
        dropped_by_kind={str(k): int(v) for k, v in _json(row["dropped_by_kind"]).items()},
        failed_by_kind={str(k): int(v) for k, v in _json(row["failed_by_kind"]).items()},
        last_error=scrub_text(str(row["last_error"])) if row["last_error"] else None,
    )


def _position_view(p: Position, now: datetime) -> PositionView:
    pnl = (
        (p.exit_value_eur - p.cost_basis_eur)
        if p.exit_value_eur is not None
        else (p.current_value_eur - p.cost_basis_eur)
    )
    return PositionView(
        position_id=p.position_id,
        mint=p.mint,
        symbol=p.symbol,
        state=str(p.state),
        opened_at=p.opened_at,
        closed_at=p.closed_at,
        cost_basis_eur=p.cost_basis_eur,
        current_value_eur=p.current_value_eur,
        exit_value_eur=p.exit_value_eur,
        pnl_eur=pnl,
        pnl_pct=p.pnl_pct,
        peak_value_eur=p.peak_value_eur,
        trailing_drawdown_pct=p.trailing_drawdown_pct,
        quantity_ui=p.quantity_ui,
        units=str(p.units),
        provenance=str(p.provenance),
        value_is_executable=p.value_is_executable,
        data_stale=p.data_stale,
        exit_reason=str(p.exit_reason) if p.exit_reason is not None else None,
        realized_pnl_eur=p.realized_pnl_eur,
        entry_signal_id=p.entry_signal_id,
        exit_signal_id=p.exit_signal_id,
        simulated=p.simulated,
        held_s=p.holding_seconds(now),
        last_valued_at=p.last_valued_at,
        last_quote_at=p.last_quote_at,
        verified_onchain=p.is_verified,
    )


def _signal_view(row: sqlite3.Row, decision: sqlite3.Row | None) -> SignalView:
    payload = _json(row["payload"])
    kind = str(row["kind"])
    score_block = payload.get("score")
    score = _flt(score_block.get("score")) if isinstance(score_block, dict) else None
    sizing = payload.get("sizing")
    recommended = _dec(sizing.get("recommended_eur")) if isinstance(sizing, dict) else None
    if kind == "SELL":
        pnl = _flt(payload.get("pnl_pct"))
        detail = f"{_text(payload.get('reason'))}"
        if pnl is not None:
            detail += f"  pnl {pnl:+.1%}"
        if payload.get("detail"):
            detail += f"  {_text(payload.get('detail'))}"
    else:
        reasons = payload.get("score", {}).get("reasons") if isinstance(score_block, dict) else None
        detail = ", ".join(str(r) for r in reasons[:3]) if isinstance(reasons, list) else ""
        detail = scrub_text(detail)
    return SignalView(
        signal_id=str(row["signal_id"]),
        kind=kind,
        status=str(row["status"]),
        mint=str(row["mint"]),
        symbol=_str(payload.get("symbol")),
        created_at=_parse_dt(row["created_at"]) or datetime.now(tz=UTC),
        expires_at=_parse_dt(row["expires_at"]) or datetime.now(tz=UTC),
        urgency=_str(payload.get("urgency")),
        score=score,
        recommended_eur=recommended,
        detail=detail,
        decision=(f"{decision['kind']} by {decision['source']}" if decision is not None else None),
        decided_at=_parse_dt(decision["decided_at"]) if decision is not None else None,
    )


def _fill_view(row: sqlite3.Row, symbols: dict[str, str | None]) -> FillView:
    payload = _json(row["payload"])
    mint = str(row["mint"])
    base = {
        "fill_id": str(row["fill_id"]),
        "signal_id": str(row["signal_id"]),
        "side": str(row["side"]),
        "mint": mint,
        "symbol": symbols.get(mint),
        "filled_at": _parse_dt(row["filled_at"]) or datetime.now(tz=UTC),
    }
    try:
        fill = dataclass_from_dict(Fill, payload)
    except (ValueError, TypeError, KeyError, InvalidOperation):
        return FillView(
            **base,  # type: ignore[arg-type]
            sol_amount=_dec(payload.get("sol_amount")),
            token_amount_ui=_dec(payload.get("token_amount_ui")),
            eur_amount=_dec(payload.get("eur_amount")),
            fee_eur=_dec(payload.get("fee_eur")),
            slippage_cost_eur=_dec(payload.get("slippage_cost_eur")),
            provenance=str(payload.get("provenance") or "UNKNOWN_LEGACY"),
            simulated=bool(payload.get("simulated")),
            units=str(payload.get("units") or "UNKNOWN_LEGACY"),
            reported_tx_signature=_str(payload.get("reported_tx_signature")),
            note="record could not be decoded with the current Fill model",
            readable=False,
            verified_onchain=bool(payload.get("verified_onchain")),
            tx_signature=_str(payload.get("tx_signature")),
        )
    return FillView(
        **base,  # type: ignore[arg-type]
        sol_amount=fill.sol_amount,
        token_amount_ui=fill.token_amount_ui,
        eur_amount=fill.eur_amount,
        fee_eur=fill.fee_eur,
        slippage_cost_eur=fill.slippage_cost_eur,
        provenance=str(fill.provenance),
        simulated=fill.simulated,
        units=str(fill.units),
        reported_tx_signature=fill.reported_tx_signature,
        note=scrub_text(fill.note),
        verified_onchain=fill.verified_onchain,
        tx_signature=fill.tx_signature,
    )


def _outcome_view(o: Outcome) -> OutcomeView:
    return OutcomeView(
        mint=o.mint,
        symbol=o.symbol,
        first_seen_at=o.first_seen_at,
        finalized_at=o.finalized_at,
        observations=o.observations,
        max_multiple=o.max_multiple,
        final_multiple=o.final_multiple,
        max_drawdown_from_peak=o.max_drawdown_from_peak,
        time_to_peak_s=o.time_to_peak_s,
        best_score=o.best_score,
        qualified=o.qualified,
        signalled=o.signalled,
        entered=o.entered,
        closed_pnl_pct=o.closed_pnl_pct,
        exit_reason=o.exit_reason,
        liquidity_collapsed=o.liquidity_collapsed,
        reject_reason=o.reject_reason,
        truncated=o.truncated,
        market_data=str(o.market_data),
        execution=str(o.execution),
    )


def _gate_reason(
    checks: dict[str, Any] | None, score: dict[str, Any] | None, transition_reason: str
) -> str:
    if checks:
        results = checks.get("results")
        if isinstance(results, list):
            rejects = [
                f"{r.get('name')}: {r.get('reason')}"
                for r in results
                if isinstance(r, dict) and r.get("verdict") == "REJECT"
            ]
            if rejects:
                return scrub_text("; ".join(rejects[:3]))
    if score:
        penalties = score.get("penalties")
        if isinstance(penalties, list) and penalties:
            return scrub_text("; ".join(str(p) for p in penalties[:3]))
    return scrub_text(transition_reason)


# ------------------------------------------------------------------ repository


@dataclass(frozen=True, slots=True)
class _Latest:
    """Latest row per mint from a bounded tail of a table."""

    rows: dict[str, sqlite3.Row]


class DashboardRepository:
    """Read-only view over one session in one SQLite database."""

    def __init__(
        self,
        db_path: Path,
        session_id: str,
        *,
        busy_timeout_s: float = 5.0,
        retries: int = len(_RETRY_DELAYS_S),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.db_path = Path(db_path)
        self.session_id = session_id
        self._busy_timeout = busy_timeout_s
        self._retries = max(0, retries)
        self._sleep = sleep
        self._schema: SchemaStatus | None = None

    # ---------------------------------------------------------------- core
    def _run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run `fn` on a fresh read-only connection; retry SQLITE_BUSY briefly."""
        attempt = 0
        while True:
            conn: sqlite3.Connection | None = None
            try:
                conn = _connect(self.db_path, self._busy_timeout)
                return fn(conn)
            except sqlite3.OperationalError as exc:
                if _is_busy(exc):
                    if attempt < self._retries:
                        self._sleep(_RETRY_DELAYS_S[min(attempt, len(_RETRY_DELAYS_S) - 1)])
                        attempt += 1
                        continue
                    raise DatabaseBusyError(BUSY_MESSAGE) from None
                if "no such table" in str(exc).lower() or "no such column" in str(exc).lower():
                    raise SchemaUnsupportedError(
                        f"unsupported database schema ({safe_exception(exc)}); {MIGRATION_HINT}"
                    ) from None
                raise DashboardError(safe_exception(exc)) from None
            except sqlite3.DatabaseError as exc:
                raise DashboardError(safe_exception(exc)) from None
            finally:
                if conn is not None:
                    conn.close()

    def _rows(self, sql: str, params: Sequence[object] = ()) -> list[sqlite3.Row]:
        return self._run(lambda c: c.execute(sql, tuple(params)).fetchall())

    def _one(self, sql: str, params: Sequence[object] = ()) -> sqlite3.Row | None:
        def go(c: sqlite3.Connection) -> sqlite3.Row | None:
            row = c.execute(sql, tuple(params)).fetchone()
            return row if isinstance(row, sqlite3.Row) else None

        return self._run(go)

    def _scalar(self, sql: str, params: Sequence[object] = ()) -> Any:
        row = self._one(sql, params)
        return None if row is None else row[0]

    def schema(self) -> SchemaStatus:
        """Inspect (never change) the schema; cached per repository instance."""
        if self._schema is not None:
            return self._schema

        def inspect(conn: sqlite3.Connection) -> SchemaStatus:
            names = {
                str(r[0])
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            missing = tuple(sorted(REQUIRED_TABLES - names))
            version = 0
            if "schema_version" in names:
                raw = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
                version = int(raw[0] or 0) if raw is not None else 0
            if missing:
                message = (
                    f"database schema unsupported: missing tables {', '.join(missing)}; "
                    f"{MIGRATION_HINT}"
                )
                return SchemaStatus(version, frozenset(names), missing, False, message)
            if version < MIN_SCHEMA_VERSION:
                message = (
                    f"database schema version {version} is older than the required "
                    f"{MIN_SCHEMA_VERSION}; {MIGRATION_HINT}"
                )
                return SchemaStatus(version, frozenset(names), (), False, message)
            return SchemaStatus(version, frozenset(names), (), True, "ok")

        self._schema = self._run(inspect)
        return self._schema

    def require_schema(self) -> None:
        status = self.schema()
        if not status.supported:
            raise SchemaUnsupportedError(status.message)

    # ------------------------------------------------------------- sessions
    def sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT session_id, mode, started_at, ended_at, config_path FROM sessions "
            "ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )
        return [
            {
                "session_id": str(r["session_id"]),
                "mode": str(r["mode"] or "UNKNOWN"),
                "started_at": _parse_dt(r["started_at"]),
                "ended_at": _parse_dt(r["ended_at"]),
                "config_path": _str(r["config_path"]),
            }
            for r in rows
        ]

    def session_row(self) -> dict[str, Any] | None:
        r = self._one(
            "SELECT session_id, mode, started_at, ended_at, config_path FROM sessions "
            "WHERE session_id = ?",
            (self.session_id,),
        )
        if r is None:
            return None
        return {
            "session_id": str(r["session_id"]),
            "mode": str(r["mode"] or "UNKNOWN"),
            "started_at": _parse_dt(r["started_at"]),
            "ended_at": _parse_dt(r["ended_at"]),
            "config_path": _str(r["config_path"]),
        }

    def paper_meta(self) -> PaperMeta | None:
        r = self._one("SELECT payload FROM paper_sessions WHERE session_id = ?", (self.session_id,))
        if r is None:
            return None
        try:
            meta = PaperSession.from_payload(_json(r["payload"]))
        except (KeyError, ValueError, InvalidOperation):
            return None
        return PaperMeta(
            session_id=meta.session_id,
            name=meta.name,
            created_at=meta.created_at,
            requested=meta.requested,
            bankroll_sol=meta.bankroll_sol,
            bankroll_eur=meta.bankroll_eur,
            sol_eur_start=meta.sol_eur_start,
            fx_source=meta.fx_source,
            fx_at=meta.fx_at,
        )

    def integrity(self) -> Integrity | None:
        return _integrity(
            self._one(
                "SELECT session_id, updated_at, dropped_total, failed_total, dropped_by_kind, "
                "failed_by_kind, complete, last_error FROM session_integrity WHERE session_id = ?",
                (self.session_id,),
            )
        )

    def market_data_provenance(self, heartbeat: Heartbeat | None = None) -> str:
        """LIVE / SYNTHETIC / MIXED from what the session recorded; a running session's
        heartbeat is used only when nothing has been recorded yet."""
        rows = self._rows(
            "SELECT DISTINCT market_provenance FROM outcomes WHERE session_id = ?",
            (self.session_id,),
        )
        kinds = {str(r[0]) for r in rows if r[0] in ("LIVE", "SYNTHETIC")}
        if not kinds:
            venues = self._rows(
                "SELECT venue, COUNT(*) AS n FROM tokens WHERE session_id = ? GROUP BY venue",
                (self.session_id,),
            )
            for r in venues:
                kinds.add("SYNTHETIC" if str(r["venue"]) == "synthetic" else "LIVE")
        if not kinds:
            sources = self._rows(
                "SELECT DISTINCT source FROM (SELECT source FROM observations "
                "WHERE session_id = ? ORDER BY id DESC LIMIT 500)",
                (self.session_id,),
            )
            for r in sources:
                kinds.add("SYNTHETIC" if str(r[0]) == "synthetic" else "LIVE")
        if len(kinds) > 1:
            return "MIXED"
        if kinds:
            return kinds.pop()
        if (
            heartbeat is not None
            and heartbeat.fresh
            and heartbeat.session_id == self.session_id
            and heartbeat.status.get("market_data_provenance") in ("LIVE", "SYNTHETIC")
        ):
            return str(heartbeat.status["market_data_provenance"])
        return "NOT YET OBSERVED"

    def _count(self, sql: str, params: Sequence[object]) -> int:
        return int(self._scalar(sql, params) or 0)

    def summary(self, ref: SessionRef, heartbeat: Heartbeat | None) -> SessionSummary:
        self.require_schema()
        sid = self.session_id
        row = self.session_row()
        paper = self.paper_meta()
        mode = row["mode"] if row else ("PAPER" if paper else ref.mode)
        latest = self._one(
            "SELECT at, payload FROM portfolio_snapshots WHERE session_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (sid,),
        )
        first = self._one(
            "SELECT payload FROM portfolio_snapshots WHERE session_id = ? ORDER BY id ASC LIMIT 1",
            (sid,),
        )
        snap: PortfolioSnapshot | None = None
        if latest is not None:
            try:
                snap = dataclass_from_dict(PortfolioSnapshot, _json(latest["payload"]))
            except (TypeError, ValueError, InvalidOperation):
                snap = None
        starting: Decimal | None = paper.bankroll_eur if paper else None
        if starting is None and first is not None:
            starting = _dec(_json(first["payload"]).get("equity_eur"))
        snapshot_count = self._count(
            "SELECT COUNT(*) FROM portfolio_snapshots WHERE session_id = ?", (sid,)
        )
        open_positions = self._count(
            "SELECT COUNT(*) FROM positions WHERE session_id = ? AND state != 'CLOSED'", (sid,)
        )
        positions_total = self._count("SELECT COUNT(*) FROM positions WHERE session_id = ?", (sid,))
        signals = self._count("SELECT COUNT(*) FROM signals WHERE session_id = ?", (sid,))
        fills = self._count("SELECT COUNT(*) FROM fills WHERE session_id = ?", (sid,))
        attempts = self._count("SELECT COUNT(*) FROM entry_attempts WHERE session_id = ?", (sid,))
        tokens = self._count("SELECT COUNT(*) FROM tokens WHERE session_id = ?", (sid,))
        errors = self._count("SELECT COUNT(*) FROM errors WHERE session_id = ?", (sid,))
        outcomes = self._count("SELECT COUNT(*) FROM outcomes WHERE session_id = ?", (sid,))
        quotes = self._count("SELECT COUNT(*) FROM quotes WHERE session_id = ?", (sid,))
        latest_attempt_row = self._one(
            "SELECT payload FROM entry_attempts WHERE session_id = ? "
            "ORDER BY qualified_at DESC LIMIT 1",
            (sid,),
        )
        latest_attempt: EntryAttempt | None = None
        if latest_attempt_row is not None:
            try:
                latest_attempt = dataclass_from_dict(
                    EntryAttempt, _json(latest_attempt_row["payload"])
                )
            except (TypeError, ValueError, InvalidOperation):
                latest_attempt = None
        last_transition = self._scalar(
            "SELECT at FROM state_transitions WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (sid,),
        )
        last_error = self._scalar(
            "SELECT at FROM errors WHERE session_id = ? ORDER BY id DESC LIMIT 1", (sid,)
        )
        candidates_last = [
            d
            for d in (
                _parse_dt(latest["at"]) if latest is not None else None,
                _parse_dt(last_transition),
                _parse_dt(last_error),
            )
            if d is not None
        ]
        last_write = max(candidates_last) if candidates_last else None

        market = self.market_data_provenance(heartbeat)
        prov = provenance_for(mode, market)
        ended_at = row["ended_at"] if row else None
        started_at = row["started_at"] if row else (paper.created_at if paper else None)
        hb_mine = heartbeat is not None and heartbeat.session_id == sid
        engine_state = str(heartbeat.status.get("state")) if hb_mine and heartbeat else None
        if hb_mine and heartbeat is not None and heartbeat.fresh:
            alive = ALIVE_RUNNING
            detail = f"heartbeat {heartbeat.age_s:.0f}s ago" if heartbeat.age_s is not None else ""
        elif ended_at is not None:
            alive = ALIVE_ENDED
            detail = f"ended {ended_at:%Y-%m-%d %H:%M:%S} UTC"
        else:
            alive = ALIVE_NOT_RUNNING
            if hb_mine and heartbeat is not None and heartbeat.status.get("stopped"):
                detail = f"stopped: {_text(heartbeat.status.get('stop_reason'))}"
            elif last_write is not None:
                detail = f"no fresh heartbeat; last write {last_write:%Y-%m-%d %H:%M:%S} UTC"
            else:
                detail = "no heartbeat and nothing written yet"
        equity = snap.equity_eur if snap else None
        ret = (
            float((equity - starting) / starting)
            if equity is not None and starting is not None and starting > 0
            else None
        )
        return SessionSummary(
            ref=ref,
            mode=mode,
            provenance=prov,
            alive=alive,
            alive_detail=detail,
            engine_state=engine_state,
            started_at=started_at,
            ended_at=ended_at,
            paper=paper,
            starting_equity_eur=starting,
            equity_eur=equity,
            cash_eur=snap.cash_eur if snap else None,
            open_exposure_eur=snap.open_exposure_eur if snap else None,
            open_value_eur=snap.open_value_eur if snap else None,
            peak_equity_eur=snap.peak_equity_eur if snap else None,
            realized_pnl_eur=snap.realized_pnl_eur if snap else None,
            unrealized_pnl_eur=snap.unrealized_pnl_eur if snap else None,
            fees_eur=snap.fees_eur if snap else None,
            slippage_eur=snap.slippage_eur if snap else None,
            return_pct=ret,
            drawdown_pct=snap.drawdown_pct if snap else None,
            wins=snap.wins if snap else 0,
            losses=snap.losses if snap else 0,
            snapshot_at=snap.at if snap else None,
            open_positions=open_positions,
            positions_total=positions_total,
            signals=signals,
            fills=fills,
            entry_attempts=attempts,
            tokens=tokens,
            errors=errors,
            outcomes=outcomes,
            quotes=quotes,
            latest_attempt=latest_attempt,
            integrity=self.integrity(),
            last_write_at=last_write,
            snapshot_count=snapshot_count,
        )

    # ------------------------------------------------------------ portfolio
    def portfolio_history(self, max_points: int = 600) -> EquityHistory:
        self.require_schema()
        sid = self.session_id
        total = self._count("SELECT COUNT(*) FROM portfolio_snapshots WHERE session_id = ?", (sid,))
        max_points = max(2, max_points)
        stride = max(1, math.ceil(total / max_points)) if total > max_points else 1
        rows = self._rows(
            "SELECT at, payload FROM ("
            "  SELECT id, at, payload, ROW_NUMBER() OVER (ORDER BY id) AS rn, "
            "         COUNT(*) OVER () AS total"
            "  FROM portfolio_snapshots WHERE session_id = ?"
            ") WHERE rn % ? = 0 OR rn = 1 OR rn = total ORDER BY id",
            (sid, stride),
        )
        points: list[EquityPoint] = []
        for r in rows:
            p = _json(r["payload"])
            at = _parse_dt(r["at"]) or _parse_dt(from_jsonable(p.get("at")))
            equity = _flt(p.get("equity_eur"))
            if at is None or equity is None:
                continue
            points.append(
                EquityPoint(
                    at=at,
                    equity_eur=equity,
                    cash_eur=_flt(p.get("cash_eur")) or 0.0,
                    open_exposure_eur=_flt(p.get("open_exposure_eur")) or 0.0,
                    open_value_eur=_flt(p.get("open_value_eur")) or 0.0,
                    realized_pnl_eur=_flt(p.get("realized_pnl_eur")) or 0.0,
                    unrealized_pnl_eur=_flt(p.get("unrealized_pnl_eur")) or 0.0,
                    drawdown_pct=_flt(p.get("drawdown_pct")) or 0.0,
                    open_positions=_int(p.get("open_positions")) or 0,
                )
            )
        # exact maximum drawdown from the full history, computed inside SQLite
        max_dd = self._scalar(
            "SELECT MAX((peak - equity) / peak) FROM ("
            "  SELECT equity, MAX(equity) OVER (ORDER BY id ROWS UNBOUNDED PRECEDING) AS peak"
            "  FROM (SELECT id, CAST(json_extract(payload, '$.equity_eur.__dec__') AS REAL) "
            "        AS equity FROM portfolio_snapshots WHERE session_id = ?)"
            ") WHERE peak > 0",
            (sid,),
        )
        return EquityHistory(
            points=tuple(points),
            total_points=total,
            sampled=stride > 1,
            max_drawdown_pct=_flt(max_dd),
        )

    # ------------------------------------------------------------ positions
    def positions(self, *, open_only: bool = True, limit: int = 500) -> list[PositionView]:
        self.require_schema()
        sql = "SELECT payload FROM positions WHERE session_id = ?"
        if open_only:
            sql += " AND state != 'CLOSED'"
        rows = self._rows(sql + " ORDER BY updated_at DESC LIMIT ?", (self.session_id, limit))
        now = datetime.now(tz=UTC)
        out: list[PositionView] = []
        for r in rows:
            try:
                out.append(_position_view(dataclass_from_dict(Position, _json(r["payload"])), now))
            except (TypeError, ValueError, InvalidOperation):
                continue
        return out

    # ----------------------------------------------------------- candidates
    def _latest_per_mint(self, table: str, tail: int, columns: str) -> _Latest:
        rows = self._rows(
            f"SELECT {columns} FROM ("
            f"  SELECT {columns}, ROW_NUMBER() OVER (PARTITION BY mint ORDER BY id DESC) AS rn"
            f"  FROM (SELECT id, {columns} FROM {table} WHERE session_id = ? "
            f"        ORDER BY id DESC LIMIT ?)"
            f") WHERE rn = 1",
            (self.session_id, tail),
        )
        return _Latest({str(r["mint"]): r for r in rows})

    def recent_candidates(self, limit: int = 200, tail: int = 4000) -> list[CandidateView]:
        """Most recently active tokens with their latest state, score, features and gate
        verdict. Six bounded queries (tails of the telemetry tables), joined in Python."""
        self.require_schema()
        transitions = self._latest_per_mint(
            "state_transitions", tail, "mint, source, target, at, reason"
        )
        if not transitions.rows:
            return []
        ordered = sorted(transitions.rows.values(), key=lambda r: str(r["at"] or ""), reverse=True)[
            :limit
        ]
        mints = [str(r["mint"]) for r in ordered]
        placeholders = ",".join("?" for _ in mints)
        token_rows = self._rows(
            "SELECT mint, symbol, name, source, venue, created_at, pool_created_at, "
            f"discovered_at, final_state FROM tokens WHERE mint IN ({placeholders})",
            mints,
        )
        tokens = {str(r["mint"]): r for r in token_rows}
        scores = self._latest_per_mint("scores", tail, "mint, score, scored_at, payload")
        features = self._latest_per_mint("features", tail, "mint, computed_at, payload")
        checks = self._latest_per_mint("check_results", tail // 2, "mint, verdict, fatal, payload")
        observations = self._latest_per_mint("observations", tail, "mint, observed_at, payload")
        now = datetime.now(tz=UTC)
        out: list[CandidateView] = []
        for tr in ordered:
            mint = str(tr["mint"])
            tok = tokens.get(mint)
            sc = scores.rows.get(mint)
            ft = features.rows.get(mint)
            ck = checks.rows.get(mint)
            ob = observations.rows.get(mint)
            fpayload = _json(ft["payload"]) if ft is not None else {}
            opayload = _json(ob["payload"]) if ob is not None else {}
            spayload = _json(sc["payload"]) if sc is not None else None
            cpayload = _json(ck["payload"]) if ck is not None else None
            created = (
                _parse_dt(tok["pool_created_at"]) or _parse_dt(tok["created_at"])
                if tok is not None
                else None
            )
            age = (now - created).total_seconds() if created is not None else None
            if age is None:
                age = _flt(fpayload.get("token_age_s"))
            out.append(
                CandidateView(
                    mint=mint,
                    symbol=_str(tok["symbol"]) if tok is not None else None,
                    name=_str(tok["name"]) if tok is not None else None,
                    state=str(tr["target"]),
                    state_at=_parse_dt(tr["at"]),
                    state_reason=_text(tr["reason"]),
                    score=_flt(sc["score"]) if sc is not None else None,
                    scored_at=_parse_dt(sc["scored_at"]) if sc is not None else None,
                    age_s=age,
                    liquidity_usd=_flt(fpayload.get("liquidity_usd"))
                    or _flt(opayload.get("liquidity_usd")),
                    volume_5m_usd=_flt(opayload.get("volume_5m_usd")),
                    buys_5m=_int(opayload.get("buys_5m")),
                    sells_5m=_int(opayload.get("sells_5m")),
                    trade_velocity_per_min=_flt(fpayload.get("trade_velocity_per_min")),
                    momentum_60s=_flt(fpayload.get("momentum_60s")),
                    momentum_10s=_flt(fpayload.get("momentum_10s")),
                    acceleration=_flt(fpayload.get("acceleration")),
                    data_age_s=_flt(fpayload.get("data_age_s")),
                    stale=bool(fpayload["stale"]) if "stale" in fpayload else None,
                    check_verdict=_str(ck["verdict"]) if ck is not None else None,
                    gate_reason=_gate_reason(cpayload, spayload, str(tr["reason"] or "")),
                    source=_str(tok["source"]) if tok is not None else None,
                    venue=_str(tok["venue"]) if tok is not None else None,
                )
            )
        return out

    # -------------------------------------------------------- entry attempts
    def entry_attempts(
        self, *, limit: int = 300, decision: str | None = None, mint: str | None = None
    ) -> list[EntryAttempt]:
        self.require_schema()
        sql = "SELECT payload FROM entry_attempts WHERE session_id = ?"
        params: list[object] = [self.session_id]
        if decision:
            sql += " AND final_decision = ?"
            params.append(decision)
        if mint:
            sql += " AND mint = ?"
            params.append(mint)
        sql += " ORDER BY qualified_at DESC LIMIT ?"
        params.append(limit)
        out: list[EntryAttempt] = []
        for r in self._rows(sql, params):
            try:
                out.append(dataclass_from_dict(EntryAttempt, _json(r["payload"])))
            except (TypeError, ValueError, InvalidOperation):
                continue
        return out

    def entry_decision_counts(self) -> dict[str, int]:
        self.require_schema()
        rows = self._rows(
            "SELECT final_decision, COUNT(*) AS n FROM entry_attempts WHERE session_id = ? "
            "GROUP BY final_decision",
            (self.session_id,),
        )
        return {str(r["final_decision"]): int(r["n"]) for r in rows}

    # -------------------------------------------------------------- signals
    def signals(self, limit: int = 200) -> list[SignalView]:
        self.require_schema()
        rows = self._rows(
            "SELECT signal_id, mint, kind, created_at, expires_at, status, payload FROM signals "
            "WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
            (self.session_id, limit),
        )
        if not rows:
            return []
        ids = [str(r["signal_id"]) for r in rows]
        placeholders = ",".join("?" for _ in ids)
        decisions = {
            str(d["signal_id"]): d
            for d in self._rows(
                "SELECT signal_id, kind, source, decided_at FROM decisions "
                f"WHERE signal_id IN ({placeholders}) ORDER BY decided_at ASC",
                ids,
            )
        }
        return [_signal_view(r, decisions.get(str(r["signal_id"]))) for r in rows]

    # ---------------------------------------------------------------- fills
    def _symbols(self, mints: Iterable[str]) -> dict[str, str | None]:
        unique = sorted(set(mints))
        if not unique:
            return {}
        placeholders = ",".join("?" for _ in unique)
        rows = self._rows(f"SELECT mint, symbol FROM tokens WHERE mint IN ({placeholders})", unique)
        return {str(r["mint"]): _str(r["symbol"]) for r in rows}

    def fills(self, limit: int = 300) -> list[FillView]:
        self.require_schema()
        rows = self._rows(
            "SELECT fill_id, signal_id, mint, side, filled_at, payload FROM fills "
            "WHERE session_id = ? ORDER BY filled_at DESC LIMIT ?",
            (self.session_id, limit),
        )
        symbols = self._symbols(str(r["mint"]) for r in rows)
        return [_fill_view(r, symbols) for r in rows]

    # --------------------------------------------------------------- health
    def provider_health(self, heartbeat: Heartbeat | None) -> list[ProviderHealth]:
        """Governor health from the running engine's heartbeat (the database does not hold
        provider state). Empty when no fresh heartbeat belongs to this session."""
        if heartbeat is None or heartbeat.session_id != self.session_id:
            return []
        raw = heartbeat.status.get("providers")
        if not isinstance(raw, dict):
            return []
        out: list[ProviderHealth] = []
        for name, info in raw.items():
            if not isinstance(info, dict):
                continue
            out.append(
                ProviderHealth(
                    name=str(name),
                    host=_str(info.get("host")),
                    state=str(info.get("state") or "UNKNOWN"),
                    cooldown_s=_flt(info.get("cooldown_s")) or 0.0,
                    inflight=_int(info.get("inflight")) or 0,
                    waiting=_int(info.get("waiting")) or 0,
                    consecutive_failures=_int(info.get("consecutive_failures")) or 0,
                    circuit_trips=_int(info.get("circuit_trips")) or 0,
                    requests=_int(info.get("requests")) or 0,
                    rate_limited=_int(info.get("rate_limited")) or 0,
                    retries=_int(info.get("retries")) or 0,
                    backoffs=_int(info.get("backoffs")) or 0,
                    fast_fails=_int(info.get("fast_fails")) or 0,
                    failures=_int(info.get("failures")) or 0,
                    recoveries=_int(info.get("recoveries")) or 0,
                    last_error=scrub_text(str(info["last_error"]))
                    if info.get("last_error")
                    else None,
                )
            )
        return sorted(out, key=lambda p: p.name)

    def engine_health(self, heartbeat: Heartbeat | None) -> EngineHealth:
        self.require_schema()
        integrity = self.integrity()
        if heartbeat is None:
            return _engine_health_empty(integrity, found=False)
        mine = heartbeat.session_id == self.session_id
        if not mine:
            return _engine_health_empty(
                integrity,
                found=True,
                other=heartbeat.session_id,
                written_at=heartbeat.written_at,
                age_s=heartbeat.age_s,
            )
        s = heartbeat.status
        eng = _dict(s.get("engine"))
        md = _dict(s.get("market_data"))
        db = _dict(s.get("database"))
        counters_raw = _dict(s.get("counters"))
        conns_raw = _dict(s.get("connections"))
        last_error_raw = s.get("last_error")
        last_error = None
        if isinstance(last_error_raw, dict):
            last_error = scrub_text(
                f"{last_error_raw.get('component')}: {last_error_raw.get('message')}"
            )
        elif last_error_raw:
            last_error = scrub_text(str(last_error_raw))
        return EngineHealth(
            heartbeat_found=True,
            heartbeat_for_this_session=True,
            heartbeat_session_id=heartbeat.session_id,
            fresh=heartbeat.fresh,
            written_at=heartbeat.written_at,
            age_s=heartbeat.age_s,
            state=_str(s.get("state")),
            healthy=bool(s.get("healthy")) if "healthy" in s else None,
            problems=tuple(_text(p) for p in (s.get("problems") or [])),
            degraded=tuple(_text(p) for p in (s.get("degraded") or [])),
            uptime_s=_flt(s.get("uptime_s")),
            pid=_int(s.get("pid")),
            hostname=_str(s.get("hostname")),
            starts=_int(s.get("starts")),
            previous_exit=_str(s.get("previous_exit")),
            last_tick_age_s=_flt(eng.get("last_tick_age_s")),
            tick_p50_ms=_flt(eng.get("tick_p50_ms")),
            tick_p95_ms=_flt(eng.get("tick_p95_ms")),
            evaluations=_int(eng.get("evaluations")),
            market_ok=bool(md["ok"]) if "ok" in md else None,
            watched=_int(md.get("watched")),
            last_snapshot_age_s=_flt(md.get("last_snapshot_age_s")),
            snapshots_last_minute=_int(md.get("snapshots_last_minute")),
            discovery_rate_per_s=_flt(md.get("discovery_rate_per_s")),
            db_ok=bool(db["ok"]) if "ok" in db else None,
            db_last_flush_age_s=_flt(db.get("last_flush_age_s")),
            db_last_error=scrub_text(str(db["last_error"])) if db.get("last_error") else None,
            db_failures=_int(db.get("failures")),
            db_dropped=_int(db.get("dropped")),
            db_queued=_int(db.get("queued")),
            db_integrity=_dict(db.get("integrity")),
            counters={str(k): _int(v) or 0 for k, v in counters_raw.items()},
            connections=tuple(
                ConnectionHealth(
                    name=str(name),
                    kind=str(info.get("kind") or "?"),
                    connected=bool(info.get("connected")),
                    last_activity_age_s=_flt(info.get("last_activity_age_s")),
                )
                for name, info in conns_raw.items()
                if isinstance(info, dict)
            ),
            tokens_monitored=_int(s.get("tokens_monitored")),
            qualified=_int(s.get("qualified")),
            pending_signals=_int(s.get("pending_signals")),
            last_signal_at=_parse_dt(s.get("last_signal_at")),
            last_error=last_error,
            records_verified_onchain=bool(s.get("records_verified_onchain", False)),
            integrity=integrity,
            stop_reason=_text(s.get("stop_reason")) if s.get("stopped") else None,
            autonomy=_autonomy_health(s.get("autonomy")),
        )

    # --------------------------------------------------------------- events
    def recent_errors(self, limit: int = 200) -> list[EventItem]:
        self.require_schema()
        rows = self._rows(
            "SELECT at, component, message, detail FROM errors WHERE session_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (self.session_id, limit),
        )
        out: list[EventItem] = []
        for r in rows:
            at = _parse_dt(r["at"])
            if at is None:
                continue
            component = _text(r["component"])
            category = (
                "PROVIDERS"
                if any(m in component.lower() for m in _PROVIDER_COMPONENT_MARKERS)
                else "ERRORS"
            )
            out.append(
                EventItem(
                    at=at,
                    category=category,
                    source="error",
                    subject=component,
                    message=_text(r["message"]),
                    detail=_text(r["detail"]),
                )
            )
        return out

    def recent_transitions(self, limit: int = 300) -> list[EventItem]:
        self.require_schema()
        rows = self._rows(
            "SELECT mint, source, target, at, reason FROM state_transitions "
            "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (self.session_id, limit),
        )
        symbols = self._symbols(str(r["mint"]) for r in rows)
        out: list[EventItem] = []
        for r in rows:
            at = _parse_dt(r["at"])
            if at is None:
                continue
            mint = str(r["mint"])
            target = str(r["target"])
            out.append(
                EventItem(
                    at=at,
                    category="TRADING" if target in TRADING_STATES else "DATA",
                    source="transition",
                    subject=symbols.get(mint) or mint[:10],
                    message=f"{r['source']} → {target}",
                    detail=_text(r["reason"]),
                )
            )
        return out

    def events(self, *, category: str = "ALL", limit: int = 300) -> list[EventItem]:
        """Merged feed: transitions, fills, signals, milestones, errors; newest first."""
        self.require_schema()
        items = self.recent_errors(limit) + self.recent_transitions(limit)
        for f in self.fills(min(limit, 100)):
            items.append(
                EventItem(
                    at=f.filled_at,
                    category="TRADING",
                    source="fill",
                    subject=f.symbol or f.mint[:10],
                    message=f"{f.side} fill ({f.provenance})",
                    detail=(f"€{f.eur_amount:.2f}" if f.eur_amount is not None else ""),
                )
            )
        for s in self.signals(min(limit, 100)):
            items.append(
                EventItem(
                    at=s.created_at,
                    category="TRADING",
                    source="signal",
                    subject=s.symbol or s.mint[:10],
                    message=f"{s.kind} signal {s.status}",
                    detail=s.detail,
                )
            )
        for m in self._rows(
            "SELECT milestone_eur, equity_eur, reached_at, direction FROM milestones "
            "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (self.session_id, 50),
        ):
            at = _parse_dt(m["reached_at"])
            if at is not None:
                items.append(
                    EventItem(
                        at=at,
                        category="TRADING",
                        source="milestone",
                        subject="equity",
                        message=f"milestone €{m['milestone_eur']} {m['direction']}",
                        detail=f"equity €{m['equity_eur']}",
                    )
                )
        if category != "ALL":
            items = [i for i in items if i.category == category]
        items.sort(key=lambda i: i.at, reverse=True)
        return items[:limit]

    # ---------------------------------------------------------------- token
    def search_tokens(self, query: str, limit: int = 20) -> list[TokenHit]:
        self.require_schema()
        q = query.strip()
        if not q:
            return []
        like = f"%{q}%"
        rows = self._rows(
            "SELECT mint, symbol, name, final_state FROM tokens "
            "WHERE mint = ? OR mint LIKE ? OR symbol LIKE ? OR name LIKE ? "
            "ORDER BY discovered_at DESC LIMIT ?",
            (q, like, like, like, limit),
        )
        return [
            TokenHit(
                mint=str(r["mint"]),
                symbol=_str(r["symbol"]),
                name=_str(r["name"]),
                final_state=_str(r["final_state"]),
            )
            for r in rows
        ]

    def token_detail(self, mint: str, *, limit: int = 500) -> TokenDetail:
        self.require_schema()
        sid = self.session_id
        tok = self._one(
            "SELECT mint, session_id, symbol, name, decimals, venue, source, pool_address, "
            "created_at, pool_created_at, discovered_at, final_state FROM tokens WHERE mint = ?",
            (mint,),
        )
        transitions = tuple(
            TransitionView(
                at=_parse_dt(r["at"]) or datetime.now(tz=UTC),
                source=str(r["source"]),
                target=str(r["target"]),
                reason=_text(r["reason"]),
            )
            for r in self._rows(
                "SELECT source, target, at, reason FROM state_transitions "
                "WHERE mint = ? ORDER BY id DESC LIMIT ?",
                (mint, limit),
            )
        )[::-1]
        scores = tuple(
            ScorePoint(
                at=_parse_dt(r["scored_at"]) or datetime.now(tz=UTC),
                score=float(r["score"]),
                reasons=tuple(str(x) for x in (_json(r["payload"]).get("reasons") or [])),
                penalties=tuple(str(x) for x in (_json(r["payload"]).get("penalties") or [])),
            )
            for r in self._rows(
                "SELECT scored_at, score, payload FROM scores WHERE mint = ? "
                "ORDER BY id DESC LIMIT ?",
                (mint, limit),
            )
        )[::-1]
        feat = self._one(
            "SELECT computed_at, payload FROM features WHERE session_id = ? AND mint = ? "
            "ORDER BY id DESC LIMIT 1",
            (sid, mint),
        )
        chk = self._one(
            "SELECT evaluated_at, verdict, payload FROM check_results WHERE mint = ? "
            "ORDER BY id DESC LIMIT 1",
            (mint,),
        )
        check_results: tuple[dict[str, Any], ...] = ()
        if chk is not None:
            raw_results = _json(chk["payload"]).get("results")
            if isinstance(raw_results, list):
                check_results = tuple(
                    {
                        "name": _text(r.get("name")),
                        "verdict": _text(r.get("verdict")),
                        "fatal": bool(r.get("fatal")),
                        "reason": _text(r.get("reason")),
                        "value": _text(r.get("value")),
                    }
                    for r in raw_results
                    if isinstance(r, dict)
                )
        quotes = tuple(
            QuoteView(
                quote_id=str(r["quote_id"]),
                quoted_at=_parse_dt(r["quoted_at"]) or datetime.now(tz=UTC),
                provider=str(r["provider"]),
                input_mint=str(r["input_mint"]),
                output_mint=str(r["output_mint"]),
                in_amount_raw=_int(_json(r["payload"]).get("in_amount_raw")),
                out_amount_raw=_int(_json(r["payload"]).get("out_amount_raw")),
                price_impact_pct=_flt(_json(r["payload"]).get("price_impact_pct")),
                slippage_bps=_int(_json(r["payload"]).get("slippage_bps")),
                route=" > ".join(str(x) for x in (_json(r["payload"]).get("route_labels") or [])),
                latency_ms=_flt(_json(r["payload"]).get("latency_ms")),
            )
            for r in self._rows(
                "SELECT quote_id, quoted_at, provider, input_mint, output_mint, payload "
                "FROM quotes WHERE mint = ? ORDER BY quoted_at DESC LIMIT 50",
                (mint,),
            )
        )
        attempts = tuple(self.entry_attempts(limit=50, mint=mint))
        sig_rows = self._rows(
            "SELECT signal_id, mint, kind, created_at, expires_at, status, payload FROM signals "
            "WHERE mint = ? ORDER BY created_at DESC LIMIT 50",
            (mint,),
        )
        decisions: dict[str, sqlite3.Row] = {}
        if sig_rows:
            ids = [str(r["signal_id"]) for r in sig_rows]
            placeholders = ",".join("?" for _ in ids)
            decisions = {
                str(d["signal_id"]): d
                for d in self._rows(
                    "SELECT signal_id, kind, source, decided_at FROM decisions "
                    f"WHERE signal_id IN ({placeholders}) ORDER BY decided_at ASC",
                    ids,
                )
            }
        signals = tuple(_signal_view(r, decisions.get(str(r["signal_id"]))) for r in sig_rows)
        symbol = _str(tok["symbol"]) if tok is not None else None
        fills = tuple(
            _fill_view(r, {mint: symbol})
            for r in self._rows(
                "SELECT fill_id, signal_id, mint, side, filled_at, payload FROM fills "
                "WHERE mint = ? ORDER BY filled_at DESC LIMIT 50",
                (mint,),
            )
        )
        outcomes: list[OutcomeView] = []
        for r in self._rows(
            "SELECT payload FROM outcomes WHERE mint = ? ORDER BY first_seen_at DESC LIMIT 20",
            (mint,),
        ):
            try:
                outcomes.append(_outcome_view(dataclass_from_dict(Outcome, _json(r["payload"]))))
            except (TypeError, ValueError, InvalidOperation):
                continue
        obs_rows = self._rows(
            "SELECT observed_at, source, payload FROM observations "
            "WHERE session_id = ? AND mint = ? ORDER BY id DESC LIMIT ?",
            (sid, mint, limit),
        )
        observations = tuple(
            ObservationPoint(
                at=_parse_dt(r["observed_at"]) or datetime.now(tz=UTC),
                price_native=_flt(_json(r["payload"]).get("price_native")),
                liquidity_usd=_flt(_json(r["payload"]).get("liquidity_usd")),
                volume_5m_usd=_flt(_json(r["payload"]).get("volume_5m_usd")),
                source=str(r["source"]),
            )
            for r in obs_rows
        )[::-1]
        cap = 20_000
        obs_count = self._count(
            "SELECT COUNT(*) FROM (SELECT 1 FROM observations WHERE session_id = ? AND mint = ? "
            "LIMIT ?)",
            (sid, mint, cap),
        )
        return TokenDetail(
            mint=mint,
            found=tok is not None,
            symbol=symbol,
            name=_str(tok["name"]) if tok is not None else None,
            decimals=_int(tok["decimals"]) if tok is not None else None,
            venue=_str(tok["venue"]) if tok is not None else None,
            source=_str(tok["source"]) if tok is not None else None,
            pool_address=_str(tok["pool_address"]) if tok is not None else None,
            created_at=_parse_dt(tok["created_at"]) if tok is not None else None,
            pool_created_at=_parse_dt(tok["pool_created_at"]) if tok is not None else None,
            discovered_at=_parse_dt(tok["discovered_at"]) if tok is not None else None,
            final_state=_str(tok["final_state"]) if tok is not None else None,
            first_session_id=_str(tok["session_id"]) if tok is not None else None,
            transitions=transitions,
            scores=scores,
            features=_json(feat["payload"]) if feat is not None else None,
            features_at=_parse_dt(feat["computed_at"]) if feat is not None else None,
            checks=check_results,
            checks_at=_parse_dt(chk["evaluated_at"]) if chk is not None else None,
            check_verdict=_str(chk["verdict"]) if chk is not None else None,
            quotes=quotes,
            attempts=attempts,
            signals=signals,
            fills=fills,
            outcomes=tuple(outcomes),
            observations=observations,
            observation_count=obs_count,
            observation_count_capped=obs_count >= cap,
        )

    # ------------------------------------------------------------- outcomes
    def outcome_summary(
        self, *, include_truncated: bool = False, min_observations: int = 5, limit: int = 100_000
    ) -> OutcomeSummary:
        """The same numbers `solana-sniper evaluate` prints for this session."""
        self.require_schema()
        rows = self._rows(
            "SELECT payload FROM outcomes WHERE session_id = ? ORDER BY first_seen_at ASC LIMIT ?",
            (self.session_id, limit),
        )
        outcomes: list[Outcome] = []
        for r in rows:
            try:
                outcomes.append(dataclass_from_dict(Outcome, _json(r["payload"])))
            except (TypeError, ValueError, InvalidOperation):
                continue
        report = summarize(
            outcomes, include_truncated=include_truncated, min_observations=min_observations
        )
        integrity = self.integrity()
        incomplete = (integrity,) if integrity is not None and not integrity.complete else ()
        recent = tuple(_outcome_view(o) for o in outcomes[-200:][::-1])
        return OutcomeSummary(
            report=report,
            total_rows=len(rows),
            incomplete=incomplete,
            include_truncated=include_truncated,
            min_observations=min_observations,
            rows=recent,
        )


def _autonomy_health(raw: object) -> AutonomyHealth | None:
    """The heartbeat's autonomy block, scrubbed like every other free text."""
    if not isinstance(raw, dict):
        return None
    caps_raw = raw.get("caps")
    return AutonomyHealth(
        armed=bool(raw.get("armed")),
        state=_text(raw.get("state")) or "unknown",
        kill_switch=bool(raw.get("kill_switch")),
        disarmed_reason=_text(raw.get("disarmed_reason")) if raw.get("disarmed_reason") else None,
        wallet_public_key=_str(raw.get("wallet_public_key")),
        wallet_sol=_str(raw.get("wallet_sol")),
        wallet_checked_at=_parse_dt(raw.get("wallet_checked_at")),
        spent_today_sol=_str(raw.get("spent_today_sol")),
        loss_sol=_str(raw.get("loss_sol")),
        max_total_loss_sol=_str(raw.get("max_total_loss_sol")),
        caps={str(k): v for k, v in caps_raw.items()} if isinstance(caps_raw, dict) else {},
        intents_in_flight=_int(raw.get("intents_in_flight")) or 0,
        sends=_int(raw.get("sends")) or 0,
        confirmed=_int(raw.get("confirmed")) or 0,
        failed=_int(raw.get("failed")) or 0,
        last_send_at=_parse_dt(raw.get("last_send_at")),
        last_confirmed_at=_parse_dt(raw.get("last_confirmed_at")),
    )


def _engine_health_empty(
    integrity: Integrity | None,
    *,
    found: bool,
    other: str | None = None,
    written_at: datetime | None = None,
    age_s: float | None = None,
) -> EngineHealth:
    return EngineHealth(
        heartbeat_found=found,
        heartbeat_for_this_session=False,
        heartbeat_session_id=other,
        fresh=False,
        written_at=written_at,
        age_s=age_s,
        state=None,
        healthy=None,
        problems=(),
        degraded=(),
        uptime_s=None,
        pid=None,
        hostname=None,
        starts=None,
        previous_exit=None,
        last_tick_age_s=None,
        tick_p50_ms=None,
        tick_p95_ms=None,
        evaluations=None,
        market_ok=None,
        watched=None,
        last_snapshot_age_s=None,
        snapshots_last_minute=None,
        discovery_rate_per_s=None,
        db_ok=None,
        db_last_flush_age_s=None,
        db_last_error=None,
        db_failures=None,
        db_dropped=None,
        db_queued=None,
        db_integrity={},
        counters={},
        connections=(),
        tokens_monitored=None,
        qualified=None,
        pending_signals=None,
        last_signal_at=None,
        last_error=None,
        records_verified_onchain=False,
        integrity=integrity,
        stop_reason=None,
    )
