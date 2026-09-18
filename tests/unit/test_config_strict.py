"""Findings 2 and 5 regression: explicit configuration errors, bounds, precedence, no echo."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from solana_sniper.config import ConfigError, load_settings
from solana_sniper.config.base58 import b58decode, is_solana_public_key
from solana_sniper.config.loader import audit_environment, unknown_keys
from solana_sniper.config.settings import (
    ExitConfig,
    MarketDataConfig,
    ProvidersConfig,
    RiskConfig,
    Settings,
    StorageConfig,
)

VALID_PUBKEY = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # USDC mint: 32 bytes, base58
SECRET_LOOKALIKE = "SENTINEL_PRIVATE_KEY_MATERIAL"


def test_nested_yaml_typos_are_rejected_with_paths(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "riskk: {}\nrisk:\n  profil: NORMAL\n  profiles:\n    EXTREME:\n      base_fraction: 0.5\n"
        "      max_fraction: 0.9\n      max_total_exposure_fraction: 0.95\n"
        "      max_open_positions: 2\n      typo_here: 1\nexit:\n  trailing:\n    tierz: []\n"
    )
    with pytest.raises(ConfigError) as info:
        load_settings(cfg)
    joined = "\n".join(info.value.problems)
    for path in (
        "'riskk'",
        "'risk.profil'",
        "'risk.profiles.EXTREME.typo_here'",
        "'exit.trailing.tierz'",
    ):
        assert path in joined, joined
    assert unknown_keys({"risk": {"profile": "NORMAL"}}, Settings) == []


def test_nested_models_reject_unknown_fields_programmatically() -> None:
    with pytest.raises(ValueError):
        RiskConfig(starting_bankroll_eurr=Decimal(5))  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        MarketDataConfig(pol_interval_s=1)  # type: ignore[call-arg]


def test_env_audit_separates_application_and_service_variables(tmp_path: Path) -> None:
    dotenv = tmp_path / "sniper.env"
    dotenv.write_text(
        "SNIPER_SERVICE_MODE=dry-run\nSNIPER_RISK__PROFIL=NORMAL\n# SNIPER_COMMENTED=1\n"
    )
    problems = audit_environment(
        {
            "PATH": "/usr/bin",
            "HOME": "/x",
            "SNIPER_RISK_PROFILE": "NORMAL",  # single underscore typo
            "SNIPER_RISK__PROFILE": "NORMAL",  # valid
            "SNIPER_PROVIDERS__HELIUS_API_KEY": "k",  # valid
            "SNIPER_RISK__PROFILES__EXTREME__BASE_FRACTION": "0.5",  # dict field: any suffix
            "SNIPER_HOME": "/tmp/x",  # service variable
            "SNIPER_VENV": "/tmp/v",
            "sniper_dashboard__refresh_hz": "2",  # case-insensitive
        },
        [dotenv],
    )
    assert problems == [
        "unknown environment variable 'SNIPER_RISK_PROFILE'",
        f"unknown variable 'SNIPER_RISK__PROFIL' in {dotenv}",
    ]


def test_env_typo_fails_loading_but_unrelated_env_does_not(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_UNRELATED_VAR", "whatever")
    monkeypatch.setenv("SNIPER_SERVICE_MODE", "signal")
    load_settings(Path("configs/default.yaml"))  # fine
    monkeypatch.setenv("SNIPER_ENTRY_MIN_SCORE", "10")
    with pytest.raises(ConfigError) as info:
        load_settings(Path("configs/default.yaml"))
    assert "SNIPER_ENTRY_MIN_SCORE" in str(info.value)


def test_bounds_and_finite_numbers() -> None:
    for bad in (
        {"poll_interval_s": float("inf")},
        {"poll_interval_s": float("nan")},
        {"poll_interval_s": -1},
        {"batch_size": 0},
        {"batch_size": 31},
        {"max_concurrent_requests": 0},
        {"stale_after_s": 0},
    ):
        with pytest.raises(ValueError):
            MarketDataConfig(**bad)
    with pytest.raises(ValueError):
        ProvidersConfig(http_timeout_s=0)
    with pytest.raises(ValueError):
        ProvidersConfig(http_max_connections=100000)
    with pytest.raises(ValueError):
        ProvidersConfig(ws_reconnect_min_s=10, ws_reconnect_max_s=1)
    with pytest.raises(ValueError):
        StorageConfig(write_batch_size=0)
    with pytest.raises(ValueError):
        StorageConfig(write_flush_interval_s=float("inf"))
    with pytest.raises(ValueError):
        ExitConfig(max_loss_pct=1.5)
    with pytest.raises(ValueError):
        RiskConfig(starting_bankroll_eur=Decimal("0"))
    with pytest.raises(ValueError):
        RiskConfig(confidence_min_score=95, confidence_full_score=90)
    assert MarketDataConfig(poll_interval_s=0.05, batch_size=30).batch_size == 30


def test_precedence_env_over_dotenv_over_yaml_over_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "sniper.env").write_text("SNIPER_ENTRY__MIN_SCORE=70\nSNIPER_ENTRY__SIGNAL_TTL_S=50\n")
    cfg = tmp_path / "c.yaml"
    cfg.write_text("entry:\n  min_score: 60\n  signal_ttl_s: 40\n  min_observations: 7\n")
    monkeypatch.setenv("SNIPER_HOME", str(home))
    monkeypatch.setenv("SNIPER_ENTRY__MIN_SCORE", "80")
    s = load_settings(cfg)
    assert s.entry.min_score == 80  # env beats dotenv and yaml
    assert s.entry.signal_ttl_s == 50  # dotenv beats yaml
    assert s.entry.min_observations == 7  # yaml beats defaults
    assert s.entry.max_pending_signals == 3  # default


def test_validation_errors_never_echo_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SNIPER_PROVIDERS__WALLET_PUBLIC_KEY", SECRET_LOOKALIKE)
    with pytest.raises(ConfigError) as info:
        load_settings(Path("configs/default.yaml"))
    assert SECRET_LOOKALIKE not in str(info.value)
    assert "providers.wallet_public_key" in str(info.value)
    monkeypatch.delenv("SNIPER_PROVIDERS__WALLET_PUBLIC_KEY")
    cfg = tmp_path / "c.yaml"
    cfg.write_text("market_data:\n  poll_interval_s: SENTINEL_BAD_VALUE\n")
    with pytest.raises(ConfigError) as info2:
        load_settings(cfg)
    assert "SENTINEL_BAD_VALUE" not in str(info2.value) and "market_data.poll_interval_s" in str(
        info2.value
    )


def test_public_key_encoding_and_length() -> None:
    assert is_solana_public_key(VALID_PUBKEY)
    assert len(b58decode(VALID_PUBKEY)) == 32
    assert ProvidersConfig(wallet_public_key=f" {VALID_PUBKEY} ").wallet_public_key == VALID_PUBKEY
    assert ProvidersConfig(wallet_public_key="").wallet_public_key is None
    private_key_like = "5" * 88  # 64-byte base58 secret keys are 87-88 chars
    for bad in (
        private_key_like,
        "[12,34,56,78,90,12,34,56,78,90,12,34,56,78,90,12]",
        "0x" + "ab" * 32,
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v0",  # '0' is not base58
        "abc",
        "1" * 44,  # decodes to fewer than 32 bytes
    ):
        assert not is_solana_public_key(bad)
        with pytest.raises(ValueError) as info:
            ProvidersConfig(wallet_public_key=bad)
        assert "PUBLIC key" in str(info.value)
        # the exception raised by our validator does not contain the supplied text
        assert bad not in " ".join(str(a) for a in info.value.args)
