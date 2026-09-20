"""Autonomous execution: the bot quotes, builds, signs and broadcasts swaps from its own hot
wallet, waits for confirmation and books the fill from what actually happened on chain.

Together with the `wallet` package this is the only module that signs or sends. It reuses the
manual adapter's order book (`submit_*`, `pending`, `find`, `expire_stale`) so the engine drives
it exactly like the dry-run adapter, through `auto_confirm(now)`.

Per order, one asyncio task, serialised by a lock (one send in flight at a time):

1. safety rails on live numbers (arming state, KILL, wallet balance, open positions, daily spend,
   total loss) → ABANDONED when blocked; the loss limit also disarms
2. a fresh quote (buy: for the clamped spend; sell: for the wallet's actual token balance), with
   a drift check against the signal's quote
3. the swap is built and signed; the signature is recorded (BUILT) before anything is sent
4. one `sendTransaction`; an uncertain transport failure is treated as sent, never re-sent
5. polling until confirmed / failed / blockhash expired
6. the confirmed transaction's balance deltas become the fill (VERIFIED_ONCHAIN)

Every step is written to the `execution_intents` table first, so `recover()` at start can ask
the chain about anything that was in flight when the process died.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from solana_sniper.app import arming
from solana_sniper.app.bus import EventBus
from solana_sniper.config.settings import AutonomyConfig, QuotesConfig
from solana_sniper.domain.clock import Clock
from solana_sniper.domain.enums import (
    DecisionKind,
    DecisionSource,
    ExitReason,
    FillProvenance,
    IntentStatus,
    SignalKind,
)
from solana_sniper.domain.events import (
    AutonomyDisarmed,
    TransactionConfirmed,
    TransactionFailed,
    TransactionSent,
)
from solana_sniper.domain.models import ExecutionIntent, Fill, SwapQuote, new_id
from solana_sniper.domain.money import (
    ZERO,
    check_decimals,
    lamports_to_sol,
    q_eur,
    raw_to_ui,
    sol_to_lamports,
)
from solana_sniper.execution.base import FillOverride, PendingOrder, Resolution
from solana_sniper.execution.manual import ManualExecution, OrderNotPendingError
from solana_sniper.portfolio.accounting import (
    InsufficientCashError,
    PortfolioAccount,
    PositionAlreadyClosedError,
)
from solana_sniper.portfolio.fx import FxProvider
from solana_sniper.quotes.base import QuoteError, QuoteProvider
from solana_sniper.storage.repository import Repository
from solana_sniper.telemetry.logging import get_logger
from solana_sniper.telemetry.metrics import Metrics
from solana_sniper.telemetry.redaction import safe_exception, scrub_text
from solana_sniper.wallet.keys import HotWallet, WalletError
from solana_sniper.wallet.rails import Exposure, RailsConfig, SafetyRails
from solana_sniper.wallet.reconcile import OnChainSwap, ReconcileError, parse_confirmed_swap
from solana_sniper.wallet.rpc import SendError, SignatureStatus

log = get_logger(__name__)

WSOL = "So11111111111111111111111111111111111111112"
Sleep = Callable[[float], Awaitable[None]]


class SendRpc(Protocol):
    """The RPC surface the executor needs (satisfied by `wallet.rpc.SolanaSendClient`)."""

    async def get_balance(self, pubkey: str) -> int: ...

    async def get_token_balance(self, owner: str, mint: str) -> tuple[int, int] | None: ...

    async def get_block_height(self) -> int: ...

    async def submit(self, signed_transaction_b64: str) -> str: ...

    async def statuses(self, signatures: list[str]) -> list[SignatureStatus]: ...

    async def get_transaction(self, signature: str) -> dict[str, Any] | None: ...


def rails_config(cfg: AutonomyConfig) -> RailsConfig:
    return RailsConfig(
        max_trade_lamports=sol_to_lamports(cfg.max_trade_sol),
        max_daily_spend_lamports=sol_to_lamports(cfg.max_daily_spend_sol),
        max_total_loss_lamports=(
            sol_to_lamports(cfg.max_total_loss_sol) if cfg.max_total_loss_sol is not None else None
        ),
        max_open_positions=cfg.max_open_positions,
        reserve_lamports=sol_to_lamports(cfg.reserve_sol),
        max_slippage_bps=cfg.max_slippage_bps,
        max_price_impact_pct=cfg.max_price_impact_pct,
        max_priority_fee_lamports=cfg.max_priority_fee_lamports,
        exits_continue_when_disarmed=cfg.exits_continue_when_disarmed,
    )


@dataclass(slots=True)
class _Confirmed:
    intent: ExecutionIntent
    swap: OnChainSwap
    sol_eur: Decimal
    token_decimals: int


@dataclass(slots=True)
class _Outcome:
    order: PendingOrder
    kind: DecisionKind  # CONFIRM (fill from chain), REJECT (buy) or IGNORE (sell)
    note: str
    intent: ExecutionIntent
    confirmed: _Confirmed | None = None


def fill_from_swap(
    *,
    intent: ExecutionIntent,
    swap: OnChainSwap,
    sol_eur: Decimal,
    token_decimals: int,
    now: datetime,
) -> Fill:
    """The fill as the chain saw it. Buy: SOL principal excludes the fee (booked separately),
    tokens are the wallet's actual token delta. Sell: gross SOL proceeds before the fee."""
    decimals = check_decimals(token_decimals)
    if intent.side is SignalKind.BUY:
        principal = max(0, swap.sol_spent_lamports - swap.fee_lamports)
        tokens_raw = swap.token_delta_raw
        if tokens_raw <= 0:
            raise ValueError("confirmed buy delivered no tokens")
        sol = lamports_to_sol(principal)
        expected = intent.expected_out_raw
        shortfall = (
            q_eur(sol * sol_eur * Decimal(expected - tokens_raw) / Decimal(expected))
            if expected > tokens_raw > 0
            else ZERO
        )
    else:
        gross = swap.sol_received_lamports + swap.fee_lamports
        tokens_raw = -swap.token_delta_raw
        if tokens_raw <= 0:
            raise ValueError("confirmed sell moved no tokens out of the wallet")
        sol = lamports_to_sol(gross)
        expected = intent.expected_out_raw
        shortfall = (
            q_eur(lamports_to_sol(expected - gross) * sol_eur) if expected > gross > 0 else ZERO
        )
    return Fill(
        fill_id=new_id("fill"),
        signal_id=intent.signal_id,
        mint=intent.mint,
        side=intent.side,
        filled_at=now,
        sol_amount=sol,
        token_amount_ui=raw_to_ui(tokens_raw, decimals),
        token_amount_raw=tokens_raw,
        token_decimals=decimals,
        eur_amount=q_eur(sol * sol_eur),
        sol_eur=sol_eur,
        fee_eur=q_eur(lamports_to_sol(swap.fee_lamports) * sol_eur),
        slippage_cost_eur=shortfall,
        provenance=FillProvenance.VERIFIED_ONCHAIN,
        simulated=False,
        verified_onchain=True,
        tx_signature=swap.signature,
        note=f"slot {swap.slot}",
    )


