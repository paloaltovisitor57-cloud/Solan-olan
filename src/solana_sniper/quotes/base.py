"""Quote provider interface. Quotes are executable estimates, never displayed prices."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from solana_sniper.domain.models import SwapQuote


class QuoteError(Exception):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class SwapBuild:
    """An UNSIGNED swap transaction built for a public key, plus what the builder said about
    it. Signing happens in the wallet package (autonomous mode) or in the user's own wallet."""

    transaction_b64: str
    last_valid_block_height: int | None  # after this height the transaction can never land
    priority_fee_lamports: int | None
    compute_unit_limit: int | None
    simulation_error: str | None  # the builder simulated it and it would fail


class QuoteProvider(Protocol):
    name: str

    async def quote(
        self, input_mint: str, output_mint: str, amount_raw: int, slippage_bps: int
    ) -> SwapQuote: ...

    async def build_swap(self, quote: SwapQuote, user_public_key: str) -> SwapBuild:
        """Build the unsigned swap for `user_public_key`; raises QuoteError when it cannot.

        Quote providers do not sign and do not broadcast; only the wallet package does, and only
        in autonomous mode.
        """
        ...

    async def prepare_unsigned_swap(self, quote: SwapQuote, user_public_key: str) -> str | None:
        """Convenience for signal mode: the unsigned transaction for the user to sign in their
        own wallet, or None when it could not be built."""
        ...
