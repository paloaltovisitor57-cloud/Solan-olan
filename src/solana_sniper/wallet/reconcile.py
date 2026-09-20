"""Turn a confirmed transaction (RPC `getTransaction`, jsonParsed) into what actually happened
to the hot wallet: the lamport delta of the owner (fee included) and the token delta for the
mint. Pure parsing; these numbers, not the quote, become the fill."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from solana_sniper.discovery.parsing import as_dict, as_int, as_list, as_str


class ReconcileError(Exception):
    """The transaction JSON does not describe a swap for this owner and mint."""


@dataclass(frozen=True, slots=True)
class OnChainSwap:
    signature: str
    slot: int
    block_time: int | None
    err: str | None
    fee_lamports: int
    sol_delta_lamports: int  # owner post - pre (negative for a buy; includes the fee)
    token_delta_raw: int  # owner token post - pre for the mint (positive for a buy)
    token_decimals: int | None

    @property
    def succeeded(self) -> bool:
        return self.err is None

    @property
    def sol_spent_lamports(self) -> int:
        """Lamports that left the wallet (buy), fee included."""
        return max(0, -self.sol_delta_lamports)

    @property
    def sol_received_lamports(self) -> int:
        """Lamports that arrived (sell), after the fee."""
        return max(0, self.sol_delta_lamports)


def _account_keys(message: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for item in as_list(message.get("accountKeys")):
        if isinstance(item, str):
            keys.append(item)
        else:
            pk = as_str((as_dict(item) or {}).get("pubkey"))
            if pk:
                keys.append(pk)
    return keys


def _token_total(entries: Any, owner: str, mint: str) -> tuple[int, int | None]:
    total = 0
    decimals: int | None = None
    for raw in as_list(entries):
        entry = as_dict(raw) or {}
        if as_str(entry.get("owner")) != owner or as_str(entry.get("mint")) != mint:
            continue
        amount = as_dict(entry.get("uiTokenAmount")) or {}
        value = as_int(amount.get("amount"))
        if value is None:
            continue
        total += value
        dec = as_int(amount.get("decimals"))
        if dec is not None:
            decimals = dec
    return total, decimals


def parse_confirmed_swap(tx: dict[str, Any], *, owner: str, mint: str) -> OnChainSwap:
    """Balance deltas of `owner` in a confirmed transaction.

    jsonParsed layout: `transaction.message.accountKeys[i].pubkey` aligns with
    `meta.preBalances[i]` / `meta.postBalances[i]` (loaded lookup-table addresses are appended
    in the same order). Token balances list `{owner, mint, uiTokenAmount}` per token account."""
    meta = as_dict(tx.get("meta"))
    transaction = as_dict(tx.get("transaction")) or {}
    message = as_dict(transaction.get("message")) or {}
    if meta is None:
        raise ReconcileError("transaction has no meta (not confirmed yet?)")
    signatures = as_list(transaction.get("signatures"))
    signature = as_str(signatures[0]) if signatures else None
    if not signature:
        raise ReconcileError("transaction has no signature")
    keys = _account_keys(message)
    if owner not in keys:
        raise ReconcileError("owner is not an account of this transaction")
    index = keys.index(owner)
    pre = as_list(meta.get("preBalances"))
    post = as_list(meta.get("postBalances"))
    pre_l = as_int(pre[index]) if index < len(pre) else None
    post_l = as_int(post[index]) if index < len(post) else None
    if pre_l is None or post_l is None:
        raise ReconcileError("balances missing for the owner")
    pre_t, dec_pre = _token_total(meta.get("preTokenBalances"), owner, mint)
    post_t, dec_post = _token_total(meta.get("postTokenBalances"), owner, mint)
    err = meta.get("err")
    slot = as_int(tx.get("slot"))
    return OnChainSwap(
        signature=signature,
        slot=slot if slot is not None else 0,
        block_time=as_int(tx.get("blockTime")),
        err=None if err is None else str(err)[:200],
        fee_lamports=as_int(meta.get("fee")) or 0,
        sol_delta_lamports=post_l - pre_l,
        token_delta_raw=post_t - pre_t,
        token_decimals=dec_post if dec_post is not None else dec_pre,
    )
