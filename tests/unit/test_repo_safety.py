"""The git-ignore safety check must be right on a fresh clone (no data/ directory), independent
of HOME / SNIPER_HOME / cwd, and must fail only when the checkout is genuinely unsafe."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from solana_sniper.app.bootrecord import read_boot, record_start, record_stop
from solana_sniper.app.repo_safety import check_repo_safety

REPO = Path(__file__).resolve().parents[2]
GITIGNORE = (REPO / ".gitignore").read_text()


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@x",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@x",
        },
    )


def _fresh_repo(
    path: Path, gitignore: str = GITIGNORE, tracked_extra: dict[str, str] | None = None
) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    (path / ".gitignore").write_text(gitignore)
    (path / "README.md").write_text("x\n")
    (path / ".env.example").write_text("SNIPER_X=\n")
    for rel, body in (tracked_extra or {}).items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(body)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _shell_check(repo: Path, env: dict[str, str]) -> tuple[int, str]:
    res = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{REPO}/scripts/common.sh"; '
            f'if repo_ignores_secrets "{repo}"; then rc=0; else rc=$?; fi; '
            'echo "$REPO_SAFETY_REASON"; exit $rc',
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return res.returncode, res.stdout.strip()


@pytest.fixture
def temp_env(tmp_path: Path) -> dict[str, str]:
    # a foreign HOME and SNIPER_HOME must not influence the result
    return {
        **os.environ,
        "HOME": str(tmp_path / "otherhome"),
        "SNIPER_HOME": str(tmp_path / "otherhome" / "sniper"),
    }


def test_fresh_clone_without_data_dir_is_safe(tmp_path: Path, temp_env: dict[str, str]) -> None:
    repo = _fresh_repo(tmp_path / "fresh")
    assert not (repo / "data").exists()  # the old check failed exactly here
    assert check_repo_safety(repo).status == "PASS"
    rc, reason = _shell_check(repo, temp_env)
    assert rc == 0, reason
    # the old, directory-based check reproduces the false failure on this very repo
    old = subprocess.run(
        ["git", "-C", str(repo), "check-ignore", "-q", str(repo / "data")],
        capture_output=True,
        check=False,
    )
    assert old.returncode == 1


def test_missing_env_pattern_is_unsafe(tmp_path: Path, temp_env: dict[str, str]) -> None:
    repo = _fresh_repo(tmp_path / "noenv", gitignore="data/\n*.db\nlogs/*\n")
    res = check_repo_safety(repo)
    assert res.status == "FAIL" and ".env is not git-ignored" in res.detail
    rc, reason = _shell_check(repo, temp_env)
    assert rc == 1 and ".env is not git-ignored" in reason


def test_tracked_secret_file_is_unsafe_even_if_ignored_now(
    tmp_path: Path, temp_env: dict[str, str]
) -> None:
    repo = _fresh_repo(tmp_path / "tracked", gitignore="data/\n", tracked_extra={".env": "K=v\n"})
    (repo / ".gitignore").write_text(GITIGNORE)  # ignored from now on, but already committed
    res = check_repo_safety(repo)
    assert res.status == "FAIL" and ".env is tracked by git" in res.detail
    rc, reason = _shell_check(repo, temp_env)
    assert rc == 1 and ".env" in reason  # git reports a tracked file as "not ignored" first


def test_not_a_checkout_is_skipped_not_failed(tmp_path: Path, temp_env: dict[str, str]) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    res = check_repo_safety(plain)
    assert res.status == "SKIP"
    rc, _ = _shell_check(plain, temp_env)
    assert rc == 2


def test_this_repository_is_safe_from_any_cwd_and_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    res = check_repo_safety()  # resolves the checkout from the package location
    assert res.status == "PASS", res.detail


def test_boot_record_counts_starts_and_reports_unclean_exit(tmp_path: Path) -> None:
    state = tmp_path / "state"
    first = record_start(state, session_id="s1", mode="PAPER")
    assert first.starts == 1 and first.previous_exit is None
    record_stop(state, reason="signal")
    second = record_start(state, session_id="s2", mode="PAPER")
    assert second.starts == 2 and second.previous_exit == "clean: signal"
    # crash: no stop marker written -> the next start reports it
    third = record_start(state, session_id="s3", mode="PAPER")
    assert third.starts == 3 and third.previous_exit == "unclean (no stop marker)"
    assert read_boot(state)["session_id"] == "s3" and read_boot(state)["stopped"] is False
