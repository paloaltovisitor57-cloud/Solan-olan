from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from solana_sniper.discovery.geckoterminal import parse_pool
from solana_sniper.discovery.parsing import as_decimal, as_int, ts_from_iso, ts_from_ms
from solana_sniper.discovery.pumpportal import parse_new_token, parse_trade
from solana_sniper.domain.enums import Venue
from solana_sniper.market_data.dexscreener import best_pairs, parse_pair
from solana_sniper.token_analysis.solana_rpc import parse_largest_accounts, parse_mint_account

FIX = Path(__file__).parent.parent / "fixtures"
NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def load(name: str) -> object:
    return json.loads((FIX / name).read_text())


def test_parse_helpers_reject_garbage() -> None:
    assert as_decimal("nan") is None
    assert as_decimal(float("inf")) is None
    assert as_decimal(True) is None
    assert as_decimal("1e3") == Decimal("1000")
    assert as_int("12.0") == 12
    assert as_int(True) is None
    assert ts_from_ms(-5) is None
    assert ts_from_ms("garbage") is None
    assert ts_from_iso("not a date") is None
    assert ts_from_iso("2026-03-01T11:58:30Z") == datetime(2026, 3, 1, 11, 58, 30, tzinfo=UTC)


def test_pumpportal_create_and_trade() -> None:
    create = load("pumpportal_create.json")
    assert isinstance(create, dict)
    token = parse_new_token(create, NOW)
    assert token is not None
    assert token.symbol == "TST"
    assert token.venue is Venue.PUMP_FUN
    assert token.age_seconds(NOW) == 0.0
    assert parse_trade(create, NOW) is None
    trade_msg = load("pumpportal_trade.json")
    assert isinstance(trade_msg, dict)
    trade = parse_trade(trade_msg, NOW)
    assert trade is not None
    assert trade.is_buy and trade.sol_amount == Decimal("0.05")
    assert trade.price_native == Decimal("31.75") / Decimal("1011500000.0")
    assert parse_new_token(trade_msg, NOW) is None
    assert parse_trade({"txType": "buy"}, NOW) is None
    assert (
        parse_trade({"txType": "sell", "mint": "m", "solAmount": "x", "tokenAmount": 1}, NOW)
        is None
    )


def test_geckoterminal_pools() -> None:
    payload = load("geckoterminal_new_pools.json")
    assert isinstance(payload, dict)
    parsed = [parse_pool(item, NOW) for item in payload["data"]]
    assert parsed[2] is None  # garbage entry
    token, snap = parsed[0]  # type: ignore[misc]
    assert token.mint == "NEWTmintAddress111111111111111111111111111"
    assert token.symbol == "NEWT"
    assert token.venue is Venue.RAYDIUM
    assert token.age_seconds(NOW) == 90.0
    assert snap.liquidity_usd == Decimal("18500.25")
    assert snap.buys_5m == 14 and snap.sells_5m == 3
    assert snap.unique_traders == 14
    flipped_token, flipped_snap = parsed[1]  # type: ignore[misc]
    assert flipped_token.mint == "FLIPmint"
    assert flipped_token.quote_mint == "So11111111111111111111111111111111111111112"
    assert flipped_snap.price_native == Decimal("0.003")
    assert flipped_token.venue is Venue.PUMP_FUN


def test_dexscreener_pairs_pick_deepest_and_skip_garbage() -> None:
    payload = load("dexscreener_tokens.json")
    assert isinstance(payload, list)
    views = best_pairs(payload, NOW)
    by_mint = {v.token.mint: v for v in views}
    newt = by_mint["NEWTmintAddress111111111111111111111111111"]
    assert newt.snapshot.liquidity_usd == Decimal("18500.25")  # raydium pair, not meteora
    assert newt.token.pool_address == "pair1Address"
    assert newt.snapshot.buys_1h == 40
    assert newt.token.pool_created_at == datetime.fromtimestamp(1772366310, tz=UTC)
    other = by_mint["OTHERmint"]
    assert other.snapshot.price_native is None
    assert other.snapshot.liquidity_usd is None
    assert other.snapshot.buys_5m == 3 and other.snapshot.sells_5m is None
    assert other.token.pool_created_at is None
    assert "0xabc" not in by_mint
    assert parse_pair("not a dict", NOW) is None


def test_rpc_mint_account_parsing() -> None:
    plain = parse_mint_account(load("rpc_mint_account.json"))
    assert plain is not None
    assert plain.mint_authority is None and plain.freeze_authority is None
    assert plain.decimals == 6 and plain.supply_raw == 999_999_999_000_000
    assert not plain.is_token_2022
    t22 = parse_mint_account(load("rpc_mint_account_2022.json"))
    assert t22 is not None
    assert t22.is_token_2022
    assert t22.transfer_fee_bps == 500
    assert t22.has_transfer_hook
    assert t22.permanent_delegate is not None
    assert t22.mint_authority is not None
    assert parse_mint_account({"result": {"value": None}}) is None
    assert parse_mint_account({"result": {"value": {"data": "base64junk"}}}) is None


def test_rpc_largest_accounts() -> None:
    dist = parse_largest_accounts(
        load("rpc_largest_accounts.json"), 999_999_999_000_000, ("poolTokenAccount",), NOW
    )
    assert dist is not None
    assert dist.largest_is_pool
    assert dist.largest_pct is not None and abs(dist.largest_pct - 0.12) < 1e-6
    assert dist.top10_pct is not None and abs(dist.top10_pct - 0.18) < 1e-6
    assert parse_largest_accounts({"result": {"value": []}}, 1, (), NOW) is None
    assert (
        parse_largest_accounts({"result": {"value": [{"address": "a", "amount": "5"}]}}, 0, (), NOW)
        is None
    )
