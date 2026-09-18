"""Regressions for the independently reproduced defects at 279641d.

All credentials are fake sentinels; databases are temporary; no network.
"""

from __future__ import annotations

import io
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from solana_sniper.config import ConfigError, load_settings
from solana_sniper.config.settings import ProvidersConfig, RiskConfig, Settings
from solana_sniper.domain.enums import FillProvenance, TokenUnits
from solana_sniper.domain.models import Fill, Position
from solana_sniper.storage.migrations import run_migrations, schema_version
from solana_sniper.storage.repository import Repository
from solana_sniper.storage.serialization import dataclass_from_dict
from solana_sniper.telemetry.logging import configure_logging
from solana_sniper.telemetry.redaction import (
    register_secret,
    registry,
    safe_exception,
    scrub_value,
)

WALLET_SENTINEL = "SENTINEL_WALLET_KEY_MATERIAL_1"
REGISTERED = "REGISTERED_FAKE_CRED_12345"
UNREGISTERED = "UNREGISTERED_SENTINEL_XYZ"


def _db_now() -> str:
    """SQLite storage format SQLAlchemy uses for DateTime columns (raw INSERTs in tests must not
    hand sqlite3 a datetime object: its default adapter is deprecated in Python 3.12)."""
    return datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    registry.clear()


# ------------------------------------------------------------ 1. suppressed context
def test_safe_exception_respects_suppressed_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SNIPER_PROVIDERS__WALLET_PUBLIC_KEY", WALLET_SENTINEL)
    with pytest.raises(ConfigError) as info:
        load_settings(Path("configs/default.yaml"))
    err = info.value
    assert err.__suppress_context__ and err.__cause__ is None  # raised with `from None`
    assert WALLET_SENTINEL not in str(err)
    rendered = safe_exception(err)
    assert WALLET_SENTINEL not in rendered, rendered
    assert "ValidationError" not in rendered  # suppressed context is not walked


def test_safe_exception_scrubs_pydantic_validation_errors_and_input_values() -> None:
    from pydantic import BaseModel, ValidationError

    class Plain(BaseModel):  # a raw pydantic model, without the StrictModel error wrapping
        amount: int

    with pytest.raises(ValidationError) as info:
        Plain.model_validate({"amount": "SENTINEL_BAD_INPUT_77"})
    assert "SENTINEL_BAD_INPUT_77" in str(info.value)  # pydantic itself echoes the input
    rendered = safe_exception(info.value)
    assert "SENTINEL_BAD_INPUT_77" not in rendered, rendered
    assert "amount" in rendered
    # a chained (non-suppressed) cause still renders, but its input is masked
    try:
        try:
            Plain.model_validate({"amount": "SENTINEL_BAD_INPUT_78"})
        except ValidationError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        rendered2 = safe_exception(outer)
    assert "SENTINEL_BAD_INPUT_78" not in rendered2 and "RuntimeError: wrapped" in rendered2
    # the StrictModel wrapper never echoes either, and keeps the field path
    with pytest.raises(ValueError) as info3:
        RiskConfig.model_validate({"starting_bankroll_eur": "SENTINEL_BAD_INPUT_79"})
    assert "SENTINEL_BAD_INPUT_79" not in str(info3.value)
    assert "starting_bankroll_eur" in str(info3.value)


# -------------------------------------------------- 2. extras and nested mappings
def test_pre_existing_handler_with_extra_is_scrubbed() -> None:
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s key=%(api_key)s ctx=%(ctx)s"))
    logger = logging.getLogger("pre-existing.defensive")
    logger.propagate = False
    logger.addHandler(handler)  # exists before configure_logging runs
    configure_logging("DEBUG", json_output=True, log_file=None, quiet_console=True)
    register_secret(REGISTERED)
    logger.warning(
        "hello",
        extra={"api_key": REGISTERED, "ctx": {"Authorization": f"Bearer {UNREGISTERED}"}},
    )
    out = buf.getvalue()
    assert REGISTERED not in out and UNREGISTERED not in out, out
    assert "hello" in out
    logger.removeHandler(handler)


