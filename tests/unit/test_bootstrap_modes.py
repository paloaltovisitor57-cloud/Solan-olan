"""Only `RunMode.AUTONOMOUS` builds a signer; every other mode never touches the wallet, whatever
the configuration says. The autonomous composition reports itself honestly in the heartbeat."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from solana_sniper.app import arming
from solana_sniper.app import bootstrap as bootstrap_module
from solana_sniper.app.bootstrap import build_runtime
from solana_sniper.config import load_settings
from solana_sniper.config.paths import ensure_home, wallet_key_path
from solana_sniper.config.settings import Settings
from solana_sniper.domain.clock import ManualClock
from solana_sniper.domain.enums import ExecutionProvenance, RunMode
from solana_sniper.wallet.keys import generate


def _settings(config: str, tmp_path: Path, name: str) -> Settings:
    settings = load_settings(Path(config))
    home = tmp_path / "home"
    ensure_home(home)
    settings.home = home
    settings.storage.database_url = f"sqlite+aiosqlite:///{home}/db/{name}.db"
    settings.telemetry.log_file = None
    return settings


def _build(settings: Settings, mode: RunMode) -> object:
    return build_runtime(
        settings,
        mode=mode,
        session_id=f"modes-{mode.lower()}",
        clock=ManualClock(datetime(2026, 3, 1, tzinfo=UTC)),
        synthetic_seed=1,
        quiet_alerts=True,
    )


async def test_non_autonomous_modes_never_load_the_wallet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(path: Path) -> object:
        raise AssertionError(f"wallet loaded from {path}")

    monkeypatch.setattr(bootstrap_module, "load_key_file", boom)
    for config, mode in (
        ("configs/default.yaml", RunMode.PAPER),
        ("configs/default.yaml", RunMode.LIVE),
        ("configs/default.yaml", RunMode.DRY_RUN),
        ("configs/synthetic.yaml", RunMode.DRY_RUN),
        ("configs/synthetic.yaml", RunMode.REPLAY),
    ):
        settings = _settings(config, tmp_path, mode.lower())
        settings.wallet.key_file = str(tmp_path / "hot-wallet.json")
        settings.autonomy.enabled = True
        settings.autonomy.acknowledge_real_money = True
        settings.autonomy.max_total_loss_sol = Decimal("1")
        rt = _build(settings, mode)
        assert rt.autonomous is None  # type: ignore[attr-defined]
        assert rt.engine.d.execution.name in ("manual", "dry-run")  # type: ignore[attr-defined]
        assert rt.engine.d.outcomes.execution is not ExecutionProvenance.AUTONOMOUS  # type: ignore[attr-defined]
        await rt.http.aclose()  # type: ignore[attr-defined]
        await rt.repo.close()  # type: ignore[attr-defined]


async def test_autonomous_mode_refuses_a_synthetic_configuration(tmp_path: Path) -> None:
    settings = _settings("configs/synthetic.yaml", tmp_path, "syn")
    wallet_path = wallet_key_path(tmp_path / "home")
    generate(wallet_path)
    settings.wallet.key_file = str(wallet_path)
    settings.autonomy.enabled = settings.autonomy.acknowledge_real_money = True
    with pytest.raises(ValueError, match="synthetic"):
        _build(settings, RunMode.AUTONOMOUS)


async def test_autonomous_mode_needs_a_wallet_file(tmp_path: Path) -> None:
    settings = _settings("configs/default.yaml", tmp_path, "nowallet")
    settings.autonomy.enabled = settings.autonomy.acknowledge_real_money = True
    from solana_sniper.wallet.keys import WalletFileError

    with pytest.raises(WalletFileError, match="wallet create"):
        _build(settings, RunMode.AUTONOMOUS)


async def test_autonomous_mode_builds_the_executor_and_reports_it(tmp_path: Path) -> None:
    settings = _settings("configs/default.yaml", tmp_path, "auto")
    home = tmp_path / "home"
    wallet_path = wallet_key_path(home)
    wallet = generate(wallet_path)
    secret = wallet_path.read_text().strip()
    settings.wallet.key_file = str(wallet_path)
    settings.autonomy.enabled = settings.autonomy.acknowledge_real_money = True
    settings.autonomy.max_total_loss_sol = Decimal("0.2")
    settings.quotes.prepare_unsigned_transaction = True
    settings.providers.wallet_public_key = "SomeoneElsesPublicKey11111111111111111111111"
    arming.arm(
        home,
        wallet_public_key=wallet.pubkey,
        start_balance_lamports=10**9,
        max_total_loss_sol=Decimal("0.2"),
        caps={},
    )
    rt = _build(settings, RunMode.AUTONOMOUS)
    auto = rt.autonomous  # type: ignore[attr-defined]
    assert auto is not None and auto.wallet_public_key == wallet.pubkey
    assert rt.engine.d.execution is auto and auto.name == "autonomous" and not auto.simulated  # type: ignore[attr-defined]
    assert rt.engine.d.outcomes.execution is ExecutionProvenance.AUTONOMOUS  # type: ignore[attr-defined]
    # the human-signing attachment is not prepared for a bot that builds its own swaps
    assert rt.engine.d.preparer._enabled is False  # type: ignore[attr-defined]
    snap = rt.health.snapshot()  # type: ignore[attr-defined]
    block = snap["autonomy"]
    assert block["wallet_public_key"] == wallet.pubkey and block["armed"] is True
    assert block["state"].startswith("armed") and block["max_total_loss_sol"] == "0.2"
    assert block["caps"]["max_trade_sol"] == "0.05" and block["sends"] == 0
    assert block["wallet_sol"] is None  # no RPC call was made at build time
    assert snap["execution_provenance"] == "AUTONOMOUS" and snap["mode"] == "AUTONOMOUS"
    assert snap["records_verified_onchain"] is False
    assert secret not in json.dumps(snap, default=str)
    arming.kill(home)
    snap2 = rt.health.snapshot()  # type: ignore[attr-defined]
    assert snap2["autonomy"]["kill_switch"] and any("KILL" in d for d in snap2["degraded"])
    assert snap2["state"] != "HEALTHY"
    await rt.http.aclose()  # type: ignore[attr-defined]
    await rt.repo.close()  # type: ignore[attr-defined]
