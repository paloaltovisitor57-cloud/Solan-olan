"""Load Settings from YAML + environment with strict validation.

Precedence (highest first): process environment > dotenv files (`.env`, then
`$SNIPER_HOME/sniper.env`) > YAML file > model defaults.

Strictness:
* every YAML key must exist in the model (nested typos such as `risk.profil` are rejected with the
  full key path);
* every `SNIPER_*` variable in the environment or the dotenv files must map to a setting or be one
  of the documented service variables; other environment variables are never inspected;
* numbers must be finite and inside their documented bounds (see settings.py);
* validation errors never echo the offending value (a mistyped private key must not end up in a
  log or report).
"""

from __future__ import annotations

import os
import types
import typing
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError
from pydantic_settings import DotEnvSettingsSource

from solana_sniper.config.paths import DB_DIR, ENV_FILE, LOG_DIR, configured_home, ensure_home
from solana_sniper.config.settings import ConfigValidationError, Settings
from solana_sniper.telemetry.redaction import register_secret, register_url_secrets

DEFAULT_CONFIG_ENV = "SNIPER_CONFIG"
DEFAULT_CONFIG_PATHS = (Path("configs/default.yaml"), Path("config.yaml"))
ENV_PREFIX = "SNIPER_"
# Variables consumed by the deployment scripts / launchd, not by the application model.
SERVICE_ENV_KEYS = frozenset(
    {
        "SNIPER_CONFIG",
        "SNIPER_HOME",
        "SNIPER_SERVICE_MODE",
        "SNIPER_VENV",
        "SNIPER_PYTHON",
        "SNIPER_SERVICE_LABEL",
        "SNIPER_LAUNCH_AGENTS_DIR",
    }
)


