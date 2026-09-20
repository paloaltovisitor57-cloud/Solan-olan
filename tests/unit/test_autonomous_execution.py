"""The autonomous executor against fakes for Jupiter and the RPC: fills only from the chain,
exactly one send per intent, every failure path safe, rails and recovery honoured."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from solders.hash import Hash
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from solana_sniper.app import arming
from solana_sniper.app.bus import EventBus
from solana_sniper.config.settings import AutonomyConfig, QuotesConfig
from solana_sniper.domain.clock import ManualClock
from solana_sniper.domain.enums import (
    DecisionKind,
    DecisionSource,
    FillProvenance,
    IntentStatus,
    SignalKind,
)
from solana_sniper.domain.events import (
    AutonomyDisarmed,
    Event,
    TransactionConfirmed,
    TransactionFailed,
    TransactionSent,
)
from solana_sniper.domain.models import ExecutionIntent, SwapQuote, new_id
from solana_sniper.execution.autonomous import WSOL, AutonomousExecution
from solana_sniper.execution.manual import OrderNotPendingError
from solana_sniper.portfolio.accounting import PortfolioAccount
from solana_sniper.portfolio.fx import StaticFx
from solana_sniper.quotes.base import QuoteError, SwapBuild
from solana_sniper.storage.repository import Repository
from solana_sniper.telemetry.redaction import registry
from solana_sniper.wallet.keys import HotWallet, generate
from solana_sniper.wallet.rpc import SendError, SignatureStatus
from tests.unit.test_execution import make_buy_signal, make_sell_signal

SOL = 1_000_000_000
MINT = "MintTest"
FEE = 205_000
TOKENS = 500_000_000  # raw tokens (6 decimals) the fake chain moves for a 0.05 SOL swap


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    registry.clear()
    yield
    registry.clear()


def unsigned_for(pubkey: str) -> str:
    payer = Pubkey.from_string(pubkey)
    ix = transfer(TransferParams(from_pubkey=payer, to_pubkey=Pubkey.default(), lamports=1))
    msg = MessageV0.try_compile(payer, [ix], [], Hash.default())
    tx = VersionedTransaction.populate(msg, [Signature.default()])
    return base64.b64encode(bytes(tx)).decode()


class FakeProvider:
    name = "fake"

    def __init__(self, pubkey: str, clock: ManualClock) -> None:
        self.pubkey = pubkey
        self.clock = clock
        # raw tokens per lamport (buy) / lamports per raw token (sell). Ten matches the
        # signal's round-trip quote (0.1 SOL -> 1e9 raw), so a fresh quote shows no drift, and
        # 0.05 SOL -> 500_000_000 raw is exactly what the fake chain delivers (zero slippage cost)
        self.rate = Decimal("10")
        self.worse_pct = 0.0
        self.impact_pct = 1.0
        self.slippage_bps = 300
        self.build_error: str | None = None
        self.simulation_error: str | None = None
        self.lvbh = 1000
        self.quotes: list[tuple[str, str, int]] = []
        self.builds = 0

    async def quote(
        self, input_mint: str, output_mint: str, amount_raw: int, slippage_bps: int
    ) -> SwapQuote:
        self.quotes.append((input_mint, output_mint, amount_raw))
        factor = Decimal(1) - Decimal(str(self.worse_pct)) / 100
        out = (
            int(Decimal(amount_raw) * self.rate * factor)
            if output_mint != WSOL
            else int(Decimal(amount_raw) / self.rate * factor)
        )
        return SwapQuote(
            quote_id=new_id("q"),
            provider="fake",
            input_mint=input_mint,
            output_mint=output_mint,
            in_amount_raw=amount_raw,
            out_amount_raw=out,
            other_amount_threshold_raw=int(out * (1 - slippage_bps / 10_000)),
            slippage_bps=slippage_bps,
            price_impact_pct=self.impact_pct,
            route_labels=("Fake",),
            fee_lamports=5000,
            quoted_at=self.clock.now(),
            latency_ms=1.0,
            raw={"fake": True},
        )

    async def build_swap(self, quote: SwapQuote, user_public_key: str) -> SwapBuild:
        self.builds += 1
        if self.build_error:
            raise QuoteError(self.build_error)
        assert user_public_key == self.pubkey
        return SwapBuild(
            transaction_b64=unsigned_for(user_public_key),
            last_valid_block_height=self.lvbh,
            priority_fee_lamports=200_000,
            compute_unit_limit=200_000,
            simulation_error=self.simulation_error,
        )

    async def prepare_unsigned_swap(self, quote: SwapQuote, user_public_key: str) -> str | None:
        return (await self.build_swap(quote, user_public_key)).transaction_b64


@dataclass
class FakeRpc:
    pubkey: str
    balance: int = SOL
    token_balance: tuple[int, int] | None = None
    height: int = 100
    submit_mode: str = "ok"  # ok | reject | transport
    status_script: list[str] = field(default_factory=lambda: ["unknown", "confirmed"])
    tx_available_after: int = 0  # extra polls before getTransaction returns the tx
    actual_spend: int = SOL // 20  # lamports leaving the wallet on a buy, fee excluded
    actual_tokens: int = TOKENS  # raw tokens moved
    actual_proceeds: int = SOL // 25  # lamports arriving on a sell, fee excluded
    onchain_err: Any = None
    sends: list[str] = field(default_factory=list)
    status_calls: int = 0
    tx_calls: int = 0
    balance_calls: int = 0

    async def get_balance(self, pubkey: str) -> int:
        self.balance_calls += 1
        return self.balance

    async def get_token_balance(self, owner: str, mint: str) -> tuple[int, int] | None:
        return self.token_balance

    async def get_block_height(self) -> int:
        return self.height

    async def submit(self, signed_transaction_b64: str) -> str:
        if self.submit_mode == "reject":
            raise SendError(
                "sendTransaction: rpc error -32002: simulation failed", maybe_sent=False
            )
        self.sends.append(signed_transaction_b64)
        if self.submit_mode == "transport":
            self.submit_mode = "ok"
            raise SendError("sendTransaction: ConnectError", maybe_sent=True)
        tx = VersionedTransaction.from_bytes(base64.b64decode(signed_transaction_b64))
        return str(tx.signatures[0])

    async def statuses(self, signatures: list[str]) -> list[SignatureStatus]:
        self.status_calls += 1
        step = self.status_script[min(self.status_calls - 1, len(self.status_script) - 1)]
        sig = signatures[0]
        if step == "unknown":
            return [SignatureStatus(sig, False, None, None, None, None)]
        if step == "failed":
            return [SignatureStatus(sig, True, 5, 1, "InstructionError", "confirmed")]
        if step == "rpc-error":
            raise SendError("getSignatureStatuses: 503", retryable=True)
        return [SignatureStatus(sig, True, 5, 3, None, "confirmed")]

    async def get_transaction(self, signature: str) -> dict[str, Any] | None:
        self.tx_calls += 1
        if self.tx_calls <= self.tx_available_after:
            return None
        return self.transaction(signature)

    def transaction(self, signature: str) -> dict[str, Any]:
        # a wallet that holds tokens is selling them; otherwise it is buying
        sold = bool(self.token_balance and self.token_balance[0] > 0)
        pre_l = 900_000_000
        if sold:
            post_l = pre_l + self.actual_proceeds - FEE
            pre_t, post_t = self.actual_tokens, 0
        else:
            post_l = pre_l - self.actual_spend - FEE
            pre_t, post_t = 0, self.actual_tokens
        entry = lambda amount: {  # noqa: E731
            "accountIndex": 1,
            "mint": MINT,
            "owner": self.pubkey,
            "uiTokenAmount": {"amount": str(amount), "decimals": 6},
        }
        return {
            "slot": 777,
            "blockTime": 1758200000,
            "meta": {
                "err": self.onchain_err,
                "fee": FEE,
                "preBalances": [pre_l, 2039280],
                "postBalances": [post_l, 2039280],
                "preTokenBalances": [entry(pre_t)] if pre_t else [],
                "postTokenBalances": [entry(post_t)] if post_t else [],
            },
            "transaction": {
                "signatures": [signature],
                "message": {"accountKeys": [{"pubkey": self.pubkey}, {"pubkey": "Ata"}]},
            },
        }


class Captured:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def handle(self, event: Event) -> None:
        self.events.append(event)

    def of(self, kind: type[Any]) -> list[Any]:
        return [e for e in self.events if isinstance(e, kind)]


@dataclass
class World:
    clock: ManualClock
    home: Path
    wallet: HotWallet
    provider: FakeProvider
    rpc: FakeRpc
    repo: Repository
    bus: EventBus
    account: PortfolioAccount
    captured: Captured
    ex: AutonomousExecution


async def fast_sleep(_: float) -> None:
    await asyncio.sleep(0.001)  # real time: the repository commits on an aiosqlite thread


async def make_world(
    tmp_path: Path, clock: ManualClock, *, cfg: AutonomyConfig | None = None, arm: bool = True
) -> World:
    home = tmp_path / "home"
    wallet = generate(home / "wallet" / "hot-wallet.json")
    provider = FakeProvider(wallet.pubkey, clock)
    rpc = FakeRpc(wallet.pubkey)
    repo = Repository("sqlite+aiosqlite:///:memory:", session_id="auto-test")
    await repo.init()
    repo.start()
    bus = EventBus()
    captured = Captured()
    bus.subscribe("test", captured.handle)
    bus.start()
    account = PortfolioAccount(clock, session_id="auto-test")
    account.deposit(Decimal("150"), "bankroll")
    cfg = cfg or AutonomyConfig(
        enabled=True, acknowledge_real_money=True, max_total_loss_sol=Decimal("0.25")
    )
    if arm:
        arming.arm(
            home,
            wallet_public_key=wallet.pubkey,
            start_balance_lamports=rpc.balance,
            max_total_loss_sol=cfg.max_total_loss_sol or Decimal("0.25"),
            caps={},
            now=clock.now(),
        )
    ex = AutonomousExecution(
        clock,
        QuotesConfig(),
        wallet=wallet,
        rpc=rpc,
        quote_provider=provider,
        autonomy=cfg,
        repo=repo,
        bus=bus,
        home=home,
        session_id="auto-test",
        account=account,
        fx=StaticFx(Decimal("150")),
        sleep=fast_sleep,
    )
    return World(clock, home, wallet, provider, rpc, repo, bus, account, captured, ex)


async def settle(world: World, *, ticks: int = 3000) -> list[Any]:
    """Drive auto_confirm like the engine does until the order resolves."""
    for _ in range(ticks):
        world.clock.advance(0.25)
        res = await world.ex.auto_confirm(world.clock.now())
        if res:
            await world.bus.drain(1.0)
            return res
        await asyncio.sleep(0.002)
    raise AssertionError("order did not resolve")


async def intents(world: World) -> list[ExecutionIntent]:
    return await world.repo.intents()


async def teardown(world: World) -> None:
    await world.bus.stop()
    await world.repo.close()


async def test_buy_is_booked_from_the_chain_not_the_quote(
    tmp_path: Path, clock: ManualClock
) -> None:
    w = await make_world(tmp_path, clock)
    order = await w.ex.submit_buy(make_buy_signal(clock))
    (res,) = await settle(w)
    assert res.decision.kind is DecisionKind.CONFIRM
    assert res.decision.source is DecisionSource.AUTONOMOUS
    fill = res.fill
    assert fill is not None and fill.provenance is FillProvenance.VERIFIED_ONCHAIN
    assert fill.verified_onchain and fill.is_verified and not fill.simulated
    assert fill.tx_signature and len(w.rpc.sends) == 1
    # amounts come from the confirmed transaction: 0.05 SOL principal, fee separate, actual tokens
    assert fill.sol_amount == Decimal("0.05")
    assert fill.fee_eur == Decimal("0.03075")  # 205000 lamports at €150/SOL
    assert fill.token_amount_raw == TOKENS and fill.token_amount_ui == Decimal("500")
    assert fill.slippage_cost_eur == Decimal("0")  # actual tokens equal the fresh quote
    (intent,) = await intents(w)
    assert intent.status is IntentStatus.CONFIRMED and intent.fill_id == fill.fill_id
    assert intent.signature == fill.tx_signature and intent.slot == 777
    assert intent.in_amount_raw == SOL // 20 and intent.sol_delta_lamports == -(SOL // 20 + FEE)
    # the fresh quote was for the clamped spend (0.1 SOL recommended, 0.05 SOL cap)
    assert w.provider.quotes == [(WSOL, MINT, SOL // 20)]
    assert w.ex.spent_today_lamports() == SOL // 20
    assert [type(e) for e in w.captured.events] == [TransactionSent, TransactionConfirmed]
    assert w.ex.find(SignalKind.BUY, order.ref) is None
    await teardown(w)


async def test_sell_uses_the_wallet_token_balance_and_books_proceeds(
    tmp_path: Path, clock: ManualClock
) -> None:
    w = await make_world(tmp_path, clock)
    w.rpc.token_balance = (TOKENS, 6)
    await w.ex.submit_sell(make_sell_signal(clock))
    (res,) = await settle(w)
    fill = res.fill
    assert fill is not None and fill.side is SignalKind.SELL and fill.verified_onchain
    assert fill.token_amount_raw == TOKENS
    assert fill.sol_amount == Decimal("0.04")  # gross proceeds; the fee is booked separately
    assert fill.fee_eur == Decimal("0.03075")
    assert w.provider.quotes == [(MINT, WSOL, TOKENS)]
    (intent,) = await intents(w)
    assert intent.exit_reason == "TRAILING_PEAK" and intent.position_id == "pos_1"
    await teardown(w)


async def test_quote_drift_abandons_before_anything_is_sent(
    tmp_path: Path, clock: ManualClock
) -> None:
    w = await make_world(tmp_path, clock)
    sig = make_buy_signal(clock)
    # the signal's quote promised more tokens per lamport than the fake will now give
    w.provider.rate = Decimal(sig.quote.buy.out_amount_raw) / Decimal(sig.quote.buy.in_amount_raw)
    w.provider.worse_pct = 5.0
    await w.ex.submit_buy(sig)
    (res,) = await settle(w)
    assert res.decision.kind is DecisionKind.REJECT and res.fill is None
    assert "worse than the signal" in res.decision.note
    assert w.rpc.sends == [] and w.provider.builds == 0
    (intent,) = await intents(w)
    assert intent.status is IntentStatus.ABANDONED and intent.signature is None
    assert [type(e) for e in w.captured.events] == [TransactionFailed]
    await teardown(w)


async def test_rejected_send_is_failed_with_exactly_one_attempt(
    tmp_path: Path, clock: ManualClock
) -> None:
    w = await make_world(tmp_path, clock)
    w.rpc.submit_mode = "reject"
    await w.ex.submit_buy(make_buy_signal(clock))
    (res,) = await settle(w)
    assert res.decision.kind is DecisionKind.REJECT and "send rejected" in res.decision.note
    (intent,) = await intents(w)
    assert intent.status is IntentStatus.FAILED and intent.signature is not None
    assert w.rpc.sends == [] and w.rpc.status_calls == 0
    await teardown(w)


async def test_uncertain_transport_failure_is_polled_never_resent(
    tmp_path: Path, clock: ManualClock
) -> None:
    w = await make_world(tmp_path, clock)
    w.rpc.submit_mode = "transport"
    await w.ex.submit_buy(make_buy_signal(clock))
    (res,) = await settle(w)
    assert res.fill is not None and res.fill.verified_onchain
    assert len(w.rpc.sends) == 1  # the transport error did not trigger a second send
    (intent,) = await intents(w)
    assert intent.status is IntentStatus.CONFIRMED and intent.attempts == 1
    await teardown(w)


async def test_blockhash_expiry_marks_the_intent_expired(
    tmp_path: Path, clock: ManualClock
) -> None:
    w = await make_world(tmp_path, clock)
    w.rpc.status_script = ["unknown"]
    w.rpc.height = 5000  # past lastValidBlockHeight 1000
    await w.ex.submit_buy(make_buy_signal(clock))
    (res,) = await settle(w)
    assert res.decision.kind is DecisionKind.REJECT and "expired" in res.decision.note
    (intent,) = await intents(w)
    assert intent.status is IntentStatus.EXPIRED and len(w.rpc.sends) == 1
    assert w.rpc.status_calls == 5  # the height is checked every fifth poll
    await teardown(w)


async def test_on_chain_error_is_a_failed_intent(tmp_path: Path, clock: ManualClock) -> None:
    w = await make_world(tmp_path, clock)
    w.rpc.status_script = ["failed"]
    await w.ex.submit_buy(make_buy_signal(clock))
    (res,) = await settle(w)
    assert res.fill is None and "failed on chain" in res.decision.note
    assert (await intents(w))[0].status is IntentStatus.FAILED
    await teardown(w)


async def test_confirmation_timeout_leaves_the_intent_sent_for_recovery(
    tmp_path: Path, clock: ManualClock
) -> None:
    cfg = AutonomyConfig(
        enabled=True,
        acknowledge_real_money=True,
        max_total_loss_sol=Decimal("0.25"),
        confirm_timeout_s=5,
        status_poll_interval_s=1,
    )
    w = await make_world(tmp_path, clock, cfg=cfg)
    w.rpc.status_script = ["rpc-error"]
    await w.ex.submit_buy(make_buy_signal(clock))
    (res,) = await settle(w)
    assert res.fill is None and "left SENT" in res.decision.note
    (intent,) = await intents(w)
    assert intent.status is IntentStatus.SENT and "no confirmation" in (intent.error or "")
    assert len(w.rpc.sends) == 1
    await teardown(w)


async def test_loss_limit_disarms_buys_but_exits_continue(
    tmp_path: Path, clock: ManualClock
) -> None:
    w = await make_world(tmp_path, clock)
    w.rpc.balance = SOL - SOL // 4  # 0.25 SOL gone since arming: the limit
    await w.ex.submit_buy(make_buy_signal(clock))
    (res,) = await settle(w)
    assert res.decision.kind is DecisionKind.REJECT and "total loss limit" in res.decision.note
    assert w.rpc.sends == []
    state = arming.read(w.home)
    assert not state.armed and state.disarmed_reason and "loss limit" in state.disarmed_reason
    assert w.captured.of(AutonomyDisarmed)
    # a sell still goes through while disarmed (exits contain the loss)
    w.rpc.token_balance = (TOKENS, 6)
    await w.ex.submit_sell(make_sell_signal(clock))
    (res2,) = await settle(w)
    assert res2.fill is not None and res2.fill.side is SignalKind.SELL
    # but a new buy stays refused until re-armed
    await w.ex.submit_buy(make_buy_signal(clock, mint="MintOther"))
    (res3,) = await settle(w)
    assert res3.fill is None and "not armed" in res3.decision.note
    await teardown(w)


async def test_kill_switch_stops_buys_and_sells(tmp_path: Path, clock: ManualClock) -> None:
    w = await make_world(tmp_path, clock)
    arming.kill(w.home)
    w.rpc.token_balance = (10, 6)
    await w.ex.submit_buy(make_buy_signal(clock))
    await w.ex.submit_sell(make_sell_signal(clock))
    resolutions = await settle(w)
    while len(resolutions) < 2:
        resolutions += await settle(w)
    assert all(r.fill is None and "KILL" in r.decision.note for r in resolutions)
    assert w.rpc.sends == [] and w.provider.quotes == []
    arming.resume(w.home)
    w.rpc.token_balance = None
    await w.ex.submit_buy(make_buy_signal(clock))
    (res,) = await settle(w)
    assert res.fill is not None
    await teardown(w)


async def test_not_armed_or_not_acknowledged_never_sends(
    tmp_path: Path, clock: ManualClock
) -> None:
    cfg = AutonomyConfig(
        enabled=True, acknowledge_real_money=False, max_total_loss_sol=Decimal("1")
    )
    w = await make_world(tmp_path, clock, cfg=cfg)
    await w.ex.submit_buy(make_buy_signal(clock))
    (res,) = await settle(w)
    assert res.fill is None and "not armed" in res.decision.note and w.rpc.sends == []
    await teardown(w)
    w2 = await make_world(tmp_path / "b", clock, arm=False)
    await w2.ex.submit_buy(make_buy_signal(clock))
    (res2,) = await settle(w2)
    assert res2.fill is None and w2.rpc.sends == []
    await teardown(w2)


async def test_humans_cannot_confirm_or_cancel_an_in_flight_order(
    tmp_path: Path, clock: ManualClock
) -> None:
    w = await make_world(tmp_path, clock)
    w.rpc.status_script = ["unknown"] * 3 + ["confirmed"]
    order = await w.ex.submit_buy(make_buy_signal(clock))
    with pytest.raises(ValueError, match="confirmed transactions"):
        await w.ex.decide(order, DecisionKind.CONFIRM, DecisionSource.HUMAN)
    await w.ex.auto_confirm(clock.now())  # launches the task
    await asyncio.sleep(0)
    with pytest.raises(ValueError, match="being executed on chain"):
        await w.ex.decide(order, DecisionKind.REJECT, DecisionSource.HUMAN)
    # the TTL elapsing while the transaction is in flight does not expire the order
    clock.advance(1000)
    assert await w.ex.expire_stale(clock.now()) == []
    (res,) = await settle(w)
    assert res.fill is not None and res.fill.verified_onchain
    with pytest.raises(OrderNotPendingError):
        await w.ex.decide(order, DecisionKind.REJECT, DecisionSource.HUMAN)
    await teardown(w)


async def test_wallet_without_tokens_cannot_sell(tmp_path: Path, clock: ManualClock) -> None:
    w = await make_world(tmp_path, clock)
    w.rpc.token_balance = None
    await w.ex.submit_sell(make_sell_signal(clock))
    (res,) = await settle(w)
    assert res.decision.kind is DecisionKind.IGNORE and "nothing to sell" in res.decision.note
    assert w.rpc.sends == []
    await teardown(w)


async def test_build_and_simulation_failures_never_send(tmp_path: Path, clock: ManualClock) -> None:
    w = await make_world(tmp_path, clock)
    w.provider.build_error = "jupiter swap build: 503"
    await w.ex.submit_buy(make_buy_signal(clock))
    (res,) = await settle(w)
    assert "swap build failed" in res.decision.note and w.rpc.sends == []
    w.provider.build_error = None
    w.provider.simulation_error = "InstructionError: slippage"
    await w.ex.submit_buy(make_buy_signal(clock, mint="MintTwo"))
    (res2,) = await settle(w)
    assert "simulation failed" in res2.decision.note and w.rpc.sends == []
    statuses = [i.status for i in await intents(w)]
    assert statuses == [IntentStatus.ABANDONED, IntentStatus.FAILED]
    await teardown(w)


async def test_recovery_books_a_swap_that_landed_after_a_crash(
    tmp_path: Path, clock: ManualClock
) -> None:
    w = await make_world(tmp_path, clock)
    signed = w.wallet.sign_swap(unsigned_for(w.wallet.pubkey))
    sent = ExecutionIntent(
        intent_id="int-crash",
        session_id="earlier",
        signal_id="sig-crash",
        order_id="ord-crash",
        mint=MINT,
        symbol="TST",
        side=SignalKind.BUY,
        created_at=clock.now() - timedelta(minutes=5),
        updated_at=clock.now() - timedelta(minutes=5),
        wallet_public_key=w.wallet.pubkey,
        status=IntentStatus.SENT,
        in_amount_raw=SOL // 20,
        expected_out_raw=TOKENS,
        signature=signed.signature,
        last_valid_block_height=1000,
        sent_at=clock.now() - timedelta(minutes=5),
        token_decimals=6,
    )
    await w.repo.save_intent_now(sent)
    never = ExecutionIntent(
        intent_id="int-expired",
        session_id="earlier",
        signal_id="sig-x",
        order_id="ord-x",
        mint="MintGone",
        symbol="GONE",
        side=SignalKind.BUY,
        created_at=clock.now() - timedelta(hours=1),
        updated_at=clock.now() - timedelta(hours=1),
        wallet_public_key=w.wallet.pubkey,
        status=IntentStatus.SENT,
        signature="5NeverLanded",
        last_valid_block_height=10,
        sent_at=clock.now() - timedelta(hours=1),
    )
    await w.repo.save_intent_now(never)
    w.rpc.status_script = ["confirmed"]

    async def statuses(signatures: list[str]) -> list[SignatureStatus]:
        sig = signatures[0]
        if sig == "5NeverLanded":
            return [SignatureStatus(sig, False, None, None, None, None)]
        return [SignatureStatus(sig, True, 5, 3, None, "finalized")]

    w.rpc.statuses = statuses  # type: ignore[method-assign]
    w.rpc.height = 50  # past the expired intent's height, below the landed one's
    lines = await w.ex.recover()
    assert any("recovered BUY of TST" in line for line in lines)
    assert any("expired, never landed" in line for line in lines)
    positions = w.account.open_positions
    assert len(positions) == 1 and positions[0].mint == MINT
    assert positions[0].provenance is FillProvenance.VERIFIED_ONCHAIN and positions[0].is_verified
    assert positions[0].quantity_raw == TOKENS and positions[0].entry_signal_id == "sig-crash"
    by_id = {i.intent_id: i for i in await intents(w)}
    assert by_id["int-crash"].status is IntentStatus.CONFIRMED and by_id["int-crash"].fill_id
    assert by_id["int-expired"].status is IntentStatus.EXPIRED
    # the landed buy (sent earlier today) counts against today's spend; recovery is a no-op twice
    assert w.ex.spent_today_lamports() == SOL // 20
    await w.bus.drain(1.0)
    assert w.captured.of(TransactionConfirmed)
    assert await w.ex.recover() == []
    fills = (await w.repo.counts())["execution_intents"]
    assert fills == 2
    await teardown(w)


async def test_secret_never_reaches_the_database_or_events(
    tmp_path: Path, clock: ManualClock
) -> None:
    w = await make_world(tmp_path, clock)
    secret = (w.home / "wallet" / "hot-wallet.json").read_text().strip()
    await w.ex.submit_buy(make_buy_signal(clock))
    (res,) = await settle(w)
    assert res.fill is not None
    for intent in await intents(w):
        assert secret not in repr(intent)
    assert secret not in repr(w.captured.events)
    assert w.wallet.pubkey in repr(w.captured.events)
    await teardown(w)
