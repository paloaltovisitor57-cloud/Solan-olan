"""Session discovery: paper databases, live databases, the running session, explicit picks."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from solana_sniper.web.session_discovery import (
    discover_sessions,
    paper_db_paths,
    read_heartbeat,
    resolve_selection,
    running_session_id,
)
from tests.web.conftest import LIVE_SID, PAPER_LIVE, PAPER_SYNTH, SeededHome
from tests.web.seed import write_heartbeat


def test_discovers_paper_sessions_with_their_own_databases(seeded_home: SeededHome) -> None:
    hb = read_heartbeat(seeded_home.home)
    refs = discover_sessions(seeded_home.home, heartbeat=hb)
    paper = [r for r in refs if r.kind == "PAPER"]
    assert {r.session_id for r in paper} == {PAPER_LIVE, PAPER_SYNTH}
    by_id = {r.session_id: r for r in paper}
    assert by_id[PAPER_LIVE].db_path == seeded_home.paper_live_db
    assert by_id[PAPER_SYNTH].db_path == seeded_home.paper_synth_db
    assert by_id[PAPER_LIVE].requested == "1 SOL" and by_id[PAPER_SYNTH].requested == "0.5 SOL"
    assert by_id[PAPER_LIVE].running and not by_id[PAPER_SYNTH].running
    assert by_id[PAPER_SYNTH].ended_at is not None and by_id[PAPER_LIVE].ended_at is None
    assert "RUNNING" in by_id[PAPER_LIVE].label and "ended" in by_id[PAPER_SYNTH].label
    # paper sessions come first, newest first
    assert [r.session_id for r in refs[:2]] == [PAPER_LIVE, PAPER_SYNTH]


def test_discovers_live_sessions_from_the_service_database(seeded_home: SeededHome) -> None:
    refs = discover_sessions(seeded_home.home)
    live = [r for r in refs if r.kind == "LIVE"]
    assert len(live) == 1
    assert live[0].session_id == LIVE_SID
    assert live[0].mode == "LIVE"
    assert live[0].db_path == seeded_home.live_db == seeded_home.home / "db" / "sniper.db"
    assert live[0].started_at is not None and live[0].started_at.tzinfo is not None


def test_explicit_paper_selection_routes_to_that_database(seeded_home: SeededHome) -> None:
    refs = discover_sessions(seeded_home.home)
    chosen = resolve_selection(refs, paper=PAPER_SYNTH)
    assert chosen is not None and chosen.db_path == seeded_home.paper_synth_db
    chosen_live = resolve_selection(refs, session=LIVE_SID)
    assert chosen_live is not None and chosen_live.db_path == seeded_home.live_db
    assert resolve_selection(refs, paper="paper-does-not-exist") is None
    assert resolve_selection(refs, session=PAPER_LIVE) is not None  # --session finds paper ids too


def test_default_selection_prefers_the_running_session_then_newest(seeded_home: SeededHome) -> None:
    hb = read_heartbeat(seeded_home.home)
    assert running_session_id(hb) == PAPER_LIVE
    refs = discover_sessions(seeded_home.home, heartbeat=hb)
    assert resolve_selection(refs) is not None
    assert resolve_selection(refs).session_id == PAPER_LIVE  # type: ignore[union-attr]
    # stale heartbeat: nothing is running, newest paper session wins
    write_heartbeat(seeded_home.home, PAPER_LIVE, age_s=600)
    hb_stale = read_heartbeat(seeded_home.home)
    assert hb_stale is not None and not hb_stale.fresh and running_session_id(hb_stale) is None
    refs2 = discover_sessions(seeded_home.home, heartbeat=hb_stale)
    assert not any(r.running for r in refs2)
    assert resolve_selection(refs2).session_id == PAPER_LIVE  # type: ignore[union-attr]


def test_unreadable_files_are_listed_not_fatal(seeded_home: SeededHome, tmp_path: Path) -> None:
    junk = seeded_home.home / "db" / "paper" / "paper-20260101-000000-junk.db"
    junk.write_bytes(b"this is not a sqlite database at all" * 40)
    refs = discover_sessions(seeded_home.home)
    bad = [r for r in refs if r.session_id == "paper-20260101-000000-junk"]
    assert len(bad) == 1 and bad[0].note is not None and bad[0].note.startswith("unreadable")
    assert len([r for r in refs if r.kind == "PAPER"]) == 3
    assert len(paper_db_paths(seeded_home.home)) == 3
    empty_home = tmp_path / "empty"
    assert discover_sessions(empty_home) == []
    assert read_heartbeat(empty_home) is None


def test_heartbeat_is_parsed_and_aged(seeded_home: SeededHome) -> None:
    hb = read_heartbeat(seeded_home.home)
    assert hb is not None and hb.fresh and hb.session_id == PAPER_LIVE
    assert hb.written_at is not None and hb.written_at.tzinfo is not None
    assert hb.age_s is not None and 0 <= hb.age_s < 30
    assert hb.written_at < datetime.now(tz=UTC)
