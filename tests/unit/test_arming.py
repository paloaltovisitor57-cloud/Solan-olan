"""Arming markers: arm/disarm/kill/resume and what each means for `armed`."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from solana_sniper.app import arming
from solana_sniper.config.paths import ARMED_FILE, DISARMED_FILE, KILL_FILE, STATE_DIR

NOW = datetime(2026, 9, 20, 8, 0, tzinfo=UTC)


def test_fresh_home_is_not_armed(tmp_path: Path) -> None:
    state = arming.read(tmp_path)
    assert not state.armed and not state.armed_marker and not state.kill
    assert state.describe() == "not armed"
    problems = arming.readiness(enabled=False, acknowledged=False, key_file=None, state=state)
    assert any("wallet create" in p for p in problems)
    assert any("autonomy.enabled" in p for p in problems)
    assert any("acknowledge_real_money" in p for p in problems)
    assert any("not armed" in p for p in problems)


def test_arm_records_wallet_balance_and_loss_limit_atomically(tmp_path: Path) -> None:
    state = arming.arm(
        tmp_path,
        wallet_public_key="HotWallet1111111111111111111111111111111111",
        start_balance_lamports=1_000_000_000,
        max_total_loss_sol=Decimal("0.25"),
        caps={"max_trade_sol": "0.05"},
        now=NOW,
    )
    assert state.armed and state.armed_at == NOW
    assert state.wallet_public_key == "HotWallet1111111111111111111111111111111111"
    assert state.start_balance_lamports == 1_000_000_000
    assert state.max_total_loss_sol == Decimal("0.25") and state.caps == {"max_trade_sol": "0.05"}
    raw = json.loads((tmp_path / STATE_DIR / ARMED_FILE).read_text())
    assert raw["max_total_loss_sol"] == "0.25"
    assert not list((tmp_path / STATE_DIR).glob(".armed.json-*"))  # temp file renamed away
    assert state.describe() == "armed (loss limit 0.25 SOL)"
    assert (
        arming.readiness(
            enabled=True, acknowledged=True, key_file="/x/hot-wallet.json", state=state
        )
        == []
    )


def test_disarm_blocks_until_re_armed(tmp_path: Path) -> None:
    arming.arm(
        tmp_path,
        wallet_public_key="w",
        start_balance_lamports=1,
        max_total_loss_sol=Decimal("1"),
        caps={},
    )
    state = arming.disarm(tmp_path, "total loss limit reached", now=NOW)
    assert not state.armed and not state.armed_marker
    assert state.disarmed_reason == "total loss limit reached" and state.disarmed_at == NOW
    assert "disarmed" in state.describe()
    assert not (tmp_path / STATE_DIR / ARMED_FILE).exists()
    assert (tmp_path / STATE_DIR / DISARMED_FILE).exists()
    problems = arming.readiness(enabled=True, acknowledged=True, key_file="k", state=state)
    assert any("disarmed (total loss limit reached)" in p for p in problems)
    # re-arming clears the disarm marker deliberately
    again = arming.arm(
        tmp_path,
        wallet_public_key="w",
        start_balance_lamports=2,
        max_total_loss_sol=Decimal("1"),
        caps={},
    )
    assert again.armed and again.disarmed_reason is None
    assert not (tmp_path / STATE_DIR / DISARMED_FILE).exists()


def test_kill_overrides_everything_until_resume(tmp_path: Path) -> None:
    arming.arm(
        tmp_path,
        wallet_public_key="w",
        start_balance_lamports=1,
        max_total_loss_sol=Decimal("1"),
        caps={},
    )
    killed = arming.kill(tmp_path, now=NOW)
    assert killed.kill and not killed.armed and killed.armed_marker
    assert "KILL" in killed.describe()
    assert (tmp_path / STATE_DIR / KILL_FILE).read_text().startswith("created 2026-09-20")
    # arming does not clear a kill switch
    still = arming.arm(
        tmp_path,
        wallet_public_key="w",
        start_balance_lamports=1,
        max_total_loss_sol=Decimal("1"),
        caps={},
    )
    assert still.kill and not still.armed
    resumed = arming.resume(tmp_path)
    assert not resumed.kill and resumed.armed
    assert arming.resume(tmp_path).armed  # idempotent


def test_corrupt_markers_are_treated_as_absent(tmp_path: Path) -> None:
    state_dir = tmp_path / STATE_DIR
    state_dir.mkdir(parents=True)
    (state_dir / ARMED_FILE).write_text("{not json")
    (state_dir / DISARMED_FILE).write_text("[]")
    state = arming.read(tmp_path)
    assert not state.armed_marker and state.disarmed_reason is None
    (state_dir / ARMED_FILE).write_text(json.dumps({"max_total_loss_sol": "abc"}))
    state = arming.read(tmp_path)
    assert state.armed_marker and state.max_total_loss_sol is None
    problems = arming.readiness(enabled=True, acknowledged=True, key_file="k", state=state)
    assert any("no loss limit" in p for p in problems)
