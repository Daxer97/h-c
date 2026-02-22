"""
notifications/crash_handler.py — Cattura automatica errori e crash.

Tre livelli di cattura:

1. LoggingBridge — Custom logging.Handler che intercetta WARNING/ERROR/CRITICAL
   dal sistema di logging Python e li inoltra al NotificationBus.

2. install_exception_hooks() — Hook su:
   - sys.excepthook → eccezioni non catturate nel main thread
   - asyncio loop exception handler → eccezioni in task async

3. LifecycleEmitter — Helper per emettere eventi di lifecycle
   (startup, shutdown, restart) in modo consistente.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import platform
from datetime import datetime, timezone

from notifications.events import Event, Severity, EventCategory

logger = logging.getLogger(__name__)


class LoggingBridge(logging.Handler):
    """
    Logging handler che cattura log Python e li instrada al NotificationBus.

    IMPORTANTE: Per evitare loop infiniti, questo handler ignora i log
    generati dal package notifications stesso.
    """

    def __init__(
        self,
        bus,  # NotificationBus — no type hint per evitare import circolare
        min_level: int = logging.ERROR,
    ):
        super().__init__(level=min_level)
        self._bus = bus
        # Nomi di logger da ignorare per evitare loop
        self._ignored_loggers = {
            "notifications",
            "notifications.bus",
            "notifications.telegram_notifier",
            "notifications.webhook_notifier",
            "notifications.file_notifier",
            "notifications.file",
        }

    def emit(self, record: logging.LogRecord):
        # Anti-loop: ignora log dal package notifications
        if any(record.name.startswith(prefix) for prefix in self._ignored_loggers):
            return

        # Mappa logging level → Severity
        severity_map = {
            logging.WARNING: Severity.WARNING,
            logging.ERROR: Severity.ERROR,
            logging.CRITICAL: Severity.CRITICAL,
        }
        severity = severity_map.get(record.levelno, Severity.ERROR)

        event = Event(
            severity=severity,
            category=EventCategory.SYSTEM,
            message=record.getMessage(),
            source=record.name,
            exception=record.exc_info[1] if record.exc_info and record.exc_info[1] else None,
        )

        # Schedule async emit sul loop corrente
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._safe_emit(event))
        except RuntimeError:
            # Nessun loop attivo — fallback sincrono (solo console)
            print(f"[LoggingBridge/no-loop] {event.format_plain()}", flush=True)

    async def _safe_emit(self, event: Event):
        try:
            await self._bus.emit(event)
        except Exception as e:
            print(f"[LoggingBridge] Errore emit: {e}", flush=True)


def install_exception_hooks(bus) -> None:
    """
    Installa hook per catturare eccezioni non gestite.

    Cattura:
      - Eccezioni nel main thread (sys.excepthook)
      - Eccezioni in task asyncio (loop.set_exception_handler)
    """

    # ── sys.excepthook ──────────────────────────────────────
    original_excepthook = sys.excepthook

    def custom_excepthook(exc_type, exc_value, exc_tb):
        # Ignora KeyboardInterrupt
        if issubclass(exc_type, KeyboardInterrupt):
            original_excepthook(exc_type, exc_value, exc_tb)
            return

        event = Event(
            severity=Severity.CRITICAL,
            category=EventCategory.CRASH,
            message=f"Eccezione non gestita: {exc_type.__name__}: {exc_value}",
            source="sys.excepthook",
            exception=exc_value,
        )

        # Prova a emettere async, fallback sincrono
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(bus.emit(event))
        except RuntimeError:
            print(f"[CRASH] {event.format_plain()}", flush=True)

        original_excepthook(exc_type, exc_value, exc_tb)

    sys.excepthook = custom_excepthook

    # ── asyncio exception handler ───────────────────────────
    def asyncio_exception_handler(loop, context):
        exception = context.get("exception")
        message = context.get("message", "Errore asyncio sconosciuto")

        event = Event(
            severity=Severity.CRITICAL,
            category=EventCategory.CRASH,
            message=f"Errore asyncio: {message}",
            source="asyncio.exception_handler",
            exception=exception,
            metadata={
                "context_keys": list(context.keys()),
            },
        )

        # Non possiamo usare await qui — schedule come task
        loop.create_task(bus.emit(event))

    try:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(asyncio_exception_handler)
    except RuntimeError:
        pass  # Verrà installato quando il loop parte

    logger.info("Exception hooks installati (sys.excepthook + asyncio)")


class LifecycleEmitter:
    """
    Helper per emettere eventi di lifecycle in modo consistente.

    Uso:
        lifecycle = LifecycleEmitter(bus)
        await lifecycle.startup()
        ...
        await lifecycle.shutdown(reason="Sigterm ricevuto")
    """

    def __init__(self, bus):
        self._bus = bus
        self._start_time: datetime | None = None

    async def startup(self):
        """Emetti evento di avvio."""
        self._start_time = datetime.now(timezone.utc)
        await self._bus.info(
            "🟢 Bot avviato",
            category=EventCategory.LIFECYCLE,
            source="lifecycle",
            metadata={
                "python": platform.python_version(),
                "platform": platform.platform(),
                "pid": str(sys.modules.get("os", type("", (), {"getpid": lambda: "?"})).__class__),
            },
        )

    async def shutdown(self, reason: str = "shutdown normale"):
        """Emetti evento di shutdown."""
        uptime = ""
        if self._start_time:
            delta = datetime.now(timezone.utc) - self._start_time
            hours, rem = divmod(int(delta.total_seconds()), 3600)
            minutes, seconds = divmod(rem, 60)
            uptime = f"{hours}h {minutes}m {seconds}s"

        await self._bus.info(
            f"🔴 Bot in shutdown — {reason}",
            category=EventCategory.LIFECYCLE,
            source="lifecycle",
            metadata={"uptime": uptime} if uptime else {},
        )

    async def error_restart(self, error: Exception):
        """Emetti evento di crash/restart."""
        await self._bus.critical(
            f"💥 Bot crash — tentativo di restart",
            category=EventCategory.CRASH,
            source="lifecycle",
            exception=error,
        )
