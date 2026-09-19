"""The Streamlit pages under Streamlit's test harness: provenance banners, every page renders
with data and without, session switching, busy state, secrets, phone-friendly overview."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from solana_sniper.telemetry.redaction import register_secret, registry
from solana_sniper.web import data as data_module
from tests.web.conftest import (
    LIVE_SID,
    PAPER_LIVE,
    PAPER_SYNTH,
    SECRET,
    SeededHome,
    all_text,
    goto,
    main_text,
    run_app,
)
from tests.web.seed import seed_paper, write_heartbeat

PAGES = (
    "Overview",
    "Equity",
    "Positions",
    "Candidates",
    "Entry attempts",
    "Signals",
    "Fills",
    "Token",
    "Outcomes",
    "Providers",
    "Engine",
    "Events",
)


def test_paper_session_shows_paper_live_simulated_disabled_header(
    seeded_home: SeededHome, web_env: Callable[..., None]
) -> None:
    web_env(seeded_home.home)
    at = run_app()
    assert not at.exception
    text = all_text(at)
    assert "PAPER" in text and "MARKET DATA:" in text and "LIVE" in text
    assert "EXECUTION:" in text and "SIMULATED" in text
    assert "REAL TRANSACTIONS:" in text and "DISABLED" in text
    assert "SYNTHETIC" not in text  # a live-data paper run is never called synthetic
    assert PAPER_LIVE in text and "RUNNING" in text
    assert "READ-ONLY" in text and "NO SIGNING" in text and "NO BROADCAST" in text
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Equity"] == "€152.75" and metrics["Return"] == "+1.83%"
    assert metrics["Open positions"] == "1" and metrics["Entry attempts"] == "3"
    assert "1.0000 SOL" in text and "€150.00" in text and "live rate at start" in text


def test_synthetic_paper_session_is_labelled_test(
    seeded_home: SeededHome, web_env: Callable[..., None]
) -> None:
    web_env(seeded_home.home, paper=PAPER_SYNTH)
    at = run_app()
    assert not at.exception
    text = all_text(at)
    assert "PAPER / TEST" in text and "MARKET DATA:" in text and "SYNTHETIC" in text
    assert "REAL TRANSACTIONS:" in text and "DISABLED" in text
    assert at.sidebar.selectbox[0].value == PAPER_SYNTH


def test_live_session_shows_signal_mode_header(
    seeded_home: SeededHome, web_env: Callable[..., None]
) -> None:
    web_env(seeded_home.home, session=LIVE_SID)
    at = run_app()
    assert not at.exception
    text = all_text(at)
    assert "LIVE / SIGNAL MODE" in text
    assert "MANUAL SIGNAL / ESTIMATED / USER-REPORTED" in text
    assert "NOT RECONCILED ON-CHAIN" in text
    assert "nothing here was signed, broadcast or reconciled on-chain" in text
    assert at.sidebar.radio[0].value == "LIVE"
    goto(at, "Fills")
    frame = at.dataframe[0].value
    assert set(frame["provenance"]) == {"ESTIMATED"}
    assert set(frame["kind"]) == {"estimated / user-reported"}
    assert set(frame["verified on-chain"]) == {"no"}


def test_session_selector_is_organised_by_mode_and_switches_databases(
    seeded_home: SeededHome, web_env: Callable[..., None]
) -> None:
    web_env(seeded_home.home)
    at = run_app()
    radio = at.sidebar.radio[0]
    assert list(radio.options) == ["PAPER sessions", "LIVE / DRY-RUN sessions"]
    assert radio.value == "PAPER"
    sel = at.sidebar.selectbox[0]
    assert [str(o).split(" · ")[0] for o in sel.options] == [PAPER_LIVE, PAPER_SYNTH]
    assert "RUNNING" in str(sel.options[0]) and "ended" in str(sel.options[1])
    assert sel.value == PAPER_LIVE
    at.sidebar.radio[0].set_value("LIVE").run()
    assert not at.exception
    assert [str(o).split(" · ")[0] for o in at.sidebar.selectbox[0].options] == [LIVE_SID]
    assert "LIVE / SIGNAL MODE" in all_text(at)
    at.sidebar.radio[0].set_value("PAPER").run()
    assert not at.exception and at.sidebar.selectbox[0].value == PAPER_LIVE


def test_switching_sessions_never_leaks_the_previous_session(
    seeded_home: SeededHome, web_env: Callable[..., None]
) -> None:
    web_env(seeded_home.home)
    at = run_app()
    first = {m.label: m.value for m in at.metric}
    assert first["Equity"] == "€152.75"
    at.sidebar.selectbox[0].set_value(PAPER_SYNTH).run()
    assert not at.exception
    second = {m.label: m.value for m in at.metric}
    assert second["Equity"] == "€77.75"  # 0.5 SOL bankroll = €75 + 11 × €0.25
    text = main_text(at)
    assert PAPER_SYNTH in text and PAPER_LIVE not in text
    assert "PAPER / TEST" in text and "RUNNING" not in text
    for page in ("Entry attempts", "Engine", "Events", "Fills"):
        goto(at, page)
        assert not at.exception
        assert PAPER_LIVE not in main_text(at).replace(
            f"heartbeat belongs to session {PAPER_LIVE}", ""
        ), page


def test_every_page_renders_with_data(
    seeded_home: SeededHome, web_env: Callable[..., None]
) -> None:
    web_env(seeded_home.home)
    at = run_app()
    for page in PAGES:
        goto(at, page)
        assert not at.exception, (page, [e.value for e in at.exception])
        assert not at.error, (page, [e.value for e in at.error])


def test_positions_page_distinguishes_executable_and_estimated_value(
    seeded_home: SeededHome, web_env: Callable[..., None]
) -> None:
    web_env(seeded_home.home)
    at = goto(run_app(), "Positions")
    assert not at.exception
    open_frame = at.dataframe[0].value
    assert list(open_frame["symbol"]) == ["BONKCAT"] and list(open_frame["state"]) == ["OPEN"]
    assert list(open_frame["value basis"]) == ["executable quote"]
    assert list(open_frame["provenance"]) == ["SIMULATED"]
    closed_frame = at.dataframe[1].value
    assert list(closed_frame["symbol"]) == ["ANALOS"] and list(closed_frame["exit"]) == [
        "TRAILING_PEAK"
    ]
    text = all_text(at)
    assert "VERIFIED ON-CHAIN: NO" in text and "No position is verified on-chain" in text
    assert len(at.expander) == 2


def test_entry_attempts_page_shows_decisions_and_forensics(
    seeded_home: SeededHome, web_env: Callable[..., None]
) -> None:
    web_env(seeded_home.home)
    at = goto(run_app(), "Entry attempts")
    assert not at.exception
    text = all_text(at)
    for decision in ("BUY_SIGNAL", "QUOTE_FAILED", "ABANDONED"):
        assert decision in text
    frame = at.dataframe[0].value
    assert list(frame["decision"]) == ["ABANDONED", "QUOTE_FAILED", "BUY_SIGNAL"]
    assert "score fell to 55.0 below hysteresis floor 57.0" in text
    assert "sell quote failed after 2 attempts" in text
    assert "hysteresis holds 2" in text and "decimals known:6" in text
    assert "3.1% estimated loss" in text and "€12.50 (0.0833 SOL)" in text
    labels = [str(e.label) for e in at.expander]
    assert len(labels) == 3 and labels[0].startswith("BONKCAT · #2 · ABANDONED")
    at.selectbox(key="attempt_decision").select("BUY_SIGNAL").run()
    assert not at.exception
    assert next(str(e.label) for e in at.expander).startswith("ANALOS · #1 · BUY_SIGNAL")
    assert len(at.expander) == 1


def test_candidates_page_shows_states_scores_and_gate_reasons(
    seeded_home: SeededHome, web_env: Callable[..., None]
) -> None:
    web_env(seeded_home.home)
    at = goto(run_app(), "Candidates")
    assert not at.exception
    frame = at.dataframe[0].value
    assert set(frame["symbol"]) == {"ANALOS", "BONKCAT", "RUGME"}
    rug = frame[frame["symbol"] == "RUGME"].iloc[0]
    assert rug["state"] == "DATA_STALE" and "largest holder 41%" in rug["gate reason"]
    assert rug["score"] == 40.0 and rug["checks"] == "REJECT"
    bonk = frame[frame["symbol"] == "BONKCAT"].iloc[0]
    assert bonk["state"] == "MONITORING" and bonk["gate reason"] == "quote failed"
    assert bonk["liquidity $"] == 18500.0 and bonk["vol 5m $"] == 2400.0
    assert bonk["buys/sells 5m"] == "30/12" and bonk["stale"] == False  # noqa: E712
    text = all_text(at)
    assert "OPEN" in text  # ANALOS is highlighted as active


def test_signals_are_display_only(seeded_home: SeededHome, web_env: Callable[..., None]) -> None:
    web_env(seeded_home.home)
    at = goto(run_app(), "Signals")
    assert not at.exception
    assert "Display only" in all_text(at)
    frame = at.dataframe[0].value
    assert list(frame["kind"]) == ["SELL", "BUY"] and list(frame["decision"]) == [
        "",
        "CONFIRM by DRY_RUN",
    ]
    assert not at.button and not at.text_input


def test_token_inspector(seeded_home: SeededHome, web_env: Callable[..., None]) -> None:
    web_env(seeded_home.home)
    at = goto(run_app(), "Token")
    assert "type a mint address or a symbol" in all_text(at)
    at.text_input(key="token_query").set_value("BONK").run()
    assert not at.exception
    text = all_text(at)
    assert "BONKCAT" in text and "known" not in text.split("decimals")[0][-5:]
    assert "decimals" in text and "6" in text
    labels = [str(t.label) for t in at.tabs]
    for name in (
        "Timeline",
        "Scores",
        "Features",
        "Checks",
        "Quotes",
        "Attempts",
        "Outcomes",
        "Price",
    ):
        assert name in labels
    timeline = at.dataframe[0].value
    assert list(timeline["to"]) == ["MONITORING", "QUALIFIED", "MONITORING"]
    assert "QUOTE_FAILED" in text and "ABANDONED" in text
    at.text_input(key="token_query").set_value("NoSuchMintXYZ").run()
    assert not at.exception and "no token recorded" in all_text(at)


def test_outcomes_page_keeps_every_warning_and_makes_no_profit_claim(
    seeded_home: SeededHome, web_env: Callable[..., None]
) -> None:
    web_env(seeded_home.home)
    at = goto(run_app(), "Outcomes")
    assert not at.exception
    text = all_text(at)
    assert "forward outcomes: 5 tokens, horizon 300s" in text
    assert "95% Wilson intervals" in text and "Past outcomes do not predict future ones" in text
    assert "no claim of profitability" in text
    assert "live Solana market observations with simulated execution" in text
    frame = at.dataframe[0].value
    assert "all followed" in list(frame["group"]) and ">=2x" in frame.columns
    assert all(v.startswith("no (n < 30)") for v in frame["meaningful"])
    assert "[" in frame.iloc[0][">=2x"] and "–" in frame.iloc[0][">=2x"]
    at.checkbox(key="oc_trunc").check().run()
    assert not at.exception and "forward outcomes: 6 tokens" in all_text(at)
    # synthetic session: the synthetic warning
    at.sidebar.selectbox[0].set_value(PAPER_SYNTH).run()
    assert "Every row here is from the synthetic world" in all_text(at)


def test_provider_health_and_engine_pages(
    seeded_home: SeededHome, web_env: Callable[..., None]
) -> None:
    web_env(seeded_home.home)
    at = goto(run_app(), "Providers")
    assert not at.exception
    text = all_text(at)
    assert "geckoterminal" in text and "RATE_LIMITED" in text and "jupiter" in text
    metrics = [m for m in at.metric if m.label == "Rate limited"]
    assert {m.value for m in metrics} == {"3", "0"}
    assert SECRET not in text and "api_key=***" in text
    goto(at, "Engine")
    assert not at.exception
    text = all_text(at)
    assert "HEALTHY" in text and "FRESH" in text and "never (no reconciliation exists)" in text
    labels = {m.label for m in at.metric}
    assert {"Uptime", "Last tick", "Storage queued", "Storage dropped", "Watched tokens"} <= labels
    assert "pumpportal" in text and "connected" in text
    # a session without a fresh heartbeat says so instead of showing stale numbers
    at.sidebar.selectbox[0].set_value(PAPER_SYNTH).run()
    text = all_text(at)
    assert "heartbeat belongs to session" in text and PAPER_LIVE in text
    goto(at, "Providers")
    assert "provider state is unknown" in all_text(at)


def test_integrity_warning_renders(
    isolated_runtime_home: Path, web_env: Callable[..., None]
) -> None:
    sid = "paper-20260918-080000-dropped"
    asyncio.run(seed_paper(isolated_runtime_home, sid, dropped=7))
    web_env(isolated_runtime_home)
    at = run_app()
    assert not at.exception
    errors = [e.value for e in at.error]
    assert any("DATA INTEGRITY COMPROMISED" in e and "dropped 7 rows" in e for e in errors)
    goto(at, "Outcomes")
    assert any("INCOMPLETE DATA" in e.value for e in at.error)
    goto(at, "Engine")
    assert "complete" in all_text(at) and "NO" in all_text(at)


def test_empty_session_renders_empty_states(
    isolated_runtime_home: Path, web_env: Callable[..., None]
) -> None:
    from solana_sniper.storage.repository import Repository
    from tests.web.seed import make_paper_meta

    sid = "paper-20260918-070000-empty"
    home = isolated_runtime_home

    async def build() -> None:
        meta = make_paper_meta(home, sid)
        (home / "db" / "paper").mkdir(parents=True, exist_ok=True)
        repo = Repository(meta.database_url, session_id=sid)
        await repo.init()
        await repo.start_session("PAPER", None)
        await repo.save_paper_session(meta)
        await repo.close()

    asyncio.run(build())
    web_env(home)
    at = run_app()
    assert not at.exception and not at.error
    text = all_text(at)
    assert "NOT RUNNING" in text and "MARKET DATA:" in text and "NOT YET OBSERVED" in text
    assert "no token has qualified yet" in text
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Equity"] == "—" and metrics["Open positions"] == "0"
    for page in PAGES:
        goto(at, page)
        assert not at.exception and not at.error, page
    goto(at, "Outcomes")
    assert "no usable outcomes (0 rows recorded" in all_text(at)
    goto(at, "Entry attempts")
    assert "no entry attempts recorded" in all_text(at)


def test_no_sessions_at_all_is_a_friendly_message(
    isolated_runtime_home: Path, web_env: Callable[..., None]
) -> None:
    web_env(isolated_runtime_home)
    at = run_app()
    assert not at.exception
    assert any("No sessions found" in i.value for i in at.info)


def test_busy_database_shows_retry_state_and_keeps_last_good_data(
    seeded_home: SeededHome, web_env: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    web_env(seeded_home.home)
    at = run_app()
    assert {m.label: m.value for m in at.metric}["Equity"] == "€152.75"
    monkeypatch.setattr(data_module, "_RETRY_DELAYS_S", (0.0, 0.0, 0.0))

    def always_busy(path: Path, timeout: float) -> sqlite3.Connection:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(data_module, "_connect", always_busy)
    import streamlit as st

    st.cache_data.clear()
    at.run()
    assert not at.exception
    assert any("Database busy — retrying" in w.value for w in at.warning)
    # the previous good summary is still on screen instead of a blank page
    assert {m.label: m.value for m in at.metric}["Equity"] == "€152.75"
    goto(at, "Fills")
    assert not at.exception and any("Database busy" in w.value for w in at.warning)


def test_unsupported_schema_is_a_clean_message(
    isolated_runtime_home: Path, web_env: Callable[..., None]
) -> None:
    from tests.web.test_data_layer import _old_schema_db

    home = isolated_runtime_home
    old = home / "db" / "paper" / "paper-20260918-060000-old.db"
    old.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(_old_schema_db(old))
    web_env(home)
    at = run_app()
    assert not at.exception
    assert any(
        "Unsupported database schema" in e.value and "solana-sniper migrate" in e.value
        for e in at.error
    )
    with sqlite3.connect(old) as c:
        assert c.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 3


def test_secrets_never_reach_the_page(
    seeded_home: SeededHome, web_env: Callable[..., None]
) -> None:
    register_secret(SECRET)
    try:
        web_env(seeded_home.home)
        at = run_app()
        for page in PAGES:
            goto(at, page)
            if page == "Token":
                at.text_input(key="token_query").set_value("BONK").run()
            assert not at.exception
            assert SECRET not in all_text(at), page
    finally:
        registry.clear()


def test_overview_is_metric_cards_not_wide_tables(
    seeded_home: SeededHome, web_env: Callable[..., None]
) -> None:
    web_env(seeded_home.home)
    at = run_app()
    assert len(at.metric) >= 12
    assert len(at.dataframe) == 0  # the phone view is cards and stacked lines
    assert not at.button and not at.text_input


def test_stopped_heartbeat_reports_not_running(
    seeded_home: SeededHome, web_env: Callable[..., None]
) -> None:
    write_heartbeat(seeded_home.home, PAPER_LIVE, stopped=True)
    web_env(seeded_home.home)
    at = run_app()
    text = all_text(at)
    assert "NOT RUNNING" in text and "stopped: SIGTERM" in text
