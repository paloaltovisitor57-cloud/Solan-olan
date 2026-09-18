"""Finding 1 regression: credentials must never reach exceptions, logs or diagnostics.

Sentinel values are fake. Every HTTP call is served by an httpx MockTransport; no network.
"""

from __future__ import annotations

import io
import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from solana_sniper.alerts.base import Alert
from solana_sniper.alerts.webhooks import DiscordAlertProvider, TelegramAlertProvider
from solana_sniper.app.doctor import run_doctor
from solana_sniper.config import load_settings
from solana_sniper.domain.enums import Urgency
from solana_sniper.infra.http import HttpClient, HttpError, MalformedResponseError
from solana_sniper.telemetry.logging import configure_logging
from solana_sniper.telemetry.redaction import (
    register_secret,
    register_url_secrets,
    registry,
    safe_exception,
    safe_url,
    scrub_text,
)

KEY = "HELIUS_KEY_SENTINEL_0123"
PW = "USERINFO_PW_SENTINEL"
BOT = "123456:TELEGRAM_TOKEN_SENTINEL"
HOOK = "DISCORD_WEBHOOK_TOKEN_SENTINEL"
SENTINELS = (KEY, PW, BOT.split(":")[1], HOOK)


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    registry.clear()


def _leaks(text: str) -> list[str]:
    return [s for s in SENTINELS if s in text]


def test_safe_url_keeps_endpoint_drops_credentials() -> None:
    assert safe_url(f"https://u:{PW}@mainnet.helius-rpc.com/?api-key={KEY}") == (
        "https://mainnet.helius-rpc.com/?<redacted>"
    )
    assert safe_url(f"https://api.telegram.org/bot{BOT}/sendMessage") == (
        "https://api.telegram.org/bot***/sendMessage"
    )
    assert safe_url(f"https://discord.com/api/webhooks/42/{HOOK}") == (
        "https://discord.com/api/webhooks/42/***"
    )
    assert safe_url("https://api.dexscreener.com/tokens/v1/solana/MintAbc") == (
        "https://api.dexscreener.com/tokens/v1/solana/MintAbc"
    )
    assert safe_url("") == "" and _leaks(safe_url("not a url ?key=" + KEY)) == []


def test_scrub_text_masks_query_userinfo_headers_and_registered_secrets() -> None:
    register_secret("PLAIN_SECRET_SENTINEL")
    register_url_secrets(f"https://u:{PW}@h/?api-key={KEY}")
    text = (
        f"GET https://u:{PW}@h/x?api-key={KEY}&other=1 x-api-key: {KEY} "
        f"Authorization: Bearer {KEY} token={KEY} body PLAIN_SECRET_SENTINEL end"
    )
    out = scrub_text(text)
    assert _leaks(out) == [] and "PLAIN_SECRET_SENTINEL" not in out
    assert "https://***@h/x?api-key=***&other=***" in out and "Bearer ***" in out
    assert scrub_text("") == ""


async def test_http_errors_never_carry_credentials_or_bodies() -> None:
    def transport_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused {request.url}")  # httpx messages may include the URL

    def echo_body(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"invalid api key {request.url.params.get('api-key')}")

    def bad_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=f"<html>{KEY}</html>")

    url = f"https://u:{PW}@mainnet.helius-rpc.com/v1?api-key={KEY}"
    for handler, exc_type in (
        (transport_error, HttpError),
        (echo_body, HttpError),
        (bad_json, MalformedResponseError),
    ):
        client = HttpClient(transport=httpx.MockTransport(handler))
        with pytest.raises(exc_type) as info:
            await client.get_json(url, retries=0, headers={"x-api-key": KEY})
        rendered = safe_exception(info.value)
        assert _leaks(str(info.value)) == [] and _leaks(rendered) == [], rendered
        assert "mainnet.helius-rpc.com" in str(info.value)  # endpoint stays identifiable
        await client.aclose()
    with pytest.raises(HttpError) as info2:
        await HttpClient(transport=httpx.MockTransport(echo_body)).get_json(url, retries=0)
    assert info2.value.status == 401 and "HTTP 401" in str(info2.value)


async def test_alert_handlers_and_all_log_sinks_are_scrubbed(tmp_path: Path) -> None:
    log_file = tmp_path / "app.log"
    configure_logging("DEBUG", json_output=True, log_file=str(log_file), quiet_console=True)
    foreign = io.StringIO()
    logging.getLogger().addHandler(logging.StreamHandler(foreign))  # handler we do not own

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"down {request.url}")

    client = HttpClient(transport=httpx.MockTransport(down))
    alert = Alert(datetime.now(tz=UTC), "t", "b", Urgency.HIGH, "x")
    await TelegramAlertProvider(client, BOT, "chat").send(alert)
    await DiscordAlertProvider(client, f"https://discord.com/api/webhooks/42/{HOOK}").send(alert)
    try:
        raise ValueError(f"chained https://h/?api-key={KEY}")
    except ValueError:
        logging.getLogger("thirdparty").exception("failure with traceback")
    logging.getLogger("thirdparty").warning("plain %s", f"token={KEY}")
    logging.shutdown()
    for text in (log_file.read_text(), foreign.getvalue()):
        assert "alert_failed" in text or "failure with traceback" in text
        assert _leaks(text) == [], text[:400]
    await client.aclose()


async def test_doctor_output_has_no_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SNIPER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(
        "SNIPER_PROVIDERS__SOLANA_RPC_URL", f"https://mainnet.helius-rpc.com/?api-key={KEY}"
    )
    monkeypatch.setenv("SNIPER_PROVIDERS__HELIUS_API_KEY", KEY)
    settings = load_settings(Path("configs/synthetic.yaml"))
    results = await run_doctor(settings)
    joined = " | ".join(f"{r.name} {r.status} {r.detail}" for r in results)
    assert _leaks(joined) == [], joined
    # the same settings object also registered its secrets for the log scrubber
    assert scrub_text(f"key {KEY}") == "key ***"
