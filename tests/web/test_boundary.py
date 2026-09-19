"""The execution boundary of the web dashboard: no execution/signing modules are ever imported,
no control on the page can act, and the launcher binds to loopback by default."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from solana_sniper.cli.main import app
from solana_sniper.web import FORBIDDEN_IMPORT_PREFIXES
from solana_sniper.web import launch as launch_module
from solana_sniper.web.app import WebConfig
from solana_sniper.web.launch import DashboardLaunchError, build_plan

WEB_DIR = Path(__file__).resolve().parents[2] / "src" / "solana_sniper" / "web"
PAGE_MODULES = (
    "app.py",
    "data.py",
    "components.py",
    "charts.py",
    "models.py",
    "session_discovery.py",
)
FORBIDDEN_TOKENS = (
    "st.button",
    "st.form",
    "download_button",
    "file_uploader",
    "sendTransaction",
    "signTransaction",
    "sign_transaction",
    "Keypair",
    "private_key",
    "secret_key",
    "seed_phrase",
    "mnemonic",
    "broadcast(",
    "command_file",
    "solana_sniper.execution",
    "solana_sniper.quotes",
    "solana_sniper.alerts",
    "app.engine",
    "app.bootstrap",
    "subprocess",
    "os.system",
    "write_text(",
    "INSERT INTO",
    "UPDATE ",
    "DELETE FROM",
    "CREATE TABLE",
    "journal_mode",
    "create_all",
    "run_migrations",
    "ngrok",
)


def test_no_execution_or_signing_module_is_imported_by_the_dashboard() -> None:
    code = (
        "import sys\n"
        "import solana_sniper.web.app, solana_sniper.web.data, solana_sniper.web.charts, "
        "solana_sniper.web.components, solana_sniper.web.session_discovery, "
        "solana_sniper.web.launch\n"
        "print(__import__('json').dumps(sorted(m for m in sys.modules "
        "if m.startswith('solana_sniper'))))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=120
    )
    loaded = json.loads(out.stdout.strip().splitlines()[-1])
    assert "solana_sniper.web.app" in loaded and "solana_sniper.web.data" in loaded
    for prefix in FORBIDDEN_IMPORT_PREFIXES:
        offenders = [m for m in loaded if m == prefix or m.startswith(prefix + ".")]
        assert not offenders, offenders
    assert not any(m.startswith("solana_sniper.execution") for m in loaded)
    assert not any(m.startswith("solana_sniper.quotes") for m in loaded)


def test_page_sources_contain_no_acting_widget_or_signing_code() -> None:
    for name in PAGE_MODULES:
        source = (WEB_DIR / name).read_text(encoding="utf-8")
        for token in FORBIDDEN_TOKENS:
            assert token not in source, f"{name} contains {token!r}"
        # no raw SQL outside data.py
        if name != "data.py":
            assert not re.search(r"\bSELECT\b", source), f"{name} runs SQL"
    launcher = (WEB_DIR / "launch.py").read_text(encoding="utf-8")
    assert "ngrok" not in launcher and "tunnel" not in launcher.lower()
    assert "0.0.0.0" not in launcher


def test_launch_plan_defaults_to_loopback(tmp_path: Path) -> None:
    plan = build_plan(home=tmp_path)
    assert plan.host == "127.0.0.1" and plan.port == 8501 and not plan.exposed
    assert plan.url == "http://localhost:8501"
    cmd = plan.command
    assert cmd[1:4] == ("-m", "streamlit", "run") and cmd[4].endswith("web/app.py")
    assert cmd[cmd.index("--server.address") + 1] == "127.0.0.1"
    assert cmd[cmd.index("--server.port") + 1] == "8501"
    assert cmd[cmd.index("--server.headless") + 1] == "false"
    assert cmd[cmd.index("--browser.gatherUsageStats") + 1] == "false"
    assert cmd[cmd.index("--server.fileWatcherType") + 1] == "none"
    assert plan.env == {"SOLANA_SNIPER_WEB_HOME": str(tmp_path), "SOLANA_SNIPER_WEB_REFRESH": "3"}
    assert not any(k.startswith("SNIPER_") for k in plan.env)
    plan2 = build_plan(
        home=tmp_path,
        paper="paper-x",
        host="0.0.0.0",
        port=9000,
        refresh_seconds=5,
        open_browser=False,
    )
    assert plan2.exposed and plan2.url == "http://0.0.0.0:9000"
    assert plan2.env["SOLANA_SNIPER_WEB_PAPER"] == "paper-x"
    assert plan2.command[plan2.command.index("--server.headless") + 1] == "true"
    with pytest.raises(DashboardLaunchError):
        build_plan(home=tmp_path, port=0)
    with pytest.raises(DashboardLaunchError):
        build_plan(home=tmp_path, refresh_seconds=0.1)
    with pytest.raises(DashboardLaunchError):
        build_plan(home=tmp_path, paper="a", session="b")


def test_web_config_reads_environment_and_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SOLANA_SNIPER_WEB_HOME", str(tmp_path))
    monkeypatch.setenv("SOLANA_SNIPER_WEB_PAPER", "paper-env")
    monkeypatch.setenv("SOLANA_SNIPER_WEB_REFRESH", "7")
    cfg = WebConfig.load([])
    assert cfg.home == tmp_path and cfg.paper == "paper-env" and cfg.refresh_seconds == 7
    cfg2 = WebConfig.load(["--paper", "paper-arg", "--refresh-seconds", "99", "--session", "s"])
    assert cfg2.paper == "paper-arg" and cfg2.session == "s" and cfg2.refresh_seconds == 30
    monkeypatch.delenv("SOLANA_SNIPER_WEB_HOME")
    monkeypatch.delenv("SOLANA_SNIPER_WEB_PAPER")
    monkeypatch.setenv("SOLANA_SNIPER_WEB_REFRESH", "junk")
    cfg3 = WebConfig.load([])
    assert cfg3.paper is None and cfg3.refresh_seconds == 3
    assert cfg3.home == Path(__import__("os").environ["SNIPER_HOME"])


def test_cli_dashboard_web_command(
    isolated_runtime_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner(env={"COLUMNS": "300"})
    res = runner.invoke(app, ["dashboard-web", "--print-command", "--no-browser"])
    assert res.exit_code == 0, res.output
    flat = " ".join(res.output.split())
    assert "WEB DASHBOARD (read-only)" in flat and "Real transactions: DISABLED" in flat
    assert "--server.address 127.0.0.1 --server.port 8501" in flat
    assert "http://localhost:8501" in flat and "WARNING" not in flat
    res2 = runner.invoke(
        app, ["dashboard-web", "--print-command", "--host", "0.0.0.0", "--port", "9001"]
    )
    assert res2.exit_code == 0
    flat2 = " ".join(res2.output.split())
    assert "WARNING: bound to 0.0.0.0" in flat2 and "--server.address 0.0.0.0" in flat2
    res3 = runner.invoke(app, ["dashboard-web", "--print-command", "--paper", "paper-missing"])
    assert res3.exit_code == 2 and "no paper session 'paper-missing'" in " ".join(
        res3.output.split()
    )
    res4 = runner.invoke(app, ["dashboard-web", "--print-command", "--refresh-seconds", "0"])
    assert res4.exit_code == 2 and "refresh-seconds" in res4.output
    monkeypatch.setattr(launch_module, "web_dependencies_available", lambda: False)
    res5 = runner.invoke(app, ["dashboard-web", "--print-command"])
    assert res5.exit_code == 2 and "web" in res5.output and "install" in res5.output
    assert "dashboard-web" in runner.invoke(app, ["--help"]).output
