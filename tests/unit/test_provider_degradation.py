"""Rate-limited providers degrade gracefully: GeckoTerminal polling waits out the cooldown,
public Solana RPC throttling leaves the affected checks UNKNOWN (never PASS), identical RPC
calls are coalesced and cached, and nothing here is logged as an engine error."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from solana_sniper.config.settings import DiscoveryConfig, FiltersConfig
from solana_sniper.discovery.geckoterminal import GeckoTerminalDiscovery
from solana_sniper.discovery.service import DiscoveryService
from solana_sniper.domain.clock import ManualClock
from solana_sniper.domain.enums import CheckVerdict
from solana_sniper.domain.models import MarketSnapshot, TokenInfo
from solana_sniper.features.engine import FeatureEngine
from solana_sniper.filters.checks import TokenChecker
from solana_sniper.infra.governor import RATE_LIMITED, HostPolicy, ProviderGovernor
from solana_sniper.infra.http import HttpClient, ProviderUnavailableError
from solana_sniper.market_data.tracker import TokenTracker
from solana_sniper.telemetry.metrics import Metrics
from solana_sniper.token_analysis.solana_rpc import SolanaRpcTokenProvider
from tests.unit.helpers import make_token
from tests.unit.test_checks_and_scoring import good_track

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MINT_ACCOUNT = json.loads((FIXTURES / "rpc_mint_account.json").read_text())


def _client(handler: Any, name: str, host: str, **policy: Any) -> tuple[HttpClient, Metrics]:
    metrics = Metrics()
    gov = ProviderGovernor(metrics=metrics, rng=lambda: 0.0)
    gov.register(host, name, HostPolicy(rate_per_s=1000, burst=1000, **policy))
    return HttpClient(
        transport=httpx.MockTransport(handler), metrics=metrics, governor=gov
    ), metrics


async def test_geckoterminal_throttling_waits_out_the_cooldown(
    clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"retry-after": "7"})

    client, metrics = _client(handler, "geckoterminal", "api.geckoterminal.com")
    gecko = GeckoTerminalDiscovery(
        client, "https://api.geckoterminal.com/api/v2", clock, poll_interval_s=1.0
    )
    sleeps: list[float] = []
    stop = asyncio.Event()

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)
        stop.set()
        await asyncio.Event().wait()  # park the poller after its first throttled wait

    monkeypatch.setattr("solana_sniper.discovery.service.asyncio.sleep", fake_sleep)
    seen: list[TokenInfo] = []

    async def sink(t: TokenInfo) -> None:
        seen.append(t)

    svc = DiscoveryService(DiscoveryConfig(), clock, metrics, sink)
    svc.add_polling(gecko)
    task = asyncio.create_task(svc._run_polling(gecko))
    await asyncio.wait_for(stop.wait(), 5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert calls == 1 and seen == []
    assert sleeps == [7.0]  # the poller waits exactly Retry-After, not the 1 s poll interval
    assert client.governor.state("api.geckoterminal.com") == RATE_LIMITED
    assert metrics.counters["rate_limited"] == 1 and metrics.counters["provider_errors"] == 0
    await client.aclose()


async def test_public_rpc_throttling_leaves_checks_unknown_not_pass(clock: ManualClock) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429)

    client, metrics = _client(
        handler, "solana-rpc", "api.mainnet-beta.solana.com", fast_fail_wait_s=0.0
    )
    rpc = SolanaRpcTokenProvider(
        client, "https://api.mainnet-beta.solana.com", clock, metrics=metrics
    )
    assert await rpc.get_authorities("MintX") is None
    assert calls == 1 and rpc.degraded_calls == 1 and metrics.counters["checks_degraded"] == 1
    # during the cooldown nothing is sent: fail fast, stay UNKNOWN
    assert await rpc.get_holder_distribution("MintX", ()) is None
    assert calls == 1 and rpc.degraded_calls == 2
    with pytest.raises(ProviderUnavailableError):
        await client.post_json("https://api.mainnet-beta.solana.com", json={})
    # the checker sees no authorities and reports UNKNOWN, never PASS
    track = good_track(clock)
    track.authorities = None
    track.holders = None
    checker = TokenChecker(FiltersConfig())
    features = FeatureEngine(20.0).compute(track, clock.now())
    report = checker.evaluate(track, features, clock.now(), None)
    by_name = {r.name: r for r in report.results}
    assert by_name["authorities"].verdict is CheckVerdict.UNKNOWN
    assert "mint_authority" not in by_name and "freeze_authority" not in by_name
    # holder concentration without any holder data (RPC degraded, market data silent) is UNKNOWN
    bare = TokenTracker()
    bare_track = bare.track(make_token("MintBare", age_s=120.0, clock=clock))
    for _ in range(3):
        bare.add_snapshot(
            MarketSnapshot(
                mint="MintBare",
                observed_at=clock.now(),
                source="t",
                price_native=Decimal("1.0"),
                liquidity_usd=Decimal("20000"),
            )
        )
        clock.advance(5)
    bare_features = FeatureEngine(20.0).compute(bare_track, clock.now())
    bare_report = checker.evaluate(bare_track, bare_features, clock.now(), None)
    bare_by_name = {r.name: r for r in bare_report.results}
    assert bare_by_name["holder_concentration"].verdict is CheckVerdict.UNKNOWN
    assert bare_by_name["authorities"].verdict is CheckVerdict.UNKNOWN
    await client.aclose()


async def test_rpc_calls_are_coalesced_and_cached(clock: ManualClock) -> None:
    calls = 0
    gate = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await gate.wait()
        return httpx.Response(200, json=MINT_ACCOUNT)

    client, metrics = _client(handler, "solana-rpc", "api.mainnet-beta.solana.com")
    rpc = SolanaRpcTokenProvider(
        client, "https://api.mainnet-beta.solana.com", clock, metrics=metrics, cache_ttl_s=60
    )
    tasks = [asyncio.create_task(rpc.get_authorities("MintY")) for _ in range(6)]
    await asyncio.sleep(0.02)
    gate.set()
    results = await asyncio.gather(*tasks)
    assert calls == 1 and all(r is not None and r.decimals == 6 for r in results)
    assert (await rpc.get_authorities("MintY")) is not None and calls == 1  # cached
    clock.advance(61)
    assert (await rpc.get_authorities("MintY")) is not None and calls == 2  # expired
    await client.aclose()


def test_rpc_default_policy_is_conservative_for_public_endpoints() -> None:
    from solana_sniper.app.bootstrap import build_governor
    from solana_sniper.config.settings import Settings

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    gov = build_governor(settings, Metrics())
    rpc = gov.policy_for("api.mainnet-beta.solana.com")
    assert rpc.rate_per_s <= 3.0 and rpc.max_concurrent <= 2 and rpc.max_waiting <= 40
    assert gov.name_of("api.mainnet-beta.solana.com") == "solana-rpc"
    assert gov.policy_for("api.geckoterminal.com").rate_per_s < 0.5
    settings.providers.solana_rpc_url = "https://mainnet.helius-rpc.com/?api-key=x"
    gov2 = build_governor(settings, Metrics())
    assert gov2.policy_for("mainnet.helius-rpc.com").rate_per_s >= 10.0
