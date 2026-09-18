"""Paper sessions: bankroll conversion at a captured FX rate, isolation, naming, report lines."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from solana_sniper.app.paper import (
    BankrollRequest,
    PaperSession,
    PaperSetupError,
    SessionReport,
    make_session_id,
    paper_db_path,
    paper_db_url,
    resolve_bankroll,
)

T0 = datetime(2026, 9, 18, 13, 25, 0, tzinfo=UTC)


class FakeFx:
    def __init__(self, rate: Decimal, live: bool) -> None:
        self._rate = rate
        self._live = live
        self.refreshes = 0

    async def refresh(self) -> None:
        self.refreshes += 1

    def sol_eur(self) -> Decimal:
        return self._rate

    def usd_eur(self) -> Decimal:
        return Decimal("0.9")

    def sol_usd_cached(self) -> Decimal | None:
        return None

    @property
    def is_live(self) -> bool:
        return self._live


async def test_bankroll_in_sol_is_converted_once_at_the_live_rate() -> None:
    fx = FakeFx(Decimal("92.47"), live=True)
    sol, eur, rate, source = await resolve_bankroll(
        fx, BankrollRequest(sol=Decimal(1)), allow_fallback_fx=False, now=T0
    )
    assert (sol, eur, rate, source) == (Decimal(1), Decimal("92.47"), Decimal("92.47"), "live")
    assert fx.refreshes == 1
    sol2, eur2, _, _ = await resolve_bankroll(
        fx, BankrollRequest(sol=Decimal("2.5")), allow_fallback_fx=False, now=T0
    )
    assert sol2 == Decimal("2.5") and eur2 == Decimal("231.175")


async def test_bankroll_in_eur_records_the_sol_equivalent() -> None:
    fx = FakeFx(Decimal("100"), live=True)
    sol, eur, _rate, source = await resolve_bankroll(
        fx, BankrollRequest(eur=Decimal(100)), allow_fallback_fx=False, now=T0
    )
    assert eur == Decimal(100) and sol == Decimal(1) and source == "live"
    sol3, _, _, _ = await resolve_bankroll(
        fx, BankrollRequest(eur=Decimal("33.33")), allow_fallback_fx=False, now=T0
    )
    assert sol3 == Decimal("0.3333")


async def test_without_live_fx_the_session_refuses_unless_fallback_allowed() -> None:
    fx = FakeFx(Decimal("150"), live=False)
    with pytest.raises(PaperSetupError, match="live SOL/EUR"):
        await resolve_bankroll(fx, BankrollRequest(sol=Decimal(1)), allow_fallback_fx=False, now=T0)
    _sol, eur, _rate, source = await resolve_bankroll(
        fx, BankrollRequest(sol=Decimal(1)), allow_fallback_fx=True, now=T0
    )
    assert (eur, source) == (Decimal(150), "fallback")


def test_bankroll_request_validation() -> None:
    with pytest.raises(PaperSetupError, match="exactly one"):
        BankrollRequest().validate()
    with pytest.raises(PaperSetupError, match="exactly one"):
        BankrollRequest(sol=Decimal(1), eur=Decimal(1)).validate()
    with pytest.raises(PaperSetupError, match="positive"):
        BankrollRequest(sol=Decimal("-1")).validate()
    with pytest.raises(PaperSetupError, match="positive"):
        BankrollRequest(eur=Decimal("NaN")).validate()
    assert BankrollRequest(sol=Decimal("1.0")).label == "1 SOL"
    assert BankrollRequest(eur=Decimal("100.50")).tag == "100.5eur"


def test_session_ids_are_unique_isolated_and_named(tmp_path: Path) -> None:
    req = BankrollRequest(sol=Decimal(1))
    a = make_session_id(req, None, T0)
    b = make_session_id(req, None, T0)
    assert a != b and a.startswith("paper-20260918-132500-1sol-")
    named = make_session_id(req, "test-a", T0)
    assert named == "paper-20260918-132500-test-a"
    with pytest.raises(PaperSetupError):
        make_session_id(req, "bad name/with slash", T0)
    assert paper_db_path(tmp_path, a) == tmp_path / "db" / "paper" / f"{a}.db"
    assert paper_db_url(tmp_path, a).endswith(f"/db/paper/{a}.db")
    assert paper_db_path(tmp_path, a) != paper_db_path(tmp_path, b)  # never the same file


def test_paper_session_payload_roundtrip() -> None:
    meta = PaperSession(
        session_id="paper-x",
        name=None,
        created_at=T0,
        requested="1 SOL",
        bankroll_sol=Decimal(1),
        bankroll_eur=Decimal("92.47"),
        sol_eur_start=Decimal("92.47"),
        fx_source="live",
        fx_at=T0,
        config_path=None,
        database_url="sqlite+aiosqlite:///x.db",
    )
    assert PaperSession.from_payload(meta.to_payload()) == meta and meta.fx_is_live


def _report(**over: object) -> SessionReport:
    base = SessionReport(
        session_id="paper-x",
        started_label="1.0000 SOL (€92.47 at €92.47/SOL, live rate)",
        finished_equity_eur=Decimal("118.31"),
        sol_equivalent=Decimal("1.2794"),
        sol_eur_now=Decimal("92.47"),
        fx_now_live=True,
        return_pct=0.2795,
        max_drawdown_pct=0.182,
        trades=7,
        wins=4,
        losses=3,
        realized_pnl_eur=Decimal("14.20"),
        unrealized_pnl_eur=Decimal("11.64"),
        fees_eur=Decimal("1.50"),
        slippage_eur=Decimal("1.41"),
        open_positions=1,
        signals=12,
        storage_failures=0,
        dropped_writes=0,
        dropped_by_kind={},
        provider_outages=2,
        rate_limits_handled=11,
        degraded_checks=3,
        data_complete=True,
        run_time_s=3 * 3600 + 42 * 60 + 18,
        stop_reason="Ctrl+C / signal",
    )
    return (
        SessionReport(**{**base.__dict__, **over}) if not base.__slots__ else _replace(base, over)
    )


def _replace(base: SessionReport, over: dict[str, object]) -> SessionReport:
    from dataclasses import replace

    return replace(base, **over)  # type: ignore[arg-type]


def test_session_report_lines_are_complete_and_honest() -> None:
    lines = _report().lines()
    text = "\n".join(lines)
    assert lines[0] == "SESSION COMPLETE"
    for needle in (
        "Started:           1.0000 SOL (€92.47 at €92.47/SOL, live rate)",
        "Finished:          €118.31",
        "SOL equivalent:    1.2794 SOL (at current €92.47)",
        "Return:            +27.95%",
        "Maximum drawdown:  -18.2%",
        "Trades:            7",
        "Wins:              4",
        "Losses:            3",
        "Realized P&L:      +€14.20",
        "Unrealized P&L:    +€11.64",
        "Fees/slippage:     €2.91",
        "Storage errors:    0",
        "Dropped writes:    0",
        "Provider outages:  2 circuit trips",
        "Rate limits:       11 handled, 3 checks degraded",
        "Run time:          03:42:18",
        "Session:           paper-x",
        "Real transactions: DISABLED",
    ):
        assert needle in text, needle
    assert "WARNING" not in text
    bad = _report(data_complete=False, dropped_writes=5, dropped_by_kind={"observation": 5})
    bad_text = "\n".join(bad.lines())
    assert bad.lines()[0].startswith("SESSION COMPLETE  (DATA INTEGRITY COMPROMISED)")
    assert "WARNING" in bad.lines()[1] and "Dropped writes:    5 (observation=5)" in bad_text
    loss = _report(realized_pnl_eur=Decimal("-3.5"), fx_now_live=False)
    assert "Realized P&L:      -€3.50" in "\n".join(loss.lines())
    assert "fallback rate" in "\n".join(loss.lines())
