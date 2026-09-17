"""Token metadata / liquidity provider interfaces."""

from __future__ import annotations

from typing import Protocol

from solana_sniper.domain.models import HolderDistribution, TokenAuthorities


class TokenMetadataProvider(Protocol):
    name: str

    async def get_authorities(self, mint: str) -> TokenAuthorities | None: ...


class LiquidityProvider(Protocol):
    """Holder distribution / pool-side facts. Named after the spec's LiquidityProvider."""

    name: str

    async def get_holder_distribution(
        self, mint: str, pool_addresses: tuple[str, ...]
    ) -> HolderDistribution | None: ...
