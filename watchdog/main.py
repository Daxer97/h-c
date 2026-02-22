"""
watchdog/main.py — Orchestratore del sidecar watchdog.

Avvia tutti i monitor e instrada gli eventi nel NotificationBus
(stessa infrastruttura del bot principale).

Monitor attivi:
  1. DockerMonitor — eventi container (die, OOM, restart loop)
  2. HealthChecker — polling HTTP health endpoint
  3. HostMonitor — CPU, RAM, disco
"""

import asyncio
import logging
import signal

from config import (
    BOT_TOKEN,
    ADMIN_CHAT_ID,
    WEBHOOK_URL,
    WEBHOOK_FORMAT,
    LOG_DIR,
    WATCHED_CONTAINER,
    HEALTH_CHECK_URL,
    HEALTH_CHECK_INTERVAL,
    HEALTH_CHECK_THRESHOLD,
    HOST_METRICS_INTERVAL,
    CPU_THRESHOLD,
    RAM_THRESHOLD,
    DISK_THRESHOLD,
)
from notifications import (
    setup_notifications,
    NotificationBus,
    LifecycleEmitter,
    EventCategory,
    Severity,
)
from notifications.events import Event
from docker_monitor import DockerMonitor
from health_checker import HealthChecker
from host_monitor import HostMonitor

# ── Logging ─────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ── Globals ─────────────────────────────────────────────────

bus: NotificationBus | None = None
lifecycle: LifecycleEmitter | None = None
docker_mon: DockerMonitor | None = None
health_chk: HealthChecker | None = None
host_mon: HostMonitor | None = None

_shutdown_event = asyncio.Event()


# ── Callback bridge ─────────────────────────────────────────
# I monitor usano callback generici (severity: str, message: str).
# Qui li bridgiamo al NotificationBus con la severity corretta.

SEVERITY_MAP = {
    "debug": Severity.DEBUG,
    "info": Severity.INFO,
    "warning": Severity.WARNING,
    "error": Severity.ERROR,
    "critical": Severity.CRITICAL,
}


async def docker_event_callback(severity: str, message: str, metadata: dict):
    """Callback dal DockerMonitor → NotificationBus."""
    if bus:
        sev = SEVERITY_MAP.get(severity, Severity.WARNING)
        await bus.emit(Event(
            severity=sev,
            category=EventCategory.SYSTEM,
            message=message,
            source="docker_monitor",
            metadata=metadata,
        ))


async def health_event_callback(severity: str, message: str, metadata: dict):
    """Callback dal HealthChecker → NotificationBus."""
    if bus:
        sev = SEVERITY_MAP.get(severity, Severity.WARNING)
        await bus.emit(Event(
            severity=sev,
            category=EventCategory.SYSTEM,
            message=message,
            source="health_checker",
            metadata=metadata,
        ))


async def host_event_callback(severity: str, message: str, metadata: dict):
    """Callback dal HostMonitor → NotificationBus."""
    if bus:
        sev = SEVERITY_MAP.get(severity, Severity.WARNING)
        await bus.emit(Event(
            severity=sev,
            category=EventCategory.SYSTEM,
            message=message,
            source="host_monitor",
            metadata=metadata,
        ))


# ── Startup / Shutdown ──────────────────────────────────────


