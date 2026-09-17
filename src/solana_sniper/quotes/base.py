"""Quote provider interface. Quotes are executable estimates, never displayed prices."""

from __future__ import annotations

from typing import Protocol

from solana_sniper.domain.models import SwapQuote


class QuoteError(Exception):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class QuoteProvider(Protocol):
    name: str

    async def quote(
        self, input_mint: str, output_mint: str, amount_raw: int, slippage_bps: int
    ) -> SwapQuote: ...

    async def prepare_unsigned_swap(self, quote: SwapQuote, user_public_key: str) -> str | None:
        """Return a base64 unsigned transaction for the user to sign in their own wallet, or None.

        Implementations must never sign or broadcast.
        """
        ...
