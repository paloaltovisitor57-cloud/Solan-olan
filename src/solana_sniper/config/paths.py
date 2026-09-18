"""Persistent runtime home: database, logs and runtime state live outside the repository.

Resolution order (identical for the launchd service, the shell scripts and the bare CLI, so
`solana-sniper status` always looks at the same database the service writes):
1. `SNIPER_HOME` environment variable (`--home` on the CLI sets it for the process)
2. the platform default:
   * macOS:  ~/Library/Application Support/SolanaSniper
   * other:  ~/.local/share/solana-sniper
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

HOME_ENV = "SNIPER_HOME"
DB_DIR = "db"
LOG_DIR = "logs"
STATE_DIR = "state"
ENV_FILE = "sniper.env"
STATUS_FILE = "status.json"
COMMANDS_FILE = "commands"


def platform_default_home() -> Path:
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "SolanaSniper"
    return Path.home() / ".local" / "share" / "solana-sniper"


_HOME_OVERRIDE: Path | None = None


def set_home_override(path: Path | None) -> None:
    """Process-local `--home` override (the CLI never mutates os.environ, so nothing leaks into
    child processes or, in tests, into later commands)."""
    global _HOME_OVERRIDE
    _HOME_OVERRIDE = path.expanduser() if path is not None else None


def configured_home() -> Path:
    """`--home` override, else `SNIPER_HOME`, else the platform default. Never None: every
    process, service or CLI, resolves the same runtime home unless told otherwise."""
    if _HOME_OVERRIDE is not None:
        return _HOME_OVERRIDE
    raw = os.environ.get(HOME_ENV)
    if raw:
        return Path(raw).expanduser()
    return platform_default_home()


def home_source() -> str:
    if _HOME_OVERRIDE is not None:
        return "--home"
    return "SNIPER_HOME" if os.environ.get(HOME_ENV) else "platform default"


def ensure_home(home: Path) -> Path:
    for sub in (DB_DIR, LOG_DIR, STATE_DIR):
        (home / sub).mkdir(parents=True, exist_ok=True)
    return home


def state_path(home: Path | None, name: str) -> Path:
    base = (home / STATE_DIR) if home else Path("data") / STATE_DIR
    base.mkdir(parents=True, exist_ok=True)
    return base / name
