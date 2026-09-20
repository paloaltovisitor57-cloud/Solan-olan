"""The plug-and-play surface of autonomous mode: `wallet create|import|show`, `arm`, `disarm`,
`kill`, `resume`, the `run --autonomous` refusals, the headless kill/disarm commands and the
doctor's wallet checks. No network: the balance lookup is replaced."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from solana_sniper.app import arming
from solana_sniper.app.doctor import run_doctor
from solana_sniper.cli import autonomy
from solana_sniper.cli.commands import CommandHandler
from solana_sniper.cli.main import app
from solana_sniper.config import load_settings
from solana_sniper.config.envfile import read_env_values
from solana_sniper.config.settings import Settings
from solana_sniper.wallet.keys import generate

SYN = "configs/synthetic.yaml"
LIVE = "configs/default.yaml"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    monkeypatch.setenv("SNIPER_HOME", str(h))
    return h


@pytest.fixture
def balance(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    state = {"lamports": 1_000_000_000}

    async def fake(settings: Settings, pubkey: str, *, timeout_s: float = 10.0) -> int:
        if state["lamports"] < 0:
            raise RuntimeError("rpc down")
        return state["lamports"]

    monkeypatch.setattr(autonomy, "fetch_balance_lamports", fake)
    return state


def test_wallet_create_show_and_refuse_overwrite(home: Path) -> None:
    runner = CliRunner()
    res = runner.invoke(app, ["wallet", "create", "-c", SYN])
    assert res.exit_code == 0, res.output
    key = home / "wallet" / "hot-wallet.json"
    assert key.exists() and oct(key.stat().st_mode)[-3:] == "600"
    assert oct((home / "wallet").stat().st_mode)[-3:] == "700"
    assert read_env_values(home / "sniper.env")["SNIPER_WALLET__KEY_FILE"] == str(key)
    assert oct((home / "sniper.env").stat().st_mode)[-3:] == "600"
    secret = key.read_text().strip()
    assert (
        secret not in res.output and "address:" in res.output and "arm --max-loss-sol" in res.output
    )
    # the loader picks the path up from sniper.env, so nothing has to be edited by hand
    settings = load_settings(Path(SYN))
    assert settings.wallet.key_file == str(key)
    again = runner.invoke(app, ["wallet", "create", "-c", SYN])
    assert again.exit_code == 1 and "already exists" in again.output
    show = runner.invoke(app, ["wallet", "show", "-c", SYN, "--json"])
    assert show.exit_code == 0, show.output
    payload = json.loads(show.output)
    assert payload["public_key"] and payload["key_file"] == str(key)
    assert payload["problem"] is None and payload["arming"] == "not armed"
    assert any("not armed" in line for line in payload["readiness"])
    assert secret not in show.output
    human = runner.invoke(app, ["wallet", "show", "-c", SYN])
    assert human.exit_code == 0 and payload["public_key"] in human.output
    assert secret not in human.output


def test_wallet_import_and_permission_refusal(home: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    exported = generate(tmp_path / "exported.json")  # a Solana CLI style JSON array
    res = runner.invoke(app, ["wallet", "import", str(tmp_path / "exported.json"), "-c", SYN])
    assert res.exit_code == 0, res.output
    assert exported.pubkey in res.output and "compromised" in res.output
    key = home / "wallet" / "hot-wallet.json"
    assert key.exists() and oct(key.stat().st_mode)[-3:] == "600"
    assert (tmp_path / "exported.json").read_text().strip() not in res.output
    key.chmod(0o644)
    show = runner.invoke(app, ["wallet", "show", "-c", SYN])
    assert show.exit_code == 1 and "chmod 600" in show.output
    phrase = tmp_path / "phrase.txt"
    phrase.write_text(" ".join(["abandon"] * 12) + "\n")
    key.unlink()
    bad = runner.invoke(app, ["wallet", "import", str(phrase), "-c", SYN])
    assert bad.exit_code == 1 and not key.exists()


def test_arm_disarm_kill_resume_cycle(home: Path, balance: dict[str, int]) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["wallet", "create", "-c", LIVE]).exit_code == 0
    # refused: synthetic configuration, missing loss limit, wallet at or below the reserve
    syn = runner.invoke(app, ["arm", "--max-loss-sol", "0.1", "-c", SYN, "--yes"])
    assert syn.exit_code == 1 and "synthetic" in syn.output
    assert runner.invoke(app, ["arm", "-c", LIVE, "--yes"]).exit_code != 0
    bad = runner.invoke(app, ["arm", "--max-loss-sol", "-1", "-c", LIVE, "--yes"])
    assert bad.exit_code == 1 and "positive" in bad.output
    balance["lamports"] = 10_000_000  # 0.01 SOL: not above the 0.02 SOL reserve
    empty = runner.invoke(app, ["arm", "--max-loss-sol", "0.1", "-c", LIVE, "--yes"])
    assert empty.exit_code == 1 and "fee reserve" in empty.output
    assert not arming.read(home).armed_marker
    balance["lamports"] = 500_000_000
    # a declined prompt arms nothing
    declined = runner.invoke(app, ["arm", "--max-loss-sol", "0.1", "-c", LIVE], input="n\n")
    assert declined.exit_code == 1 and not arming.read(home).armed_marker
    res = runner.invoke(
        app, ["arm", "--max-loss-sol", "0.1", "--max-trade-sol", "0.02", "-c", LIVE, "--yes"]
    )
    assert res.exit_code == 0, res.output
    assert "armed" in res.output and "public RPC" in res.output  # default.yaml: public endpoint
    env = read_env_values(home / "sniper.env")
    assert env["SNIPER_AUTONOMY__ENABLED"] == "true"
    assert env["SNIPER_AUTONOMY__ACKNOWLEDGE_REAL_MONEY"] == "true"
    assert env["SNIPER_AUTONOMY__MAX_TOTAL_LOSS_SOL"] == "0.1"
    assert env["SNIPER_AUTONOMY__MAX_TRADE_SOL"] == "0.02"
    state = arming.read(home)
    assert state.armed and state.start_balance_lamports == 500_000_000
    assert state.max_total_loss_sol == Decimal("0.1")
    settings = load_settings(Path(LIVE))
    assert settings.autonomy.enabled and settings.autonomy.acknowledge_real_money
    assert settings.autonomy.max_trade_sol == Decimal("0.02")
    assert settings.autonomy.max_total_loss_sol == Decimal("0.1")
    st = runner.invoke(app, ["status", "--json", "-c", LIVE])
    assert st.exit_code == 0, st.output
    assert json.loads(st.output)["autonomy"]["armed"] is True
    # disarm stops buys, kill stops everything, resume lifts only the kill
    dis = runner.invoke(app, ["disarm", "-c", LIVE, "--reason", "testing"])
    assert dis.exit_code == 0 and "exits continue" in dis.output
    state = arming.read(home)
    assert not state.armed and state.disarmed_reason == "testing"
    assert runner.invoke(app, ["kill", "-c", LIVE]).exit_code == 0 and arming.read(home).kill
    assert runner.invoke(app, ["resume", "-c", LIVE]).exit_code == 0
    state = arming.read(home)
    assert not state.kill and state.disarmed_reason == "testing"
    assert load_settings(Path(LIVE)).autonomy.enabled is True  # disarm never edits sniper.env
    # re-arming clears the disarm and records the new limit
    res2 = runner.invoke(app, ["arm", "--max-loss-sol", "0.05", "-c", LIVE, "--yes"])
    assert res2.exit_code == 0, res2.output
    state = arming.read(home)
    assert state.armed and state.max_total_loss_sol == Decimal("0.05")
    # an unreadable balance is a hard stop
    balance["lamports"] = -1
    down = runner.invoke(app, ["arm", "--max-loss-sol", "0.05", "-c", LIVE, "--yes"])
    assert down.exit_code == 1 and "cannot read the wallet balance" in down.output


def test_run_autonomous_refuses_until_armed(home: Path, balance: dict[str, int]) -> None:
    runner = CliRunner()
    both = runner.invoke(app, ["run", "--dry-run", "--autonomous", "-c", LIVE])
    assert both.exit_code == 2 and "mutually exclusive" in both.output
    res = runner.invoke(app, ["run", "--autonomous", "-c", LIVE])
    assert res.exit_code == 2, res.output
    assert "cannot start autonomous mode" in res.output and "wallet create" in res.output
    assert runner.invoke(app, ["wallet", "create", "-c", LIVE]).exit_code == 0
    res2 = runner.invoke(app, ["run", "--autonomous", "-c", LIVE])
    assert res2.exit_code == 2 and "arm --max-loss-sol" in res2.output
    syn = runner.invoke(app, ["run", "--autonomous", "-c", SYN])
    assert syn.exit_code == 2 and "synthetic" in syn.output
    assert runner.invoke(app, ["arm", "--max-loss-sol", "0.1", "-c", LIVE, "--yes"]).exit_code == 0
    pubkey, state = autonomy.preflight(load_settings(Path(LIVE)))
    assert state.armed and len(pubkey) > 30
    # disarmed or killed still starts: exits continue and `resume` can lift the kill
    arming.kill(home)
    _, killed = autonomy.preflight(load_settings(Path(LIVE)))
    assert killed.kill
    arming.resume(home)
    arming.disarm(home, "loss limit")
    _, disarmed = autonomy.preflight(load_settings(Path(LIVE)))
    assert disarmed.disarmed_reason == "loss limit"


async def test_headless_kill_disarm_resume_commands(home: Path) -> None:
    said: list[str] = []
    engine = SimpleNamespace(settings=SimpleNamespace(home=home))
    handler = CommandHandler(engine, lambda: None, said.append)  # type: ignore[arg-type]
    await handler.handle("kill")
    assert arming.read(home).kill and "KILL" in said[-1]
    await handler.handle("disarm too volatile")
    assert arming.read(home).disarmed_reason == "too volatile"
    await handler.handle("resume")
    assert not arming.read(home).kill and "removed" in said[-1]
    await handler.handle("a")
    assert "disarmed: too volatile" in said[-1]
    await handler.handle("h")
    assert "kill" in said[-1] and "disarm" in said[-1]


async def test_doctor_reports_wallet_and_autonomy(home: Path) -> None:
    settings = load_settings(Path(SYN))
    rows = {r.name: r for r in await run_doctor(settings)}
    assert rows["wallet"].status == "SKIP" and rows["autonomy"].status == "SKIP"
    runner = CliRunner()
    assert runner.invoke(app, ["wallet", "create", "-c", SYN]).exit_code == 0
    settings = load_settings(Path(SYN))
    rows = {r.name: r for r in await run_doctor(settings)}
    assert rows["wallet"].status == "PASS" and "mode 600" in rows["wallet"].detail
    assert rows["autonomy"].status == "WARN" and "arm" in rows["autonomy"].detail
    secret = (home / "wallet" / "hot-wallet.json").read_text().strip()
    assert all(secret not in r.detail for r in rows.values())
    arming.arm(
        home,
        wallet_public_key="x",
        start_balance_lamports=1,
        max_total_loss_sol=Decimal("0.1"),
        caps={},
    )
    settings.autonomy.enabled = True
    settings.autonomy.acknowledge_real_money = True
    rows = {r.name: r for r in await run_doctor(settings)}
    assert rows["autonomy"].status == "PASS" and "loss limit 0.1 SOL" in rows["autonomy"].detail
    (home / "wallet" / "hot-wallet.json").chmod(0o640)
    rows = {r.name: r for r in await run_doctor(settings)}
    assert rows["wallet"].status == "FAIL" and "chmod 600" in rows["wallet"].detail