def test_scrub_value_is_key_aware_for_nested_mappings() -> None:
    nested = scrub_value(
        {
            "headers": {"Authorization": f"Bearer {UNREGISTERED}", "X-Api-Key": UNREGISTERED},
            "body": {"password": UNREGISTERED, "user": "alice"},
            "list": [{"token": UNREGISTERED}],
            "harmless": "keep me",
        }
    )
    dumped = json.dumps(nested)
    assert UNREGISTERED not in dumped
    assert nested["harmless"] == "keep me" and nested["body"]["user"] == "alice"
    assert nested["headers"]["Authorization"] == "***"


def test_structlog_event_nested_credentials_are_masked(tmp_path: Path) -> None:
    log_file = tmp_path / "app.log"
    configure_logging("DEBUG", json_output=True, log_file=str(log_file), quiet_console=True)
    from solana_sniper.telemetry.logging import get_logger

    get_logger("t").warning("request_failed", headers={"authorization": f"Basic {UNREGISTERED}"})
    logging.shutdown()
    text_out = log_file.read_text()
    assert "request_failed" in text_out and UNREGISTERED not in text_out


# ------------------------------------------------- 3. legacy simulated fills
LEGACY_FILL = {
    "fill_id": "fill_sim",
    "signal_id": "s",
    "mint": "m",
    "side": "BUY",
    "filled_at": {"__dt__": "2026-01-01T00:00:00+00:00"},
    "sol_amount": {"__dec__": "1"},
    "token_amount": {"__dec__": "1000"},
    "eur_amount": {"__dec__": "1"},
    "sol_eur": {"__dec__": "1"},
    "fee_eur": {"__dec__": "0"},
    "slippage_cost_eur": {"__dec__": "0"},
    "simulated": True,
}
LEGACY_POS = {
    "position_id": "pos_sim",
    "mint": "m",
    "symbol": "T",
    "opened_at": {"__dt__": "2026-01-01T00:00:00+00:00"},
    "entry_price_native": {"__dec__": "1"},
    "entry_sol_eur": {"__dec__": "150"},
    "quantity": {"__dec__": "1000"},
    "cost_basis_eur": {"__dec__": "15"},
    "entry_sol": {"__dec__": "0.1"},
    "state": "OPEN",
    "simulated": True,
}


async def _seed(url: str, *, already_v2: bool) -> None:
    await run_migrations(url)
    engine = create_async_engine(url)
    fill = dict(LEGACY_FILL)
    pos = dict(LEGACY_POS)
    if already_v2:  # the invalid combination produced by the original v2 migration
        fill.update(
            {
                "provenance": "UNKNOWN_LEGACY",
                "units": "UNKNOWN_LEGACY",
                "legacy_token_amount": fill.pop("token_amount"),
                "token_amount_ui": {"__dec__": "0"},
                "token_amount_raw": 0,
                "token_decimals": 0,
            }
        )
        pos.update(
            {
                "provenance": "UNKNOWN_LEGACY",
                "units": "UNKNOWN_LEGACY",
                "quantity_ui": pos.pop("quantity"),
                "quantity_raw": 0,
                "token_decimals": 0,
            }
        )
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM schema_version WHERE version > :v"), {"v": 2 if already_v2 else 1}
        )
        await conn.execute(
            text(
                "INSERT INTO fills (fill_id, session_id, signal_id, mint, side, filled_at, payload)"
                " VALUES ('fill_sim','s','sig','m','BUY',:t,:p)"
            ),
            {"t": _db_now(), "p": json.dumps(fill)},
        )
        await conn.execute(
            text(
                "INSERT INTO positions (position_id, session_id, mint, state, payload, updated_at)"
                " VALUES ('pos_sim','s','m','OPEN',:p,:t)"
            ),
            {"t": _db_now(), "p": json.dumps(pos)},
        )
    await engine.dispose()


async def _payloads(url: str) -> tuple[dict[str, object], dict[str, object]]:
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        f = json.loads((await conn.execute(text("SELECT payload FROM fills"))).scalar_one())
        p = json.loads((await conn.execute(text("SELECT payload FROM positions"))).scalar_one())
    await engine.dispose()
    return f, p


