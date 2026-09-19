"""Find every session in the runtime home and tell which one the engine is running.

* PAPER sessions: one database each under `<home>/db/paper/paper-*.db`
* LIVE / DRY_RUN / REPLAY sessions: rows of the `sessions` table in `<home>/db/*.db`
  (`sniper.db` for the service, `sniper-synthetic.db` for the offline config)

The running session is whichever the heartbeat (`<home>/state/status.json`) names while it is
fresh. Discovery only reads: every database is opened read-only and closed immediately.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from solana_sniper.app.health import read_status, status_is_fresh
from solana_sniper.app.paper import PAPER_DIR
from solana_sniper.config.paths import DB_DIR, STATE_DIR, STATUS_FILE, configured_home
from solana_sniper.telemetry.redaction import safe_exception
from solana_sniper.web.data import DashboardError, DashboardRepository
from solana_sniper.web.models import Heartbeat, SessionRef

HEARTBEAT_FRESH_S = 30.0


def runtime_home() -> Path:
    return configured_home()


def paper_dir(home: Path) -> Path:
    return home / DB_DIR / PAPER_DIR


def live_db_paths(home: Path) -> list[Path]:
    folder = home / DB_DIR
    if not folder.exists():
        return []
    return sorted(p for p in folder.glob("*.db") if p.is_file())


def paper_db_paths(home: Path) -> list[Path]:
    folder = paper_dir(home)
    if not folder.exists():
        return []
    return sorted((p for p in folder.glob("paper-*.db") if p.is_file()), reverse=True)


def read_heartbeat(home: Path, *, fresh_after_s: float = HEARTBEAT_FRESH_S) -> Heartbeat | None:
    path = home / STATE_DIR / STATUS_FILE
    status = read_status(path)
    if status is None:
        return None
    written_raw = status.get("written_at")
    written: datetime | None = None
    if isinstance(written_raw, str):
        try:
            written = datetime.fromisoformat(written_raw)
        except ValueError:
            written = None
    age = (datetime.now(tz=UTC) - written).total_seconds() if written is not None else None
    sid = status.get("session_id")
    return Heartbeat(
        path=path,
        status=status,
        fresh=status_is_fresh(status, fresh_after_s),
        session_id=str(sid) if sid else None,
        written_at=written,
        age_s=age,
    )


def running_session_id(heartbeat: Heartbeat | None) -> str | None:
    if heartbeat is None or not heartbeat.fresh:
        return None
    return heartbeat.session_id


def _paper_ref(path: Path, running: str | None, busy_timeout_s: float) -> SessionRef:
    sid = path.stem
    repo = DashboardRepository(path, sid, busy_timeout_s=busy_timeout_s, retries=1)
    try:
        meta = repo.paper_meta()
        row = repo.session_row()
    except DashboardError as exc:
        return SessionRef(
            kind="PAPER",
            session_id=sid,
            db_path=path,
            mode="PAPER",
            name=None,
            started_at=None,
            ended_at=None,
            running=sid == running,
            requested=None,
            note=f"unreadable: {safe_exception(exc)}"[:120],
        )
    return SessionRef(
        kind="PAPER",
        session_id=sid,
        db_path=path,
        mode=row["mode"] if row else "PAPER",
        name=meta.name if meta else None,
        started_at=(row["started_at"] if row else None) or (meta.created_at if meta else None),
        ended_at=row["ended_at"] if row else None,
        running=sid == running,
        requested=meta.requested if meta else None,
        note=None if meta else "no paper metadata",
    )


def _live_refs(
    path: Path, running: str | None, busy_timeout_s: float, limit: int
) -> list[SessionRef]:
    repo = DashboardRepository(path, "", busy_timeout_s=busy_timeout_s, retries=1)
    try:
        rows = repo.sessions(limit=limit)
    except DashboardError as exc:
        return [
            SessionRef(
                kind="LIVE",
                session_id=path.stem,
                db_path=path,
                mode="UNKNOWN",
                name=None,
                started_at=None,
                ended_at=None,
                running=False,
                note=f"unreadable: {safe_exception(exc)}"[:120],
            )
        ]
    return [
        SessionRef(
            kind="LIVE",
            session_id=str(r["session_id"]),
            db_path=path,
            mode=str(r["mode"]),
            name=None,
            started_at=r["started_at"],
            ended_at=r["ended_at"],
            running=str(r["session_id"]) == running,
        )
        for r in rows
    ]


def _newest_first(ref: SessionRef) -> tuple[datetime, str]:
    return (ref.started_at or datetime.min.replace(tzinfo=UTC), ref.session_id)


def discover_sessions(
    home: Path,
    *,
    heartbeat: Heartbeat | None = None,
    busy_timeout_s: float = 2.0,
    live_limit: int = 50,
) -> list[SessionRef]:
    """PAPER sessions first (newest first), then LIVE/DRY_RUN/REPLAY sessions of every
    top-level database, newest first."""
    running = running_session_id(heartbeat)
    refs: list[SessionRef] = [_paper_ref(p, running, busy_timeout_s) for p in paper_db_paths(home)]
    refs.sort(key=_newest_first, reverse=True)
    live: list[SessionRef] = []
    for path in live_db_paths(home):
        live.extend(_live_refs(path, running, busy_timeout_s, live_limit))
    live.sort(key=_newest_first, reverse=True)
    return refs + live


def resolve_selection(
    refs: list[SessionRef], *, paper: str | None = None, session: str | None = None
) -> SessionRef | None:
    """Explicit `--paper` / `--session` wins, then the running session, then the newest."""
    if paper:
        for r in refs:
            if r.kind == "PAPER" and r.session_id == paper:
                return r
        return None
    if session:
        for r in refs:
            if r.session_id == session:
                return r
        return None
    for r in refs:
        if r.running:
            return r
    return refs[0] if refs else None
