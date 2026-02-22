"""
notifications/bus.py — Bus centrale di notifica.

Singleton che gestisce il dispatch degli eventi a tutti i notifier registrati.
Thread-safe per uso con asyncio.

Uso:
    bus = NotificationBus()
    bus.register(TelegramNotifier(...))
    bus.register(WebhookNotifier(...))
    await bus.emit(Event(severity=Severity.ERROR, ...))

    # Shortcut per emissioni rapide:
    await bus.info("Bot avviato", category="lifecycle")
    await bus.error("Registrazione fallita", category="registration", exception=e)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from notifications.events import Event, Severity, EventCategory
from notifications.base import BaseNotifier

logger = logging.getLogger(__name__)


class NotificationBus:
    """
    Bus centrale — smista eventi ai notifier registrati.

    Pattern singleton soft: puoi avere più istanze se serve (testing),
    ma in produzione usi get_bus() per avere l'istanza globale.
    """

    _instance: NotificationBus | None = None

    def __init__(self):
        self._notifiers: list[BaseNotifier] = []
        self._event_log: list[Event] = []
        self._max_log_size = 100

    @classmethod
    def get_bus(cls) -> NotificationBus:
        """Restituisce l'istanza singleton."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── Registrazione notifier ──────────────────────────────

    def register(self, notifier: BaseNotifier) -> None:
        """Registra un notifier."""
        self._notifiers.append(notifier)
        logger.info(f"Notifier registrato: {notifier}")

    def unregister(self, name: str) -> bool:
        """Rimuovi un notifier per nome."""
        before = len(self._notifiers)
        self._notifiers = [n for n in self._notifiers if n.name != name]
        removed = len(self._notifiers) < before
        if removed:
            logger.info(f"Notifier rimosso: {name}")
        return removed

    @property
    def notifiers(self) -> list[BaseNotifier]:
        return list(self._notifiers)

    # ── Dispatch ────────────────────────────────────────────

    async def emit(self, event: Event) -> dict[str, bool]:
        """
        Dispatch evento a tutti i notifier che lo accettano.

        Returns:
            Dict {notifier_name: success} per ogni notifier che ha tentato l'invio.
        """
        # Log interno
        self._event_log.append(event)
        if len(self._event_log) > self._max_log_size:
            self._event_log = self._event_log[-self._max_log_size:]

        results = {}
        tasks = []

        for notifier in self._notifiers:
            if notifier.accepts(event):
                tasks.append((notifier.name, notifier.send(event)))

        if not tasks:
            return results

        # Esegui tutti i send in parallelo
        coros = [t[1] for t in tasks]
        names = [t[0] for t in tasks]

        outcomes = await asyncio.gather(*coros, return_exceptions=True)

        for name, outcome in zip(names, outcomes):
            if isinstance(outcome, Exception):
                logger.error(
                    f"Notifier '{name}' ha fallito: {outcome}", exc_info=outcome
                )
                results[name] = False
            else:
                results[name] = bool(outcome)

        return results

    # ── Shortcut methods ────────────────────────────────────

    async def debug(
        self, message: str, category: str = EventCategory.SYSTEM, **kwargs
    ) -> dict[str, bool]:
        return await self.emit(
            Event(severity=Severity.DEBUG, category=category, message=message, **kwargs)
        )

    async def info(
        self, message: str, category: str = EventCategory.SYSTEM, **kwargs
    ) -> dict[str, bool]:
        return await self.emit(
            Event(severity=Severity.INFO, category=category, message=message, **kwargs)
        )

    async def warning(
        self, message: str, category: str = EventCategory.SYSTEM, **kwargs
    ) -> dict[str, bool]:
        return await self.emit(
            Event(
                severity=Severity.WARNING, category=category, message=message, **kwargs
            )
        )

    async def error(
        self,
        message: str,
        category: str = EventCategory.SYSTEM,
        exception: BaseException | None = None,
        **kwargs,
    ) -> dict[str, bool]:
        return await self.emit(
            Event(
                severity=Severity.ERROR,
                category=category,
                message=message,
                exception=exception,
                **kwargs,
            )
        )

    async def critical(
        self,
        message: str,
        category: str = EventCategory.CRASH,
        exception: BaseException | None = None,
        **kwargs,
    ) -> dict[str, bool]:
        return await self.emit(
            Event(
                severity=Severity.CRITICAL,
                category=category,
                message=message,
                exception=exception,
                **kwargs,
            )
        )

    # ── Lifecycle ───────────────────────────────────────────

    async def close(self):
        """Chiudi tutti i notifier registrati."""
        for notifier in self._notifiers:
            try:
                await notifier.close()
            except Exception as e:
                logger.error(f"Errore chiusura notifier '{notifier.name}': {e}")

    # ── Diagnostica ─────────────────────────────────────────

    def get_status(self) -> dict[str, Any]:
        """Status del bus per debug/monitoring."""
        return {
            "notifiers": [
                {
                    "name": n.name,
                    "type": n.__class__.__name__,
                    "min_severity": n.min_severity.label,
                    "enabled": n.enabled,
                }
                for n in self._notifiers
            ],
            "event_log_size": len(self._event_log),
            "last_event": (
                self._event_log[-1].format_plain() if self._event_log else None
            ),
        }

    @property
    def recent_events(self) -> list[Event]:
        return list(self._event_log[-20:])
