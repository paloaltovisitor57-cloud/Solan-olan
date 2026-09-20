"""The AUTONOMOUS composition driven end to end with mocked transports and a fake chain.

Same live provider path as `test_live_pipeline_mocked` (PumpPortal WS, GeckoTerminal, DexScreener,
Solana RPC, Jupiter, CoinGecko), but the executor re-quotes, builds, signs and broadcasts a real
`VersionedTransaction` to a fake node that executes the swap against the pool and answers
`getSignatureStatuses` / `getTransaction` like mainnet does. Proves, offline: exactly one
`sendTransaction` per intent, fills booked from the confirmed transaction (VERIFIED_ONCHAIN),
the exit sold on-chain and the position closed, the heartbeat's autonomy block, and that the
secret never reaches the database or the heartbeat.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from solders.hash import Hash
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from solana_sniper.app import arming
from solana_sniper.app.bootstrap import build_runtime
from solana_sniper.config import load_settings
from solana_sniper.config.paths import ensure_home, wallet_key_path
from solana_sniper.domain.enums import CandidateState as S
from solana_sniper.domain.enums import FillProvenance, IntentStatus, RunMode, SignalKind
from solana_sniper.wallet.keys import generate
from tests.integration.test_live_pipeline_mocked import (
    MINT,
    WSOL,
    FakePool,
    FakeWsConn,
    make_http_handler,
)

FEE = 5_000
NOT_HANDLED = object()
_builds = 0


def unsigned_for(pubkey: str) -> str:
    """A distinct unsigned transaction per build, as Jupiter's fresh blockhash would give."""
    global _builds
    _builds += 1
    payer = Pubkey.from_string(pubkey)
    ix = transfer(TransferParams(from_pubkey=payer, to_pubkey=Pubkey.default(), lamports=_builds))
    msg = MessageV0.try_compile(payer, [ix], [], Hash.default())
    tx = VersionedTransaction.populate(msg, [Signature.default()])
    return base64.b64encode(bytes(tx)).decode()


class FakeChain:
    """Executes each broadcast swap against the pool and serves the RPC methods the sender uses."""

    def __init__(self, pool: FakePool, pubkey: str) -> None:
        self.pool = pool
        self.pubkey = pubkey
        self.lamports = 10**9
        self.tokens = 0
        self.height = 100
        self.last_quote: dict[str, Any] | None = None
        self.txs: dict[str, dict[str, Any]] = {}
        self.sends: list[str] = []
        self.polls: dict[str, int] = {}
        self.spent: list[int] = []
        self.received: list[int] = []

    def _entry(self, amount: int) -> dict[str, Any]:
        return {
            "accountIndex": 2,
            "mint": MINT,
            "owner": self.pubkey,
            "uiTokenAmount": {"amount": str(amount), "decimals": 6},
        }

    def rpc(self, body: dict[str, Any]) -> Any:
        method, params = body["method"], body.get("params", [])
        if method == "getBalance":
            return {"context": {"slot": 1}, "value": self.lamports}
        if method == "getBlockHeight":
            self.height += 1
            return self.height
        if method == "getTokenAccountsByOwner":
            if self.tokens <= 0:
                return {"context": {"slot": 1}, "value": []}
            info = {
                "mint": MINT,
                "owner": self.pubkey,
                "tokenAmount": {"amount": str(self.tokens), "decimals": 6},
            }
            account = {"data": {"parsed": {"info": info, "type": "account"}}}
            return {"context": {"slot": 1}, "value": [{"pubkey": "Ata111", "account": account}]}
        if method == "sendTransaction":
            tx = VersionedTransaction.from_bytes(base64.b64decode(params[0]))
            sig = str(tx.signatures[0])
            self.sends.append(sig)
            q = self.last_quote
            assert q is not None, "a swap must be built before it is sent"
            pre_l, pre_t = self.lamports, self.tokens
            if q["inputMint"] == WSOL:
                spend, got = int(q["inAmount"]), int(q["outAmount"])
                self.lamports -= spend + FEE
                self.tokens += got
                self.spent.append(spend)
            else:
                sold, got = int(q["inAmount"]), int(q["outAmount"])
                self.tokens -= sold
                self.lamports += got - FEE
                self.received.append(got)
            self.txs[sig] = {
                "slot": 1000 + len(self.sends),
                "blockTime": int(time.time()),
                "meta": {
                    "err": None,
                    "fee": FEE,
                    "preBalances": [pre_l, 0, 2039280],
                    "postBalances": [self.lamports, 0, 2039280],
                    "preTokenBalances": [self._entry(pre_t)] if pre_t else [],
                    "postTokenBalances": [self._entry(self.tokens)] if self.tokens else [],
                },
                "transaction": {
                    "signatures": [sig],
                    "message": {
                        "accountKeys": [
                            {"pubkey": self.pubkey, "signer": True, "writable": True},
                            {"pubkey": "Curve111", "signer": False, "writable": True},
                            {"pubkey": "Ata111", "signer": False, "writable": True},
                        ]
                    },
                },
            }
            return sig
        if method == "getSignatureStatuses":
            out: list[Any] = []
            for sig in params[0]:
                if sig not in self.txs:
                    out.append(None)
                    continue
                self.polls[sig] = self.polls.get(sig, 0) + 1
                if self.polls[sig] < 2:  # first poll: not visible yet, like a real node
                    out.append(None)
                else:
                    out.append(
                        {
                            "slot": self.txs[sig]["slot"],
                            "confirmations": 12,
                            "err": None,
                            "confirmationStatus": "confirmed",
                        }
                    )
            return {"context": {"slot": 1}, "value": out}
        if method == "getTransaction":
            return self.txs.get(params[0])
        return NOT_HANDLED


