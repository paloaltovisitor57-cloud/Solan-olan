"""Typed configuration. YAML supplies strategy settings; environment supplies secrets/overrides.

Environment variables use the prefix SNIPER_ and "__" as the nesting delimiter, e.g.
SNIPER_RISK__PROFILE=EXTREME or SNIPER_PROVIDERS__HELIUS_API_KEY=...
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from solana_sniper.config.base58 import is_solana_public_key
from solana_sniper.domain.enums import RiskProfileName


class ConfigValidationError(ValueError):
    """Validation failure whose text names field paths and reasons but never the rejected
    input (a mistyped private key must not surface in an exception message, log or report)."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("configuration invalid: " + "; ".join(problems))


def validation_problems(exc: ValidationError) -> list[str]:
    """Field paths and reasons only. Nested StrictModels raise ConfigValidationError from their
    own __init__ (pydantic wraps it as a value error at the parent field); unwrap those so the
    full dotted path is reported. Inputs are never included."""
    out: list[str] = []
    for err in exc.errors(include_url=False, include_input=False, include_context=True):
        loc = ".".join(str(part) for part in err.get("loc", ()))
        nested = (err.get("ctx") or {}).get("error")
        if isinstance(nested, ConfigValidationError):
            for problem in nested.problems:
                inner_loc, _, reason = problem.partition(": ")
                path = f"{loc}.{inner_loc}" if loc and inner_loc != "<root>" else (loc or inner_loc)
                out.append(f"{path}: {reason}")
            continue
        out.append(f"{loc or '<root>'}: {err.get('msg', 'invalid')}")
    return out


