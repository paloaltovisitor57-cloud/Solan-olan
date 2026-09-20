"""Solana JSON-RPC calls that autonomous mode needs beyond the token-analysis reads: balances,
block height, sendTransaction, signature statuses and confirmed transactions.

Unlike `token_analysis/solana_rpc.py`, nothing here degrades silently: every failure raises a
`SendError` that says whether the request may have reached the network, because a failed
`sendTransaction` is not the same as an unknown one. Sends are never retried by the HTTP layer
(`retries=0`): a retried broadcast could land twice.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from solana_sniper.discovery.parsing import as_dict, as_int, as_list, as_str
from solana_sniper.domain.clock import Clock
from solana_sniper.infra.governor import ProviderUnavailableError
from solana_sniper.infra.http import HttpClient, HttpError, MalformedResponseError
from solana_sniper.telemetry.logging import get_logger
from solana_sniper.telemetry.metrics import Metrics
from solana_sniper.telemetry.redaction import safe_exception

log = get_logger(__name__)


class SendError(Exception):
    """An RPC call for the hot wallet failed. `maybe_sent` is True when a sendTransaction may
    have reached the network anyway (transport error, gateway error): the caller must then wait
    for the signature instead of sending again."""

    def __init__(self, message: str, *, retryable: bool = False, maybe_sent: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.maybe_sent = maybe_sent


@dataclass(frozen=True, slots=True)
class SignatureStatus:
    signature: str
    found: bool
    slot: int | None
    confirmations: int | None
    err: str | None
    confirmation_status: str | None  # processed | confirmed | finalized

    @property
    def confirmed(self) -> bool:
        return (
            self.found
            and self.err is None
            and self.confirmation_status in ("confirmed", "finalized")
        )

    @property
    def failed(self) -> bool:
        return self.found and self.err is not None


class SolanaSendClient:
    """Thin JSON-RPC client over the shared `HttpClient` (governed, redacted)."""

    name = "solana-send"

    def __init__(
        self,
        http: HttpClient,
        url: str,
        clock: Clock,
        *,
        metrics: Metrics | None = None,
        timeout_s: float = 12.0,
    ) -> None:
        self._http = http
        self._url = url
        self._clock = clock
        self._metrics = metrics
        self._timeout = timeout_s
        self._req_id = 0
        self.sends = 0

    @property
    def url(self) -> str:
        return self._url

    async def _rpc(
        self, method: str, params: list[Any], *, retries: int = 1, maybe_sent: bool = False
    ) -> Any:
        self._req_id += 1
        payload = {"jsonrpc": "2.0", "id": self._req_id, "method": method, "params": params}
        started = time.perf_counter()
        try:
            res = await self._http.post_json(
                self._url, json=payload, retries=retries, timeout_s=self._timeout
            )
        except ProviderUnavailableError as exc:
            raise SendError(f"{method}: {safe_exception(exc)}", retryable=True) from None
        except MalformedResponseError as exc:
            # the request reached a server that answered garbage: a send may have been accepted
            raise SendError(
                f"{method}: {safe_exception(exc)}", retryable=False, maybe_sent=maybe_sent
            ) from None
        except HttpError as exc:
            transport_or_gateway = exc.status is None or exc.status >= 500
            raise SendError(
                f"{method}: {safe_exception(exc)}",
                retryable=exc.retryable,
                maybe_sent=maybe_sent and transport_or_gateway,
            ) from None
        if self._metrics is not None:
            self._metrics.observe("send_rpc_latency", (time.perf_counter() - started) * 1000.0)
        body = as_dict(res.json)
        if body is None:
            raise SendError(f"{method}: non-object response", maybe_sent=maybe_sent)
        if "error" in body:
            err = as_dict(body.get("error")) or {}
            code = as_int(err.get("code"))
            message = as_str(err.get("message")) or str(body.get("error"))
            # an RPC error is a definitive answer: the node did not accept the request
            raise SendError(f"{method}: rpc error {code}: {message}", retryable=code == -32005)
        return body.get("result")

    # ------------------------------------------------------------ reads
    async def get_balance(self, pubkey: str) -> int:
        result = as_dict(await self._rpc("getBalance", [pubkey, {"commitment": "confirmed"}]))
        value = as_int((result or {}).get("value"))
        if value is None:
            raise SendError("getBalance: malformed result")
        return value

    async def get_token_balance(self, owner: str, mint: str) -> tuple[int, int] | None:
        """(raw amount, decimals) summed over the owner's token accounts for `mint`, or None
        when the owner holds no account for it."""
        result = as_dict(
            await self._rpc(
                "getTokenAccountsByOwner",
                [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
            )
        )
        total = 0
        decimals: int | None = None
        for item in as_list((result or {}).get("value")):
            account = as_dict((as_dict(item) or {}).get("account")) or {}
            parsed = as_dict((as_dict(account.get("data")) or {}).get("parsed")) or {}
            info = as_dict(parsed.get("info")) or {}
            amount = as_dict(info.get("tokenAmount")) or {}
            raw = as_int(amount.get("amount"))
            dec = as_int(amount.get("decimals"))
            if raw is None or dec is None:
                continue
            total += raw
            decimals = dec if decimals is None else decimals
        if decimals is None:
            return None
        return total, decimals

    async def get_block_height(self) -> int:
        value = as_int(await self._rpc("getBlockHeight", [{"commitment": "confirmed"}]))
        if value is None:
            raise SendError("getBlockHeight: malformed result")
        return value

    async def statuses(self, signatures: list[str]) -> list[SignatureStatus]:
        result = as_dict(
            await self._rpc(
                "getSignatureStatuses", [signatures, {"searchTransactionHistory": True}]
            )
        )
        values = as_list((result or {}).get("value"))
        out: list[SignatureStatus] = []
        for sig, raw in zip(signatures, values, strict=False):
            item = as_dict(raw)
            if item is None:
                out.append(SignatureStatus(sig, False, None, None, None, None))
                continue
            err = item.get("err")
            out.append(
                SignatureStatus(
                    signature=sig,
                    found=True,
                    slot=as_int(item.get("slot")),
                    confirmations=as_int(item.get("confirmations")),
                    err=None if err is None else str(err)[:200],
                    confirmation_status=as_str(item.get("confirmationStatus")),
                )
            )
        while len(out) < len(signatures):
            out.append(SignatureStatus(signatures[len(out)], False, None, None, None, None))
        return out

    async def get_transaction(self, signature: str) -> dict[str, Any] | None:
        result = await self._rpc(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "jsonParsed",
                    "commitment": "confirmed",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        )
        return as_dict(result)

    # ------------------------------------------------------------- send
    async def submit(self, signed_transaction_b64: str) -> str:
        """Broadcast a signed transaction once. Returns the signature the node acknowledged.

        Preflight simulation stays on: a swap that would fail is rejected here without costing
        a fee. `maxRetries` lets the node re-send until the blockhash expires; this client
        itself never re-sends."""
        self.sends += 1
        if self._metrics is not None:
            self._metrics.inc("autonomous_sends")
        result = await self._rpc(
            "sendTransaction",
            [
                signed_transaction_b64,
                {
                    "encoding": "base64",
                    "skipPreflight": False,
                    "preflightCommitment": "confirmed",
                    "maxRetries": 3,
                },
            ],
            retries=0,
            maybe_sent=True,
        )
        signature = as_str(result)
        if not signature:
            raise SendError("sendTransaction: no signature in response", maybe_sent=True)
        return signature