def make_autonomous_handler(
    pool: FakePool, chain: FakeChain, created_ms: int, counters: dict[str, int], pubkey: str
) -> Any:
    base = make_http_handler(pool, created_ms, counters)

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "lite-api.jup.ag" and request.method == "POST":
            body = json.loads(request.read())
            assert url.path == "/swap/v1/swap" and body["userPublicKey"] == pubkey
            chain.last_quote = body["quoteResponse"]
            counters["swap_builds"] += 1
            return httpx.Response(
                200,
                json={
                    "swapTransaction": unsigned_for(pubkey),
                    "lastValidBlockHeight": chain.height + 300,
                    "prioritizationFeeLamports": 5000,
                    "computeUnitLimit": 200_000,
                },
            )
        if url.host == "api.mainnet-beta.solana.com":
            body = json.loads(request.read())
            result = chain.rpc(body)
            if result is not NOT_HANDLED:
                counters["send_rpc"] += 1
                return httpx.Response(
                    200, json={"jsonrpc": "2.0", "id": body.get("id", 1), "result": result}
                )
        return base(request)

    return handler


@pytest.mark.timeout(300)
async def test_autonomous_composition_trades_on_the_fake_chain(tmp_path: Path) -> None:
    settings = load_settings(Path("configs/default.yaml"))
    home = tmp_path / "home"
    ensure_home(home)
    settings.home = home
    settings.storage.database_url = f"sqlite+aiosqlite:///{home}/db/auto.db"
    settings.telemetry.log_file = None
    settings.dashboard.enabled = False
    settings.market_data.poll_interval_s = 0.3
    settings.market_data.stale_after_s = 5
    settings.discovery.poll_interval_s = 0.5
    settings.filters.min_token_age_s = 2
    settings.filters.min_liquidity_usd = Decimal("500")
    settings.filters.min_volume_5m_usd = Decimal("100")
    settings.filters.min_buys_5m = 3
    settings.filters.min_unique_traders = 3
    settings.entry.min_score = 55
    settings.entry.min_observations = 3
    settings.entry.min_trade_velocity_per_min = 3
    settings.quotes.refresh_interval_s = 0.5
    settings.exit.min_holding_s_before_trailing = 1
    settings.exit.trailing.tiers[0].drawdown_pct = 0.15
    settings.exit.momentum_drop_threshold = -0.08
    settings.portfolio.fx_refresh_s = 1000
    settings.portfolio.snapshot_interval_s = 2
    wallet_path = wallet_key_path(home)
    wallet = generate(wallet_path)
    secret = wallet_path.read_text().strip()
    settings.wallet.key_file = str(wallet_path)
    a = settings.autonomy
    a.enabled = a.acknowledge_real_money = True
    a.max_total_loss_sol = Decimal("0.5")
    a.max_trade_sol = Decimal("0.05")
    a.status_poll_interval_s = 0.2
    a.confirm_timeout_s = 20.0
    a.requote_max_worse_pct = 15.0  # the fake pool moves several % per second during the pump
    arming.arm(
        home,
        wallet_public_key=wallet.pubkey,
        start_balance_lamports=10**9,
        max_total_loss_sol=Decimal("0.5"),
        caps={},
    )
    pool = FakePool()
    chain = FakeChain(pool, wallet.pubkey)
    counters = {
        "dexscreener": 0,
        "gecko": 0,
        "rpc": 0,
        "jupiter": 0,
        "fx": 0,
        "swap_builds": 0,
        "send_rpc": 0,
    }
    conn = FakeWsConn(pool)

    async def ws_connect(url: str) -> Any:
        return conn

    runtime = build_runtime(
        settings,
        mode=RunMode.AUTONOMOUS,
        session_id="auto-mocked",
        quiet_alerts=True,
        http_transport=httpx.MockTransport(
            make_autonomous_handler(pool, chain, int(time.time() * 1000), counters, wallet.pubkey)
        ),
        ws_connect=ws_connect,
    )
    auto = runtime.autonomous
    assert auto is not None and auto.wallet_public_key == wallet.pubkey
    await runtime.start()
    engine = runtime.engine
    try:
        assert auto.last_wallet_lamports == 10**9  # the start read the balance over the fake RPC
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and not runtime.account.open_positions:
            await asyncio.sleep(0.5)
        assert runtime.account.open_positions, (
            f"no position; stats={engine.stats} cands="
            f"{[(c.symbol, c.state, c.gate_reasons) for c in engine.candidates.values()]} "
            f"counters={counters} recent={engine.stats.recent[-8:]} "
            f"intents={[(i.status, i.error) for i in await runtime.repo.intents()]}"
        )
        pos = runtime.account.open_positions[0]
        cand = engine.candidates[MINT]
        assert cand.state is S.OPEN
        # the fill is what the chain says, not what the quote promised
        assert pos.provenance is FillProvenance.VERIFIED_ONCHAIN and pos.is_verified
        assert len(chain.sends) == 1 and counters["swap_builds"] == 1
        assert pos.quantity_raw == chain.tokens and chain.tokens > 0
        assert pos.entry_sol == Decimal(chain.spent[0]) / Decimal(10**9)
        assert Decimal("0") < pos.entry_sol <= Decimal("0.05")
        intents = await runtime.repo.intents()
        assert [i.status for i in intents] == [IntentStatus.CONFIRMED]
        assert intents[0].signature == chain.sends[0] and intents[0].side is SignalKind.BUY
        assert intents[0].fill_id  # the engine opens the position from that fill
        # the scripted dump produces an exit; the executor sells the wallet's actual balance
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and pos.state is not S.CLOSED:
            await asyncio.sleep(0.5)
        assert pos.state is S.CLOSED, (
            f"state={cand.state} pos={pos} recent={engine.stats.recent[-6:]} "
            f"intents={[(i.side, i.status, i.error) for i in await runtime.repo.intents()]}"
        )
        assert len(chain.sends) == 2 and chain.tokens == 0 and counters["swap_builds"] == 2
        assert pos.exit_reason is not None and engine.stats.exits >= 1
        intents = await runtime.repo.intents()
        assert sorted((i.side, i.status) for i in intents) == [
            (SignalKind.BUY, IntentStatus.CONFIRMED),
            (SignalKind.SELL, IntentStatus.CONFIRMED),
        ]
        sell = next(i for i in intents if i.side is SignalKind.SELL)
        assert sell.position_id == pos.position_id and sell.exit_reason == str(pos.exit_reason)
        assert sell.fill_id and sell.signature == chain.sends[1]
        assert runtime.account.cash > 0 and len(runtime.account.ledger) >= 3
        assert runtime.health is not None
        snap = runtime.health.snapshot()
        block = snap["autonomy"]
        assert block["sends"] == 2 and block["confirmed"] == 2 and block["failed"] == 0
        assert block["wallet_public_key"] == wallet.pubkey and block["armed"] is True
        assert block["intents_in_flight"] == 0 and block["last_confirmed_at"]
        assert Decimal(block["spent_today_sol"]) == Decimal(chain.spent[0]) / Decimal(10**9)
        assert snap["records_verified_onchain"] is True
        assert snap["execution_provenance"] == "AUTONOMOUS" and snap["mode"] == "AUTONOMOUS"
        assert secret not in json.dumps(snap, default=str)
        runtime.health.write_once()
        assert secret not in (home / "state" / "status.json").read_text()
        assert snap["connections"]["pumpportal"]["connected"]
    finally:
        await runtime.stop(reason="test")
    assert secret.encode() not in (home / "db" / "auto.db").read_bytes()
    # nothing was ever re-sent: one broadcast per intent, and every broadcast was a new signature
    assert len(set(chain.sends)) == 2