class StrictModel(BaseModel):
    """Configuration models reject unknown keys (typos) and non-finite numbers, and raise
    ConfigValidationError (input-free) instead of pydantic's ValidationError."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    def __init__(self, /, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise ConfigValidationError(validation_problems(exc)) from None

    @classmethod
    def model_validate(cls, obj: Any, **kwargs: Any) -> Self:
        try:
            return super().model_validate(obj, **kwargs)
        except ValidationError as exc:
            raise ConfigValidationError(validation_problems(exc)) from None


DiscoverySource = Literal["pumpportal", "geckoterminal", "dexscreener", "synthetic"]
MarketSource = Literal["dexscreener", "pumpportal", "synthetic"]
QuoteSource = Literal["jupiter", "synthetic"]


class HostLimit(StrictModel):
    """Pacing and concurrency for one provider host (see infra/governor.py)."""

    rate_per_s: float = Field(default=5.0, gt=0, le=1000)
    burst: int = Field(default=10, ge=1, le=10000)
    max_concurrent: int = Field(default=4, ge=1, le=256)
    max_waiting: int = Field(default=64, ge=1, le=100000)


class CircuitConfig(StrictModel):
    """Cooldown and circuit-breaker behaviour shared by all providers."""

    cooldown_min_s: float = Field(default=1.0, gt=0, le=3600)
    cooldown_max_s: float = Field(default=120.0, gt=0, le=86400)
    fast_fail_wait_s: float = Field(default=3.0, ge=0, le=600)
    trip_after: int = Field(default=5, ge=1, le=1000)
    open_s: float = Field(default=30.0, gt=0, le=86400)
    open_max_s: float = Field(default=300.0, gt=0, le=86400)

    @model_validator(mode="after")
    def _order(self) -> CircuitConfig:
        if self.cooldown_min_s > self.cooldown_max_s:
            raise ValueError("cooldown_min_s must be <= cooldown_max_s")
        if self.open_s > self.open_max_s:
            raise ValueError("open_s must be <= open_max_s")
        return self


class RateLimitsConfig(StrictModel):
    """Realistic defaults for the free/public endpoints. Public Solana RPC is the tightest:
    getTokenLargestAccounts and getAccountInfo are throttled per IP, so the default keeps
    at most two requests in flight at three per second and fails fast during cooldowns
    (the affected checks stay UNKNOWN rather than blocking the engine)."""

    solana_rpc: HostLimit = HostLimit(rate_per_s=3.0, burst=4, max_concurrent=2, max_waiting=40)
    helius: HostLimit = HostLimit(rate_per_s=10.0, burst=20, max_concurrent=4, max_waiting=100)
    geckoterminal: HostLimit = HostLimit(rate_per_s=0.4, burst=2, max_concurrent=1, max_waiting=4)
    dexscreener: HostLimit = HostLimit(rate_per_s=4.0, burst=8, max_concurrent=3, max_waiting=40)
    jupiter: HostLimit = HostLimit(rate_per_s=1.0, burst=3, max_concurrent=2, max_waiting=20)
    jupiter_pro: HostLimit = HostLimit(rate_per_s=8.0, burst=8, max_concurrent=4, max_waiting=40)
    coingecko: HostLimit = HostLimit(rate_per_s=0.3, burst=2, max_concurrent=1, max_waiting=4)
    pumpfun: HostLimit = HostLimit(rate_per_s=2.0, burst=4, max_concurrent=2, max_waiting=20)
    circuit: CircuitConfig = CircuitConfig()


class ProvidersConfig(StrictModel):
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    solana_ws_url: str = "wss://api.mainnet-beta.solana.com"
    helius_api_key: SecretStr | None = None
    jupiter_api_key: SecretStr | None = None
    jupiter_base_url: str = "https://lite-api.jup.ag"
    jupiter_pro_base_url: str = "https://api.jup.ag"
    dexscreener_base_url: str = "https://api.dexscreener.com"
    geckoterminal_base_url: str = "https://api.geckoterminal.com/api/v2"
    pumpportal_ws_url: str = "wss://pumpportal.fun/api/data"
    pumpfun_api_url: str = "https://frontend-api-v3.pump.fun"
    coingecko_base_url: str = "https://api.coingecko.com/api/v3"
    wallet_public_key: str | None = None  # PUBLIC key only; used to prepare unsigned txs
    http_timeout_s: float = Field(default=8.0, gt=0, le=120)
    http_max_connections: int = Field(default=32, ge=1, le=1000)
    ws_reconnect_min_s: float = Field(default=1.0, ge=0.1, le=300)
    ws_reconnect_max_s: float = Field(default=30.0, ge=0.1, le=3600)
    rate_limits: RateLimitsConfig = RateLimitsConfig()

    @field_validator("wallet_public_key")
    @classmethod
    def _reject_secret_material(cls, v: str | None) -> str | None:
        if v is None:
            return None
        candidate = v.strip()
        if candidate == "":
            return None
        # A Solana public key is 32 bytes, base58 (32-44 chars). Anything else (private keys are
        # 64 bytes / JSON byte arrays / hex, seed phrases, ...) is refused. The supplied value is
        # deliberately NOT included in the error message.
        if not is_solana_public_key(candidate):
            raise ValueError(
                "wallet_public_key is not a valid base58 32-byte PUBLIC key "
                "(private keys, seed phrases and byte arrays are never accepted)"
            )
        return candidate

    @field_validator(
        "solana_rpc_url",
        "solana_ws_url",
        "jupiter_base_url",
        "jupiter_pro_base_url",
        "dexscreener_base_url",
        "geckoterminal_base_url",
        "pumpportal_ws_url",
        "pumpfun_api_url",
        "coingecko_base_url",
    )
    @classmethod
    def _url_scheme(cls, v: str) -> str:
        if not v.startswith(("http://", "https://", "ws://", "wss://")):
            raise ValueError("provider URLs must start with http(s):// or ws(s)://")
        return v

    @model_validator(mode="after")
    def _backoff_order(self) -> ProvidersConfig:
        if self.ws_reconnect_min_s > self.ws_reconnect_max_s:
            raise ValueError("ws_reconnect_min_s must be <= ws_reconnect_max_s")
        return self


class DiscoveryConfig(StrictModel):
    sources: list[DiscoverySource] = ["pumpportal", "geckoterminal"]
    max_token_age_s: float = Field(default=1800.0, gt=0, le=86400)  # 30 minutes
    poll_interval_s: float = Field(default=5.0, ge=0.2, le=3600)
    max_tracked_tokens: int = Field(default=150, ge=1, le=10000)
    dedupe_ttl_s: float = Field(default=3600.0, gt=0, le=604800)
    quote_mints: list[str] = ["So11111111111111111111111111111111111111112"]


class MarketDataConfig(StrictModel):
    sources: list[MarketSource] = ["dexscreener", "pumpportal"]
    poll_interval_s: float = Field(default=1.5, ge=0.05, le=3600)
    batch_size: int = Field(default=30, ge=1, le=30)
    stale_after_s: float = Field(default=20.0, ge=0.5, le=3600)
    trade_window_s: float = Field(default=300.0, gt=0, le=86400)
    buffer_seconds: float = Field(default=900.0, gt=0, le=86400)
    max_concurrent_requests: int = Field(default=6, ge=1, le=100)


class FiltersConfig(StrictModel):
    max_token_age_s: float = Field(default=1800.0, gt=0, le=86400)
    min_token_age_s: float = Field(default=15.0, ge=0, le=86400)
    min_liquidity_usd: Decimal = Field(default=Decimal("3000"), ge=0)
    max_liquidity_usd: Decimal = Field(default=Decimal("750000"), gt=0)
    min_volume_5m_usd: Decimal = Field(default=Decimal("500"), ge=0)
    min_buys_5m: int = Field(default=8, ge=0)
    min_unique_traders: int = Field(default=6, ge=0)
    max_top10_holder_pct: float = Field(default=0.45, ge=0, le=1)
    max_largest_holder_pct: float = Field(default=0.18, ge=0, le=1)
    require_mint_authority_revoked: bool = True
    require_freeze_authority_revoked: bool = True
    reject_transfer_fee_bps_over: int = Field(default=100, ge=0, le=10000)
    reject_transfer_hook: bool = True
    max_spread_bps: int = Field(default=1500, ge=0, le=100000)
    max_estimated_slippage_bps: int = Field(default=800, ge=0, le=10000)
    max_estimated_exit_slippage_bps: int = Field(default=1200, ge=0, le=10000)
    max_round_trip_loss_pct: float = Field(default=0.25, ge=0, le=1)
    max_buy_ratio: float = Field(default=0.97, ge=0, le=1)
    min_buy_ratio: float = Field(default=0.35, ge=0, le=1)
    liquidity_drop_reject_pct: float = Field(default=0.40, gt=0, le=1)
    unknown_policy: Literal["allow", "reject"] = "allow"

    @model_validator(mode="after")
    def _ranges(self) -> FiltersConfig:
        if self.min_token_age_s > self.max_token_age_s:
            raise ValueError("filters.min_token_age_s must be <= max_token_age_s")
        if self.min_liquidity_usd > self.max_liquidity_usd:
            raise ValueError("filters.min_liquidity_usd must be <= max_liquidity_usd")
        if self.min_buy_ratio > self.max_buy_ratio:
            raise ValueError("filters.min_buy_ratio must be <= max_buy_ratio")
        return self

    max_unknown_checks: int = Field(default=3, ge=0, le=100)


class EntryWeights(StrictModel):
    freshness: float = 12.0
    liquidity_growth: float = 12.0
    trade_velocity: float = 12.0
    participation_growth: float = 10.0
    momentum: float = 14.0
    acceleration: float = 10.0
    demand_balance: float = 8.0
    concentration: float = 6.0
    exit_viability: float = 10.0
    slippage: float = 6.0

    def total(self) -> float:
        return sum(
            (
                self.freshness,
                self.liquidity_growth,
                self.trade_velocity,
                self.participation_growth,
                self.momentum,
                self.acceleration,
                self.demand_balance,
                self.concentration,
                self.exit_viability,
                self.slippage,
            )
        )


class EntryConfig(StrictModel):
    min_score: float = Field(default=65.0, ge=0, le=100)
    weights: EntryWeights = EntryWeights()
    min_observations: int = Field(default=4, ge=1, le=100000)
    min_momentum_30s: float = 0.0
    min_momentum_60s: float = -0.05
    min_trade_velocity_per_min: float = Field(default=6.0, ge=0)
    min_volume_5m_usd: Decimal = Field(default=Decimal("500"), ge=0)
    ideal_buy_ratio: float = Field(default=0.72, ge=0, le=1)
    signal_ttl_s: float = Field(default=45.0, gt=0, le=3600)
    cooldown_after_cancel_s: float = Field(default=120.0, ge=0, le=86400)
    cooldown_after_exit_s: float = Field(default=300.0, ge=0, le=86400)
    cooldown_after_reject_s: float = Field(default=900.0, ge=0, le=86400)
    freshness_half_life_s: float = Field(default=480.0, gt=0, le=86400)
    velocity_full_score_per_min: float = Field(default=60.0, gt=0)
    momentum_full_score: float = Field(default=0.25, gt=0)
    liquidity_growth_full_score: float = Field(default=0.30, gt=0)
    max_pending_signals: int = Field(default=3, ge=1, le=100)


class RiskProfile(StrictModel):
    base_fraction: float = Field(ge=0, le=1)
    max_fraction: float = Field(ge=0, le=1)
    max_total_exposure_fraction: float = Field(ge=0, le=1)
    max_open_positions: int = Field(ge=1, le=100)


class RiskTier(StrictModel):
    name: str = Field(min_length=1, max_length=32)
    min_equity_eur: Decimal = Field(ge=0)
    fraction_multiplier: float = Field(ge=0, le=5)
    max_open_positions: int | None = Field(default=None, ge=1, le=100)


class HardLimits(StrictModel):
    max_single_position_fraction: float = Field(default=0.95, gt=0, le=1)
    max_total_exposure_fraction: float = Field(default=0.98, gt=0, le=1)
    min_position_eur: Decimal = Field(default=Decimal("5"), gt=0)
    max_position_eur: Decimal = Field(default=Decimal("250000"), gt=0)
    max_fraction_of_pool_liquidity: float = Field(default=0.03, gt=0, le=1)
    max_entry_price_impact_pct: float = Field(default=4.0, ge=0, le=100)


class RiskConfig(StrictModel):
    profile: RiskProfileName = RiskProfileName.EXTREME
    starting_bankroll_eur: Decimal = Field(default=Decimal("50"), gt=0)
    profiles: dict[RiskProfileName, RiskProfile] = {
        RiskProfileName.NORMAL: RiskProfile(
            base_fraction=0.10,
            max_fraction=0.20,
            max_total_exposure_fraction=0.50,
            max_open_positions=3,
        ),
        RiskProfileName.AGGRESSIVE: RiskProfile(
            base_fraction=0.25,
            max_fraction=0.45,
            max_total_exposure_fraction=0.80,
            max_open_positions=3,
        ),
        RiskProfileName.EXTREME: RiskProfile(
            base_fraction=0.50,
            max_fraction=0.90,
            max_total_exposure_fraction=0.95,
            max_open_positions=2,
        ),
    }
    hard_limits: HardLimits = HardLimits()
    tiers: list[RiskTier] = [
        RiskTier(name="micro", min_equity_eur=Decimal("0"), fraction_multiplier=1.0),
        RiskTier(name="small", min_equity_eur=Decimal("300"), fraction_multiplier=0.85),
        RiskTier(name="mid", min_equity_eur=Decimal("1500"), fraction_multiplier=0.65),
        RiskTier(name="large", min_equity_eur=Decimal("5000"), fraction_multiplier=0.45),
        RiskTier(
            name="xl",
            min_equity_eur=Decimal("25000"),
            fraction_multiplier=0.30,
            max_open_positions=3,
        ),
        RiskTier(
            name="whale",
            min_equity_eur=Decimal("100000"),
            fraction_multiplier=0.20,
            max_open_positions=4,
        ),
    ]
    milestones_eur: list[Decimal] = [
        Decimal(x)
        for x in (50, 100, 150, 300, 700, 1500, 3000, 5000, 10000, 25000, 50000, 100000, 300000)
    ]
    milestone_hysteresis_pct: float = Field(default=0.05, ge=0, lt=1)
    confidence_min_score: float = Field(default=60.0, ge=0, le=100)
    confidence_full_score: float = Field(default=90.0, ge=0, le=100)
    confidence_min_multiplier: float = Field(default=0.5, ge=0, le=1)
    drawdown_start: float = Field(default=0.15, ge=0, le=1)
    drawdown_full: float = Field(default=0.50, ge=0, le=1)
    drawdown_floor_multiplier: float = Field(default=0.35, ge=0, le=1)
    streak_window: int = Field(default=10, ge=1, le=1000)
    loss_step_multiplier: float = Field(default=0.15, ge=0, le=1)
    streak_min_multiplier: float = Field(default=0.40, ge=0, le=5)
    win_step_multiplier: float = Field(default=0.08, ge=0, le=1)
    streak_max_multiplier: float = Field(default=1.25, ge=0, le=5)
    expectancy_negative_multiplier: float = Field(default=0.75, ge=0, le=1)
    slippage_penalty_start_bps: int = Field(default=300, ge=0, le=10000)
    slippage_penalty_full_bps: int = Field(default=1000, ge=0, le=10000)
    slippage_penalty_floor: float = Field(default=0.5, ge=0, le=1)

    @model_validator(mode="after")
    def _validate(self) -> RiskConfig:
        if self.profile not in self.profiles:
            raise ValueError(f"risk.profile {self.profile} has no matching entry in risk.profiles")
        if not self.tiers:
            raise ValueError("risk.tiers must not be empty")
        if sorted(self.tiers, key=lambda t: t.min_equity_eur) != self.tiers:
            raise ValueError("risk.tiers must be sorted by min_equity_eur")
        if not self.milestones_eur or any(m <= 0 for m in self.milestones_eur):
            raise ValueError("risk.milestones_eur must be non-empty positive amounts")
        if self.confidence_min_score > self.confidence_full_score:
            raise ValueError("risk.confidence_min_score must be <= confidence_full_score")
        if self.drawdown_start > self.drawdown_full:
            raise ValueError("risk.drawdown_start must be <= drawdown_full")
        if self.slippage_penalty_start_bps > self.slippage_penalty_full_bps:
            raise ValueError("risk.slippage_penalty_start_bps must be <= slippage_penalty_full_bps")
        if self.streak_min_multiplier > self.streak_max_multiplier:
            raise ValueError("risk.streak_min_multiplier must be <= streak_max_multiplier")
        for name, profile in self.profiles.items():
            if profile.base_fraction > profile.max_fraction:
                raise ValueError(f"risk.profiles.{name}: base_fraction must be <= max_fraction")
        hl = self.hard_limits
        if hl.min_position_eur > hl.max_position_eur:
            raise ValueError("risk.hard_limits.min_position_eur must be <= max_position_eur")
        return self


class TrailingTier(StrictModel):
    min_multiple: float = Field(ge=0)
    drawdown_pct: float = Field(gt=0, le=1)


class TrailingConfig(StrictModel):
    tiers: list[TrailingTier] = [
        TrailingTier(min_multiple=0.0, drawdown_pct=0.30),
        TrailingTier(min_multiple=1.3, drawdown_pct=0.24),
        TrailingTier(min_multiple=1.8, drawdown_pct=0.18),
        TrailingTier(min_multiple=3.0, drawdown_pct=0.14),
        TrailingTier(min_multiple=5.0, drawdown_pct=0.10),
        TrailingTier(min_multiple=10.0, drawdown_pct=0.07),
    ]
    reference_volatility: float = Field(default=0.06, gt=0)
    max_volatility_widen: float = Field(default=1.5, ge=1, le=10)
    low_liquidity_usd: Decimal = Field(default=Decimal("8000"), ge=0)
    low_liquidity_tighten: float = Field(default=0.7, gt=0, le=1)
    negative_momentum_threshold: float = Field(default=-0.05, le=0)
    negative_momentum_tighten: float = Field(default=0.8, gt=0, le=1)
    duration_tighten_after_s: float = Field(default=600.0, ge=0)
    duration_tighten: float = Field(default=0.85, gt=0, le=1)
    min_drawdown_pct: float = Field(default=0.04, gt=0, le=1)
    max_drawdown_pct: float = Field(default=0.50, gt=0, le=1)
    activate_after_gain_pct: float = Field(default=0.0, ge=0)

    @model_validator(mode="after")
    def _validate(self) -> TrailingConfig:
        if not self.tiers:
            raise ValueError("exit.trailing.tiers must not be empty")
        if sorted(self.tiers, key=lambda t: t.min_multiple) != self.tiers:
            raise ValueError("exit.trailing.tiers must be sorted by min_multiple")
        if self.min_drawdown_pct > self.max_drawdown_pct:
            raise ValueError("exit.trailing.min_drawdown_pct must be <= max_drawdown_pct")
        return self


class ExitConfig(StrictModel):
    trailing: TrailingConfig = TrailingConfig()
    momentum_window_s: float = Field(default=30.0, gt=0, le=3600)
    momentum_drop_threshold: float = Field(default=-0.12, le=0)
    momentum_requires_prior_gain_pct: float = Field(default=0.10, ge=0)
    liquidity_collapse_drop_pct: float = Field(default=0.35, gt=0, le=1)
    liquidity_collapse_window_s: float = Field(default=60.0, gt=0, le=86400)
    volume_collapse_ratio: float = Field(default=0.15, gt=0, le=1)
    volume_collapse_min_holding_s: float = Field(default=90.0, ge=0)
    max_loss_pct: float = Field(default=0.35, gt=0, le=1)
    max_holding_s: float = Field(default=1800.0, gt=0, le=604800)
    abnormal_price_spike_pct: float = Field(default=3.0, gt=0)
    abnormal_spread_bps: int = Field(default=2000, ge=0, le=100000)
    stale_exit_after_s: float = Field(default=45.0, gt=0, le=86400)
    exit_signal_ttl_s: float = Field(default=30.0, gt=0, le=3600)
    exit_signal_cooldown_s: float = Field(default=10.0, ge=0, le=3600)
    min_holding_s_before_trailing: float = Field(default=5.0, ge=0)


class QuotesConfig(StrictModel):
    source: QuoteSource = "jupiter"
    slippage_bps: int = Field(default=300, ge=0, le=10000)
    max_quote_age_s: float = Field(default=8.0, gt=0, le=600)
    quote_timeout_s: float = Field(default=4.0, gt=0, le=120)
    max_price_impact_pct: float = Field(default=6.0, ge=0, le=100)
    round_trip_max_loss_pct: float = Field(default=0.25, ge=0, le=1)
    refresh_interval_s: float = Field(default=3.0, gt=0, le=600)
    prepare_unsigned_transaction: bool = False
    priority_fee_lamports: int = Field(default=200_000, ge=0, le=10**9)
    estimated_network_fee_lamports: int = Field(default=10_000, ge=0, le=10**9)
    extra_fill_slippage_bps: int = Field(default=50, ge=0, le=10000)


class PortfolioConfig(StrictModel):
    base_currency: str = "EUR"
    sol_eur_fallback: Decimal = Field(default=Decimal("150"), gt=0)
    fx_refresh_s: float = Field(default=60.0, gt=0, le=86400)
    snapshot_interval_s: float = Field(default=15.0, gt=0, le=86400)


class AlertsConfig(StrictModel):
    terminal: bool = True
    discord_webhook_url: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    min_urgency_for_push: Literal["NORMAL", "HIGH", "URGENT"] = "HIGH"


class StorageConfig(StrictModel):
    database_url: str = "sqlite+aiosqlite:///./data/sniper.db"
    write_batch_size: int = Field(default=200, ge=1, le=100000)
    write_flush_interval_s: float = Field(default=0.5, gt=0, le=60)
    max_queued_telemetry: int = Field(default=20_000, ge=100, le=1_000_000)
    observation_retention_days: int = Field(default=14, ge=0, le=3650)


class TelemetryConfig(StrictModel):
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_json: bool = False
    log_file: str | None = "logs/sniper.log"
    metrics_interval_s: float = Field(default=30.0, gt=0, le=86400)


class DryRunConfig(StrictModel):
    confirm_delay_s: float = Field(default=2.0, ge=0, le=3600)
    auto_confirm_buys: bool = True
    auto_confirm_sells: bool = True


class OutcomesConfig(StrictModel):
    """Forward outcome tracking of every observed candidate (measurement, not prediction)."""

    enabled: bool = True
    horizon_s: float = Field(default=3600.0, ge=60, le=86400)
    silence_timeout_s: float = Field(default=900.0, ge=30, le=86400)
    rug_liquidity_drop_pct: float = Field(default=0.6, gt=0, le=1)
    max_followed: int = Field(default=400, ge=1, le=5000)


class DashboardConfig(StrictModel):
    enabled: bool = True
    refresh_hz: float = Field(default=4.0, ge=0.1, le=60)
    top_candidates: int = Field(default=8, ge=1, le=100)
    event_lines: int = Field(default=14, ge=1, le=200)


class Settings(BaseSettings):
    """Top level is strict too: unknown keys are errors. Dotenv files are *not* read here (they
    may legitimately contain service and unrelated variables); the loader supplies a filtered
    dotenv source that only forwards known application keys."""

    model_config = SettingsConfigDict(
        env_prefix="SNIPER_",
        env_nested_delimiter="__",
        env_file=None,
        extra="forbid",
    )

    def __init__(self, /, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise ConfigValidationError(validation_problems(exc)) from None

    @classmethod
    def model_validate(cls, obj: Any, **kwargs: Any) -> Self:
        try:
            return super().model_validate(obj, **kwargs)
        except ValidationError as exc:
            raise ConfigValidationError(validation_problems(exc)) from None

    providers: ProvidersConfig = ProvidersConfig()
    discovery: DiscoveryConfig = DiscoveryConfig()
    market_data: MarketDataConfig = MarketDataConfig()
    filters: FiltersConfig = FiltersConfig()
    entry: EntryConfig = EntryConfig()
    risk: RiskConfig = RiskConfig()
    exit: ExitConfig = ExitConfig()
    quotes: QuotesConfig = QuotesConfig()
    portfolio: PortfolioConfig = PortfolioConfig()
    alerts: AlertsConfig = AlertsConfig()
    storage: StorageConfig = StorageConfig()
    telemetry: TelemetryConfig = TelemetryConfig()
    dry_run: DryRunConfig = DryRunConfig()
    dashboard: DashboardConfig = DashboardConfig()
    outcomes: OutcomesConfig = OutcomesConfig()
    config_path: Path | None = Field(default=None, exclude=True)
    home: Path | None = Field(default=None, exclude=True)  # SNIPER_HOME, if configured

    @property
    def is_synthetic(self) -> bool:
        return self.discovery.sources == ["synthetic"]

    def secret_values(self) -> list[str]:
        """Every configured credential, for log redaction registration. Never logged itself."""
        p, a = self.providers, self.alerts
        values = [
            p.helius_api_key.get_secret_value() if p.helius_api_key else None,
            p.jupiter_api_key.get_secret_value() if p.jupiter_api_key else None,
            a.discord_webhook_url.get_secret_value() if a.discord_webhook_url else None,
            a.telegram_bot_token.get_secret_value() if a.telegram_bot_token else None,
        ]
        return [v for v in values if v]

    def credential_urls(self) -> list[str]:
        """URLs that may embed credentials (query strings, userinfo) for redaction."""
        p, a = self.providers, self.alerts
        urls = [
            p.solana_rpc_url,
            p.solana_ws_url,
            p.jupiter_pro_base_url,
            self.storage.database_url,
        ]
        if a.discord_webhook_url:
            urls.append(a.discord_webhook_url.get_secret_value())
        return urls

    @property
    def state_dir(self) -> Path:
        return (self.home / "state") if self.home else Path("data") / "state"
