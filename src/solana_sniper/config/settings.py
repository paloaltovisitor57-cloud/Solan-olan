"""Typed configuration. YAML supplies strategy settings; environment supplies secrets/overrides.

Environment variables use the prefix SNIPER_ and "__" as the nesting delimiter, e.g.
SNIPER_RISK__PROFILE=EXTREME or SNIPER_PROVIDERS__HELIUS_API_KEY=...
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from solana_sniper.domain.enums import RiskProfileName

DiscoverySource = Literal["pumpportal", "geckoterminal", "dexscreener", "synthetic"]
MarketSource = Literal["dexscreener", "pumpportal", "synthetic"]
QuoteSource = Literal["jupiter", "synthetic"]


class ProvidersConfig(BaseModel):
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
    http_timeout_s: float = 8.0
    http_max_connections: int = 32
    ws_reconnect_min_s: float = 1.0
    ws_reconnect_max_s: float = 30.0

    @field_validator("wallet_public_key")
    @classmethod
    def _no_private_key(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        # Solana public keys are 32-44 base58 chars. Private keys are 64 bytes / 87-88 chars
        # or JSON arrays. Refuse anything that looks like a secret.
        if len(v) > 50 or v.strip().startswith("["):
            raise ValueError(
                "wallet_public_key must be a PUBLIC key; private keys are never accepted"
            )
        return v


class DiscoveryConfig(BaseModel):
    sources: list[DiscoverySource] = ["pumpportal", "geckoterminal"]
    max_token_age_s: float = 1800.0  # 30 minutes
    poll_interval_s: float = 5.0
    max_tracked_tokens: int = 150
    dedupe_ttl_s: float = 3600.0
    quote_mints: list[str] = ["So11111111111111111111111111111111111111112"]


class MarketDataConfig(BaseModel):
    sources: list[MarketSource] = ["dexscreener", "pumpportal"]
    poll_interval_s: float = 1.5
    batch_size: int = 30
    stale_after_s: float = 20.0
    trade_window_s: float = 300.0
    buffer_seconds: float = 900.0
    max_concurrent_requests: int = 6


class FiltersConfig(BaseModel):
    max_token_age_s: float = 1800.0
    min_token_age_s: float = 15.0
    min_liquidity_usd: Decimal = Decimal("3000")
    max_liquidity_usd: Decimal = Decimal("750000")
    min_volume_5m_usd: Decimal = Decimal("500")
    min_buys_5m: int = 8
    min_unique_traders: int = 6
    max_top10_holder_pct: float = 0.45
    max_largest_holder_pct: float = 0.18
    require_mint_authority_revoked: bool = True
    require_freeze_authority_revoked: bool = True
    reject_transfer_fee_bps_over: int = 100
    reject_transfer_hook: bool = True
    max_spread_bps: int = 1500
    max_estimated_slippage_bps: int = 800
    max_estimated_exit_slippage_bps: int = 1200
    max_round_trip_loss_pct: float = 0.25
    max_buy_ratio: float = 0.97
    min_buy_ratio: float = 0.35
    liquidity_drop_reject_pct: float = 0.40
    unknown_policy: Literal["allow", "reject"] = "allow"
    max_unknown_checks: int = 3


class EntryWeights(BaseModel):
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


class EntryConfig(BaseModel):
    min_score: float = 65.0
    weights: EntryWeights = EntryWeights()
    min_observations: int = 4
    min_momentum_30s: float = 0.0
    min_momentum_60s: float = -0.05
    min_trade_velocity_per_min: float = 6.0
    min_volume_5m_usd: Decimal = Decimal("500")
    ideal_buy_ratio: float = 0.72
    signal_ttl_s: float = 45.0
    cooldown_after_cancel_s: float = 120.0
    cooldown_after_exit_s: float = 300.0
    cooldown_after_reject_s: float = 900.0
    freshness_half_life_s: float = 480.0
    velocity_full_score_per_min: float = 60.0
    momentum_full_score: float = 0.25
    liquidity_growth_full_score: float = 0.30
    max_pending_signals: int = 3


class RiskProfile(BaseModel):
    base_fraction: float
    max_fraction: float
    max_total_exposure_fraction: float
    max_open_positions: int


class RiskTier(BaseModel):
    name: str
    min_equity_eur: Decimal
    fraction_multiplier: float
    max_open_positions: int | None = None


class HardLimits(BaseModel):
    max_single_position_fraction: float = 0.95
    max_total_exposure_fraction: float = 0.98
    min_position_eur: Decimal = Decimal("5")
    max_position_eur: Decimal = Decimal("250000")
    max_fraction_of_pool_liquidity: float = 0.03
    max_entry_price_impact_pct: float = 4.0


class RiskConfig(BaseModel):
    profile: RiskProfileName = RiskProfileName.EXTREME
    starting_bankroll_eur: Decimal = Decimal("50")
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
    confidence_min_score: float = 60.0
    confidence_full_score: float = 90.0
    confidence_min_multiplier: float = 0.5
    drawdown_start: float = 0.15
    drawdown_full: float = 0.50
    drawdown_floor_multiplier: float = 0.35
    streak_window: int = 10
    loss_step_multiplier: float = 0.15
    streak_min_multiplier: float = 0.40
    win_step_multiplier: float = 0.08
    streak_max_multiplier: float = 1.25
    expectancy_negative_multiplier: float = 0.75
    slippage_penalty_start_bps: int = 300
    slippage_penalty_full_bps: int = 1000
    slippage_penalty_floor: float = 0.5

    @model_validator(mode="after")
    def _validate(self) -> RiskConfig:
        if self.profile not in self.profiles:
            raise ValueError(f"risk.profile {self.profile} has no matching entry in risk.profiles")
        if not self.tiers:
            raise ValueError("risk.tiers must not be empty")
        if sorted(self.tiers, key=lambda t: t.min_equity_eur) != self.tiers:
            raise ValueError("risk.tiers must be sorted by min_equity_eur")
        return self


class TrailingTier(BaseModel):
    min_multiple: float
    drawdown_pct: float


class TrailingConfig(BaseModel):
    tiers: list[TrailingTier] = [
        TrailingTier(min_multiple=0.0, drawdown_pct=0.30),
        TrailingTier(min_multiple=1.3, drawdown_pct=0.24),
        TrailingTier(min_multiple=1.8, drawdown_pct=0.18),
        TrailingTier(min_multiple=3.0, drawdown_pct=0.14),
        TrailingTier(min_multiple=5.0, drawdown_pct=0.10),
        TrailingTier(min_multiple=10.0, drawdown_pct=0.07),
    ]
    reference_volatility: float = 0.06
    max_volatility_widen: float = 1.5
    low_liquidity_usd: Decimal = Decimal("8000")
    low_liquidity_tighten: float = 0.7
    negative_momentum_threshold: float = -0.05
    negative_momentum_tighten: float = 0.8
    duration_tighten_after_s: float = 600.0
    duration_tighten: float = 0.85
    min_drawdown_pct: float = 0.04
    max_drawdown_pct: float = 0.50
    activate_after_gain_pct: float = 0.0

    @model_validator(mode="after")
    def _validate(self) -> TrailingConfig:
        if not self.tiers:
            raise ValueError("exit.trailing.tiers must not be empty")
        if sorted(self.tiers, key=lambda t: t.min_multiple) != self.tiers:
            raise ValueError("exit.trailing.tiers must be sorted by min_multiple")
        return self


class ExitConfig(BaseModel):
    trailing: TrailingConfig = TrailingConfig()
    momentum_window_s: float = 30.0
    momentum_drop_threshold: float = -0.12
    momentum_requires_prior_gain_pct: float = 0.10
    liquidity_collapse_drop_pct: float = 0.35
    liquidity_collapse_window_s: float = 60.0
    volume_collapse_ratio: float = 0.15
    volume_collapse_min_holding_s: float = 90.0
    max_loss_pct: float = 0.35
    max_holding_s: float = 1800.0
    abnormal_price_spike_pct: float = 3.0
    abnormal_spread_bps: int = 2000
    stale_exit_after_s: float = 45.0
    exit_signal_ttl_s: float = 30.0
    exit_signal_cooldown_s: float = 10.0
    min_holding_s_before_trailing: float = 5.0


class QuotesConfig(BaseModel):
    source: QuoteSource = "jupiter"
    slippage_bps: int = 300
    max_quote_age_s: float = 8.0
    quote_timeout_s: float = 4.0
    max_price_impact_pct: float = 6.0
    round_trip_max_loss_pct: float = 0.25
    refresh_interval_s: float = 3.0
    prepare_unsigned_transaction: bool = False
    priority_fee_lamports: int = 200_000
    estimated_network_fee_lamports: int = 10_000
    extra_fill_slippage_bps: int = 50  # dry-run pessimism beyond the quote


class PortfolioConfig(BaseModel):
    base_currency: str = "EUR"
    sol_eur_fallback: Decimal = Decimal("150")
    fx_refresh_s: float = 60.0
    snapshot_interval_s: float = 15.0


class AlertsConfig(BaseModel):
    terminal: bool = True
    discord_webhook_url: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    min_urgency_for_push: Literal["NORMAL", "HIGH", "URGENT"] = "HIGH"


class StorageConfig(BaseModel):
    database_url: str = "sqlite+aiosqlite:///./data/sniper.db"
    write_batch_size: int = 200
    write_flush_interval_s: float = 0.5
    observation_retention_days: int = 14


class TelemetryConfig(BaseModel):
    log_level: str = "INFO"
    log_json: bool = False
    log_file: str | None = "logs/sniper.log"
    metrics_interval_s: float = 30.0


class DryRunConfig(BaseModel):
    confirm_delay_s: float = 2.0
    auto_confirm_buys: bool = True
    auto_confirm_sells: bool = True


class DashboardConfig(BaseModel):
    enabled: bool = True
    refresh_hz: float = 4.0
    top_candidates: int = 8
    event_lines: int = 14


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SNIPER_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

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
    config_path: Path | None = Field(default=None, exclude=True)

    @property
    def is_synthetic(self) -> bool:
        return self.discovery.sources == ["synthetic"]
