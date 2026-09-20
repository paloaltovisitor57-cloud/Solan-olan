"""Safety rails: every cap blocks, the loss limit disarms, exits continue, KILL stops all."""

from __future__ import annotations

from solana_sniper.wallet.rails import Exposure, RailsConfig, SafetyRails, Verdict

SOL = 1_000_000_000


def rails(**over: object) -> SafetyRails:
    base: dict[str, object] = {
        "max_trade_lamports": SOL // 20,  # 0.05
        "max_daily_spend_lamports": SOL // 2,  # 0.5
        "max_total_loss_lamports": SOL // 4,  # 0.25
        "max_open_positions": 2,
        "reserve_lamports": SOL // 50,  # 0.02
        "max_slippage_bps": 300,
        "max_price_impact_pct": 3.0,
        "max_priority_fee_lamports": 1_000_000,
        "exits_continue_when_disarmed": True,
    }
    base.update(over)
    return SafetyRails(RailsConfig(**base))  # type: ignore[arg-type]


def exposure(**over: object) -> Exposure:
    base: dict[str, object] = {
        "armed": True,
        "kill": False,
        "wallet_lamports": SOL,
        "open_positions": 0,
        "spent_today_lamports": 0,
        "loss_lamports": 0,
    }
    base.update(over)
    return Exposure(**base)  # type: ignore[arg-type]


def buy(r: SafetyRails, e: Exposure, spend: int = SOL // 20, **quote: object) -> Verdict:
    q: dict[str, object] = {
        "slippage_bps": 250,
        "price_impact_pct": 1.0,
        "priority_fee_lamports": 200_000,
    }
    q.update(quote)
    return r.check_buy(e, spend_lamports=spend, **q)  # type: ignore[arg-type]


def test_a_normal_buy_is_allowed() -> None:
    assert buy(rails(), exposure()).ok


def test_every_entry_cap_blocks_with_a_reason() -> None:
    r = rails()
    assert "KILL" in buy(r, exposure(kill=True)).reason
    assert "not armed" in buy(r, exposure(armed=False)).reason
    assert "nothing to spend" in buy(r, exposure(), spend=0).reason
    assert "per-trade cap" in buy(r, exposure(), spend=SOL // 10).reason
    assert "reserve" in buy(r, exposure(wallet_lamports=SOL // 20 + 1000)).reason
    assert "daily spend cap" in buy(r, exposure(spent_today_lamports=SOL // 2 - 1000)).reason
    assert "open positions (max 2)" in buy(r, exposure(open_positions=2)).reason
    assert "slippage" in buy(r, exposure(), slippage_bps=301).reason
    assert "price impact" in buy(r, exposure(), price_impact_pct=3.5).reason
    assert "priority fee" in buy(r, exposure(), priority_fee_lamports=2_000_000).reason
    assert "no total loss limit" in buy(rails(max_total_loss_lamports=None), exposure()).reason


def test_loss_limit_blocks_buys_and_asks_to_disarm() -> None:
    r = rails()
    v = buy(r, exposure(loss_lamports=SOL // 4))
    assert not v.ok and v.disarm and "total loss limit reached" in v.reason
    assert r.loss_limit_reached(SOL // 4) and not r.loss_limit_reached(SOL // 4 - 1)
    assert SafetyRails.loss_lamports(SOL, SOL // 2, SOL // 4) == SOL // 4
    # the loss check comes before the armed check: a disarmed wallet over the limit still says why
    v2 = buy(r, exposure(armed=False, loss_lamports=SOL))
    assert v2.disarm


def test_exits_continue_when_disarmed_but_never_under_kill() -> None:
    r = rails()
    ok = r.check_sell(
        exposure(armed=False), slippage_bps=300, price_impact_pct=2.0, priority_fee_lamports=1
    )
    assert ok.ok
    frozen = rails(exits_continue_when_disarmed=False).check_sell(
        exposure(armed=False), slippage_bps=1, price_impact_pct=0.1, priority_fee_lamports=1
    )
    assert not frozen.ok and "frozen" in frozen.reason
    killed = r.check_sell(
        exposure(kill=True), slippage_bps=1, price_impact_pct=0.1, priority_fee_lamports=1
    )
    assert not killed.ok and "KILL" in killed.reason
    bad_quote = r.check_sell(
        exposure(), slippage_bps=1, price_impact_pct=9.0, priority_fee_lamports=1
    )
    assert not bad_quote.ok and "price impact" in bad_quote.reason


def test_clamp_spend_respects_cap_and_reserve() -> None:
    r = rails()
    assert r.clamp_spend(SOL, SOL) == SOL // 20  # per-trade cap
    assert r.clamp_spend(SOL // 100, SOL) == SOL // 100  # recommendation below the cap
    assert r.clamp_spend(SOL // 20, SOL // 20) == SOL // 20 - SOL // 50  # reserve kept
    assert r.clamp_spend(SOL // 20, SOL // 100) == 0  # nothing above the reserve
