"""Build and run the `streamlit run` command for `solana-sniper dashboard-web`.

The server binds to 127.0.0.1 unless told otherwise; the command never relays the page to any
outside service and sets no credentials. Everything the page needs is passed as environment
variables with a prefix the strict config loader does not audit, plus the runtime home the CLI
resolved.
"""

from __future__ import annotations

import importlib.util
import ipaddress
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8501
DEFAULT_REFRESH_S = 3.0
ENV_PREFIX = "SOLANA_SNIPER_WEB_"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
INSTALL_HINT = (
    'the web dashboard needs the optional "web" extra: run ./install-macos.sh --skip-service '
    '(or: uv pip install -e ".[web]") and try again'
)


class DashboardLaunchError(Exception):
    """A problem the user must fix before the dashboard can start."""


@dataclass(frozen=True, slots=True)
class LaunchPlan:
    command: tuple[str, ...]
    env: dict[str, str]
    host: str
    port: int
    url: str
    exposed: bool  # bound to something other than loopback

    @property
    def display(self) -> str:
        return " ".join(self.command)


def app_path() -> Path:
    return Path(__file__).resolve().with_name("app.py")


def web_dependencies_available() -> bool:
    return all(importlib.util.find_spec(name) is not None for name in ("streamlit", "plotly"))


def is_loopback(host: str) -> bool:
    if host in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def build_plan(
    *,
    home: Path,
    paper: str | None = None,
    session: str | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    refresh_seconds: float = DEFAULT_REFRESH_S,
    open_browser: bool = True,
    python: str | None = None,
) -> LaunchPlan:
    if not 1 <= port <= 65535:
        raise DashboardLaunchError(f"port must be between 1 and 65535, got {port}")
    if not 1.0 <= refresh_seconds <= 60.0:
        raise DashboardLaunchError("--refresh-seconds must be between 1 and 60")
    if paper and session:
        raise DashboardLaunchError("give either --paper or --session, not both")
    host = host.strip() or DEFAULT_HOST
    exposed = not is_loopback(host)
    command = [
        python or sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app_path()),
        "--server.address",
        host,
        "--server.port",
        str(port),
        "--server.headless",
        "true" if not open_browser else "false",
        "--server.fileWatcherType",
        "none",
        "--server.runOnSave",
        "false",
        "--server.enableStaticServing",
        "false",
        "--server.enableXsrfProtection",
        "true",
        "--server.enableCORS",
        "true",
        "--server.maxUploadSize",
        "1",
        "--browser.gatherUsageStats",
        "false",
        "--browser.serverAddress",
        "localhost" if not exposed else host,
        "--browser.serverPort",
        str(port),
        "--client.toolbarMode",
        "minimal",
        "--client.showErrorDetails",
        "none",
        "--theme.base",
        "dark",
        "--theme.primaryColor",
        "#5ec4a6",
        "--theme.backgroundColor",
        "#0b0f14",
        "--theme.secondaryBackgroundColor",
        "#10151b",
        "--theme.textColor",
        "#d7dde5",
        "--theme.font",
        "monospace",
        "--global.developmentMode",
        "false",
    ]
    env = {
        ENV_PREFIX + "HOME": str(home),
        ENV_PREFIX + "REFRESH": f"{refresh_seconds:g}",
    }
    if paper:
        env[ENV_PREFIX + "PAPER"] = paper
    if session:
        env[ENV_PREFIX + "SESSION"] = session
    shown_host = "localhost" if not exposed else host
    return LaunchPlan(
        command=tuple(command),
        env=env,
        host=host,
        port=port,
        url=f"http://{shown_host}:{port}",
        exposed=exposed,
    )


def run_plan(plan: LaunchPlan) -> int:
    """Run Streamlit in the foreground until Ctrl+C. Returns the exit code."""
    env = {**os.environ, **plan.env}
    # never let a parent's SNIPER_* audit trip the strict loader inside the dashboard process
    try:
        completed = subprocess.run(list(plan.command), env=env, check=False)
    except KeyboardInterrupt:
        return 130
    return int(completed.returncode)