class AutonomousExecution(ManualExecution):
    """Live autonomous mode. Orders resolve only from confirmed transactions."""

    name = "autonomous"
    simulated = False

    def __init__(
        self,
        clock: Clock,
        quotes: QuotesConfig,
        *,
        wallet: HotWallet,
        rpc: SendRpc,
        quote_provider: QuoteProvider,
        autonomy: AutonomyConfig,
        repo: Repository,
        bus: EventBus,
        home: Path,
        session_id: str,
        account: PortfolioAccount,
        fx: FxProvider,
        metrics: Metrics | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        super().__init__(clock, quotes)
        self._wallet = wallet
        self._rpc = rpc
        self._provider = quote_provider
        self._cfg = autonomy
        self._rails = SafetyRails(rails_config(autonomy))
        self._repo = repo
        self._bus = bus
        self._home = home
        self._session_id = session_id
        self._account = account
        self._fx = fx
        self._metrics = metrics
        self._sleep = sleep
        self._send_lock = asyncio.Lock()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._launched: set[str] = set()
        self._finished: list[_Outcome] = []
        self._onchain: dict[str, _Confirmed] = {}
        self._intents: dict[str, ExecutionIntent] = {}  # order_id -> intent (for fill_id)
        self._spent: dict[date, int] = {}
        self.milestones_reached: Callable[[], list[Decimal]] = list
        self.sends = 0
        self.confirmed = 0
        self.failed = 0
        self.last_send_at: datetime | None = None
        self.last_confirmed_at: datetime | None = None
        # last exposure the rails saw (heartbeat / dashboard); None until the first order
        self.last_wallet_lamports: int | None = None
        self.last_loss_lamports: int | None = None
        self.last_exposure_at: datetime | None = None

    # ------------------------------------------------------------ properties
    @property
    def wallet_public_key(self) -> str:
        return self._wallet.pubkey

    @property
    def in_flight(self) -> int:
        return len(self._tasks)

    def spent_today_lamports(self) -> int:
        return self._spent.get(self._clock.now().date(), 0)

    def arming_state(self) -> arming.ArmingState:
        return arming.read(self._home)

    @property
    def armed_by_config(self) -> bool:
        """The configuration half of "armed": the markers are the other half."""
        return self._cfg.enabled and self._cfg.acknowledge_real_money

    def caps(self) -> dict[str, Any]:
        """The caps in force, for the heartbeat and the start banner (all public values)."""
        c = self._cfg
        return {
            "max_trade_sol": str(c.max_trade_sol),
            "max_daily_spend_sol": str(c.max_daily_spend_sol),
            "max_total_loss_sol": str(c.max_total_loss_sol) if c.max_total_loss_sol else None,
            "max_open_positions": c.max_open_positions,
            "reserve_sol": str(c.reserve_sol),
            "max_slippage_bps": c.max_slippage_bps,
            "max_price_impact_pct": c.max_price_impact_pct,
            "max_priority_fee_lamports": c.max_priority_fee_lamports,
            "confirm_timeout_s": c.confirm_timeout_s,
            "exits_continue_when_disarmed": c.exits_continue_when_disarmed,
        }

    # --------------------------------------------------------------- decide
    async def decide(
        self,
        order: PendingOrder,
        kind: DecisionKind,
        source: DecisionSource,
        *,
        override: FillOverride | None = None,
        note: str = "",
    ) -> Resolution:
        if kind is DecisionKind.CONFIRM and source is not DecisionSource.AUTONOMOUS:
            raise ValueError("autonomous mode: fills come from confirmed transactions, not `b N`")
        if source is not DecisionSource.AUTONOMOUS and order.order_id in self._tasks:
            raise ValueError(
                "this order is being executed on chain and cannot be cancelled; "
                "`solana-sniper kill` stops further sends"
            )
        if kind is DecisionKind.CONFIRM:
            # a confirmed transaction is a fact even when the signal's TTL elapsed meanwhile
            live = self._orders.get(order.order_id)
            if live is not None:
                live.expires_at = max(live.expires_at, self._clock.now() + timedelta(seconds=1))
        return await super().decide(order, kind, source, override=override, note=note)

    async def expire_stale(self, now: datetime) -> list[Resolution]:
        expired: list[Resolution] = []
        for order in list(self.pending()):
            if order.order_id in self._tasks:
                continue  # never expire an order whose transaction may be in flight
            if now > order.expires_at:
                expired.append(
                    await self.decide(
                        order, DecisionKind.EXPIRE, DecisionSource.SYSTEM, note="ttl elapsed"
                    )
                )
        return expired

    # ---------------------------------------------------------- auto confirm
    async def auto_confirm(self, now: datetime) -> list[Resolution]:
        """Launch execution for new orders; hand finished ones back as resolutions."""
        for order in self.pending():
            if order.order_id in self._launched:
                continue
            self._launched.add(order.order_id)
            self._tasks[order.order_id] = asyncio.create_task(
                self._run(order), name=f"autonomous-{order.kind.lower()}-{order.mint[:6]}"
            )
        out: list[Resolution] = []
        while self._finished:
            outcome = self._finished.pop(0)
            self._tasks.pop(outcome.order.order_id, None)
            if outcome.confirmed is not None:
                self._onchain[outcome.order.order_id] = outcome.confirmed
            try:
                res = await self.decide(
                    outcome.order,
                    outcome.kind,
                    DecisionSource.AUTONOMOUS,
                    note=outcome.note,
                )
            except OrderNotPendingError:
                self._onchain.pop(outcome.order.order_id, None)
                log.error(
                    "autonomous_order_vanished",
                    order=outcome.order.order_id,
                    intent=outcome.intent.intent_id,
                )
                continue
            if res.fill is not None:
                outcome.intent.fill_id = res.fill.fill_id
                await self._save(outcome.intent)
                self.confirmed += 1
                self.last_confirmed_at = self._clock.now()
                self._bus.publish(TransactionConfirmed(outcome.intent, res.fill))
            out.append(res)
        return out

    def _build_fill(
        self, order: PendingOrder, now: datetime, override: FillOverride | None
    ) -> Fill:
        data = self._onchain.pop(order.order_id, None)
        if data is None:
            raise ValueError("autonomous fills are built only from confirmed transactions")
        return fill_from_swap(
            intent=data.intent,
            swap=data.swap,
            sol_eur=data.sol_eur,
            token_decimals=data.token_decimals,
            now=now,
        )

    # -------------------------------------------------------------- execute
    async def _run(self, order: PendingOrder) -> None:
        try:
            outcome = await self._execute(order)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("autonomous_execution_error", order=order.order_id, error=safe_exception(exc))
            intent = self._intents.get(order.order_id) or self._new_intent(order)
            outcome = await self._abandon(
                order, intent, f"internal error: {safe_exception(exc)}", status=IntentStatus.FAILED
            )
        self._finished.append(outcome)

    def _new_intent(self, order: PendingOrder) -> ExecutionIntent:
        now = self._clock.now()
        intent = ExecutionIntent(
            intent_id=new_id("int"),
            session_id=self._session_id,
            signal_id=order.signal_id,
            order_id=order.order_id,
            mint=order.mint,
            symbol=order.symbol,
            side=order.kind,
            created_at=now,
            updated_at=now,
            wallet_public_key=self._wallet.pubkey,
            position_id=order.position_id,
            exit_reason=str(order.sell.reason) if order.sell is not None else None,
        )
        self._intents[order.order_id] = intent
        return intent

    async def _save(self, intent: ExecutionIntent) -> None:
        intent.updated_at = self._clock.now()
        await self._repo.save_intent_now(intent)

    async def _exposure(self) -> tuple[Exposure, arming.ArmingState]:
        state = arming.read(self._home)
        wallet_lamports = await self._rpc.get_balance(self._wallet.pubkey)
        sol_eur = self._fx.sol_eur()
        open_value_eur = sum((p.current_value_eur for p in self._account.open_positions), ZERO)
        open_value_lamports = sol_to_lamports(open_value_eur / sol_eur) if sol_eur > 0 else 0
        start = (
            state.start_balance_lamports
            if state.start_balance_lamports is not None
            else wallet_lamports
        )
        armed = state.armed and self._cfg.enabled and self._cfg.acknowledge_real_money
        exposure = Exposure(
            armed=armed,
            kill=state.kill,
            wallet_lamports=wallet_lamports,
            open_positions=len(self._account.open_positions),
            spent_today_lamports=self.spent_today_lamports(),
            loss_lamports=SafetyRails.loss_lamports(start, wallet_lamports, open_value_lamports),
        )
        self.last_wallet_lamports = wallet_lamports
        self.last_loss_lamports = exposure.loss_lamports
        self.last_exposure_at = self._clock.now()
        return exposure, state

    async def refresh_exposure(self) -> None:
        """Read the wallet balance for the heartbeat (the CLI banner and health use it)."""
        await self._exposure()

    def _failure_kind(self, order: PendingOrder) -> DecisionKind:
        return DecisionKind.REJECT if order.kind is SignalKind.BUY else DecisionKind.IGNORE

    async def _abandon(
        self,
        order: PendingOrder,
        intent: ExecutionIntent,
        reason: str,
        *,
        status: IntentStatus = IntentStatus.ABANDONED,
        disarm: bool = False,
    ) -> _Outcome:
        reason = scrub_text(reason)
        intent.status = status
        intent.error = reason[:300]
        await self._save(intent)
        self.failed += 1
        if self._metrics is not None:
            self._metrics.inc("autonomous_failed")
        if disarm:
            arming.disarm(self._home, reason, now=self._clock.now())
            self._bus.publish(AutonomyDisarmed(self._clock.now(), reason))
            if self._metrics is not None:
                self._metrics.inc("autonomy_disarmed")
            log.error("autonomy_disarmed", reason=reason)
        self._bus.publish(TransactionFailed(intent, reason))
        log.warning(
            "autonomous_order_not_executed",
            side=str(order.kind),
            mint=order.mint,
            status=str(status),
            reason=reason,
        )
        return _Outcome(order, self._failure_kind(order), reason, intent)

    async def _execute(self, order: PendingOrder) -> _Outcome:
        async with self._send_lock:
            intent = self._new_intent(order)
            try:
                exposure, _state = await self._exposure()
            except SendError as exc:
                return await self._abandon(order, intent, f"wallet balance unavailable: {exc}")
            if order.kind is SignalKind.BUY:
                return await self._execute_buy(order, intent, exposure)
            return await self._execute_sell(order, intent, exposure)

    async def _execute_buy(
        self, order: PendingOrder, intent: ExecutionIntent, exposure: Exposure
    ) -> _Outcome:
        sig = order.buy
        assert sig is not None
        priority = self._quotes.priority_fee_lamports
        spend = self._rails.clamp_spend(
            sol_to_lamports(sig.sizing.recommended_sol), exposure.wallet_lamports
        )
        pre = self._rails.check_buy(
            exposure,
            spend_lamports=spend,
            slippage_bps=sig.quote.buy.slippage_bps,
            price_impact_pct=sig.quote.entry_price_impact_pct,
            priority_fee_lamports=priority,
        )
        if not pre.ok:
            return await self._abandon(order, intent, pre.reason, disarm=pre.disarm)
        try:
            fresh = await self._provider.quote(WSOL, order.mint, spend, self._quotes.slippage_bps)
        except QuoteError as exc:
            return await self._abandon(order, intent, f"fresh quote failed: {exc}")
        drift = _drift_pct(sig.quote.buy, fresh)
        if drift > self._cfg.requote_max_worse_pct:
            return await self._abandon(
                order,
                intent,
                f"fresh quote is {drift:.2f}% worse than the signal's "
                f"(limit {self._cfg.requote_max_worse_pct:.2f}%)",
            )
        verdict = self._rails.check_buy(
            exposure,
            spend_lamports=spend,
            slippage_bps=fresh.slippage_bps,
            price_impact_pct=fresh.price_impact_pct,
            priority_fee_lamports=priority,
        )
        if not verdict.ok:
            return await self._abandon(order, intent, verdict.reason, disarm=verdict.disarm)
        _note_quote(intent, fresh, priority)
        intent.token_decimals = sig.token_decimals
        return await self._send_and_confirm(order, intent, fresh, sig.token_decimals)

    async def _execute_sell(
        self, order: PendingOrder, intent: ExecutionIntent, exposure: Exposure
    ) -> _Outcome:
        sig = order.sell
        assert sig is not None
        priority = self._quotes.priority_fee_lamports
        pre = self._rails.check_sell(
            exposure,
            slippage_bps=sig.exit_quote.slippage_bps if sig.exit_quote else 0,
            price_impact_pct=sig.exit_quote.price_impact_pct if sig.exit_quote else 0.0,
            priority_fee_lamports=priority,
        )
        if not pre.ok:
            return await self._abandon(order, intent, pre.reason)
        try:
            balance = await self._rpc.get_token_balance(self._wallet.pubkey, order.mint)
        except SendError as exc:
            return await self._abandon(order, intent, f"token balance unavailable: {exc}")
        if balance is None or balance[0] <= 0:
            return await self._abandon(
                order, intent, "the wallet holds no tokens of this mint; nothing to sell"
            )
        raw, decimals = balance
        try:
            fresh = await self._provider.quote(order.mint, WSOL, raw, self._quotes.slippage_bps)
        except QuoteError as exc:
            return await self._abandon(order, intent, f"fresh exit quote failed: {exc}")
        verdict = self._rails.check_sell(
            exposure,
            slippage_bps=fresh.slippage_bps,
            price_impact_pct=fresh.price_impact_pct,
            priority_fee_lamports=priority,
        )
        if not verdict.ok:
            return await self._abandon(order, intent, verdict.reason)
        _note_quote(intent, fresh, priority)
        intent.token_decimals = decimals
        return await self._send_and_confirm(order, intent, fresh, decimals)

    async def _send_and_confirm(
        self, order: PendingOrder, intent: ExecutionIntent, quote: SwapQuote, decimals: int
    ) -> _Outcome:
        intent.status = IntentStatus.PREPARED
        await self._save(intent)
        try:
            build = await self._provider.build_swap(quote, self._wallet.pubkey)
        except QuoteError as exc:
            return await self._abandon(order, intent, f"swap build failed: {exc}")
        if build.simulation_error:
            return await self._abandon(
                order,
                intent,
                f"swap simulation failed: {build.simulation_error}",
                status=IntentStatus.FAILED,
            )
        if (
            build.priority_fee_lamports is not None
            and build.priority_fee_lamports > self._cfg.max_priority_fee_lamports
        ):
            return await self._abandon(
                order,
                intent,
                f"builder priority fee {build.priority_fee_lamports} above cap "
                f"{self._cfg.max_priority_fee_lamports}",
            )
        try:
            signed = self._wallet.sign_swap(build.transaction_b64)
        except WalletError as exc:
            return await self._abandon(order, intent, f"signing refused: {exc}")
        intent.signature = signed.signature
        intent.last_valid_block_height = build.last_valid_block_height
        if build.priority_fee_lamports is not None:
            intent.priority_fee_lamports = build.priority_fee_lamports
        intent.status = IntentStatus.BUILT
        await self._save(intent)
        send_note = ""
        try:
            await self._rpc.submit(signed.transaction_b64)
        except SendError as exc:
            if not exc.maybe_sent:
                return await self._abandon(
                    order, intent, f"send rejected: {exc}", status=IntentStatus.FAILED
                )
            send_note = f"send uncertain ({exc}); tracking the signature"
            log.warning("autonomous_send_uncertain", tx=signed.signature, error=str(exc))
        intent.status = IntentStatus.SENT
        intent.sent_at = self._clock.now()
        intent.attempts += 1
        await self._save(intent)
        self.sends += 1
        self.last_send_at = intent.sent_at
        if order.kind is SignalKind.BUY:
            day = intent.sent_at.date()
            self._spent[day] = self._spent.get(day, 0) + intent.in_amount_raw
        if self._metrics is not None:
            self._metrics.inc("autonomous_sent")
        self._bus.publish(TransactionSent(intent))
        log.warning(
            "autonomous_transaction_sent",
            side=str(order.kind),
            mint=order.mint,
            tx=signed.signature,
            note=send_note,
        )
        status, swap, reason = await self._await_confirmation(intent)
        if status is IntentStatus.CONFIRMED and swap is not None:
            _note_swap(intent, swap)
            intent.status = IntentStatus.CONFIRMED
            intent.confirmed_at = self._clock.now()
            await self._save(intent)
            if self._metrics is not None:
                self._metrics.inc("autonomous_confirmed")
            return _Outcome(
                order,
                DecisionKind.CONFIRM,
                f"confirmed on chain {swap.signature}",
                intent,
                _Confirmed(intent, swap, self._fx.sol_eur(), decimals),
            )
        if status is IntentStatus.SENT:
            # unknown outcome: keep the durable record SENT so recovery can still book it
            intent.error = reason[:300]
            await self._save(intent)
            self.failed += 1
            self._bus.publish(TransactionFailed(intent, reason))
            return _Outcome(order, self._failure_kind(order), reason, intent)
        if swap is not None:
            _note_swap(intent, swap)
        return await self._abandon(order, intent, reason, status=status)

    async def _await_confirmation(
        self, intent: ExecutionIntent
    ) -> tuple[IntentStatus, OnChainSwap | None, str]:
        assert intent.signature is not None
        signature = intent.signature
        interval = self._cfg.status_poll_interval_s
        polls = max(1, math.ceil(self._cfg.confirm_timeout_s / interval))
        rpc_errors = 0
        for i in range(polls):
            await self._sleep(interval)
            try:
                status = (await self._rpc.statuses([signature]))[0]
            except SendError as exc:
                rpc_errors += 1
                log.debug("autonomous_status_poll_failed", error=str(exc))
                continue
            if status.failed:
                return IntentStatus.FAILED, None, f"transaction failed on chain: {status.err}"
            if status.confirmed:
                tx = await self._fetch_transaction(signature)
                if tx is None:
                    continue
                try:
                    swap = parse_confirmed_swap(tx, owner=self._wallet.pubkey, mint=intent.mint)
                except ReconcileError as exc:
                    return (
                        IntentStatus.FAILED,
                        None,
                        f"confirmed but unreadable ({exc}); reconcile signature {signature} "
                        "by hand",
                    )
                if swap.err is not None:
                    return IntentStatus.FAILED, swap, f"transaction errored on chain: {swap.err}"
                return IntentStatus.CONFIRMED, swap, ""
            if not status.found and i % 5 == 4 and intent.last_valid_block_height is not None:
                try:
                    height = await self._rpc.get_block_height()
                except SendError:
                    continue
                if height > intent.last_valid_block_height:
                    return (
                        IntentStatus.EXPIRED,
                        None,
                        f"blockhash expired (valid until height {intent.last_valid_block_height}, "
                        f"now {height}); the transaction cannot land",
                    )
        return (
            IntentStatus.SENT,
            None,
            f"no confirmation after {self._cfg.confirm_timeout_s:.0f}s "
            f"({rpc_errors} RPC errors); left SENT for reconciliation at the next start",
        )

    async def _fetch_transaction(self, signature: str) -> dict[str, Any] | None:
        for _ in range(3):
            try:
                tx = await self._rpc.get_transaction(signature)
            except SendError:
                tx = None
            if tx is not None:
                return tx
            await self._sleep(self._cfg.status_poll_interval_s)
        return None

    # ------------------------------------------------------------- recovery
    async def recover(self) -> list[str]:
        """Reconcile intents that were signed or sent before a crash; book landed fills."""
        lines: list[str] = []
        today = self._clock.now().date()
        for past in await self._repo.intents(statuses=("SENT", "CONFIRMED")):
            if past.side is SignalKind.BUY and past.sent_at and past.sent_at.date() == today:
                self._spent[today] = self._spent.get(today, 0) + past.in_amount_raw
        for intent in await self._repo.in_flight_intents():
            if intent.signature is None:
                intent.status = IntentStatus.ABANDONED
                intent.error = "built without a signature; never sent"
                await self._save(intent)
                continue
            try:
                status = (await self._rpc.statuses([intent.signature]))[0]
            except SendError as exc:
                lines.append(f"{intent.intent_id}: RPC unavailable ({exc}); left {intent.status}")
                continue
            if status.failed:
                intent.status = IntentStatus.FAILED
                intent.error = f"failed on chain: {status.err}"
                await self._save(intent)
                lines.append(f"{intent.intent_id}: {intent.error}")
                continue
            if status.confirmed:
                lines.append(await self._book_recovered(intent))
                continue
            try:
                height = await self._rpc.get_block_height()
            except SendError:
                lines.append(f"{intent.intent_id}: still unconfirmed; left SENT")
                continue
            if (
                intent.last_valid_block_height is not None
                and height > intent.last_valid_block_height
            ):
                intent.status = IntentStatus.EXPIRED
                intent.error = "blockhash expired before confirmation"
                await self._save(intent)
                lines.append(f"{intent.intent_id}: expired, never landed")
            else:
                lines.append(f"{intent.intent_id}: still pending on chain; left SENT")
        return lines

    async def _book_recovered(self, intent: ExecutionIntent) -> str:
        assert intent.signature is not None
        tx = await self._fetch_transaction(intent.signature)
        if tx is None:
            return f"{intent.intent_id}: confirmed but the transaction is not readable yet"
        try:
            swap = parse_confirmed_swap(tx, owner=self._wallet.pubkey, mint=intent.mint)
        except ReconcileError as exc:
            intent.status = IntentStatus.FAILED
            intent.error = f"confirmed but unreadable: {exc}"
            await self._save(intent)
            return f"{intent.intent_id}: {intent.error}"
        _note_swap(intent, swap)
        if swap.err is not None:
            intent.status = IntentStatus.FAILED
            intent.error = f"errored on chain: {swap.err}"
            await self._save(intent)
            return f"{intent.intent_id}: {intent.error}"
        now = self._clock.now()
        sol_eur = self._fx.sol_eur()
        decimals = intent.token_decimals or swap.token_decimals or 0
        try:
            fill = fill_from_swap(
                intent=intent, swap=swap, sol_eur=sol_eur, token_decimals=decimals, now=now
            )
        except ValueError as exc:
            intent.status = IntentStatus.CONFIRMED
            intent.error = f"landed but not bookable: {exc}"
            await self._save(intent)
            return f"{intent.intent_id}: {intent.error}"
        acct = self._account
        try:
            if intent.side is SignalKind.BUY:
                if any(p.mint == intent.mint for p in acct.open_positions):
                    intent.status = IntentStatus.CONFIRMED
                    intent.error = "landed; a position for this mint is already open"
                    await self._save(intent)
                    return f"{intent.intent_id}: {intent.error}"
                entry_price = (
                    fill.sol_amount / fill.token_amount_ui if fill.token_amount_ui > 0 else ZERO
                )
                position = acct.open_position(
                    fill,
                    symbol=intent.symbol,
                    entry_price_native=entry_price,
                    entry_signal_id=intent.signal_id,
                )
            else:
                if not intent.position_id:
                    raise ValueError("sell intent without a position id")
                reason = ExitReason(intent.exit_reason or "MANUAL")
                position = acct.close_position(intent.position_id, fill, reason)
        except (InsufficientCashError, PositionAlreadyClosedError, KeyError, ValueError) as exc:
            intent.status = IntentStatus.CONFIRMED
            intent.error = f"landed but not booked: {safe_exception(exc)}"
            await self._save(intent)
            return f"{intent.intent_id}: {intent.error}"
        await self._repo.save_fill_now(fill)
        await self._repo.save_position_now(position)
        await self._repo.save_ledger_now(acct.ledger[-1:])
        await self._repo.save_account_state_now(
            cash=acct.cash,
            peak_equity=acct.peak_equity,
            realized_pnl=acct.realized_pnl,
            fees=acct.fees_total,
            slippage=acct.slippage_total,
            wins=acct.wins,
            losses=acct.losses,
            recent_results=acct.recent_performance().results,
            milestones_reached=self.milestones_reached(),
        )
        intent.status = IntentStatus.CONFIRMED
        intent.fill_id = fill.fill_id
        intent.confirmed_at = now
        intent.error = None
        await self._save(intent)
        self.confirmed += 1
        self._bus.publish(TransactionConfirmed(intent, fill))
        return (
            f"{intent.intent_id}: recovered {intent.side} of {intent.symbol or intent.mint[:8]} "
            f"from signature {swap.signature}"
        )


def _drift_pct(signal_quote: SwapQuote, fresh: SwapQuote) -> float:
    """How much worse (positive %) the fresh output per input unit is than the signal's."""
    if signal_quote.in_amount_raw <= 0 or fresh.in_amount_raw <= 0:
        return 0.0
    expected = signal_quote.out_amount_raw / signal_quote.in_amount_raw
    actual = fresh.out_amount_raw / fresh.in_amount_raw
    if expected <= 0:
        return 0.0
    return max(0.0, (expected - actual) / expected * 100.0)


def _note_quote(intent: ExecutionIntent, quote: SwapQuote, priority_fee_lamports: int) -> None:
    intent.quote_id = quote.quote_id
    intent.in_amount_raw = quote.in_amount_raw
    intent.expected_out_raw = quote.out_amount_raw
    intent.min_out_raw = quote.other_amount_threshold_raw
    intent.slippage_bps = quote.slippage_bps
    intent.price_impact_pct = quote.price_impact_pct
    intent.priority_fee_lamports = priority_fee_lamports


def _note_swap(intent: ExecutionIntent, swap: OnChainSwap) -> None:
    intent.slot = swap.slot
    intent.sol_delta_lamports = swap.sol_delta_lamports
    intent.token_delta_raw = swap.token_delta_raw
    intent.fee_lamports = swap.fee_lamports
    if swap.token_decimals is not None:
        intent.token_decimals = swap.token_decimals
