"""
notifications — Sistema di notifica pluggabile.

Uso rapido:
    from notifications import setup_notifications

    bus = await setup_notifications()
    await bus.info("Tutto ok!")
    await bus.error("Qualcosa è andato storto", exception=e)

Per registrare notifier custom:
    from notifications import NotificationBus, BaseNotifier

    class MyNotifier(BaseNotifier):
        async def send(self, event): ...

    bus = NotificationBus.get_bus()
    bus.register(MyNotifier(name="custom"))
"""

from notifications.events import Event, Severity, EventCategory
from notifications.base import BaseNotifier
from notifications.bus import NotificationBus
from notifications.telegram_notifier import TelegramNotifier
from notifications.webhook_notifier import WebhookNotifier
from notifications.file_notifier import FileNotifier
from notifications.crash_handler import (
    LoggingBridge,
    install_exception_hooks,
    LifecycleEmitter,
)

__all__ = [
    # Core
    "Event",
    "Severity",
    "EventCategory",
    "BaseNotifier",
    "NotificationBus",
    # Notifiers
    "TelegramNotifier",
    "WebhookNotifier",
    "FileNotifier",
    # Crash handling
    "LoggingBridge",
    "install_exception_hooks",
    "LifecycleEmitter",
    # Factory
    "setup_notifications",
]


async def setup_notifications(
    telegram_token: str = "",
    telegram_chat_id: str = "",
    webhook_url: str = "",
    webhook_format: str | None = None,
    log_dir: str = "/app/logs",
    install_hooks: bool = True,
) -> NotificationBus:
    """
    Factory function — configura il bus con i notifier abilitati.

    Registra automaticamente:
      - FileNotifier (sempre, come fallback)
      - TelegramNotifier (se token + chat_id presenti)
      - WebhookNotifier (se url presente)
      - LoggingBridge (se install_hooks=True)
      - Exception hooks (se install_hooks=True)

    Returns:
        NotificationBus configurato e pronto all'uso.
    """
    import logging

    bus = NotificationBus.get_bus()

    # 1. File/Console — sempre attivo (safety net)
    bus.register(FileNotifier(
        log_dir=log_dir,
        min_severity=Severity.DEBUG,
        name="file",
    ))

    # 2. Telegram — se configurato
    if telegram_token and telegram_chat_id:
        bus.register(TelegramNotifier(
            bot_token=telegram_token,
            chat_id=telegram_chat_id,
            min_severity=Severity.INFO,
            name="telegram",
        ))

    # 3. Webhook — se configurato
    if webhook_url:
        bus.register(WebhookNotifier(
            url=webhook_url,
            payload_builder=webhook_format,
            min_severity=Severity.WARNING,
            name="webhook",
        ))

    # 4. Logging bridge — cattura log Python >= ERROR
    if install_hooks:
        bridge = LoggingBridge(bus, min_level=logging.ERROR)
        logging.getLogger().addHandler(bridge)
        install_exception_hooks(bus)

    return bus
