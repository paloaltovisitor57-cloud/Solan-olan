"""Composition root: builds every component from Settings and wires them together.

The same builder serves live signal mode, dry-run (same live data, simulated confirmations),
synthetic offline runs and replay. Only the execution adapter and data sources differ.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from websockets.asyncio.client import ClientConnection

from solana_sniper.alerts.service import AlertService
from solana_sniper.alerts.terminal import TerminalAlertProvider
from solana_sniper.alerts.webhooks import DiscordAlertProvider, TelegramAlertProvider
from solana_sniper.app.bus import EventBus
from solana_sniper.app.engine import Engine, EngineDeps
from solana_sniper.app.health import ConnectionProbe, HealthReporter
from solana_sniper.app.persistence import PersistenceSubscriber
from solana_sniper.config.paths import STATUS_FILE, state_path
from solana_sniper.config.settings import Settings
from solana_sniper.discovery.dexscreener import DexScreenerDiscovery
from solana_sniper.discovery.geckoterminal import GeckoTerminalDiscovery
from solana_sniper.discovery.pumpportal import PumpPortalClient, PumpPortalDiscovery
from solana_sniper.discovery.service import DiscoveryService
from solana_sniper.domain.clock import Clock, SystemClock
from solana_sniper.domain.enums import RunMode, Urgency
from solana_sniper.domain.models import MarketSnapshot, TokenInfo, TradeEvent
from solana_sniper.execution.base import ExecutionInterface
from solana_sniper.execution.manual import DryRunExecution, ManualExecution
from solana_sniper.execution.preparer import TransactionPreparer
from solana_sniper.features.engine import FeatureEngine
from solana_sniper.filters.checks import TokenChecker
from solana_sniper.infra.http import HttpClient
from solana_sniper.market_data.dexscreener import DexScreenerMarketData
from solana_sniper.market_data.pumpportal_stream import PumpPortalMarketData
from solana_sniper.market_data.service import MarketDataService
from solana_sniper.market_data.synthetic import (
    SyntheticDiscovery,
    SyntheticMarketData,
    SyntheticQuoteProvider,
    SyntheticTicker,
    SyntheticTokenProvider,
    SyntheticWorld,
)
from solana_sniper.market_data.tracker import TokenTracker
from solana_sniper.portfolio.accounting import PortfolioAccount
from solana_sniper.portfolio.fx import CoinGeckoFx, FxProvider, StaticFx
from solana_sniper.positions.monitor import PositionMonitor
from solana_sniper.quotes.base import QuoteProvider
from solana_sniper.quotes.jupiter import JupiterQuoteProvider
from solana_sniper.quotes.round_trip import RoundTripEvaluator
from solana_sniper.risk.engine import RiskEngine
from solana_sniper.risk.milestones import MilestoneTracker
from solana_sniper.storage.repository import Repository
from solana_sniper.strategy.gate import EntryGate
from solana_sniper.strategy.scoring import EntryScorer
from solana_sniper.telemetry.logging import get_logger
from solana_sniper.telemetry.metrics import Metrics
from solana_sniper.token_analysis.base import LiquidityProvider, TokenMetadataProvider
from solana_sniper.token_analysis.solana_rpc import SolanaRpcTokenProvider

log = get_logger(__name__)

Task = Callable[[], Coroutine[Any, Any, None]]


def new_session_id(mode: RunMode) -> str:
    stamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
    return f"{mode.lower()}-{stamp}-{uuid.uuid4().hex[:6]}"


@dataclass
class Runtime:
    settings: Settings
    mode: RunMode
    session_id: str
    clock: Clock
    engine: Engine
    bus: EventBus
    repo: Repository
    metrics: Metrics
    account: PortfolioAccount
    discovery: DiscoveryService
    market: MarketDataService
    alerts: AlertService
    terminal_alerts: TerminalAlertProvider
    http: HttpClient
    background: list[tuple[str, Task]] = field(default_factory=list)
    synthetic_world: SyntheticWorld | None = None
    quote_provider: QuoteProvider | None = None
    health: HealthReporter | None = None
    _tasks: list[asyncio.Task[None]] = field(default_factory=list)

    async def start(self) -> None:
        await self.repo.init()
        self.repo.start()
        await self.repo.start_session(str(self.mode), str(self.settings.config_path or ""))
        await self._restore_account()
        self.bus.start()
        for name, task in self.background:
            self._tasks.append(asyncio.create_task(task(), name=name))
        self._tasks.append(asyncio.create_task(self.engine.run(), name="engine"))
        if self.health is not None:
            self._tasks.append(asyncio.create_task(self.health.run(), name="health"))

    async def _restore_account(self) -> None:
        state = await self.repo.load_state()
        if state.has_account:
            self.account.restore(
                cash=state.cash,
                ledger=state.ledger,
                positions=state.positions,
                peak_equity=state.peak_equity,
                realized_pnl=state.realized_pnl,
                fees=state.fees,
                slippage=state.slippage,
                wins=state.wins,
                losses=state.losses,
                recent_results=state.recent_results,
            )
            self.engine.d.milestones.restore(state.milestones_reached)
            log.info(
                "portfolio_restored",
                cash=str(self.account.cash),
                open_positions=len(self.account.open_positions),
                equity=str(self.account.equity),
            )
        else:
            self.account.deposit(self.settings.risk.starting_bankroll_eur, "starting bankroll")
            await self.repo.save_ledger_now(self.account.ledger)
            await self.repo.save_account_state_now(
                cash=self.account.cash,
                peak_equity=self.account.peak_equity,
                realized_pnl=Decimal(0),
                fees=Decimal(0),
                slippage=Decimal(0),
                wins=0,
                losses=0,
                recent_results=[],
                milestones_reached=[],
            )
            log.info("portfolio_initialised", bankroll=str(self.account.cash))
        self.engine.d.milestones.update(self.account.equity, self.clock.now())

    async def wait(self) -> None:
        done, _ = await asyncio.wait(self._tasks, return_when=asyncio.FIRST_EXCEPTION)
        for t in done:
            if t.exception() is not None and not isinstance(t.exception(), asyncio.CancelledError):
                log.error("task_crashed", task=t.get_name(), error=str(t.exception()))

    async def stop(self, reason: str = "shutdown") -> None:
        """Graceful shutdown: stop tasks, drain the bus, flush storage, persist portfolio state."""
        self.engine.stop()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await self.bus.stop()
        with contextlib.suppress(Exception):
            snap = self.account.snapshot(self.clock.now())
            self.repo.save_portfolio_snapshot(snap)
            await self.repo.save_account_state_now(
                cash=self.account.cash,
                peak_equity=self.account.peak_equity,
                realized_pnl=self.account.realized_pnl,
                fees=self.account.fees_total,
                slippage=self.account.slippage_total,
                wins=self.account.wins,
                losses=self.account.losses,
                recent_results=self.account.recent_performance().results,
                milestones_reached=sorted(self.engine.d.milestones.reached),
            )
            for p in self.account.open_positions:
                await self.repo.save_position_now(p)
            await self.repo.end_session()
        await self.repo.close()
        await self.http.aclose()
        if self.health is not None:
            self.health.write_final(reason)

    def install_signal_handlers(self, on_stop: Callable[[], None]) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, on_stop)


def build_runtime(
    settings: Settings,
    *,
    mode: RunMode,
    session_id: str | None = None,
    clock: Clock | None = None,
    synthetic_seed: int = 7,
    quiet_alerts: bool = False,
    http_transport: httpx.AsyncBaseTransport | None = None,
    ws_connect: Callable[[str], Awaitable[ClientConnection]] | None = None,
) -> Runtime:
    """Build all components. `http_transport`/`ws_connect` let tests mock the live providers."""
    clock = clock or SystemClock()
    sid = session_id or new_session_id(mode)
    metrics = Metrics()
    http = HttpClient(
        timeout_s=settings.providers.http_timeout_s,
        max_connections=settings.providers.http_max_connections,
        metrics=metrics,
        transport=http_transport,
    )
    bus = EventBus()
    repo = Repository(
        settings.storage.database_url,
        session_id=sid,
        batch_size=settings.storage.write_batch_size,
        flush_interval_s=settings.storage.write_flush_interval_s,
        metrics=metrics,
    )
    account = PortfolioAccount(clock, session_id=sid, streak_window=settings.risk.streak_window)
    tracker = TokenTracker(settings.discovery.max_tracked_tokens)
    synthetic = settings.is_synthetic or settings.quotes.source == "synthetic"

    # ---- FX
    fx: FxProvider
    if synthetic:
        fx = StaticFx(settings.portfolio.sol_eur_fallback)
    else:
        fx = CoinGeckoFx(
            http,
            settings.providers.coingecko_base_url,
            fallback_sol_eur=settings.portfolio.sol_eur_fallback,
            refresh_s=settings.portfolio.fx_refresh_s,
        )

    engine_holder: dict[str, Engine] = {}

    def priority() -> list[str]:
        eng = engine_holder.get("engine")
        return eng.priority_mints() if eng else []

    async def emit_snapshot(snap: MarketSnapshot) -> None:
        eng = engine_holder.get("engine")
        if eng is not None:
            await eng.on_snapshot(snap)

    async def emit_trade(trade: TradeEvent) -> None:
        eng = engine_holder.get("engine")
        if eng is not None:
            await eng.on_trade(trade)

    async def emit_token(token: TokenInfo) -> None:
        eng = engine_holder.get("engine")
        if eng is not None:
            await eng.on_token(token)

    market = MarketDataService(
        settings.market_data,
        clock,
        metrics,
        emit_snapshot=emit_snapshot,
        emit_trade=emit_trade,
        priority_mints=priority,
    )
    discovery = DiscoveryService(settings.discovery, clock, metrics, emit_token)
    background: list[tuple[str, Task]] = []
    probes: list[ConnectionProbe] = []

    # ---- providers
    world: SyntheticWorld | None = None
    quote_provider: QuoteProvider
    metadata: TokenMetadataProvider | None
    liquidity: LiquidityProvider | None
    if synthetic:
        world = SyntheticWorld(
            clock, seed=synthetic_seed, sol_usd=fx.sol_usd_cached() or Decimal(160)
        )
        syn_disc = SyntheticDiscovery(world)
        syn_md = SyntheticMarketData(world)
        discovery.add_streaming(syn_disc)
        market.add_streaming(syn_md)
        ticker = SyntheticTicker(
            world, clock, syn_disc, syn_md, tick_s=settings.market_data.poll_interval_s
        )
        background.append(("synthetic-ticker", ticker.run))
        quote_provider = SyntheticQuoteProvider(world, clock)
        token_provider = SyntheticTokenProvider(world, clock)
        metadata, liquidity = token_provider, token_provider
    else:
        pump: PumpPortalClient | None = None
        if (
            "pumpportal" in settings.discovery.sources
            or "pumpportal" in settings.market_data.sources
        ):
            pump = PumpPortalClient(
                settings.providers.pumpportal_ws_url,
                clock,
                metrics=metrics,
                min_backoff_s=settings.providers.ws_reconnect_min_s,
                max_backoff_s=settings.providers.ws_reconnect_max_s,
                ws_connect=ws_connect,
            )
            background.append(("pumpportal-ws", pump.run))
            probes.append(pump)
        dex_md = DexScreenerMarketData(
            http, settings.providers.dexscreener_base_url, clock, settings.market_data.batch_size
        )
        for source in settings.discovery.sources:
            if source == "pumpportal" and pump is not None:
                discovery.add_streaming(PumpPortalDiscovery(pump))
            elif source == "geckoterminal":
                discovery.add_polling(
                    GeckoTerminalDiscovery(
                        http,
                        settings.providers.geckoterminal_base_url,
                        clock,
                        poll_interval_s=settings.discovery.poll_interval_s,
                        max_token_age_s=settings.discovery.max_token_age_s,
                    )
                )
            elif source == "dexscreener":
                discovery.add_polling(
                    DexScreenerDiscovery(
                        http,
                        settings.providers.dexscreener_base_url,
                        clock,
                        dex_md,
                        poll_interval_s=max(10.0, settings.discovery.poll_interval_s),
                        max_token_age_s=settings.discovery.max_token_age_s,
                    )
                )
        for source in settings.market_data.sources:
            if source == "dexscreener":
                market.add_polling(dex_md)
            elif source == "pumpportal" and pump is not None:
                market.add_streaming(PumpPortalMarketData(pump, fx))
        key = settings.providers.jupiter_api_key
        quote_provider = JupiterQuoteProvider(
            http,
            clock,
            base_url=settings.providers.jupiter_base_url,
            pro_base_url=settings.providers.jupiter_pro_base_url,
            api_key=key.get_secret_value() if key else None,
            timeout_s=settings.quotes.quote_timeout_s,
            priority_fee_lamports=settings.quotes.priority_fee_lamports,
            metrics=metrics,
        )
        helius = settings.providers.helius_api_key
        rpc = SolanaRpcTokenProvider(
            http,
            settings.providers.solana_rpc_url,
            clock,
            helius_api_key=helius.get_secret_value() if helius else None,
        )
        metadata, liquidity = rpc, rpc

    # ---- execution
    execution: ExecutionInterface
    if mode is RunMode.LIVE:
        execution = ManualExecution(clock, settings.quotes)
    else:
        execution = DryRunExecution(
            clock,
            settings.quotes,
            confirm_delay_s=settings.dry_run.confirm_delay_s,
            auto_confirm_buys=settings.dry_run.auto_confirm_buys,
            auto_confirm_sells=settings.dry_run.auto_confirm_sells,
        )
    preparer = TransactionPreparer(
        quote_provider,
        clock,
        wallet_public_key=settings.providers.wallet_public_key,
        enabled=settings.quotes.prepare_unsigned_transaction,
        simulated=mode is not RunMode.LIVE,
        session_id=sid,
    )

    # ---- strategy components
    checker = TokenChecker(settings.filters)
    deps = EngineDeps(
        clock=clock,
        bus=bus,
        repo=repo,
        account=account,
        risk=RiskEngine(settings.risk),
        milestones=MilestoneTracker(
            settings.risk.milestones_eur, sid, settings.risk.milestone_hysteresis_pct
        ),
        features=FeatureEngine(settings.market_data.stale_after_s),
        checker=checker,
        scorer=EntryScorer(settings.entry, settings.filters),
        gate=EntryGate(settings.entry, checker),
        monitor=PositionMonitor(settings.exit, account),
        round_trip=RoundTripEvaluator(quote_provider, settings.quotes, clock, metrics),
        execution=execution,
        preparer=preparer,
        fx=fx,
        metadata=metadata,
        liquidity=liquidity,
        market=market,
        tracker=tracker,
        metrics=metrics,
    )
    engine = Engine(settings, deps, mode=mode, session_id=sid)
    engine_holder["engine"] = engine

    # ---- subscribers
    persistence = PersistenceSubscriber(
        repo, store_features=mode is not RunMode.REPLAY, store_market=mode is not RunMode.REPLAY
    )
    bus.subscribe("storage", persistence.handle)
    alerts = AlertService(Urgency(settings.alerts.min_urgency_for_push))
    terminal = TerminalAlertProvider()
    if settings.alerts.terminal and not quiet_alerts:
        alerts.add_terminal(terminal)
    if settings.alerts.discord_webhook_url:
        alerts.add_push(
            DiscordAlertProvider(http, settings.alerts.discord_webhook_url.get_secret_value())
        )
    if settings.alerts.telegram_bot_token and settings.alerts.telegram_chat_id:
        alerts.add_push(
            TelegramAlertProvider(
                http,
                settings.alerts.telegram_bot_token.get_secret_value(),
                settings.alerts.telegram_chat_id,
            )
        )
    bus.subscribe("alerts", alerts.handle)

    background.append(("discovery", discovery.run))
    background.append(("market-data", market.run))
    runtime = Runtime(
        settings=settings,
        mode=mode,
        session_id=sid,
        clock=clock,
        engine=engine,
        bus=bus,
        repo=repo,
        metrics=metrics,
        account=account,
        discovery=discovery,
        market=market,
        alerts=alerts,
        terminal_alerts=terminal,
        http=http,
        background=background,
        synthetic_world=world,
        quote_provider=quote_provider,
    )
    if mode is not RunMode.REPLAY:
        health = HealthReporter(
            runtime,
            state_path(settings.home, STATUS_FILE),
            stale_after_s=settings.market_data.stale_after_s,
        )
        for probe in probes:
            health.add_probe(probe)
        health.add_probe(market)
        health.add_probe(discovery)
        runtime.health = health
    return runtime
