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


def configured_home() -> Path:
    """`SNIPER_HOME` if set, else the platform default. Never None: every process, service or
    CLI, resolves the same runtime home unless told otherwise."""
    raw = os.environ.get(HOME_ENV)
    if raw:
        return Path(raw).expanduser()
    return platform_default_home()


def home_source() -> str:
    return "SNIPER_HOME" if os.environ.get(HOME_ENV) else "platform default"


def ensure_home(home: Path) -> Path:
    for sub in (DB_DIR, LOG_DIR, STATE_DIR):
        (home / sub).mkdir(parents=True, exist_ok=True)
    return home


def state_path(home: Path | None, name: str) -> Path:
    base = (home / STATE_DIR) if home else Path("data") / STATE_DIR
    base.mkdir(parents=True, exist_ok=True)
    return base / name
