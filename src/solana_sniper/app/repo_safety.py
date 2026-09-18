"""Repository safety: secrets and runtime data must be git-ignored and never tracked.

The check runs `git` from the repository root with paths relative to it, so it does not depend
on the caller's working directory, HOME, or SNIPER_HOME. It checks *file paths inside* ignored
directories (`data/x.db`) rather than the directories themselves, because a directory pattern
such as `data/` only matches a directory that exists on disk, and a correct installation keeps
its database outside the repository, so `data/` usually does not exist.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Paths that must be ignored (relative to the repository root).
MUST_IGNORE: tuple[str, ...] = (
    ".env",
    "sniper.env",
    "data/sniper.db",
    "data/state/status.json",
    "logs/sniper.log",
)
# Paths that must never be tracked, even if a later .gitignore edit would ignore them.
MUST_NOT_TRACK: tuple[str, ...] = (".env", "sniper.env", "data", "logs/sniper.log")


@dataclass(frozen=True, slots=True)
class SafetyResult:
    status: str  # PASS | FAIL | SKIP
    detail: str


def repo_root_from_package() -> Path | None:
    """The checkout this package was installed from (editable install); None when not a git
    working tree (e.g. a wheel install)."""
    root = Path(__file__).resolve().parents[3]
    return root if (root / ".git").exists() else None


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def check_repo_safety(root: Path | None = None) -> SafetyResult:
    root = root or repo_root_from_package()
    if root is None:
        return SafetyResult("SKIP", "not running from a git checkout")
    if shutil.which("git") is None:
        return SafetyResult("SKIP", "git not found on PATH; cannot verify .gitignore")
    if not (root / ".git").exists():
        return SafetyResult("SKIP", f"{root} is not a git repository")
    try:
        probe = _git(root, "rev-parse", "--is-inside-work-tree")
    except (OSError, subprocess.SubprocessError) as exc:
        return SafetyResult("SKIP", f"git unavailable: {exc}")
    if probe.returncode != 0:
        return SafetyResult("SKIP", f"git cannot read {root}: {probe.stderr.strip()[:120]}")
    unsafe: list[str] = []
    for rel in MUST_IGNORE:
        res = _git(root, "check-ignore", "-q", "--", rel)
        if res.returncode == 1:
            unsafe.append(f"{rel} is not git-ignored")
        elif res.returncode not in (0, 1):
            return SafetyResult("SKIP", f"git check-ignore failed: {res.stderr.strip()[:120]}")
    tracked = _git(root, "ls-files", "--", *MUST_NOT_TRACK)
    for line in tracked.stdout.splitlines():
        if line.strip():
            unsafe.append(f"{line.strip()} is tracked by git")
    if unsafe:
        return SafetyResult("FAIL", "; ".join(unsafe))
    return SafetyResult("PASS", "secrets and runtime data are git-ignored and untracked")
