"""`solana-sniper paper` end to end on the synthetic world: fresh isolated sessions, explicit
resume, listing, graceful Ctrl+C and the final report. No network."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from solana_sniper.app.paper import PaperSession
from solana_sniper.cli.main import app
from solana_sniper.storage.repository import Repository

REPO = Path(__file__).resolve().parents[2]
CFG = ["-c", "configs/synthetic.yaml"]


def _runner() -> CliRunner:
    return CliRunner(env={"COLUMNS": "200"})


def _paper_dbs(home: Path) -> list[Path]:
    return sorted((home / "db" / "paper").glob("paper-*.db"))


async def _meta(
    db: Path,
) -> tuple[PaperSession | None, dict[str, object] | None, list[dict[str, object]]]:
    repo = Repository(f"sqlite+aiosqlite:///{db}", session_id="t")
    await repo.init()
    try:
        meta = await repo.paper_session(db.stem)
        state = await repo.load_state()
        sessions = await repo.list_sessions()
    finally:
        await repo.close()
    account = {"cash": str(state.cash), "has_account": state.has_account} if state else None
    return meta, account, sessions


def test_fresh_paper_session_never_inherits_an_older_bankroll(
    isolated_runtime_home: Path,
) -> None:
    home = isolated_runtime_home
    res = _runner().invoke(
        app, ["paper", *CFG, "--bankroll-eur", "100", "--duration", "2", "--no-dashboard"]
    )
    assert res.exit_code == 0, res.output
    out = res.output
    assert "SOLANA SNIPER — PAPER" in out and "Real transactions: DISABLED" in out
    assert "Starting equity:   €100.00" in out and "SESSION COMPLETE" in out
    assert "Session:           paper-" in out and "-100eur-" in out
    dbs = _paper_dbs(home)
    assert len(dbs) == 1
    # second experiment with a different bankroll: new session, new database, fresh account
    res2 = _runner().invoke(
        app,
        [
            "paper",
            *CFG,
            "--bankroll-sol",
            "1",
            "--duration",
            "2",
            "--no-dashboard",
            "--name",
            "test-a",
        ],
    )
    assert res2.exit_code == 0, res2.output
    assert "Starting bankroll: 1.0000 SOL" in res2.output
    assert "Starting equity:   €150.00" in res2.output  # synthetic static rate €150/SOL
    assert "synthetic rate" in res2.output and "Resumed equity" not in res2.output
    dbs2 = _paper_dbs(home)
    assert len(dbs2) == 2 and any(p.stem.endswith("-test-a") for p in dbs2)
    import asyncio

    for db in dbs2:
        meta, account, sessions = asyncio.run(_meta(db))
        assert meta is not None and meta.session_id == db.stem and account is not None
        assert account["has_account"] and sessions and sessions[0]["ended_at"] is not None
    metas = {asyncio.run(_meta(db))[0].requested for db in dbs2}  # type: ignore[union-attr]
    assert metas == {"100 EUR", "1 SOL"}
    # sessions are listed, and the global --paper option reads one session's database
    lst = _runner().invoke(app, ["paper", *CFG, "--list"])
    assert lst.exit_code == 0 and "100 EUR" in lst.output and "1 SOL" in lst.output
    named = next(p.stem for p in dbs2 if p.stem.endswith("-test-a"))
    st = _runner().invoke(app, ["--paper", named, "status", *CFG, "--json"])
    assert st.exit_code == 0, st.output
    payload = json.loads(st.output)
    assert payload["database_url"].endswith(f"/db/paper/{named}.db")
    assert payload["sessions"][0]["session_id"] == named and payload["portfolio"] is not None
    missing = _runner().invoke(app, ["--paper", "paper-nope", "status", *CFG])
    assert missing.exit_code == 2 and "no paper session" in missing.output


def test_explicit_resume_restores_the_recorded_bankroll(isolated_runtime_home: Path) -> None:
    home = isolated_runtime_home
    res = _runner().invoke(
        app, ["paper", *CFG, "--bankroll-eur", "80", "--duration", "2", "--no-dashboard"]
    )
    assert res.exit_code == 0, res.output
    (db,) = _paper_dbs(home)
    sid = db.stem
    res2 = _runner().invoke(
        app, ["paper", *CFG, "--resume", sid, "--duration", "2", "--no-dashboard"]
    )
    assert res2.exit_code == 0, res2.output
    assert f"Session:           {sid}" in res2.output and "Starting equity:   €80.00" in res2.output
    assert len(_paper_dbs(home)) == 1  # no new database
    log = (home / "logs" / "paper" / f"{sid}.log").read_text(errors="replace")
    assert log.count("portfolio_initialised") == 1 and "portfolio_restored" in log
    # guard rails
    bad = _runner().invoke(app, ["paper", *CFG, "--resume", sid, "--bankroll-sol", "1"])
    assert bad.exit_code == 2 and "do not combine" in bad.output
    missing = _runner().invoke(app, ["paper", *CFG, "--resume", "paper-nope", "--duration", "1"])
    assert missing.exit_code == 2 and "no paper session" in missing.output
    nan = _runner().invoke(app, ["paper", *CFG, "--bankroll-sol", "abc"])
    assert nan.exit_code == 2 and "not a number" in nan.output
    both = _runner().invoke(app, ["paper", *CFG, "--bankroll-sol", "1", "--bankroll-eur", "5"])
    assert both.exit_code == 2 and "exactly one" in both.output


@pytest.mark.timeout(120)
def test_ctrl_c_stops_cleanly_and_prints_the_report(isolated_runtime_home: Path) -> None:
    home = isolated_runtime_home
    env = {**os.environ, "SNIPER_HOME": str(home), "COLUMNS": "200", "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "solana_sniper.cli.main",
            "paper",
            *CFG,
            "--bankroll-eur",
            "50",
            "--no-dashboard",
            "--quiet",
        ],
        cwd=REPO,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 60
        status = home / "state" / "status.json"
        while time.monotonic() < deadline:
            if status.exists() and json.loads(status.read_text()).get("healthy"):
                break
            time.sleep(0.5)
        else:
            proc.kill()
            pytest.fail("paper session never became healthy")
        time.sleep(2)
        proc.send_signal(signal.SIGINT)
        out, _ = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, out
    assert (
        "SESSION COMPLETE" in out
        and "Ctrl+C / signal" in out
        and "Real transactions: DISABLED" in out
    )
    final = json.loads(status.read_text())
    assert final["stopped"] is True and final["state"] == "STOPPED"
    boot = json.loads((home / "state" / "boot.json").read_text())
    assert boot["stopped"] is True and boot["stop_reason"] == "Ctrl+C / signal"
    (db,) = _paper_dbs(home)
    import asyncio

    meta, _account, sessions = asyncio.run(_meta(db))
    assert meta is not None and sessions[0]["ended_at"] is not None
    assert "Storage errors:    0" in out and "Dropped writes:    0" in out