class ConfigError(Exception):
    """Configuration problem with a human-readable, value-free summary."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("configuration invalid:\n  - " + "\n  - ".join(problems))


# ---------------------------------------------------------------------------- YAML
def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh) or {}
    if not isinstance(loaded, dict):
        raise ConfigError([f"{path}: top level must be a mapping"])
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
    home = configured_home()
    user_override = home / "config.yaml"
    if user_override.exists():
        return user_override
    for candidate in DEFAULT_CONFIG_PATHS:
        if candidate.exists():
            return candidate
    # Not running from the checkout directory (e.g. `solana-sniper status` from $HOME): use the
    # default config that ships with the installed package's checkout.
    packaged = Path(__file__).resolve().parents[3] / "configs" / "default.yaml"
    if packaged.exists():
        return packaged
    return None


def _model_of(annotation: Any) -> type[BaseModel] | None:
    """The BaseModel class named by an annotation (unwrapping Optional/Union), else None."""
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        for arg in typing.get_args(annotation):
            found = _model_of(arg)
            if found is not None:
                return found
        return None
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None


def _dict_value_model(annotation: Any) -> type[BaseModel] | None:
    if typing.get_origin(annotation) is dict:
        args = typing.get_args(annotation)
        if len(args) == 2:
            return _model_of(args[1])
    return None


def unknown_keys(data: Mapping[str, Any], model: type[BaseModel], prefix: str = "") -> list[str]:
    """Dotted paths of keys that no model field accepts, recursing into nested models."""
    problems: list[str] = []
    fields = model.model_fields
    for key, value in data.items():
        path = f"{prefix}{key}"
        field = fields.get(str(key))
        if field is None:
            problems.append(f"unknown key '{path}'")
            continue
        nested = _model_of(field.annotation)
        if nested is not None and isinstance(value, Mapping):
            problems.extend(unknown_keys(value, nested, f"{path}."))
            continue
        value_model = _dict_value_model(field.annotation)
        if value_model is not None and isinstance(value, Mapping):
            for sub_key, sub_value in value.items():
                if isinstance(sub_value, Mapping):
                    problems.extend(unknown_keys(sub_value, value_model, f"{path}.{sub_key}."))
    return problems


# ---------------------------------------------------------------- environment
def known_env_prefixes(model: type[BaseModel], prefix: str = ENV_PREFIX) -> set[str]:
    """Upper-case env names (or prefixes for dict/list fields) the model can consume."""
    names: set[str] = set()
    for name, field in model.model_fields.items():
        if field.exclude:
            continue
        env_name = f"{prefix}{name.upper()}"
        nested = _model_of(field.annotation)
        if nested is not None:
            names.add(env_name)  # a whole nested section may be supplied as JSON
            names |= known_env_prefixes(nested, f"{env_name}__")
        elif _dict_value_model(field.annotation) is not None or typing.get_origin(
            field.annotation
        ) in (dict, list):
            names.add(env_name)
            names.add(f"{env_name}__*")
        else:
            names.add(env_name)
    return names


def _env_name_known(name: str, known: set[str]) -> bool:
    upper = name.upper()
    if upper in known or upper in SERVICE_ENV_KEYS:
        return True
    return any(upper.startswith(k[:-1]) for k in known if k.endswith("*"))


def _dotenv_keys(path: Path) -> list[str]:
    keys: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return keys
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        keys.append(key)
    return keys


def audit_environment(
    environ: Mapping[str, str], dotenv_paths: list[Path], model: type[BaseModel] = Settings
) -> list[str]:
    """Names of SNIPER_* variables (process env or dotenv) that map to nothing. Values are never
    read, so the audit cannot leak them."""
    known = known_env_prefixes(model)
    problems: list[str] = []
    for name in sorted(environ):
        if name.upper().startswith(ENV_PREFIX) and not _env_name_known(name, known):
            problems.append(f"unknown environment variable '{name}'")
    for path in dotenv_paths:
        for name in _dotenv_keys(path):
            if name.upper().startswith(ENV_PREFIX) and not _env_name_known(name, known):
                problems.append(f"unknown variable '{name}' in {path}")
    return problems


def _validation_problems(exc: ValidationError) -> list[str]:
    """Field paths and messages only: the offending input is deliberately omitted."""
    out: list[str] = []
    for err in exc.errors(include_url=False, include_input=False, include_context=False):
        loc = ".".join(str(part) for part in err.get("loc", ()))
        out.append(f"{loc or '<root>'}: {err.get('msg', 'invalid')}")
    return out


# -------------------------------------------------------------------- loading
def load_settings(config_path: Path | None = None, **overrides: Any) -> Settings:
    path = resolve_config_path(config_path)
    yaml_data: dict[str, Any] = _read_yaml(path) if path else {}
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(yaml_data.get(key), dict):
            yaml_data[key] = {**yaml_data[key], **value}
        else:
            yaml_data[key] = value
    problems = [f"{path or 'overrides'}: {p}" for p in unknown_keys(yaml_data, Settings)]

    home = configured_home()
    env_files: list[str] = [".env"]
    ensure_home(home)
    env_files.append(str(home / ENV_FILE))
    problems.extend(audit_environment(os.environ, [Path(f) for f in env_files if Path(f).exists()]))
    if problems:
        raise ConfigError(problems)

    known = known_env_prefixes(Settings)
    present_env_files = [f for f in env_files if Path(f).exists()]

    class _FilteredDotEnv(DotEnvSettingsSource):
        """Dotenv source that forwards only known application keys. Service variables
        (SNIPER_SERVICE_MODE, ...) and unrelated tool variables are dropped here, so the strict
        Settings model never sees them; unknown SNIPER_* keys were already reported above."""

        def _read_env_files(self) -> Mapping[str, str | None]:
            raw = super()._read_env_files()
            return {
                k: v
                for k, v in raw.items()
                if k.upper().startswith(ENV_PREFIX)
                and k.upper() not in SERVICE_ENV_KEYS
                and _env_name_known(k, known)
            }

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

            filtered_dotenv = _FilteredDotEnv(
                settings_cls,
                env_file=present_env_files,
                env_file_encoding="utf-8",
                env_prefix=ENV_PREFIX,
                env_nested_delimiter="__",
                case_sensitive=False,
            )
            # highest priority first
            return (env_settings, filtered_dotenv, yaml_source, init_settings)

    try:
        settings = _YamlSettings()
    except ConfigValidationError as exc:
        raise ConfigError(exc.problems) from None
    except ValidationError as exc:
        raise ConfigError(_validation_problems(exc)) from None
    settings.config_path = path
    settings.home = home
    apply_home(settings, home)
    for secret in settings.secret_values():
        register_secret(secret)
    for url in settings.credential_urls():
        register_url_secrets(url)
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
