"""The send client: JSON-RPC shapes, one send per call (never retried), honest errors."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from solana_sniper.domain.clock import ManualClock
from solana_sniper.infra.governor import ProviderUnavailableError
from solana_sniper.infra.http import HttpClient
from solana_sniper.telemetry.metrics import Metrics
from solana_sniper.wallet.rpc import SendError, SolanaSendClient

RPC = "https://rpc.example.com/"
OWNER = "HotWallet1111111111111111111111111111111111"
MINT = "MintTokenAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


class FakeRpc:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_send_transport = False
        self.send_error: dict[str, Any] | None = None
        self.gateway_500 = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        self.calls.append(body)
        method = body["method"]
        if method == "sendTransaction":
            if self.fail_send_transport:
                raise httpx.ConnectError("boom")
            if self.gateway_500:
                return httpx.Response(502, text="bad gateway")
            if self.send_error is not None:
                return httpx.Response(
                    200, json={"jsonrpc": "2.0", "id": 1, "error": self.send_error}
                )
            assert body["params"][1] == {
                "encoding": "base64",
                "skipPreflight": False,
                "preflightCommitment": "confirmed",
                "maxRetries": 3,
            }
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "5sigSENT"})
        if method == "getBalance":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": 1234}},
            )
        if method == "getBlockHeight":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 300_000})
        if method == "getTokenAccountsByOwner":
            accounts = [
                {
                    "pubkey": "Ata1",
                    "account": {
                        "data": {
                            "parsed": {
                                "info": {
                                    "mint": MINT,
                                    "owner": OWNER,
                                    "tokenAmount": {"amount": "700", "decimals": 6},
                                },
                                "type": "account",
                            }
                        }
                    },
                },
                {
                    "pubkey": "Ata2",
                    "account": {
                        "data": {
                            "parsed": {
                                "info": {"tokenAmount": {"amount": "300", "decimals": 6}},
                                "type": "account",
                            }
                        }
                    },
                },
            ]
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "context": {"slot": 1},
                        "value": accounts if body["params"][1]["mint"] == MINT else [],
                    },
                },
            )
        if method == "getSignatureStatuses":
            sigs = body["params"][0]
            values = [
                None
                if s == "unknown"
                else {
                    "slot": 5,
                    "confirmations": None if s == "final" else 3,
                    "err": {"InstructionError": [0, "Custom"]} if s == "bad" else None,
                    "confirmationStatus": "finalized" if s == "final" else "confirmed",
                }
                for s in sigs
            ]
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"context": {"slot": 9}, "value": values},
                },
            )
        if method == "getTransaction":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": None if body["params"][0] == "unknown" else {"slot": 5},
                },
            )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": None})


def make(fake: FakeRpc) -> SolanaSendClient:
    http = HttpClient(transport=httpx.MockTransport(fake.handler), metrics=Metrics())
    return SolanaSendClient(http, RPC, ManualClock(), metrics=Metrics())


async def test_reads_use_the_documented_shapes() -> None:
    fake = FakeRpc()
    client = make(fake)
    assert await client.get_balance(OWNER) == 1234
    assert fake.calls[-1]["params"] == [OWNER, {"commitment": "confirmed"}]
    assert await client.get_block_height() == 300_000
    assert await client.get_token_balance(OWNER, MINT) == (1000, 6)
    assert await client.get_token_balance(OWNER, "Other") is None
    assert fake.calls[-1]["params"][2] == {"encoding": "jsonParsed", "commitment": "confirmed"}
    statuses = await client.statuses(["ok", "bad", "unknown", "final"])
    assert [s.confirmed for s in statuses] == [True, False, False, True]
    assert statuses[1].failed and "Custom" in (statuses[1].err or "")
    assert not statuses[2].found and not statuses[2].failed
    assert fake.calls[-1]["params"][1] == {"searchTransactionHistory": True}
    assert await client.get_transaction("sig") == {"slot": 5}
    assert fake.calls[-1]["params"][1]["maxSupportedTransactionVersion"] == 0
    assert await client.get_transaction("unknown") is None


async def test_submit_sends_exactly_once_and_returns_the_signature() -> None:
    fake = FakeRpc()
    client = make(fake)
    assert await client.submit("AQID") == "5sigSENT"
    sends = [c for c in fake.calls if c["method"] == "sendTransaction"]
    assert len(sends) == 1 and sends[0]["params"][0] == "AQID"
    assert client.sends == 1


async def test_transport_failure_during_send_is_reported_as_maybe_sent() -> None:
    fake = FakeRpc()
    fake.fail_send_transport = True
    client = make(fake)
    with pytest.raises(SendError) as exc:
        await client.submit("AQID")
    assert exc.value.maybe_sent  # the node may have received it: poll, never re-send
    sends = [c for c in fake.calls if c["method"] == "sendTransaction"]
    assert len(sends) == 1  # retries=0: the HTTP layer did not resend


async def test_gateway_error_during_send_is_maybe_sent_but_rpc_errors_are_definitive() -> None:
    fake = FakeRpc()
    fake.gateway_500 = True
    with pytest.raises(SendError) as exc:
        await make(fake).submit("AQID")
    assert exc.value.maybe_sent
    fake = FakeRpc()
    fake.send_error = {
        "code": -32002,
        "message": "Transaction simulation failed: custom program error",
    }
    with pytest.raises(SendError) as exc2:
        await make(fake).submit("AQID")
    assert not exc2.value.maybe_sent and "simulation failed" in str(exc2.value)
    assert not exc2.value.retryable


async def test_provider_cooldown_is_a_retryable_error_without_a_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeRpc()
    client = make(fake)

    async def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise ProviderUnavailableError("solana-send", 3.0, "cooldown")

    monkeypatch.setattr(client._http, "post_json", unavailable)
    with pytest.raises(SendError) as exc:
        await client.submit("AQID")
    assert exc.value.retryable and not exc.value.maybe_sent
    assert fake.calls == []


async def test_malformed_results_raise() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"value": "x"}})

    http = HttpClient(transport=httpx.MockTransport(handler), metrics=Metrics())
    client = SolanaSendClient(http, RPC, ManualClock())
    with pytest.raises(SendError, match="malformed"):
        await client.get_balance(OWNER)
