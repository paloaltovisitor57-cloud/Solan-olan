"""On-chain reconciliation: the confirmed transaction, not the quote, decides the fill."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from solana_sniper.wallet.reconcile import ReconcileError, parse_confirmed_swap

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
OWNER = "HotWallet1111111111111111111111111111111111"
MINT = "MintTokenAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def load(name: str) -> dict[str, Any]:
    body = json.loads((FIXTURES / name).read_text())
    result: dict[str, Any] = body["result"]
    return result


def test_buy_reconciles_sol_spent_and_tokens_received() -> None:
    swap = parse_confirmed_swap(load("rpc_get_transaction_buy.json"), owner=OWNER, mint=MINT)
    assert swap.succeeded and swap.err is None
    assert swap.signature.startswith("5BuySignature")
    assert swap.slot == 300000123 and swap.block_time == 1758200000
    assert swap.fee_lamports == 205000
    assert swap.sol_delta_lamports == -100_205_000
    assert swap.sol_spent_lamports == 100_205_000 and swap.sol_received_lamports == 0
    assert swap.token_delta_raw == 1_000_000_000 and swap.token_decimals == 6


def test_sell_reconciles_sol_received_and_tokens_sold() -> None:
    swap = parse_confirmed_swap(load("rpc_get_transaction_sell.json"), owner=OWNER, mint=MINT)
    assert swap.succeeded
    assert swap.sol_delta_lamports == 49_795_000
    assert swap.sol_received_lamports == 49_795_000 and swap.sol_spent_lamports == 0
    assert swap.token_delta_raw == -1_000_000_000 and swap.token_decimals == 6


def test_other_owners_and_mints_are_ignored() -> None:
    tx = load("rpc_get_transaction_buy.json")
    other = parse_confirmed_swap(tx, owner=OWNER, mint="OtherMint111111111111111111111111111111111")
    assert other.token_delta_raw == 0 and other.token_decimals is None
    pool = parse_confirmed_swap(tx, owner="PoolVault11111111111111111111111111111111111", mint=MINT)
    assert pool.sol_delta_lamports == 0


def test_failed_transaction_keeps_the_error_and_fee() -> None:
    tx = copy.deepcopy(load("rpc_get_transaction_buy.json"))
    tx["meta"]["err"] = {"InstructionError": [3, {"Custom": 6001}]}
    tx["meta"]["postBalances"] = [999_795_000, 2039280, 0, 1, 3000000000]
    tx["meta"]["postTokenBalances"] = tx["meta"]["preTokenBalances"]
    swap = parse_confirmed_swap(tx, owner=OWNER, mint=MINT)
    assert not swap.succeeded and "6001" in (swap.err or "")
    assert swap.sol_spent_lamports == 205_000 and swap.token_delta_raw == 0


def test_malformed_transactions_raise() -> None:
    tx = load("rpc_get_transaction_buy.json")
    with pytest.raises(ReconcileError, match="not an account"):
        parse_confirmed_swap(tx, owner="Nobody", mint=MINT)
    no_meta = copy.deepcopy(tx)
    no_meta["meta"] = None
    with pytest.raises(ReconcileError, match="no meta"):
        parse_confirmed_swap(no_meta, owner=OWNER, mint=MINT)
    no_sig = copy.deepcopy(tx)
    no_sig["transaction"]["signatures"] = []
    with pytest.raises(ReconcileError, match="no signature"):
        parse_confirmed_swap(no_sig, owner=OWNER, mint=MINT)
    short = copy.deepcopy(tx)
    short["meta"]["postBalances"] = []
    with pytest.raises(ReconcileError, match="balances missing"):
        parse_confirmed_swap(short, owner=OWNER, mint=MINT)


def test_plain_string_account_keys_are_supported() -> None:
    tx = copy.deepcopy(load("rpc_get_transaction_sell.json"))
    tx["transaction"]["message"]["accountKeys"] = [
        k["pubkey"] for k in tx["transaction"]["message"]["accountKeys"]
    ]
    swap = parse_confirmed_swap(tx, owner=OWNER, mint=MINT)
    assert swap.sol_received_lamports == 49_795_000