async def startup():
    global bus, lifecycle, docker_mon, health_chk, host_mon

    # 1. Notification bus
    bus = await setup_notifications(
        telegram_token=BOT_TOKEN,
        telegram_chat_id=ADMIN_CHAT_ID,
        webhook_url=WEBHOOK_URL,
        webhook_format=WEBHOOK_FORMAT,
        log_dir=LOG_DIR,
        install_hooks=True,
    )

    lifecycle = LifecycleEmitter(bus)
    await lifecycle.startup()
    await bus.info(
        f"🐕 Watchdog avviato — monitoraggio '{WATCHED_CONTAINER}'",
        category=EventCategory.LIFECYCLE,
        source="watchdog",
    )

    # 2. Docker events monitor
    docker_mon = DockerMonitor(
        container_name=WATCHED_CONTAINER,
        event_callback=docker_event_callback,
    )
    docker_mon.start()

    # 3. Health checker
    health_chk = HealthChecker(
        url=HEALTH_CHECK_URL,
        interval=HEALTH_CHECK_INTERVAL,
        threshold=HEALTH_CHECK_THRESHOLD,
        event_callback=health_event_callback,
    )
    health_chk.start()

    # 4. Host metrics
    host_mon = HostMonitor(
        interval=HOST_METRICS_INTERVAL,
        cpu_threshold=CPU_THRESHOLD,
        ram_threshold=RAM_THRESHOLD,
        disk_threshold=DISK_THRESHOLD,
        event_callback=host_event_callback,
    )
    host_mon.start()

    logger.info(
        f"Watchdog operativo — "
        f"Docker events ✅ | Health check ✅ ({HEALTH_CHECK_URL}) | "
        f"Host metrics ✅ (CPU>{CPU_THRESHOLD}% RAM>{RAM_THRESHOLD}% "
        f"Disk>{DISK_THRESHOLD}%)"
    )


async def shutdown():
    logger.info("Watchdog: shutdown...")

    if docker_mon:
        docker_mon.stop()
    if health_chk:
        health_chk.stop()
    if host_mon:
        host_mon.stop()

    if lifecycle:
        await lifecycle.shutdown(reason="Watchdog shutdown")

    if bus:
        await bus.close()


# ── Status report periodico (opzionale) ─────────────────────


async def periodic_status_report(interval: int = 3600):
    """Invia un report di stato ogni N secondi (default: 1 ora)."""
    await asyncio.sleep(60)  # Attendi che tutto si stabilizzi

    while not _shutdown_event.is_set():
        try:
            if bus and docker_mon and health_chk and host_mon:
                d_stats = docker_mon.get_status()
                h_stats = health_chk.get_status()
                m_stats = host_mon.get_status()

                report = (
                    f"📊 Watchdog Status Report\n\n"
                    f"Container '{WATCHED_CONTAINER}': "
                    f"{d_stats.get('current_status', '?')}\n"
                    f"Restart count: {d_stats.get('restart_count', 0)} | "
                    f"OOM count: {d_stats.get('oom_count', 0)}\n\n"
                    f"Health: {'✅' if h_stats.get('is_healthy') else '❌'} "
                    f"(uptime {h_stats.get('uptime_percent', 0)}%)\n"
                    f"Last response: {h_stats.get('last_response_ms', '?')}ms\n\n"
                    f"CPU: {m_stats.get('cpu_percent', 0)}% | "
                    f"RAM: {m_stats.get('ram_percent', 0)}% "
                    f"({m_stats.get('ram_used_gb', 0):.1f}/"
                    f"{m_stats.get('ram_total_gb', 0):.1f}GB) | "
                    f"Disk: {m_stats.get('disk_percent', 0)}%"
                )

                await bus.debug(
                    report,
                    category=EventCategory.SYSTEM,
                    source="watchdog_report",
                )

        except Exception as e:
            logger.error(f"Errore status report: {e}")

        # Attendi con possibilità di interruzione
        try:
            await asyncio.wait_for(
                _shutdown_event.wait(), timeout=interval
            )
            break  # Se l'evento è settato, esci
        except asyncio.TimeoutError:
            continue


# ── Signal handling ──────────────────────────────────────────


def handle_signal(sig):
    logger.info(f"Ricevuto signal {sig.name}")
    _shutdown_event.set()


# ── Main ─────────────────────────────────────────────────────


async def main():
    loop = asyncio.get_running_loop()

    # Registra signal handlers
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: handle_signal(s))

    await startup()

    # Avvia report periodico in background
    report_task = asyncio.create_task(periodic_status_report(3600))

    # Attendi shutdown signal
    await _shutdown_event.wait()

    report_task.cancel()
    await shutdown()


if __name__ == "__main__":
    asyncio.run(main())
