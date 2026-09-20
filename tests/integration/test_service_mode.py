"""`scripts/common.sh` maps SNIPER_SERVICE_MODE to the `run` arguments the launchd agent gets."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _service_args(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", "source scripts/common.sh; service_args | tr '\\n' ' '"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_service_args_follow_the_service_mode(tmp_path: Path) -> None:
    base = {k: v for k, v in os.environ.items() if k != "SNIPER_SERVICE_MODE"}
    base["SNIPER_HOME"] = str(tmp_path / "h")
    for mode, expected in (
        ("dry-run", ["run", "--dry-run", "--no-dashboard", "--quiet"]),
        ("signal", ["run", "--no-dashboard", "--quiet"]),
        ("autonomous", ["run", "--autonomous", "--no-dashboard", "--quiet"]),
    ):
        res = _service_args({**base, "SNIPER_SERVICE_MODE": mode})
        assert res.returncode == 0, res.stderr
        assert res.stdout.split() == expected
    bad = _service_args({**base, "SNIPER_SERVICE_MODE": "yolo"})
    assert bad.returncode != 0 and "autonomous" in bad.stderr
    # sniper.env decides when the environment does not
    home = tmp_path / "h2"
    home.mkdir()
    (home / "sniper.env").write_text("SNIPER_SERVICE_MODE=autonomous\n")
    res = _service_args({**base, "SNIPER_HOME": str(home)})
    assert res.returncode == 0 and res.stdout.split() == [
        "run",
        "--autonomous",
        "--no-dashboard",
        "--quiet",
    ]
    default = _service_args(base)
    assert default.stdout.split() == ["run", "--dry-run", "--no-dashboard", "--quiet"]
