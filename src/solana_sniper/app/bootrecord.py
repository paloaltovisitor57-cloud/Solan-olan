"""Boot record: how many times the engine started in this runtime home, and how the previous
run ended. Written to <state>/boot.json at start and stop so `status.sh` and the health
snapshot can show restarts (launchd or otherwise) instead of hiding them."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

FILE = "boot.json"


@dataclass(frozen=True, slots=True)
class BootInfo:
    starts: int
    previous_exit: str | None  # "clean: <reason>" | "unclean (no stop marker)" | None
    previous_pid: int | None
    previous_started_at: str | None


def _read(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def record_start(state_dir: Path, *, session_id: str, mode: str) -> BootInfo:
    path = state_dir / FILE
    prev = _read(path)
    raw_starts = prev.get("starts")
    starts = (raw_starts if isinstance(raw_starts, int) else 0) + 1
    previous_exit: str | None = None
    previous_pid = prev.get("pid")
    if prev:
        stop_reason = prev.get("stop_reason")
        previous_exit = (
            f"clean: {stop_reason}" if prev.get("stopped") else "unclean (no stop marker)"
        )
    _write(
        path,
        {
            "starts": starts,
            "pid": os.getpid(),
            "parent_pid": os.getppid(),
            "session_id": session_id,
            "mode": mode,
            "started_at": datetime.now(tz=UTC).isoformat(),
            "stopped": False,
            "stop_reason": None,
            "previous_exit": previous_exit,
            "previous_pid": previous_pid,
        },
    )
    return BootInfo(
        starts=starts,
        previous_exit=previous_exit,
        previous_pid=int(previous_pid) if isinstance(previous_pid, int) else None,
        previous_started_at=str(prev.get("started_at")) if prev.get("started_at") else None,
    )


def record_stop(state_dir: Path, *, reason: str) -> None:
    path = state_dir / FILE
    data = _read(path)
    if data.get("pid") not in (None, os.getpid()):
        return  # another process owns the record now
    data.update(
        {"stopped": True, "stop_reason": reason, "stopped_at": datetime.now(tz=UTC).isoformat()}
    )
    _write(path, data)


def read_boot(state_dir: Path) -> dict[str, object]:
    return _read(state_dir / FILE)