@pytest.mark.parametrize("already_v2", [False, True])
async def test_legacy_simulated_records_migrate_to_a_valid_state(
    tmp_path: Path, already_v2: bool
) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path}/legacy-{already_v2}.db"
    await _seed(url, already_v2=already_v2)
    applied = await run_migrations(url)
    assert applied  # something above the seeded version was applied
    fill_payload, pos_payload = await _payloads(url)
    fill = dataclass_from_dict(Fill, fill_payload)  # must deserialize
    pos = dataclass_from_dict(Position, pos_payload)
    assert fill.simulated and fill.provenance is FillProvenance.SIMULATED
    assert fill.units is TokenUnits.UNKNOWN_LEGACY and not fill.verified_onchain
    assert fill_payload["legacy_simulated"] is True and fill_payload["legacy_token_amount"] == {
        "__dec__": "1000"
    }
    assert (
        pos.simulated
        and pos.provenance is FillProvenance.SIMULATED
        and pos.units is TokenUnits.UNKNOWN_LEGACY
    )
    assert pos.quantity_ui == Decimal("1000") and not pos.is_verified
    # repeat execution is a no-op and preserves the records
    assert await run_migrations(url) == []
    assert (await _payloads(url)) == (fill_payload, pos_payload)
    assert await schema_version(url) >= 3
    repo = Repository(url, session_id="check")
    await repo.init()
    state = await repo.load_state()
    await repo.close()
    assert state.positions[0].provenance is FillProvenance.SIMULATED


def test_fill_rejects_unknown_legacy_with_simulated_flag() -> None:
    bad = {
        **LEGACY_FILL,
        "provenance": "UNKNOWN_LEGACY",
        "units": "UNKNOWN_LEGACY",
        "token_amount_ui": {"__dec__": "0"},
        "token_amount_raw": 0,
        "token_decimals": 0,
    }
    bad.pop("token_amount")
    with pytest.raises(ValueError):
        dataclass_from_dict(Fill, bad)


# --------------------------------------------- 4. top-level typos and direct errors
def test_top_level_settings_typo_is_rejected_but_service_dotenv_is_tolerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError):
        Settings(risk_typo=1)  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        Settings.model_validate({"riskk": {"profile": "NORMAL"}})
    home = tmp_path / "home"
    home.mkdir()
    (home / "sniper.env").write_text(
        "SNIPER_SERVICE_MODE=signal\nSNIPER_VENV=/x\nUNRELATED_TOOL_VAR=1\nSNIPER_RISK__PROFILE=NORMAL\n"
    )
    monkeypatch.setenv("SNIPER_HOME", str(home))
    s = load_settings(Path("configs/default.yaml"))
    assert str(s.risk.profile) == "NORMAL"  # application key from dotenv applied
    (home / "sniper.env").write_text("SNIPER_RISK__PROFIL=NORMAL\n")
    with pytest.raises(ConfigError) as info:
        load_settings(Path("configs/default.yaml"))
    assert "SNIPER_RISK__PROFIL" in str(info.value)


def test_direct_model_validation_errors_hide_rejected_values() -> None:
    for build in (
        lambda: ProvidersConfig(wallet_public_key="SENTINEL_DIRECT_VALUE_ABC"),
        lambda: ProvidersConfig.model_validate({"wallet_public_key": "SENTINEL_DIRECT_VALUE_ABC"}),
        lambda: RiskConfig(starting_bankroll_eur="SENTINEL_DIRECT_VALUE_ABC"),  # type: ignore[arg-type]
        lambda: Settings.model_validate(
            {"providers": {"wallet_public_key": "SENTINEL_DIRECT_VALUE_ABC"}}
        ),
    ):
        with pytest.raises(ValueError) as info:
            build()
        assert "SENTINEL_DIRECT_VALUE_ABC" not in str(info.value), str(info.value)
        assert "SENTINEL_DIRECT_VALUE_ABC" not in safe_exception(info.value)
