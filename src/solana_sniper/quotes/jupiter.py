"""Jupiter swap quotes (lite-api.jup.ag without a key, api.jup.ag with x-api-key).

quote:  GET  /swap/v1/quote?inputMint&outputMint&amount&slippageBps
swap:   POST /swap/v1/swap  -> base64 *unsigned* transaction for a public key.
This module does not sign and does not broadcast: signal mode hands the unsigned transaction
to the user, autonomous mode hands it to the wallet package.
"""

from __future__ import annotations

import time
from typing import Any

from solana_sniper.discovery.parsing import as_dict, as_float, as_int, as_list, as_str
from solana_sniper.domain.clock import Clock
from solana_sniper.domain.models import SwapQuote, new_id
from solana_sniper.infra.http import HttpClient, HttpError, RateLimitedError
from solana_sniper.quotes.base import QuoteError, SwapBuild
from solana_sniper.telemetry.logging import get_logger
from solana_sniper.telemetry.metrics import Metrics
from solana_sniper.telemetry.redaction import safe_exception

log = get_logger(__name__)


def parse_quote(payload: Any, latency_ms: float, quoted_at: Any) -> SwapQuote:
    body = as_dict(payload)
    if body is None:
        raise QuoteError("quote response is not an object")
    if "error" in body:
        msg = str(body.get("error"))
        retryable = "rate" in msg.lower() or "timeout" in msg.lower()
        raise QuoteError(f"jupiter: {msg}", retryable=retryable)
    in_amount = as_int(body.get("inAmount"))
    out_amount = as_int(body.get("outAmount"))
    threshold = as_int(body.get("otherAmountThreshold"))
    input_mint = as_str(body.get("inputMint"))
    output_mint = as_str(body.get("outputMint"))
    if (
        None in (in_amount, out_amount, input_mint, output_mint)
        or out_amount is None
        or out_amount <= 0
    ):
        raise QuoteError("jupiter: malformed quote (missing amounts or mints)")
    impact = as_float(body.get("priceImpactPct")) or 0.0
    labels: list[str] = []
    fee_lamports = 0
    for hop in as_list(body.get("routePlan")):
        info = as_dict((as_dict(hop) or {}).get("swapInfo")) or {}
        label = as_str(info.get("label"))
        if label:
            labels.append(label)
        if as_str(info.get("feeMint")) == "So11111111111111111111111111111111111111112":
            fee_lamports += as_int(info.get("feeAmount")) or 0
    return SwapQuote(
        quote_id=new_id("jup"),
        provider="jupiter",
        input_mint=input_mint or "",
        output_mint=output_mint or "",
        in_amount_raw=in_amount or 0,
        out_amount_raw=out_amount,
        other_amount_threshold_raw=threshold if threshold is not None else out_amount,
        slippage_bps=as_int(body.get("slippageBps")) or 0,
        price_impact_pct=impact * 100.0 if impact < 1.0 else impact,  # Jupiter returns a fraction
        route_labels=tuple(labels),
        fee_lamports=fee_lamports,
        quoted_at=quoted_at,
        latency_ms=latency_ms,
        raw=body,
    )


class JupiterQuoteProvider:
    name = "jupiter"

    def __init__(
        self,
        http: HttpClient,
        clock: Clock,
        *,
        base_url: str,
        pro_base_url: str,
        api_key: str | None,
        timeout_s: float = 4.0,
        priority_fee_lamports: int = 200_000,
        metrics: Metrics | None = None,
    ) -> None:
        self._http = http
        self._clock = clock
        self._api_key = api_key
        self._base = (pro_base_url if api_key else base_url).rstrip("/")
        self._timeout = timeout_s
        self._priority_fee = priority_fee_lamports
        self._metrics = metrics
        host = self._base.split("//", 1)[-1].split("/", 1)[0]
        http.set_rate_limit(host, rate_per_s=8.0 if api_key else 1.0, burst=4)

    def _headers(self) -> dict[str, str]:
        h = {"accept": "application/json"}
        if self._api_key:
            h["x-api-key"] = self._api_key
        return h

    async def quote(
        self, input_mint: str, output_mint: str, amount_raw: int, slippage_bps: int
    ) -> SwapQuote:
        if amount_raw <= 0:
            raise QuoteError("amount must be positive")
        started = time.perf_counter()
        try:
            res = await self._http.get_json(
                f"{self._base}/swap/v1/quote",
                params={
                    "inputMint": input_mint,
                    "outputMint": output_mint,
                    "amount": str(amount_raw),
                    "slippageBps": str(slippage_bps),
                    "restrictIntermediateTokens": "true",
                },
                headers=self._headers(),
                retries=1,
                timeout_s=self._timeout,
            )
        except RateLimitedError as exc:
            raise QuoteError(f"jupiter rate limited: {exc}", retryable=True) from exc
        except HttpError as exc:
            if exc.status == 400 or exc.status == 404:
                raise QuoteError(f"jupiter: no route ({exc})", retryable=False) from exc
            raise QuoteError(f"jupiter: {exc}", retryable=exc.retryable) from exc
        latency = (time.perf_counter() - started) * 1000.0
        if self._metrics:
            self._metrics.observe("quote_latency", latency)
            self._metrics.inc("quotes")
        return parse_quote(res.json, latency, self._clock.now())

    async def build_swap(self, quote: SwapQuote, user_public_key: str) -> SwapBuild:
        """Ask Jupiter to build the swap for a PUBLIC key. Returned unsigned, with the block
        height after which it can no longer land."""
        if not quote.raw:
            raise QuoteError("quote carries no provider payload to build a swap from")
        try:
            res = await self._http.post_json(
                f"{self._base}/swap/v1/swap",
                json={
                    "quoteResponse": quote.raw,
                    "userPublicKey": user_public_key,
                    "wrapAndUnwrapSol": True,
                    "dynamicComputeUnitLimit": True,
                    "prioritizationFeeLamports": self._priority_fee,
                },
                headers=self._headers(),
                retries=0,
                timeout_s=self._timeout,
            )
        except RateLimitedError as exc:
            raise QuoteError(f"jupiter swap build rate limited: {exc}", retryable=True) from exc
        except HttpError as exc:
            raise QuoteError(f"jupiter swap build: {exc}", retryable=exc.retryable) from exc
        body = as_dict(res.json) or {}
        transaction = as_str(body.get("swapTransaction"))
        if not transaction:
            detail = body.get("error") or body.get("simulationError") or "no transaction returned"
            raise QuoteError(f"jupiter swap build: {str(detail)[:160]}")
        sim = body.get("simulationError")
        return SwapBuild(
            transaction_b64=transaction,
            last_valid_block_height=as_int(body.get("lastValidBlockHeight")),
            priority_fee_lamports=as_int(body.get("prioritizationFeeLamports")),
            compute_unit_limit=as_int(body.get("computeUnitLimit")),
            simulation_error=None if not sim else str(sim)[:200],
        )

    async def prepare_unsigned_swap(self, quote: SwapQuote, user_public_key: str) -> str | None:
        """Signal mode: the unsigned swap for the user's own wallet, or None when unavailable."""
        try:
            return (await self.build_swap(quote, user_public_key)).transaction_b64
        except QuoteError as exc:
            log.warning("jupiter_swap_build_failed", error=safe_exception(exc))
            return None
