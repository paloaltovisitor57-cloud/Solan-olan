"""Exercise the macOS deployment scripts end to end with launchd mocked.

`uname` reports Darwin, `plutil` validates with plistlib, and `launchctl` really starts/stops the
ProgramArguments from the rendered plist, so install → status → cmd → stop → restart → doctor all
run for real against the synthetic engine. This proves the script logic and plist rendering; it
cannot prove launchd itself (see README "Verification status").
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

FAKE_LAUNCHCTL = r"""#!/usr/bin/env bash
# Minimal launchctl stand-in: bootstrap runs the plist's ProgramArguments in the background.
set -euo pipefail
STATE="${FAKE_LAUNCHD_STATE}"
mkdir -p "$STATE"
cmd="${1:-}"; shift || true
case "$cmd" in
  bootstrap)
    domain="$1"; plist="$2"
    label="$(python3 -c "import plistlib,sys; print(plistlib.load(open(sys.argv[1],'rb'))['Label'])" "$plist")"
    python3 - "$plist" "$STATE/$label.pid" <<'PY'
import os, plistlib, subprocess, sys
p = plistlib.load(open(sys.argv[1], "rb"))
env = dict(os.environ); env.update(p.get("EnvironmentVariables", {}))
out = open(p["StandardOutPath"], "ab"); err = open(p["StandardErrorPath"], "ab")
proc = subprocess.Popen(p["ProgramArguments"], cwd=p["WorkingDirectory"], env=env, stdout=out, stderr=err, stdin=subprocess.DEVNULL, start_new_session=True)
open(sys.argv[2], "w").write(str(proc.pid))
PY
    ;;
  bootout)
    target="$1"; label="${target##*/}"
    if [[ -f "$STATE/$label.pid" ]]; then
      pid="$(cat "$STATE/$label.pid")"
      kill -TERM "$pid" 2>/dev/null || true
      for _ in $(seq 1 100); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
      rm -f "$STATE/$label.pid"
    else
      exit 3
    fi
    ;;
  print)
    target="$1"; label="${target##*/}"
    if [[ -f "$STATE/$label.pid" ]] && kill -0 "$(cat "$STATE/$label.pid")" 2>/dev/null; then
      echo "gui/$(id -u)/$label = {"; echo "	pid = $(cat "$STATE/$label.pid")"; echo "}"
    else
      exit 113
    fi
    ;;
  enable|kickstart) ;;
  *) echo "fake launchctl: unsupported $cmd" >&2; exit 1 ;;
