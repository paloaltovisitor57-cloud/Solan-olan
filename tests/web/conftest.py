"""Fixtures for the read-only web dashboard tests: seeded runtime homes and AppTest runners."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from solana_sniper.domain.enums import MarketDataProvenance
from tests.web.seed import seed_live, seed_paper, write_heartbeat

APP_PATH = Path(__file__).resolve().parents[2] / "src" / "solana_sniper" / "web" / "app.py"
PAPER_LIVE = "paper-20260918-120000-1sol-abc123"
PAPER_SYNTH = "paper-20260918-110000-half-synth"
LIVE_SID = "live-20260918-100000-aaaaaa"
SECRET = "SECRETVALUE123"


@dataclass(frozen=True, slots=True)
class SeededHome:
    home: Path
    paper_live_db: Path
    paper_synth_db: Path
    live_db: Path


@pytest.fixture
def seeded_home(isolated_runtime_home: Path) -> SeededHome:
    home = isolated_runtime_home

    async def build() -> SeededHome:
        p3 = await seed_live(home, LIVE_SID)
        p2 = await seed_paper(
            home,
            PAPER_SYNTH,
            market=MarketDataProvenance.SYNTHETIC,
            bankroll_sol="0.5",
            requested="0.5 SOL",
        )
        p1 = await seed_paper(home, PAPER_LIVE, end=False, error_secret=SECRET)
        return SeededHome(home, p1, p2, p3)

    seeded = asyncio.run(build())
    write_heartbeat(home, PAPER_LIVE)
    return seeded


@pytest.fixture
def web_env(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Point the app at a runtime home the way the CLI launcher does (environment variables)."""

    def apply(home: Path, **extra: str) -> None:
        for key in list(os.environ):
            if key.startswith("SOLANA_SNIPER_WEB_"):
                monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("SOLANA_SNIPER_WEB_HOME", str(home))
        for k, v in extra.items():
            monkeypatch.setenv("SOLANA_SNIPER_WEB_" + k.upper(), v)

    return apply


@pytest.fixture(autouse=True)
def clear_streamlit_cache() -> Iterator[None]:
    st.cache_data.clear()
    yield
    st.cache_data.clear()


def run_app(timeout: float = 60) -> AppTest:
    at = AppTest.from_file(str(APP_PATH), default_timeout=timeout)
    at.run()
    return at


def all_text(at: AppTest) -> str:
    """Every piece of text the harness can see, for negative assertions (no secret, ...)."""
    parts: list[str] = []
    for coll in (
        at.markdown,
        at.caption,
        at.text,
        at.error,
        at.warning,
        at.info,
        at.success,
        at.title,
        at.header,
        at.subheader,
    ):
        parts.extend(str(el.value) for el in coll)
    parts.extend(f"{m.label} {m.value} {m.delta}" for m in at.metric)
    parts.extend(str(e.label) for e in at.expander)
    parts.extend(str(t.label) for t in at.tabs)
    for df in at.dataframe:
        parts.append(df.value.to_string())
    for sel in at.selectbox:
        parts.append(" ".join(str(o) for o in sel.options))
        parts.append(str(sel.value))
    return "\n".join(parts)


def main_text(at: AppTest) -> str:
    """Text of the main area only (the sidebar legitimately lists every session)."""
    main = at.main
    parts: list[str] = []
    for coll in (main.markdown, main.caption, main.error, main.warning, main.info):
        parts.extend(str(el.value) for el in coll)
    parts.extend(f"{m.label} {m.value}" for m in main.metric)
    parts.extend(str(e.label) for e in main.expander)
    for df in main.dataframe:
        parts.append(df.value.to_string())
    return "\n".join(parts)


def goto(at: AppTest, page: str) -> AppTest:
    at.segmented_control[0].set_value(page).run()
    return at
