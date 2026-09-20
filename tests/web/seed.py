"""Builders that write realistic session databases with the *engine's* repository (the writer),
so the read-only dashboard is exercised against exactly the rows the engine produces."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from solana_sniper.app.paper import PaperSession, paper_db_path, paper_db_url
from solana_sniper.config.paths import DB_DIR, STATE_DIR, STATUS_FILE
from solana_sniper.domain.enums import (
    CandidateState,
    CheckVerdict,
    EntryDecision,
    ExecutionProvenance,
    ExitReason,
    FillProvenance,
    MarketDataProvenance,
    SignalKind,
    TokenUnits,
    Venue,
)
from solana_sniper.domain.models import (
    CheckReport,
    CheckResult,
    EntryAttempt,
    EntryScore,
    ErrorRecord,
    FeatureVector,
    Fill,
    MarketSnapshot,
    MilestoneEvent,
    PortfolioSnapshot,
    Position,
    ScoreComponent,
    TokenInfo,
)
from solana_sniper.domain.money import ui_to_raw
from solana_sniper.storage.repository import Repository
from solana_sniper.strategy.outcomes import Outcome

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
MINT_A = "AnaLosMint1111111111111111111111111111111111"
MINT_B = "BonkCatMint111111111111111111111111111111111"
MINT_C = "RugPullMint111111111111111111111111111111111"


def _token(mint: str, symbol: str, *, venue: Venue = Venue.RAYDIUM, age_s: float = 90) -> TokenInfo:
    return TokenInfo(
        mint=mint,
        symbol=symbol,
        name=f"{symbol} token",
        decimals=6,
        created_at=T0 - timedelta(seconds=age_s),
        pool_created_at=T0 - timedelta(seconds=age_s),
        venue=venue,
        pool_address=f"pool-{symbol}",
        quote_mint="So11111111111111111111111111111111111111112",
        source="synthetic" if venue is Venue.SYNTHETIC else "geckoterminal",
        discovered_at=T0 - timedelta(seconds=age_s - 5),
    )


def _features(mint: str, at: datetime, *, stale: bool = False) -> FeatureVector:
    return FeatureVector(
        mint=mint,
        computed_at=at,
        observation_count=12,
        token_age_s=120.0,
        price_native=0.000012,
        liquidity_usd=18_500.0,
        momentum_10s=0.02,
        momentum_30s=0.05,
        momentum_60s=0.11,
        momentum_180s=0.20,
        acceleration=0.01,
        liquidity_growth_60s=0.08,
        liquidity_acceleration=0.0,
        volume_acceleration=0.3,
        trade_velocity_per_min=42.0,
        buy_velocity_per_min=30.0,
        sell_velocity_per_min=12.0,
        buy_sell_imbalance=0.43,
        buy_ratio=0.71,
        unique_trader_growth=0.5,
        holder_growth=0.2,
        drawdown_from_peak=0.03,
        seconds_since_peak=4.0,
        market_depth_usd=9_000.0,
        estimated_slippage_bps=120,
        estimated_exit_slippage_bps=150,
        volatility_60s=0.04,
        data_age_s=0.8,
        stale=stale,
        acceleration_raw=0.012,
    )


def _score(mint: str, at: datetime, score: float) -> EntryScore:
    return EntryScore(
        mint=mint,
        scored_at=at,
        score=score,
        components=(
            ScoreComponent(name="momentum", raw=0.11, normalized=0.6, weight=20, contribution=12),
            ScoreComponent(name="liquidity", raw=18500, normalized=0.5, weight=20, contribution=10),
        ),
        reasons=("momentum strong", "liquidity ok"),
        penalties=("holder concentration",) if score < 65 else (),
    )


def _checks(mint: str, at: datetime, *, reject: bool = False) -> CheckReport:
    results = [
        CheckResult(
            name="mint_authority", verdict=CheckVerdict.PASS, reason="revoked", observed_at=at
        ),
        CheckResult(
            name="freeze_authority", verdict=CheckVerdict.PASS, reason="revoked", observed_at=at
        ),
    ]
    if reject:
        results.append(
            CheckResult(
                name="top_holder",
                verdict=CheckVerdict.REJECT,
                reason="largest holder 41% (max 25%)",
                observed_at=at,
                value="0.41",
                fatal=True,
            )
        )
    return CheckReport(mint=mint, evaluated_at=at, results=tuple(results))


def _snapshot(mint: str, at: datetime, price: str, liquidity: str, source: str) -> MarketSnapshot:
    return MarketSnapshot(
        mint=mint,
        observed_at=at,
        source=source,
        price_native=Decimal(price),
        price_usd=Decimal(price) * 150,
        liquidity_usd=Decimal(liquidity),
        volume_5m_usd=Decimal("2400"),
        buys_5m=30,
        sells_5m=12,
        venue=Venue.SYNTHETIC if source == "synthetic" else Venue.RAYDIUM,
    )


def _attempt(
    mint: str,
    symbol: str,
    at: datetime,
    decision: EntryDecision,
    *,
    number: int = 1,
    reason: str | None = None,
    signal_id: str | None = None,
) -> EntryAttempt:
    a = EntryAttempt(
        attempt_id=f"att-{symbol}-{number}",
        session_id="",
        mint=mint,
        symbol=symbol,
        qualified_at=at,
        qualified_score=72.4,
        latch_until=at + timedelta(seconds=8),
        qualified_features={"momentum_60s": 0.11, "liquidity_usd": 18500.0},
        qualified_checks="PASS",
        attempt_number=number,
        decimals_status="known:6",
        sizing_attempted=True,
        recommended_eur=Decimal("12.50"),
        recommended_sol=Decimal("0.0833"),
        sizing_reason=None,
        quote_attempts=2,
        quote_started_at=at + timedelta(seconds=1),
        quote_finished_at=at + timedelta(seconds=3),
        buy_quote_status="ok",
        sell_quote_status="ok" if decision is EntryDecision.BUY_SIGNAL else "failed",
        quote_error=None
        if decision is EntryDecision.BUY_SIGNAL
        else "sell quote: 429 rate limited api_key=SECRETVALUE123",
        quote_ids=["q-1", "q-2"],
        entry_price_impact_pct=0.8,
        exit_price_impact_pct=1.1,
        round_trip_loss_pct=0.031,
        round_trip_viable=decision is EntryDecision.BUY_SIGNAL,
        post_quote_score=70.1,
        min_score_seen=66.0,
        max_score_seen=74.0,
        evaluations=9,
        hysteresis_holds=2,
        final_decision=decision,
        block_reason=reason,
        signal_id=signal_id,
        completed_at=at + timedelta(seconds=6),
    )
    return a


def _fill(
    mint: str, side: SignalKind, at: datetime, *, provenance: FillProvenance, signal_id: str
) -> Fill:
    qty = Decimal("1000000")
    verified = provenance is FillProvenance.VERIFIED_ONCHAIN
    return Fill(
        fill_id=f"fill-{side}-{signal_id}",
        signal_id=signal_id,
        mint=mint,
        side=side,
        filled_at=at,
        sol_amount=Decimal("0.0833"),
        token_amount_ui=qty,
        token_amount_raw=ui_to_raw(qty, 6, exact=True),
        token_decimals=6,
        eur_amount=Decimal("12.50"),
        sol_eur=Decimal("150"),
        fee_eur=Decimal("0.02"),
        slippage_cost_eur=Decimal("0.10"),
        provenance=provenance,
        simulated=provenance is FillProvenance.SIMULATED,
        reported_tx_signature="sig-user-typed"
        if provenance is FillProvenance.USER_REPORTED
        else None,
        verified_onchain=verified,
        tx_signature=f"5VerifiedOnChain{side}{signal_id[-6:]}" if verified else None,
    )


def _position(
    mint: str, symbol: str, at: datetime, *, closed: bool, provenance: FillProvenance, sid: str
) -> Position:
    p = Position(
        position_id=f"pos-{symbol}",
        mint=mint,
        symbol=symbol,
        opened_at=at,
        entry_price_native=Decimal("0.000012"),
        entry_sol_eur=Decimal("150"),
        quantity_ui=Decimal("1000000"),
        cost_basis_eur=Decimal("12.50"),
        entry_sol=Decimal("0.0833"),
        quantity_raw=ui_to_raw(Decimal("1000000"), 6, exact=True),
        token_decimals=6,
        units=TokenUnits.UI,
        provenance=provenance,
        peak_value_eur=Decimal("15.10"),
        current_value_eur=Decimal("13.80"),
        current_price_native=Decimal("0.0000132"),
        last_valued_at=at + timedelta(seconds=40),
        value_is_executable=True,
        simulated=provenance is FillProvenance.SIMULATED,
        session_id=sid,
        entry_signal_id=f"sig-buy-{symbol}",
    )
    if closed:
        p.state = CandidateState.CLOSED
        p.closed_at = at + timedelta(seconds=90)
        p.exit_value_eur = Decimal("14.20")
        p.exit_reason = ExitReason.TRAILING_PEAK
        p.realized_pnl_eur = Decimal("1.70")
        p.exit_signal_id = f"sig-sell-{symbol}"
    return p


def _portfolio(
    at: datetime, equity: str, cash: str, exposure: str, sid: str, *, dd: float = 0.0
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        at=at,
        cash_eur=Decimal(cash),
        open_exposure_eur=Decimal(exposure),
        open_value_eur=Decimal(exposure),
        equity_eur=Decimal(equity),
        peak_equity_eur=Decimal(equity),
        drawdown_pct=dd,
        realized_pnl_eur=Decimal("1.70"),
        unrealized_pnl_eur=Decimal("1.30"),
        fees_eur=Decimal("0.04"),
        slippage_eur=Decimal("0.20"),
        open_positions=1,
        wins=1,
        losses=0,
        session_id=sid,
    )


def _outcome(
    mint: str,
    symbol: str,
    at: datetime,
    *,
    market: MarketDataProvenance,
    execution: ExecutionProvenance,
    max_multiple: float,
    score: float | None,
    truncated: bool = False,
) -> Outcome:
    return Outcome(
        mint=mint,
        symbol=symbol,
        source="geckoterminal" if market is MarketDataProvenance.LIVE else "synthetic",
        first_seen_at=at,
        finalized_at=at + timedelta(seconds=300),
        horizon_s=300.0,
        observations=40,
        first_price=Decimal("0.00001"),
        max_multiple=max_multiple,
        time_to_peak_s=90.0,
        max_drawdown_from_peak=0.35,
        final_multiple=max(0.4, max_multiple * 0.6),
        qualified=score is not None and score >= 65,
        qualified_multiple=1.4 if score is not None and score >= 65 else None,
        best_score=score,
        signalled=score is not None and score >= 70,
        entered=score is not None and score >= 70,
        closed_pnl_pct=0.13 if score is not None and score >= 70 else None,
        exit_reason="TRAILING_PEAK" if score is not None and score >= 70 else None,
        liquidity_collapsed=max_multiple < 1.0,
        reject_reason=None if score is not None else "top_holder",
        simulated=execution is ExecutionProvenance.SIMULATED,
        truncated=truncated,
        market_data=market,
        execution=execution,
    )


async def seed_session(
    db_url: str,
    session_id: str,
    *,
    mode: str,
    market: MarketDataProvenance = MarketDataProvenance.LIVE,
    paper: PaperSession | None = None,
    fill_provenance: FillProvenance = FillProvenance.SIMULATED,
    snapshots: int = 12,
    end: bool = True,
    dropped: int = 0,
    outcomes: int = 6,
    error_secret: str | None = None,
) -> None:
    """Write a complete, self-consistent session with the engine's writer."""
    if fill_provenance is FillProvenance.SIMULATED:
        execution = ExecutionProvenance.SIMULATED
    elif fill_provenance is FillProvenance.VERIFIED_ONCHAIN:
        execution = ExecutionProvenance.AUTONOMOUS
    else:
        execution = ExecutionProvenance.MANUAL_SIGNAL
    decided_by = {
        ExecutionProvenance.SIMULATED: "DRY_RUN",
        ExecutionProvenance.AUTONOMOUS: "AUTONOMOUS",
    }.get(execution, "HUMAN")
    source = "synthetic" if market is MarketDataProvenance.SYNTHETIC else "dexscreener"
    venue = Venue.SYNTHETIC if market is MarketDataProvenance.SYNTHETIC else Venue.RAYDIUM
    repo = Repository(db_url, session_id=session_id)
    await repo.init()
    repo.start()
    try:
        await repo.start_session(mode, "configs/synthetic.yaml" if source == "synthetic" else None)
        if paper is not None:
            await repo.save_paper_session(paper)
        tokens = [
            (MINT_A, "ANALOS"),
            (MINT_B, "BONKCAT"),
            (MINT_C, "RUGME"),
        ]
        for mint, symbol in tokens:
            await repo.save_token_now(_token(mint, symbol, venue=venue))
        t = T0
        # transitions + telemetry
        for mint, symbol in tokens:
            repo.save_transition(mint, "DISCOVERED", "MONITORING", t, "first snapshot")
            for i in range(6):
                at = t + timedelta(seconds=5 * i)
                repo.save_observation(_snapshot(mint, at, "0.000012", "18500", source))
                repo.save_features(_features(mint, at))
                repo.save_score(_score(mint, at, 58.0 + 3 * i if symbol != "RUGME" else 40.0))
            repo.save_checks(_checks(mint, t + timedelta(seconds=30), reject=symbol == "RUGME"))
        repo.save_transition(
            MINT_A, "MONITORING", "QUALIFIED", t + timedelta(seconds=31), "score 72.4 >= 65"
        )
        repo.save_transition(
            MINT_A, "QUALIFIED", "BUY_SIGNAL", t + timedelta(seconds=36), "round trip viable"
        )
        repo.save_transition(
            MINT_A, "BUY_SIGNAL", "OPEN", t + timedelta(seconds=37), "simulated fill"
        )
        repo.save_transition(
            MINT_B, "MONITORING", "QUALIFIED", t + timedelta(seconds=40), "score 71.0 >= 65"
        )
        repo.save_transition(
            MINT_B, "QUALIFIED", "MONITORING", t + timedelta(seconds=48), "quote failed"
        )
        repo.save_transition(
            MINT_C, "MONITORING", "REJECTED", t + timedelta(seconds=31), "top_holder fatal"
        )
        repo.save_transition(
            MINT_C, "REJECTED", "DATA_STALE", t + timedelta(seconds=60), "no snapshot for 20s"
        )
        # entry attempts
        a1 = _attempt(
            MINT_A,
            "ANALOS",
            t + timedelta(seconds=31),
            EntryDecision.BUY_SIGNAL,
            signal_id="sig-buy-ANALOS",
        )
        a2 = _attempt(
            MINT_B,
            "BONKCAT",
            t + timedelta(seconds=40),
            EntryDecision.QUOTE_FAILED,
            reason="sell quote failed after 2 attempts (jupiter rate limited)",
        )
        a3 = _attempt(
            MINT_B,
            "BONKCAT",
            t + timedelta(seconds=80),
            EntryDecision.ABANDONED,
            number=2,
            reason="score fell to 55.0 below hysteresis floor 57.0",
        )
        for a in (a1, a2, a3):
            a.session_id = session_id
            await repo.save_entry_attempt_now(a)
        # signals: written through the engine's signal writer needs a full BuySignal; store a
        # minimal payload directly the way the row is shaped (kind/status columns + payload)
        from sqlalchemy.ext.asyncio import AsyncSession

        from solana_sniper.storage.models import DecisionRow, SignalRow

        async def signals_op(s: AsyncSession) -> None:
            await s.merge(
                SignalRow(
                    signal_id="sig-buy-ANALOS",
                    session_id=session_id,
                    mint=MINT_A,
                    kind="BUY",
                    created_at=t + timedelta(seconds=36),
                    expires_at=t + timedelta(seconds=66),
                    status="CONFIRMED",
                    payload={
                        "symbol": "ANALOS",
                        "urgency": "NORMAL",
                        "score": {"score": 72.4, "reasons": ["momentum strong", "liquidity ok"]},
                        "sizing": {"recommended_eur": {"__dec__": "12.50"}},
                    },
                )
            )
            await s.merge(
                SignalRow(
                    signal_id="sig-sell-ANALOS",
                    session_id=session_id,
                    mint=MINT_A,
                    kind="SELL",
                    created_at=t + timedelta(seconds=120),
                    expires_at=t + timedelta(seconds=150),
                    status="CONFIRMED",
                    payload={
                        "symbol": "ANALOS",
                        "urgency": "HIGH",
                        "reason": "TRAILING_PEAK",
                        "pnl_pct": 0.136,
                        "detail": "trailing drawdown 8.6% > 8.0%",
                    },
                )
            )
            await s.merge(
                DecisionRow(
                    decision_id="dec-1",
                    session_id=session_id,
                    signal_id="sig-buy-ANALOS",
                    kind="CONFIRM",
                    source=decided_by,
                    decided_at=t + timedelta(seconds=37),
                    note="",
                )
            )

        await repo.persist_now(signals_op, "signal")
        await repo.save_fill_now(
            _fill(
                MINT_A,
                SignalKind.BUY,
                t + timedelta(seconds=37),
                provenance=fill_provenance,
                signal_id="sig-buy-ANALOS",
            )
        )
        await repo.save_fill_now(
            _fill(
                MINT_A,
                SignalKind.SELL,
                t + timedelta(seconds=127),
                provenance=fill_provenance,
                signal_id="sig-sell-ANALOS",
            )
        )
        await repo.save_position_now(
            _position(
                MINT_A,
                "ANALOS",
                t + timedelta(seconds=37),
                closed=True,
                provenance=fill_provenance,
                sid=session_id,
            )
        )
        await repo.save_position_now(
            _position(
                MINT_B,
                "BONKCAT",
                t + timedelta(seconds=200),
                closed=False,
                provenance=fill_provenance,
                sid=session_id,
            )
        )
        start_equity = paper.bankroll_eur if paper is not None else Decimal("150")
        for i in range(snapshots):
            at = t + timedelta(seconds=10 * i)
            equity = (
                start_equity
                + Decimal(i) * Decimal("0.25")
                - (Decimal("1.5") if i == snapshots // 2 else Decimal(0))
            )
            dd = 0.01 if i == snapshots // 2 else 0.0
            repo.save_portfolio_snapshot(
                _portfolio(
                    at, str(equity), str(equity - Decimal("12.5")), "12.5", session_id, dd=dd
                )
            )
        repo.save_milestone(
            MilestoneEvent(
                milestone_eur=Decimal("160"),
                equity_eur=Decimal("160.5"),
                reached_at=t + timedelta(seconds=50),
                direction="UP",
            )
        )
        repo.save_error(
            ErrorRecord(
                at=t + timedelta(seconds=45),
                component="provider.jupiter",
                message="429 rate limited" + (f" token={error_secret}" if error_secret else ""),
                detail="backoff 2s",
                session_id=session_id,
            )
        )
        repo.save_error(
            ErrorRecord(
                at=t + timedelta(seconds=46),
                component="engine",
                message="evaluation failed" + (f" ({error_secret})" if error_secret else ""),
                detail="ValueError: bad decimals",
                session_id=session_id,
            )
        )
        rows: list[Outcome] = []
        for i in range(outcomes):
            mint, symbol = tokens[i % 3]
            rows.append(
                _outcome(
                    mint,
                    symbol,
                    t + timedelta(seconds=i),
                    market=market,
                    execution=execution,
                    max_multiple=[2.4, 1.1, 0.7, 5.2, 1.8, 0.9][i % 6],
                    score=[72.4, 71.0, None, 80.0, 61.0, 45.0][i % 6],
                    truncated=i == outcomes - 1,
                )
            )
        await repo.save_outcomes_now(rows)
        await repo.flush()
        if dropped:
            for _ in range(dropped):
                repo.note_dropped("observation", "test")
        await repo.record_integrity()
        if end:
            await repo.end_session()
        await repo.flush()
    finally:
        await repo.close()


def make_paper_meta(
    home: Path,
    session_id: str,
    *,
    bankroll_sol: str = "1",
    requested: str = "1 SOL",
    fx_source: str = "live",
    name: str | None = None,
) -> PaperSession:
    sol = Decimal(bankroll_sol)
    return PaperSession(
        session_id=session_id,
        name=name,
        created_at=T0,
        requested=requested,
        bankroll_sol=sol,
        bankroll_eur=(sol * Decimal("150")).quantize(Decimal("0.00000001")),
        sol_eur_start=Decimal("150"),
        fx_source=fx_source,
        fx_at=T0,
        config_path=None,
        database_url=paper_db_url(home, session_id),
    )


async def seed_paper(
    home: Path,
    session_id: str,
    *,
    market: MarketDataProvenance = MarketDataProvenance.LIVE,
    bankroll_sol: str = "1",
    requested: str = "1 SOL",
    end: bool = True,
    snapshots: int = 12,
    dropped: int = 0,
    error_secret: str | None = None,
    name: str | None = None,
) -> Path:
    meta = make_paper_meta(
        home, session_id, bankroll_sol=bankroll_sol, requested=requested, name=name
    )
    path = paper_db_path(home, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    await seed_session(
        paper_db_url(home, session_id),
        session_id,
        mode="PAPER",
        market=market,
        paper=meta,
        end=end,
        snapshots=snapshots,
        dropped=dropped,
        error_secret=error_secret,
    )
    return path


async def seed_live(
    home: Path,
    session_id: str,
    *,
    mode: str = "LIVE",
    fill_provenance: FillProvenance = FillProvenance.ESTIMATED,
    end: bool = True,
    db_name: str = "sniper.db",
) -> Path:
    path = home / DB_DIR / db_name
    path.parent.mkdir(parents=True, exist_ok=True)
    await seed_session(
        f"sqlite+aiosqlite:///{path}",
        session_id,
        mode=mode,
        market=MarketDataProvenance.LIVE,
        fill_provenance=fill_provenance,
        end=end,
    )
    return path


def write_heartbeat(
    home: Path,
    session_id: str,
    *,
    age_s: float = 2.0,
    state: str = "HEALTHY",
    extra: dict[str, Any] | None = None,
    stopped: bool = False,
) -> Path:
    path = home / STATE_DIR / STATUS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    written = datetime.now(tz=UTC) - timedelta(seconds=age_s)
    status: dict[str, Any] = {
        "written_at": written.isoformat(),
        "pid": 4242,
        "hostname": "mac-mini",
        "starts": 3,
        "previous_exit": "clean",
        "mode": "PAPER",
        "market_data_provenance": "LIVE",
        "execution_provenance": "SIMULATED",
        "session_id": session_id,
        "started_at": (written - timedelta(seconds=600)).isoformat(),
        "uptime_s": 600.0,
        "state": state,
        "healthy": state == "HEALTHY",
        "problems": [] if state != "UNHEALTHY" else ["engine not ticking"],
        "degraded": [] if state == "HEALTHY" else ["provider geckoterminal rate_limited"],
        "providers": {
            "geckoterminal": {
                "host": "api.geckoterminal.com",
                "state": "RATE_LIMITED",
                "cooldown_s": 4.0,
                "inflight": 0,
                "waiting": 1,
                "consecutive_failures": 2,
                "circuit_trips": 0,
                "last_error": "429 api_key=SECRETVALUE123",
                "requests": 120,
                "rate_limited": 3,
                "retries": 5,
                "backoffs": 3,
                "fast_fails": 0,
                "failures": 2,
                "recoveries": 1,
            },
            "jupiter": {
                "host": "quote-api.jup.ag",
                "state": "HEALTHY",
                "cooldown_s": 0.0,
                "inflight": 0,
                "waiting": 0,
                "consecutive_failures": 0,
                "circuit_trips": 0,
                "last_error": None,
                "requests": 40,
                "rate_limited": 0,
                "retries": 0,
                "backoffs": 0,
                "fast_fails": 0,
                "failures": 0,
                "recoveries": 0,
            },
        },
        "records_verified_onchain": False,
        "engine": {
            "last_tick_age_s": 0.3,
            "tick_p50_ms": 4.0,
            "tick_p95_ms": 11.0,
            "evaluations": 900,
        },
        "connections": {
            "pumpportal": {"kind": "websocket", "connected": True, "last_activity_age_s": 1.2}
        },
        "market_data": {
            "ok": True,
            "watched": 14,
            "last_snapshot_age_s": 0.9,
            "snapshots_total": 5000,
            "trades_total": 300,
            "snapshots_last_minute": 120,
            "discovery_rate_per_s": 0.4,
        },
        "database": {
            "url": "sqlite+aiosqlite:///x.db",
            "ok": True,
            "last_flush_age_s": 0.4,
            "last_error": None,
            "failures": 0,
            "dropped": 0,
            "queued": 3,
            "integrity": {
                "complete": True,
                "dropped_total": 0,
                "failed_total": 0,
                "dropped_by_kind": {},
                "failed_by_kind": {},
                "queued": 3,
                "telemetry_queued": 3,
                "telemetry_budget": 20000,
                "last_flush_error": None,
            },
        },
        "tokens_monitored": 14,
        "qualified": 1,
        "pending_signals": 0,
        "open_positions": [],
        "last_signal_at": None,
        "last_error": {
            "at": written.isoformat(),
            "component": "provider.jupiter",
            "message": "429 api_key=SECRETVALUE123",
        },
        "counters": {
            "discovered": 40,
            "rejected": 20,
            "qualified_total": 3,
            "signals": 1,
            "confirmed": 1,
            "cancelled": 0,
            "exits": 1,
            "outcomes_following": 12,
            "outcomes_finalized": 6,
        },
        "portfolio": {"equity_eur": "152.75"},
        "fx": {"sol_eur": "150", "live": True},
    }
    if stopped:
        status.update(
            {"healthy": False, "state": "STOPPED", "stopped": True, "stop_reason": "SIGTERM"}
        )
    if extra:
        status.update(extra)
    path.write_text(json.dumps(status, indent=2), encoding="utf-8")
    return path


__all__ = [
    "MINT_A",
    "MINT_B",
    "MINT_C",
    "T0",
    "make_paper_meta",
    "replace",
    "seed_live",
    "seed_paper",
    "seed_session",
    "write_heartbeat",
]