esac
"""

FAKE_PLUTIL = r"""#!/usr/bin/env bash
# plutil -lint FILE
python3 -c "import plistlib,sys; plistlib.load(open(sys.argv[1],'rb'))" "$2"
"""

FAKE_UNAME = r"""#!/usr/bin/env bash
if [[ "${1:-}" == "-s" ]]; then echo Darwin; else /usr/bin/uname "$@"; fi
"""


@pytest.fixture
def deploy_env(tmp_path: Path) -> dict[str, str]:
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    for name, body in (
        ("launchctl", FAKE_LAUNCHCTL),
        ("plutil", FAKE_PLUTIL),
        ("uname", FAKE_UNAME),
    ):
        f = fake_bin / name
        f.write_text(body)
        f.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    venv = Path(
        sys.prefix
    )  # the venv running pytest (never resolve symlinks: uv venvs link to the base python)
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "SNIPER_HOME": str(home / "Library" / "Application Support" / "SolanaSniper"),
        "SNIPER_CONFIG": str(REPO / "configs" / "synthetic.yaml"),
        "SNIPER_VENV": str(venv),
        "SNIPER_LAUNCH_AGENTS_DIR": str(home / "Library" / "LaunchAgents"),
        "FAKE_LAUNCHD_STATE": str(tmp_path / "launchd-state"),
        "SNIPER_SERVICE_LABEL": "com.solanasniper.test",
    }
    env.pop("SNIPER_SERVICE_MODE", None)
    return env


def run(
    script: str, env: dict[str, str], *args: str, check: bool = True, timeout: int = 240
) -> subprocess.CompletedProcess[str]:
    res = subprocess.run(
        [str(REPO / script), *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and res.returncode != 0:
        raise AssertionError(
            f"{script} {args} failed ({res.returncode}):\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        )
    return res


def wait_for(predicate: object, timeout_s: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return True
        time.sleep(0.5)
    return False


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_install_start_status_cmd_stop_restart_doctor(deploy_env: dict[str, str]) -> None:
    env = deploy_env
    home = Path(env["SNIPER_HOME"])
    plist = Path(env["SNIPER_LAUNCH_AGENTS_DIR"]) / "com.solanasniper.test.plist"
    status_file = home / "state" / "status.json"
    try:
        res = run("install-macos.sh", env, "--skip-tests")
        assert "service is running and healthy" in res.stdout, res.stdout
        assert plist.exists()
        data = plistlib.loads(plist.read_bytes())
        assert data["ProgramArguments"][1:] == ["run", "--dry-run", "--no-dashboard", "--quiet"]
        assert data["KeepAlive"] == {"SuccessfulExit": False} and data["RunAtLoad"] is True
        assert data["EnvironmentVariables"]["SNIPER_HOME"] == str(home)
        assert (home / "sniper.env").exists() and oct((home / "sniper.env").stat().st_mode)[
            -3:
        ] == "600"
        assert (home / "db" / "sniper-synthetic.db").exists()
        # status shows running + heartbeat fields
        st = run("status.sh", env)
        assert "running" in st.stdout and "pid=" in st.stdout and "HEALTHY" in st.stdout, st.stdout
        for needle in (
            "connection",
            "market data",
            "database",
            "tokens monitored",
            "open positions",
            "last signal",
            "last error",
        ):
            assert needle in st.stdout, st.stdout
        # headless command reaches the engine
        run("cmd.sh", env, "h")
        assert (home / "state" / "commands").read_text().strip().endswith("h")
        assert wait_for(
            lambda: "file_command" in (home / "logs" / "sniper.log").read_text(errors="replace"), 15
        )
        # graceful stop marks the heartbeat as stopped and the process exits
        pid = int(json.loads(status_file.read_text())["pid"])
        run("stop.sh", env)
        assert wait_for(lambda: json.loads(status_file.read_text()).get("stopped") is True, 30)
        assert not _alive(pid)
        st2 = run("status.sh", env, check=False)
        assert "stopped" in st2.stdout.lower()
        log = (home / "logs" / "sniper.log").read_text(errors="replace")
        assert "portfolio_initialised" in log or "portfolio_restored" in log
        # restart restores state and resumes
        res = run("restart.sh", env)
        assert "service running" in res.stdout, res.stdout
        assert wait_for(
            lambda: (
                status_file.exists() and json.loads(status_file.read_text()).get("healthy") is True
            ),
            45,
        )
        assert wait_for(
            lambda: (
                "portfolio_restored" in (home / "logs" / "sniper.log").read_text(errors="replace")
            ),
            20,
        )
        # doctor passes with the synthetic config (network checks skipped)
        doc = run("doctor.sh", env)
        assert "checks passed" in doc.stdout, doc.stdout + doc.stderr
        # logs.sh finds the files (non-following tail sanity: -F would block, so just check -n path)
        assert (home / "logs" / "service.out.log").exists()
        # update.sh refuses to run with local modifications and never restarts
        marker = REPO / "configs" / "default.yaml"
        original = marker.read_text()
        marker.write_text(original + "\n# local edit\n")
        try:
            upd = run("update.sh", env, check=False)
            assert upd.returncode != 0 and "local modifications" in upd.stderr, (
                upd.stdout + upd.stderr
            )
        finally:
            marker.write_text(original)
    finally:
        run("stop.sh", env, check=False)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
