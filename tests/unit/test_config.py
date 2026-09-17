from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from solana_sniper.config import load_settings
from solana_sniper.config.settings import ProvidersConfig, RiskConfig, Settings
from solana_sniper.domain.enums import RiskProfileName


def test_defaults_load() -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.risk.profile is RiskProfileName.EXTREME
    assert s.discovery.max_token_age_s == 1800
    assert s.entry.weights.total() == pytest.approx(100.0)


def test_yaml_and_env_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "risk:\n  profile: NORMAL\n  starting_bankroll_eur: 75\nentry:\n  min_score: 70\n"
    )
    monkeypatch.setenv("SNIPER_RISK__PROFILE", "AGGRESSIVE")
    s = load_settings(cfg)
    assert s.risk.profile is RiskProfileName.AGGRESSIVE  # env wins
    assert s.risk.starting_bankroll_eur == Decimal("75")  # yaml applied
    assert s.entry.min_score == 70
    assert s.config_path == cfg


def test_repo_default_config_parses() -> None:
    s = load_settings(Path("configs/default.yaml"))
    assert s.risk.milestones_eur[0] == Decimal("50")
    assert s.exit.trailing.tiers[-1].min_multiple == 10.0
    synth = load_settings(Path("configs/synthetic.yaml"))
    assert synth.is_synthetic


def test_private_key_rejected() -> None:
    with pytest.raises(ValueError):
        ProvidersConfig(wallet_public_key="[1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16]")
    with pytest.raises(ValueError):
        ProvidersConfig(wallet_public_key="5" * 88)
    assert ProvidersConfig(wallet_public_key="").wallet_public_key is None


def test_risk_profile_must_exist() -> None:
    with pytest.raises(ValueError):
        RiskConfig(profile=RiskProfileName.EXTREME, profiles={})


def test_missing_config_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_settings(tmp_path / "nope.yaml")
