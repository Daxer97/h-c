"""
notifications/webhook_notifier.py — Notifier HTTP generico.

Invia eventi come JSON POST a qualsiasi endpoint webhook.
Compatibile out-of-the-box con Slack, Discord, e custom webhooks.

Configurazione formato payload tramite `payload_builder`:
  - None → formato raw (Event.format_json())
  - "slack" → formato Slack incoming webhook
  - "discord" → formato Discord webhook
  - callable → funzione custom (event) -> dict
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Any

import aiohttp

from notifications.base import BaseNotifier
from notifications.events import Event, Severity

logger = logging.getLogger(__name__)


def _slack_payload(event: Event) -> dict:
    """Formatta evento per Slack incoming webhook."""
    color_map = {
        Severity.DEBUG: "#808080",
        Severity.INFO: "#36a64f",
        Severity.WARNING: "#ff9900",
        Severity.ERROR: "#ff0000",
        Severity.CRITICAL: "#990000",
    }
    return {
        "attachments": [
            {
                "color": color_map.get(event.severity, "#808080"),
                "title": f"{event.severity.emoji} [{event.severity.label}] {event.category}",
                "text": event.message,
                "footer": event.source or "tempmail-bot",
                "ts": int(event.timestamp.timestamp()),
                "fields": [
                    {"title": k, "value": str(v), "short": True}
                    for k, v in event.metadata.items()
                ][:5],
            }
        ]
    }


def _discord_payload(event: Event) -> dict:
    """Formatta evento per Discord webhook."""
    color_map = {
        Severity.DEBUG: 0x808080,
        Severity.INFO: 0x36A64F,
        Severity.WARNING: 0xFF9900,
        Severity.ERROR: 0xFF0000,
        Severity.CRITICAL: 0x990000,
    }
    embed = {
        "title": f"{event.severity.emoji} [{event.severity.label}] {event.category}",
        "description": event.message[:2000],
        "color": color_map.get(event.severity, 0x808080),
        "timestamp": event.timestamp.isoformat(),
    }
    if event.source:
        embed["footer"] = {"text": event.source}
    if event.metadata:
        embed["fields"] = [
            {"name": str(k), "value": str(v)[:200], "inline": True}
            for k, v in list(event.metadata.items())[:5]
        ]
    if event.traceback_str:
        embed["fields"] = embed.get("fields", []) + [
            {"name": "Traceback", "value": f"```{event.traceback_str[:500]}```"}
        ]

    return {"embeds": [embed]}


BUILTIN_BUILDERS: dict[str, Callable[[Event], dict]] = {
    "slack": _slack_payload,
    "discord": _discord_payload,
}


class WebhookNotifier(BaseNotifier):
    """
    Invia eventi come HTTP POST a un webhook URL.

    Args:
        url: Endpoint webhook
        payload_builder: "slack", "discord", callable, o None per JSON raw
        headers: Header aggiuntivi (es. auth)
        timeout: Timeout richiesta in secondi
        max_retries: Numero massimo di retry su errori transitori
    """

    def __init__(
        self,
        url: str,
        payload_builder: str | Callable[[Event], dict] | None = None,
        headers: dict[str, str] | None = None,
        min_severity: Severity = Severity.WARNING,
        enabled: bool = True,
        name: str = "webhook",
        timeout: int = 10,
        max_retries: int = 2,
    ):
        super().__init__(name=name, min_severity=min_severity, enabled=enabled)

        if url and not url.startswith("https://"):
            logger.warning(
                f"WebhookNotifier: URL does not use HTTPS ({url[:40]}...). "
                f"Event data will be transmitted in cleartext."
            )
        self._url = url
        self._headers = headers or {}
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._max_retries = max_retries
        self._session: aiohttp.ClientSession | None = None

        # Risolvi payload builder
        if payload_builder is None:
            self._builder = lambda e: e.format_json()
        elif isinstance(payload_builder, str):
            if payload_builder not in BUILTIN_BUILDERS:
                raise ValueError(
                    f"Builder sconosciuto: {payload_builder}. "
                    f"Disponibili: {list(BUILTIN_BUILDERS.keys())}"
                )
            self._builder = BUILTIN_BUILDERS[payload_builder]
        elif callable(payload_builder):
            self._builder = payload_builder
        else:
            raise TypeError(f"payload_builder deve essere str, callable o None")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def send(self, event: Event) -> bool:
        payload = self._builder(event)
        session = await self._get_session()

        headers = {"Content-Type": "application/json", **self._headers}

        for attempt in range(1, self._max_retries + 1):
            try:
                async with session.post(
                    self._url, json=payload, headers=headers
                ) as resp:
                    if resp.status < 300:
                        return True

                    body = await resp.text()

                    if resp.status == 429 or resp.status >= 500:
                        logger.warning(
                            f"Webhook {self.name}: {resp.status}, "
                            f"retry {attempt}/{self._max_retries}"
                        )
                        await asyncio.sleep(2 ** attempt)
                        continue

                    logger.error(
                        f"Webhook {self.name}: errore {resp.status}: {body[:200]}"
                    )
                    return False

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning(
                    f"Webhook {self.name}: network error: {e}, "
                    f"retry {attempt}/{self._max_retries}"
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(2 ** attempt)

        return False

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
