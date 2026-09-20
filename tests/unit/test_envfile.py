from __future__ import annotations

from pathlib import Path

from solana_sniper.config.envfile import read_env_values, set_env_values


def test_set_env_values_fills_template_lines_and_preserves_the_rest(tmp_path: Path) -> None:
    p = tmp_path / "sniper.env"
    p.write_text(
        "# header\n"
        "# SNIPER_SERVICE_MODE=dry-run   # dry-run (default) or signal\n"
        "SNIPER_SERVICE_MODE=dry-run\n"
        "#SNIPER_WALLET__KEY_FILE=\n"
        'SNIPER_AUTONOMY__ENABLED="false"\n'
        "OTHER=1\n"
        "SNIPER_AUTONOMY__ENABLED=false\n"
    )
    set_env_values(
        p,
        {
            "SNIPER_WALLET__KEY_FILE": "/k/hot.json",
            "SNIPER_AUTONOMY__ENABLED": "true",
            "SNIPER_AUTONOMY__MAX_TOTAL_LOSS_SOL": "0.1",
            "SNIPER_SERVICE_MODE": "autonomous",
        },
    )
    assert p.read_text().splitlines() == [
        "# header",
        "# SNIPER_SERVICE_MODE=dry-run   # dry-run (default) or signal",  # a real line exists
        "SNIPER_SERVICE_MODE=autonomous",
        "SNIPER_WALLET__KEY_FILE=/k/hot.json",  # the template line, filled in place
        "SNIPER_AUTONOMY__ENABLED=true",
        "OTHER=1",  # the later duplicate is gone
        "SNIPER_AUTONOMY__MAX_TOTAL_LOSS_SOL=0.1",
    ]
    assert oct(p.stat().st_mode)[-3:] == "600"
    assert read_env_values(p) == {
        "SNIPER_SERVICE_MODE": "autonomous",
        "SNIPER_WALLET__KEY_FILE": "/k/hot.json",
        "SNIPER_AUTONOMY__ENABLED": "true",
        "OTHER": "1",
        "SNIPER_AUTONOMY__MAX_TOTAL_LOSS_SOL": "0.1",
    }
    set_env_values(p, {"SNIPER_AUTONOMY__ENABLED": None})
    assert "# SNIPER_AUTONOMY__ENABLED=" in p.read_text()
    assert "SNIPER_AUTONOMY__ENABLED" not in read_env_values(p)


def test_set_env_values_creates_a_private_file(tmp_path: Path) -> None:
    p = tmp_path / "home" / "sniper.env"
    set_env_values(p, {"SNIPER_WALLET__KEY_FILE": "/k/hot.json"})
    assert p.read_text().startswith("# solana-sniper local configuration")
    assert read_env_values(p) == {"SNIPER_WALLET__KEY_FILE": "/k/hot.json"}
    assert oct(p.stat().st_mode)[-3:] == "600"
    assert read_env_values(tmp_path / "missing.env") == {}
