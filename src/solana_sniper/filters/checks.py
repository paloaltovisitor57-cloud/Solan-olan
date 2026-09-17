"""Token checks. Every check yields PASS / REJECT / UNKNOWN with a reason; nothing is ever "safe".

A REJECT may be fatal (authorities, transfer hooks, too old, liquidity pulled) which ends the
candidate, or non-fatal (not enough liquidity/volume yet) which merely blocks qualification.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from solana_sniper.config.settings import FiltersConfig
from solana_sniper.domain.enums import CheckVerdict
from solana_sniper.domain.models import CheckReport, CheckResult, FeatureVector, RoundTripQuote
from solana_sniper.market_data.tracker import TokenTrack

PASS = CheckVerdict.PASS
REJECT = CheckVerdict.REJECT
UNKNOWN = CheckVerdict.UNKNOWN


class TokenChecker:
    def __init__(self, config: FiltersConfig) -> None:
        self._cfg = config

    def evaluate(
        self,
        track: TokenTrack,
        features: FeatureVector,
        now: datetime,
        round_trip: RoundTripQuote | None = None,
    ) -> CheckReport:
        results: list[CheckResult] = []
        results.extend(self._age_checks(features, now))
        results.extend(self._market_checks(track, features, now))
        results.extend(self._authority_checks(track, now))
        results.extend(self._holder_checks(track, features, now))
        if round_trip is not None:
            results.extend(self._quote_checks(round_trip, now))
        return CheckReport(mint=track.mint, evaluated_at=now, results=tuple(results))

    def qualifies(self, report: CheckReport) -> tuple[bool, str]:
        if report.rejections:
            return False, report.summary()
        unknowns = report.unknowns
        if unknowns and self._cfg.unknown_policy == "reject":
            return False, f"unknown checks not allowed: {report.summary()}"
        if len(unknowns) > self._cfg.max_unknown_checks:
            return False, f"too many unknown checks ({len(unknowns)})"
        return True, "ok"

    # ------------------------------------------------------------------ groups
    def _age_checks(self, f: FeatureVector, now: datetime) -> list[CheckResult]:
        c = self._cfg
        age = f.token_age_s
        if age is None:
            return [CheckResult("token_age", UNKNOWN, "creation time unknown", now)]
        if age > c.max_token_age_s:
            return [
                CheckResult(
                    "token_age", REJECT, f"too old ({age:.0f}s)", now, f"{age:.0f}", fatal=True
                )
            ]
        if age < c.min_token_age_s:
            return [CheckResult("token_age", REJECT, f"too young ({age:.0f}s)", now, f"{age:.0f}")]
        return [CheckResult("token_age", PASS, f"{age:.0f}s old", now, f"{age:.0f}")]

    def _market_checks(
        self, track: TokenTrack, f: FeatureVector, now: datetime
    ) -> list[CheckResult]:
        c = self._cfg
        out: list[CheckResult] = []
        latest = track.latest
        if f.stale:
            out.append(CheckResult("data_fresh", REJECT, f"stale data ({f.data_age_s:.0f}s)", now))
        else:
            out.append(CheckResult("data_fresh", PASS, f"{f.data_age_s:.1f}s", now))
        liq = latest.liquidity_usd if latest else None
        if liq is None:
            out.append(CheckResult("liquidity", UNKNOWN, "liquidity unavailable", now))
        elif liq < c.min_liquidity_usd:
            out.append(
                CheckResult(
                    "liquidity",
                    REJECT,
                    f"${liq:,.0f} < min ${c.min_liquidity_usd:,.0f}",
                    now,
                    str(liq),
                )
            )
        elif liq > c.max_liquidity_usd:
            out.append(
                CheckResult(
                    "liquidity",
                    REJECT,
                    f"${liq:,.0f} > max ${c.max_liquidity_usd:,.0f}",
                    now,
                    str(liq),
                )
            )
        else:
            out.append(CheckResult("liquidity", PASS, f"${liq:,.0f}", now, str(liq)))
        out.append(self._liquidity_drop(track, now))
        vol = latest.volume_5m_usd if latest else None
        if vol is None and track.trades:
            sol_usd = None
            if latest is not None and latest.price_usd and latest.price_native:
                sol_usd = latest.price_usd / latest.price_native
            if sol_usd:
                vol = track.trade_window(now, 300).volume_sol * sol_usd
        if vol is None:
            out.append(CheckResult("volume_5m", UNKNOWN, "volume unavailable", now))
        elif vol < c.min_volume_5m_usd:
            out.append(
                CheckResult(
                    "volume_5m", REJECT, f"${vol:,.0f} < ${c.min_volume_5m_usd:,.0f}", now, str(vol)
                )
            )
        else:
            out.append(CheckResult("volume_5m", PASS, f"${vol:,.0f}", now, str(vol)))
        buys = latest.buys_5m if latest and latest.buys_5m is not None else None
        if buys is None and track.trades:
            buys = track.trade_window(now, 300).buys
        if buys is None:
            out.append(CheckResult("buys_5m", UNKNOWN, "buy count unavailable", now))
        elif buys < c.min_buys_5m:
            out.append(CheckResult("buys_5m", REJECT, f"{buys} < {c.min_buys_5m}", now, str(buys)))
        else:
            out.append(CheckResult("buys_5m", PASS, str(buys), now, str(buys)))
        if f.buy_ratio is None:
            out.append(CheckResult("demand_balance", UNKNOWN, "no buy/sell data", now))
        elif f.buy_ratio > c.max_buy_ratio and (buys or 0) >= 10:
            out.append(
                CheckResult(
                    "demand_balance", REJECT, f"absurdly one-sided ({f.buy_ratio:.0%} buys)", now
                )
            )
        elif f.buy_ratio < c.min_buy_ratio:
            out.append(
                CheckResult(
                    "demand_balance", REJECT, f"sell pressure ({f.buy_ratio:.0%} buys)", now
                )
            )
        else:
            out.append(CheckResult("demand_balance", PASS, f"{f.buy_ratio:.0%} buys", now))
        spread = latest.spread_bps if latest else None
        if spread is not None and spread > c.max_spread_bps:
            out.append(
                CheckResult("spread", REJECT, f"{spread}bps > {c.max_spread_bps}", now, str(spread))
            )
        elif spread is not None:
            out.append(CheckResult("spread", PASS, f"{spread}bps", now, str(spread)))
        return out

    def _liquidity_drop(self, track: TokenTrack, now: datetime) -> CheckResult:
        snaps = track.snapshots_since(now, 300)
        liqs = [s.liquidity_usd for s in snaps if s.liquidity_usd is not None]
        if len(liqs) < 2:
            return CheckResult("liquidity_stability", UNKNOWN, "not enough liquidity history", now)
        peak = max(liqs)
        cur = liqs[-1]
        if peak <= 0:
            return CheckResult("liquidity_stability", UNKNOWN, "zero liquidity history", now)
        drop = float((peak - cur) / peak)
        if drop >= self._cfg.liquidity_drop_reject_pct:
            return CheckResult(
                "liquidity_stability",
                REJECT,
                f"liquidity fell {drop:.0%} from ${peak:,.0f}",
                now,
                f"{drop:.3f}",
                fatal=True,
            )
        return CheckResult("liquidity_stability", PASS, f"max drop {drop:.0%}", now, f"{drop:.3f}")

    def _authority_checks(self, track: TokenTrack, now: datetime) -> list[CheckResult]:
        c = self._cfg
        auth = track.authorities
        if auth is None:
            return [CheckResult("authorities", UNKNOWN, "mint account not fetched", now)]
        out: list[CheckResult] = []
        if auth.mint_authority is not None and c.require_mint_authority_revoked:
            out.append(
                CheckResult(
                    "mint_authority",
                    REJECT,
                    "mint authority still active",
                    now,
                    auth.mint_authority,
                    fatal=True,
                )
            )
        else:
            out.append(
                CheckResult(
                    "mint_authority",
                    PASS,
                    "revoked" if auth.mint_authority is None else "present (allowed)",
                    now,
                )
            )
        if auth.freeze_authority is not None and c.require_freeze_authority_revoked:
            out.append(
                CheckResult(
                    "freeze_authority",
                    REJECT,
                    "freeze authority active",
                    now,
                    auth.freeze_authority,
                    fatal=True,
                )
            )
        else:
            out.append(
                CheckResult(
                    "freeze_authority",
                    PASS,
                    "revoked" if auth.freeze_authority is None else "present (allowed)",
                    now,
                )
            )
        if (
            auth.transfer_fee_bps is not None
            and auth.transfer_fee_bps > c.reject_transfer_fee_bps_over
        ):
            out.append(
                CheckResult(
                    "transfer_fee",
                    REJECT,
                    f"transfer fee {auth.transfer_fee_bps}bps",
                    now,
                    fatal=True,
                )
            )
        if auth.has_transfer_hook and c.reject_transfer_hook:
            out.append(
                CheckResult("transfer_hook", REJECT, "transfer hook program set", now, fatal=True)
            )
        if auth.non_transferable:
            out.append(
                CheckResult("transferable", REJECT, "non-transferable token", now, fatal=True)
            )
        if auth.permanent_delegate is not None:
            out.append(
                CheckResult(
                    "permanent_delegate",
                    REJECT,
                    "permanent delegate can seize tokens",
                    now,
                    fatal=True,
                )
            )
        if auth.supply_raw <= 0:
            out.append(CheckResult("supply", REJECT, "zero supply", now, fatal=True))
        else:
            out.append(CheckResult("supply", PASS, str(auth.supply_raw), now))
        return out

    def _holder_checks(
        self, track: TokenTrack, f: FeatureVector, now: datetime
    ) -> list[CheckResult]:
        c = self._cfg
        out: list[CheckResult] = []
        latest = track.latest
        top10 = (
            track.holders.top10_pct
            if track.holders
            else (latest.top10_holder_pct if latest else None)
        )
        largest = (
            track.holders.largest_pct
            if track.holders
            else (latest.largest_holder_pct if latest else None)
        )
        if top10 is None:
            out.append(CheckResult("holder_concentration", UNKNOWN, "holder data unavailable", now))
        elif top10 > c.max_top10_holder_pct:
            out.append(
                CheckResult(
                    "holder_concentration", REJECT, f"top10 hold {top10:.0%}", now, f"{top10:.3f}"
                )
            )
        else:
            out.append(
                CheckResult("holder_concentration", PASS, f"top10 {top10:.0%}", now, f"{top10:.3f}")
            )
        if largest is not None and largest > c.max_largest_holder_pct:
            out.append(
                CheckResult(
                    "largest_holder", REJECT, f"largest holder {largest:.0%}", now, f"{largest:.3f}"
                )
            )
        elif largest is not None:
            out.append(CheckResult("largest_holder", PASS, f"{largest:.0%}", now, f"{largest:.3f}"))
        traders = (
            len(track.traders) if track.traders else (latest.unique_traders if latest else None)
        )
        if traders is None:
            out.append(CheckResult("unique_traders", UNKNOWN, "trader data unavailable", now))
        elif traders < c.min_unique_traders:
            out.append(
                CheckResult(
                    "unique_traders",
                    REJECT,
                    f"{traders} < {c.min_unique_traders}",
                    now,
                    str(traders),
                )
            )
        else:
            out.append(CheckResult("unique_traders", PASS, str(traders), now, str(traders)))
        return out

    def _quote_checks(self, rt: RoundTripQuote, now: datetime) -> list[CheckResult]:
        c = self._cfg
        out: list[CheckResult] = []
        if rt.entry_slippage_bps > c.max_estimated_slippage_bps:
            out.append(
                CheckResult(
                    "entry_slippage",
                    REJECT,
                    f"{rt.entry_slippage_bps}bps > {c.max_estimated_slippage_bps}",
                    now,
                )
            )
        else:
            out.append(CheckResult("entry_slippage", PASS, f"{rt.entry_slippage_bps}bps", now))
        if rt.sell is None or rt.immediate_exit_sol is None:
            out.append(
                CheckResult(
                    "exit_quote", REJECT, "no viable sell quote: " + "; ".join(rt.reasons), now
                )
            )
            return out
        exit_bps = rt.exit_slippage_bps or 0
        if exit_bps > c.max_estimated_exit_slippage_bps:
            out.append(
                CheckResult(
                    "exit_slippage",
                    REJECT,
                    f"{exit_bps}bps > {c.max_estimated_exit_slippage_bps}",
                    now,
                )
            )
        else:
            out.append(CheckResult("exit_slippage", PASS, f"{exit_bps}bps", now))
        loss = rt.round_trip_loss_pct if rt.round_trip_loss_pct is not None else 1.0
        if loss > c.max_round_trip_loss_pct * 2:
            out.append(
                CheckResult(
                    "round_trip",
                    REJECT,
                    f"catastrophic round trip loss {loss:.0%}",
                    now,
                    f"{loss:.3f}",
                    fatal=True,
                )
            )
        elif loss > c.max_round_trip_loss_pct:
            out.append(
                CheckResult("round_trip", REJECT, f"round trip loss {loss:.0%}", now, f"{loss:.3f}")
            )
        else:
            out.append(
                CheckResult("round_trip", PASS, f"round trip loss {loss:.1%}", now, f"{loss:.3f}")
            )
        if not rt.viable:
            out.append(
                CheckResult(
                    "quote_viable", REJECT, "; ".join(rt.reasons) or "quote not viable", now
                )
            )
        return out


def liquidity_from_snapshot(liq: Decimal | None) -> float | None:
    return float(liq) if liq is not None else None
