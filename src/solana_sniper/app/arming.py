"""Arming state of autonomous mode: three marker files under `<home>/state/`.

* `armed.json`     written by `solana-sniper arm`: wallet, starting balance, loss limit, caps.
* `disarmed.json`  written when a safety rail trips (loss limit) or the user runs `disarm`;
                   while it exists no new buy is placed. `arm` removes it deliberately.
* `KILL`           created by `solana-sniper kill` (or `./cmd.sh kill`): stops buys and sells
                   within one engine tick. `resume` removes it.

The engine never writes `armed.json`; it only reads these files (every tick, they are tiny) and
writes `disarmed.json` when it disarms itself. All writes are atomic (temp file + rename), like
the heartbeat, so a reader never sees a half-written marker. The dashboard does not read these
files; it reads the heartbeat, which reports the same state.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from solana_sniper.config.paths import ARMED_FILE, DISARMED_FILE, KILL_FILE, STATE_DIR


@dataclass(frozen=True, slots=True)
class ArmingState:
    armed_marker: bool
    armed_at: datetime | None
    wallet_public_key: str | None
    start_balance_lamports: int | None
    max_total_loss_sol: Decimal | None
    caps: dict[str, Any]
    kill: bool
    disarmed_reason: str | None
    disarmed_at: datetime | None

    @property
    def armed(self) -> bool:
        """Armed = the marker exists and nothing has since disarmed or killed it."""
        return self.armed_marker and not self.kill and self.disarmed_reason is None

    def describe(self) -> str:
        if self.kill:
            return "KILL switch active (buys and sells stopped)"
        if self.disarmed_reason is not None:
            return f"disarmed: {self.disarmed_reason}"
        if self.armed_marker:
            return f"armed (loss limit {self.max_total_loss_sol} SOL)"
        return "not armed"


def _state_dir(home: Path) -> Path:
    return home / STATE_DIR


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)
        os.replace(tmp, path)
    except BaseException:
        with __import__("contextlib").suppress(OSError):
            os.unlink(tmp)
        raise


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _dt(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def read(home: Path) -> ArmingState:
    state = _state_dir(home)
    armed = _read_json(state / ARMED_FILE)
    disarmed = _read_json(state / DISARMED_FILE)
    kill = (state / KILL_FILE).exists()
    loss: Decimal | None = None
    start: int | None = None
    if armed is not None:
        try:
            loss = (
                Decimal(str(armed["max_total_loss_sol"]))
                if armed.get("max_total_loss_sol")
                else None
            )
        except (InvalidOperation, ValueError):
            loss = None
        raw_start = armed.get("start_balance_lamports")
        start = (
            int(raw_start)
            if isinstance(raw_start, int | float | str) and str(raw_start).lstrip("-").isdigit()
            else None
        )
    return ArmingState(
        armed_marker=armed is not None,
        armed_at=_dt(armed.get("armed_at")) if armed else None,
        wallet_public_key=str(armed["wallet_public_key"])
        if armed and armed.get("wallet_public_key")
        else None,
        start_balance_lamports=start,
        max_total_loss_sol=loss,
        caps=dict(armed.get("caps") or {}) if armed else {},
        kill=kill,
        disarmed_reason=str(disarmed.get("reason") or "unknown") if disarmed is not None else None,
        disarmed_at=_dt(disarmed.get("at")) if disarmed else None,
    )


def arm(
    home: Path,
    *,
    wallet_public_key: str,
    start_balance_lamports: int,
    max_total_loss_sol: Decimal,
    caps: dict[str, Any],
    now: datetime | None = None,
) -> ArmingState:
    """Record the arming decision and clear a previous disarm. Never clears KILL."""
    state = _state_dir(home)
    _write_json(
        state / ARMED_FILE,
        {
            "armed_at": (now or datetime.now(tz=UTC)).isoformat(),
            "wallet_public_key": wallet_public_key,
            "start_balance_lamports": int(start_balance_lamports),
            "max_total_loss_sol": str(max_total_loss_sol),
            "caps": caps,
        },
    )
    (state / DISARMED_FILE).unlink(missing_ok=True)
    return read(home)


def disarm(home: Path, reason: str, *, now: datetime | None = None) -> ArmingState:
    """Stop new buys (exits may continue per config). Used by the user and by the rails."""
    state = _state_dir(home)
    _write_json(
        state / DISARMED_FILE,
        {"at": (now or datetime.now(tz=UTC)).isoformat(), "reason": reason[:300]},
    )
    (state / ARMED_FILE).unlink(missing_ok=True)
    return read(home)


def kill(home: Path, *, now: datetime | None = None) -> ArmingState:
    state = _state_dir(home)
    state.mkdir(parents=True, exist_ok=True)
    (state / KILL_FILE).write_text(
        f"created {(now or datetime.now(tz=UTC)).isoformat()}\n", encoding="utf-8"
    )
    return read(home)


def resume(home: Path) -> ArmingState:
    (_state_dir(home) / KILL_FILE).unlink(missing_ok=True)
    return read(home)


def readiness(
    *, enabled: bool, acknowledged: bool, key_file: str | None, state: ArmingState
) -> list[str]:
    """Everything that still stands between this configuration and autonomous trading."""
    problems: list[str] = []
    if not key_file:
        problems.append("no hot wallet: run `solana-sniper wallet create`")
    if not enabled:
        problems.append("autonomy.enabled is false (run `solana-sniper arm`)")
    if not acknowledged:
        problems.append("autonomy.acknowledge_real_money is false (run `solana-sniper arm`)")
    if not state.armed_marker:
        problems.append("not armed: run `solana-sniper arm --max-loss-sol <amount>`")
    if state.max_total_loss_sol is None and state.armed_marker:
        problems.append("armed.json has no loss limit; run `solana-sniper arm` again")
    if state.disarmed_reason is not None:
        problems.append(f"disarmed ({state.disarmed_reason}); run `solana-sniper arm` to re-arm")
    if state.kill:
        problems.append("KILL switch active; run `solana-sniper resume`")
    return problems
