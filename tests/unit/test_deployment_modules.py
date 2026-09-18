from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from typer.testing import CliRunner

from solana_sniper.app.command_file import FileCommandSource
from solana_sniper.app.health import read_status, status_is_fresh
from solana_sniper.cli.main import app
from solana_sniper.config import load_settings
from solana_sniper.config.paths import platform_default_home
from solana_sniper.storage.migrations import run_migrations, schema_version


def test_loader_relocates_state_under_sniper_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("SNIPER_HOME", str(home))
    (home).mkdir()
    (home / "sniper.env").write_text("SNIPER_RISK__PROFILE=NORMAL\n")
    s = load_settings(Path("configs/default.yaml"))
    assert s.home == home
    assert s.storage.database_url == f"sqlite+aiosqlite:///{home / 'db' / 'sniper.db'}"
    assert s.telemetry.log_file == str(home / "logs" / "sniper.log")
    assert s.state_dir == home / "state"
    assert (home / "db").is_dir() and (home / "logs").is_dir() and (home / "state").is_dir()
    assert str(s.risk.profile) == "NORMAL"  # sniper.env is honoured
    monkeypatch.setenv("SNIPER_STORAGE__DATABASE_URL", "sqlite+aiosqlite:////abs/custom.db")
    s2 = load_settings(Path("configs/default.yaml"))
    assert (
        s2.storage.database_url == "sqlite+aiosqlite:////abs/custom.db"
    )  # absolute path respected
    monkeypatch.delenv("SNIPER_HOME")
    monkeypatch.delenv("SNIPER_STORAGE__DATABASE_URL")
    s3 = load_settings(Path("configs/default.yaml"))
    assert s3.home is None and s3.storage.database_url.startswith("sqlite+aiosqlite:///./data/")
    assert platform_default_home().name in ("SolanaSniper", "solana-sniper")


async def test_file_command_source_consumes_complete_lines(tmp_path: Path) -> None:
    path = tmp_path / "commands"
    path.write_text("stale command\n")  # written before the process started: skipped
    src = FileCommandSource(path, poll_s=0.01)
    seen: list[str] = []

    async def handler(line: str) -> None:
        seen.append(line)

    task = asyncio.create_task(src.run(handler))
    await asyncio.sleep(0.05)
    with path.open("a") as fh:
        fh.write("b 1\n")
        fh.write("s 2")  # incomplete line: must wait for the newline
    await asyncio.sleep(0.05)
    assert seen == ["b 1"]
    with path.open("a") as fh:
        fh.write("\n\n  i 3  \n")
    await asyncio.sleep(0.05)
    assert seen == ["b 1", "s 2", "i 3"]
    path.write_text("")  # operator truncates the file
    with path.open("a") as fh:
        fh.write("p\n")
    await asyncio.sleep(0.05)
    assert seen[-1] == "p" and src.processed == 4
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_migrations_are_idempotent(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path}/m.db"
    assert await run_migrations(url) == [1, 2, 3]
    assert await run_migrations(url) == []
    assert await schema_version(url) == 3


def test_cli_config_check_and_migrate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SNIPER_HOME", str(tmp_path / "home"))
    runner = CliRunner()
    res = runner.invoke(app, ["config-check", "-c", "configs/synthetic.yaml"])
    assert res.exit_code == 0, res.output
    assert "configuration ok" in res.output and str(tmp_path / "home") in res.output
    bad = tmp_path / "bad.yaml"
    bad.write_text("entry:\n  min_score: 150\n")
    res_bad = runner.invoke(app, ["config-check", "-c", str(bad)])
    assert res_bad.exit_code == 1 and "min_score" in res_bad.output
    broken = tmp_path / "broken.yaml"
    broken.write_text("risk:\n  profile: NOPE\n")
    assert runner.invoke(app, ["config-check", "-c", str(broken)]).exit_code == 1
    res_m = runner.invoke(app, ["migrate", "-c", "configs/synthetic.yaml"])
    assert res_m.exit_code == 0 and "schema version 3" in res_m.output, res_m.output
    assert (tmp_path / "home" / "db" / "sniper-synthetic.db").exists()
    res_h = runner.invoke(app, ["health", "-c", "configs/synthetic.yaml"])
    assert res_h.exit_code == 2 and "no heartbeat" in res_h.output
    assert (
        runner.invoke(app, ["health", "-c", "configs/synthetic.yaml", "--quiet-check"]).exit_code
        == 2
    )


def test_status_helpers(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    assert read_status(path) is None
    path.write_text("not json")
    assert read_status(path) is None
    path.write_text('{"written_at": "2000-01-01T00:00:00+00:00", "healthy": true}')
    status = read_status(path)
    assert status is not None and not status_is_fresh(status)
    from datetime import UTC, datetime

    path.write_text(
        f'{{"written_at": "{datetime.now(tz=UTC).isoformat()}", "healthy": true, "stopped": true}}'
    )
    assert not status_is_fresh(read_status(path) or {})
