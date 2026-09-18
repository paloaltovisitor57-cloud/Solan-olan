"""Optional push adapters: Discord webhook and Telegram bot. Both are plain HTTP POSTs."""

from __future__ import annotations

from solana_sniper.alerts.base import Alert
from solana_sniper.domain.enums import Urgency
from solana_sniper.infra.http import HttpClient, HttpError
from solana_sniper.telemetry.logging import get_logger
from solana_sniper.telemetry.redaction import register_secret, register_url_secrets, safe_exception

log = get_logger(__name__)


class DiscordAlertProvider:
    name = "discord"

    def __init__(self, http: HttpClient, webhook_url: str) -> None:
        self._http = http
        self._url = webhook_url
        register_url_secrets(webhook_url)

    async def send(self, alert: Alert) -> None:
        marker = (
            "🚨 "
            if alert.urgency is Urgency.URGENT
            else ("⚠️ " if alert.urgency is Urgency.HIGH else "")
        )
        try:
            await self._http.post_json(
                self._url,
                json={"content": f"{marker}**{alert.title}**\n{alert.body}"[:1900]},
                retries=1,
            )
        except HttpError as exc:
            log.warning("discord_alert_failed", error=safe_exception(exc))


class TelegramAlertProvider:
    name = "telegram"

    def __init__(self, http: HttpClient, bot_token: str, chat_id: str) -> None:
        self._http = http
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        register_secret(bot_token)

    async def send(self, alert: Alert) -> None:
        marker = (
            "🚨 "
            if alert.urgency is Urgency.URGENT
            else ("⚠️ " if alert.urgency is Urgency.HIGH else "")
        )
        try:
            await self._http.post_json(
                self._url,
                json={
                    "chat_id": self._chat_id,
                    "text": f"{marker}{alert.title}\n{alert.body}"[:4000],
                },
                retries=1,
            )
        except HttpError as exc:
            log.warning("telegram_alert_failed", error=safe_exception(exc))
