"""Load Settings from YAML + environment, with a documented precedence: env > yaml > defaults."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from solana_sniper.config.paths import DB_DIR, ENV_FILE, LOG_DIR, configured_home, ensure_home
from solana_sniper.config.settings import Settings

DEFAULT_CONFIG_ENV = "SNIPER_CONFIG"
DEFAULT_CONFIG_PATHS = (Path("configs/default.yaml"), Path("config.yaml"))


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    return loaded


def resolve_config_path(explicit: Path | None) -> Path | None:
    if explicit is not None:
        if not explicit.exists():
            raise FileNotFoundError(f"config file not found: {explicit}")
        return explicit
    env_path = os.environ.get(DEFAULT_CONFIG_ENV)
    if env_path:
        p = Path(env_path)
        if not p.exists():
            raise FileNotFoundError(f"{DEFAULT_CONFIG_ENV}={env_path} does not exist")
        return p
    for candidate in DEFAULT_CONFIG_PATHS:
        if candidate.exists():
            return candidate
    return None


def load_settings(config_path: Path | None = None, **overrides: Any) -> Settings:
    """Build Settings. YAML values are passed as init kwargs; env vars override them.

    pydantic-settings precedence: init kwargs < env < dotenv is NOT what we want (env should
    beat YAML), so we feed YAML through `_yaml_defaults` and let pydantic-settings apply env on top.
    """
    path = resolve_config_path(config_path)
    yaml_data: dict[str, Any] = _read_yaml(path) if path else {}
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(yaml_data.get(key), dict):
            yaml_data[key] = {**yaml_data[key], **value}
        else:
            yaml_data[key] = value

    class _YamlSettings(Settings):
        @classmethod
        def settings_customise_sources(  # type: ignore[override]
            cls,
            settings_cls: type[Settings],
            init_settings: Any,
            env_settings: Any,
            dotenv_settings: Any,
            file_secret_settings: Any,
        ) -> tuple[Any, ...]:
            def yaml_source() -> dict[str, Any]:
                return yaml_data

            # highest priority first
            return (env_settings, dotenv_settings, yaml_source, init_settings)

    home = configured_home()
    env_files: list[str] = [".env"]
    if home is not None:
        ensure_home(home)
        env_files.append(str(home / ENV_FILE))
    settings = _YamlSettings(_env_file=env_files)
    settings.config_path = path
    settings.home = home
    if home is not None:
        apply_home(settings, home)
    return settings


DEFAULT_DB_PREFIX = "sqlite+aiosqlite:///./"


def apply_home(settings: Settings, home: Path) -> None:
    """Relocate relative database/log paths under SNIPER_HOME. Absolute paths are respected."""
    url = settings.storage.database_url
    if url.startswith(DEFAULT_DB_PREFIX):
        name = Path(url.removeprefix(DEFAULT_DB_PREFIX)).name
        settings.storage.database_url = f"sqlite+aiosqlite:///{home / DB_DIR / name}"
    log_file = settings.telemetry.log_file
    if log_file and not Path(log_file).is_absolute():
        settings.telemetry.log_file = str(home / LOG_DIR / Path(log_file).name)
