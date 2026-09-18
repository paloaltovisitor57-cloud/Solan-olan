"""Paper trading sessions: live market data, simulated fills, no real funds, one isolated
database per experiment.

A paper session is created with a bankroll of exactly N SOL (converted once, at start, with the
live SOL/EUR rate) or N EUR. The starting bankroll is recorded in the session metadata and is
never redefined by later FX moves; the dashboard may additionally show the *current* SOL
equivalent of the equity, clearly labelled as such.

Isolation: every new session gets its own SQLite file under <home>/db/paper/, so an old account
can never leak into a freshly requested bankroll. Resuming is explicit (`--resume ID`).

Nothing in this module can sign or broadcast: paper mode wires the simulated execution
interface (`DryRunExecution`) and never touches a private key.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

from solana_sniper.config.paths import DB_DIR
from solana_sniper.config.settings import Settings
from solana_sniper.portfolio.fx import FxProvider

PAPER_DIR = "paper"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,40}$")
_EIGHT = Decimal("0.00000001")


class PaperSetupError(Exception):
    """A paper session cannot start as requested (bad options, no live FX, unknown session)."""


@dataclass(frozen=True, slots=True)
class PaperSession:
    session_id: str
    name: str | None
    created_at: datetime
    requested: str  # "1 SOL" | "100 EUR"
    bankroll_sol: Decimal
    bankroll_eur: Decimal
    sol_eur_start: Decimal
    fx_source: str  # "live" | "fallback"
    fx_at: datetime
    config_path: str | None
    database_url: str

    @property
    def fx_is_live(self) -> bool:
        return self.fx_source == "live"

    def to_payload(self) -> dict[str, Any]:
        data = asdict(self)
        for k, v in list(data.items()):
            if isinstance(v, Decimal):
                data[k] = str(v)
            elif isinstance(v, datetime):
                data[k] = v.isoformat()
        return data

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> PaperSession:
        return cls(
            session_id=str(data["session_id"]),
            name=data.get("name"),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            requested=str(data["requested"]),
            bankroll_sol=Decimal(str(data["bankroll_sol"])),
            bankroll_eur=Decimal(str(data["bankroll_eur"])),
            sol_eur_start=Decimal(str(data["sol_eur_start"])),
            fx_source=str(data["fx_source"]),
            fx_at=datetime.fromisoformat(str(data["fx_at"])),
            config_path=data.get("config_path"),
            database_url=str(data["database_url"]),
        )


@dataclass(frozen=True, slots=True)
class BankrollRequest:
    sol: Decimal | None = None
    eur: Decimal | None = None

    def validate(self) -> None:
        if (self.sol is None) == (self.eur is None):
            raise PaperSetupError("give exactly one of --bankroll-sol or --bankroll-eur")
        amount = self.sol if self.sol is not None else self.eur
        assert amount is not None
        if not amount.is_finite() or amount <= 0:
            raise PaperSetupError("the bankroll must be a positive number")
        if amount > Decimal("1000000"):
            raise PaperSetupError("the bankroll is unrealistically large for a paper session")

    @property
    def label(self) -> str:
        if self.sol is not None:
            return f"{_trim(self.sol)} SOL"
        assert self.eur is not None
        return f"{_trim(self.eur)} EUR"

    @property
    def tag(self) -> str:
        return self.label.lower().replace(" ", "")


def _trim(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text if "." not in text else text.rstrip("0").rstrip(".")


def slugify_name(name: str) -> str:
    if not _NAME_RE.match(name):
        raise PaperSetupError(
            "--name may contain letters, digits, '.', '_' and '-' only (max 41 characters)"
        )
    return name


def make_session_id(request: BankrollRequest, name: str | None, now: datetime) -> str:
    stamp = now.astimezone(UTC).strftime("%Y%m%d-%H%M%S")
    if name:
        return f"paper-{stamp}-{slugify_name(name)}"
    return f"paper-{stamp}-{request.tag}-{uuid.uuid4().hex[:6]}"


def paper_db_url(home: Path, session_id: str) -> str:
    return f"sqlite+aiosqlite:///{home / DB_DIR / PAPER_DIR / (session_id + '.db')}"


def paper_db_path(home: Path, session_id: str) -> Path:
    return home / DB_DIR / PAPER_DIR / f"{session_id}.db"


async def resolve_bankroll(
    fx: FxProvider,
    request: BankrollRequest,
    *,
    allow_fallback_fx: bool,
    now: datetime,
) -> tuple[Decimal, Decimal, Decimal, str]:
    """Returns (bankroll_sol, bankroll_eur, sol_eur_rate, fx_source).

    The live rate is fetched once. Without a live rate the session refuses to start unless the
    caller explicitly allowed the configured fallback, and the metadata then says so."""
    request.validate()
    await fx.refresh()
    rate = fx.sol_eur()
    if fx.is_live:
        source = "live"
    elif allow_fallback_fx:
        source = "fallback"
    else:
        raise PaperSetupError(
            f"could not fetch a live SOL/EUR rate (fallback would be €{rate}). Check the network "
            "or pass --allow-fallback-fx to start anyway with the fallback rate, clearly marked."
        )
    if rate <= 0:
        raise PaperSetupError("SOL/EUR rate is not positive")
    if request.sol is not None:
        sol = request.sol
        eur = (sol * rate).quantize(_EIGHT, rounding=ROUND_HALF_EVEN)
    else:
        assert request.eur is not None
        eur = request.eur.quantize(_EIGHT, rounding=ROUND_HALF_EVEN)
        sol = (eur / rate).quantize(_EIGHT, rounding=ROUND_HALF_EVEN)
    return sol, eur, rate, source


def apply_paper_settings(settings: Settings, session: PaperSession) -> None:
    """Point the settings at the session's own database and starting bankroll."""
    settings.storage.database_url = session.database_url
    settings.risk.starting_bankroll_eur = session.bankroll_eur


