"""Persistent runtime home: database, logs and runtime state live outside the repository.

Resolution order:
1. `SNIPER_HOME` environment variable (set by the launchd service and the shell scripts)
2. nothing → paths in the YAML config are used as-is (relative to the working directory)

Platform defaults offered to the install scripts:
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


def configured_home() -> Path | None:
    raw = os.environ.get(HOME_ENV)
    if not raw:
        return None
    return Path(raw).expanduser()


def ensure_home(home: Path) -> Path:
    for sub in (DB_DIR, LOG_DIR, STATE_DIR):
        (home / sub).mkdir(parents=True, exist_ok=True)
    return home


def state_path(home: Path | None, name: str) -> Path:
    base = (home / STATE_DIR) if home else Path("data") / STATE_DIR
    base.mkdir(parents=True, exist_ok=True)
    return base / name
