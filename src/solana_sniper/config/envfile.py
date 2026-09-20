"""Read and update `<home>/sniper.env` (KEY=value lines) without disturbing anything else.

`solana-sniper wallet create` and `solana-sniper arm` use this so the autonomous setup needs no
manual editing: they write the key-file *path* and the `SNIPER_AUTONOMY__*` caps. Values written
here are never key material; the wallet key lives in its own 0600 file.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

HEADER = "# solana-sniper local configuration (never committed). Loaded by the service and the CLI."


def _key_of(line: str) -> str | None:
    """The KEY of a `KEY=value` line (also `export KEY=value`), else None."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key = stripped.split("=", 1)[0].strip()
    if key.startswith("export "):
        key = key[len("export ") :].strip()
    return key or None


def _commented_key_of(line: str) -> str | None:
    """The KEY of a commented-out `#KEY=` template line, so it can be filled in place."""
    stripped = line.strip()
    if not stripped.startswith("#"):
        return None
    return _key_of(stripped.lstrip("#").strip())


def read_env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw in lines:
        key = _key_of(raw)
        if key is None:
            continue
        value = raw.strip().split("=", 1)[1].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def set_env_values(path: Path, values: Mapping[str, str | None]) -> None:
    """Set `KEY=value` lines in place (None comments the key out). Other lines are kept; a key
    with no real line fills its commented template line `#KEY=` if there is one, else it is
    appended; later duplicates of a written key are dropped (the last line would otherwise win
    in dotenv precedence). Written atomically with mode 0600."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = [HEADER]
    pending = dict(values)
    real_keys = {k for k in (_key_of(line) for line in lines) if k is not None}
    written: set[str] = set()
    out: list[str] = []
    for raw in lines:
        key = _key_of(raw)
        if key is not None and key in written:
            continue
        target = key
        if target is None:
            commented = _commented_key_of(raw)
            if commented is not None and commented not in real_keys:
                target = commented
        if target is not None and target in pending and target not in written:
            new = pending[target]
            out.append(f"{target}={new}" if new is not None else f"# {target}=")
            written.add(target)
            continue
        out.append(raw)
    for key, new in pending.items():
        if key not in written and new is not None:
            out.append(f"{key}={new}")
    text = "\n".join(out).rstrip("\n") + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