# ------------------------------------------------------------------ report
@dataclass(frozen=True, slots=True)
class SessionReport:
    session_id: str
    started_label: str
    finished_equity_eur: Decimal
    sol_equivalent: Decimal | None
    sol_eur_now: Decimal | None
    fx_now_live: bool
    return_pct: float
    max_drawdown_pct: float
    trades: int
    wins: int
    losses: int
    realized_pnl_eur: Decimal
    unrealized_pnl_eur: Decimal
    fees_eur: Decimal
    slippage_eur: Decimal
    open_positions: int
    signals: int
    storage_failures: int
    dropped_writes: int
    dropped_by_kind: dict[str, int]
    provider_outages: int
    rate_limits_handled: int
    degraded_checks: int
    data_complete: bool
    run_time_s: float
    stop_reason: str

    def lines(self) -> list[str]:
        sol_eq = (
            f"{self.sol_equivalent:.4f} SOL (at current €{self.sol_eur_now:.2f}"
            f"{'' if self.fx_now_live else ', fallback rate'})"
            if self.sol_equivalent is not None and self.sol_eur_now is not None
            else "n/a"
        )
        out = [
            "SESSION COMPLETE"
            if self.data_complete
            else "SESSION COMPLETE  (DATA INTEGRITY COMPROMISED)",
            f"Started:           {self.started_label}",
            f"Finished:          €{self.finished_equity_eur:.2f}",
            f"SOL equivalent:    {sol_eq}",
            f"Return:            {self.return_pct:+.2%}",
            f"Maximum drawdown:  {-self.max_drawdown_pct:.1%}",
            f"Trades:            {self.trades}",
            f"Wins:              {self.wins}",
            f"Losses:            {self.losses}",
            f"Open positions:    {self.open_positions}",
            f"Signals:           {self.signals}",
            f"Realized P&L:      {_signed(self.realized_pnl_eur)}",
            f"Unrealized P&L:    {_signed(self.unrealized_pnl_eur)}",
            f"Fees/slippage:     €{self.fees_eur + self.slippage_eur:.2f}",
            f"Storage errors:    {self.storage_failures}",
            f"Dropped writes:    {self.dropped_writes}"
            + (
                f" ({', '.join(f'{k}={v}' for k, v in self.dropped_by_kind.items())})"
                if self.dropped_by_kind
                else ""
            ),
            f"Provider outages:  {self.provider_outages} circuit trips",
            f"Rate limits:       {self.rate_limits_handled} handled, "
            f"{self.degraded_checks} checks degraded",
            f"Run time:          {_hms(self.run_time_s)}  (stopped: {self.stop_reason})",
            f"Session:           {self.session_id}",
        ]
        if not self.data_complete:
            out.insert(
                1,
                "WARNING: the storage writer dropped or failed writes during this session; "
                "the figures above are computed from incomplete records and must not be "
                "trusted as a measurement.",
            )
        out.append("Real transactions: DISABLED (nothing was signed or broadcast)")
        return out


def _signed(value: Decimal) -> str:
    return f"{'+' if value >= 0 else '-'}€{abs(value):.2f}"


def _hms(seconds: float) -> str:
    total = int(max(0.0, seconds))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"
