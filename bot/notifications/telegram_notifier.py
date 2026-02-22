"""
notifications/telegram_notifier.py — Notifier per Telegram.

Invia eventi come messaggi formattati HTML.
Gestisce rate limiting di Telegram (max ~30 msg/sec per bot).
"""

from __future__ import annotations

import asyncio
import logging
import time

import aiohttp

from notifications.base import BaseNotifier
from notifications.events import Event, Severity

logger = logging.getLogger(__name__)

# Telegram rate limit: ~30 msg/s globale, ~1 msg/s per chat
MIN_INTERVAL_SECONDS = 1.5


class TelegramNotifier(BaseNotifier):
    """
    Invia notifiche su Telegram via Bot API.

    Gestisce:
      - Formattazione HTML
      - Rate limiting (min 1.5s tra messaggi allo stesso chat)
      - Troncamento a 4096 chars (limite Telegram)
      - Retry su errori transitori (429, 5xx)
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str | int,
        min_severity: Severity = Severity.INFO,
        enabled: bool = True,
        name: str = "telegram",
        max_retries: int = 3,
    ):
        super().__init__(name=name, min_severity=min_severity, enabled=enabled)
        self._bot_token = bot_token
        self._chat_id = str(chat_id)
        self._max_retries = max_retries
        self._last_send_time: float = 0
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def send(self, event: Event) -> bool:
        if not self._chat_id:
            logger.warning("TelegramNotifier: chat_id non configurato")
            return False

        # Rate limiting
        now = time.monotonic()
        elapsed = now - self._last_send_time
        if elapsed < MIN_INTERVAL_SECONDS:
            await asyncio.sleep(MIN_INTERVAL_SECONDS - elapsed)

        text = event.format_html()
        # Telegram ha un limite di 4096 chars per messaggio
        if len(text) > 4096:
            text = text[:4090] + "\n…"

        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        session = await self._get_session()

        for attempt in range(1, self._max_retries + 1):
            try:
                async with session.post(url, json=payload) as resp:
                    self._last_send_time = time.monotonic()

                    if resp.status == 200:
                        return True

                    body = await resp.text()

                    # Rate limited (429) — rispetta retry_after
                    if resp.status == 429:
                        try:
                            data = await resp.json()
                            retry_after = data.get("parameters", {}).get(
                                "retry_after", 5
                            )
                        except Exception:
                            retry_after = 5
                        logger.warning(
                            f"Telegram rate limit, retry tra {retry_after}s "
                            f"(attempt {attempt}/{self._max_retries})"
                        )
                        await asyncio.sleep(retry_after)
                        continue

                    # Server error — retry
                    if resp.status >= 500:
                        logger.warning(
                            f"Telegram server error {resp.status}, "
                            f"attempt {attempt}/{self._max_retries}"
                        )
                        await asyncio.sleep(2 ** attempt)
                        continue

                    # Client error (4xx non-429) — non ritentare
                    logger.error(
                        f"Telegram errore {resp.status}: {body[:200]}"
                    )
                    return False

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning(
                    f"Telegram network error: {e}, "
                    f"attempt {attempt}/{self._max_retries}"
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(2 ** attempt)
                continue

        logger.error(f"Telegram: max retry raggiunto per evento {event.category}")
        return False

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
