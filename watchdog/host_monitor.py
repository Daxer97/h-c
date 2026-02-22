"""
watchdog/host_monitor.py — Monitor risorse host.

Raccoglie metriche di sistema e alerta quando le soglie vengono superate.
Usa psutil per leggere CPU, RAM e disco.

Logica alert:
  - Alert quando una metrica supera la soglia
  - Un solo alert per "incidente" (non spamma)
  - Alert di recovery quando torna sotto soglia
"""

import asyncio
import logging
from datetime import datetime, timezone

import psutil

logger = logging.getLogger(__name__)


class HostMonitor:
    """
    Monitora le risorse dell'host: CPU, RAM, disco.

    Alerta quando una risorsa supera la soglia configurata.
    """

    def __init__(
        self,
        interval: int = 60,
        cpu_threshold: float = 90.0,
        ram_threshold: float = 85.0,
        disk_threshold: float = 90.0,
        event_callback=None,
    ):
        self._interval = interval
        self._thresholds = {
            "cpu": cpu_threshold,
            "ram": ram_threshold,
            "disk": disk_threshold,
        }
        self._callback = event_callback
        self._running = False
        self._task: asyncio.Task | None = None

        # Alert state: True = siamo sopra soglia e abbiamo già alertato
        self._alert_active = {
            "cpu": False,
            "ram": False,
            "disk": False,
        }

        # Ultime metriche
        self.metrics = {
            "cpu_percent": 0.0,
            "ram_percent": 0.0,
            "ram_used_gb": 0.0,
            "ram_total_gb": 0.0,
            "disk_percent": 0.0,
            "disk_used_gb": 0.0,
            "disk_total_gb": 0.0,
            "last_check": None,
        }

    async def _notify(self, severity: str, message: str, metadata: dict | None = None):
        if self._callback:
            await self._callback(severity, message, metadata or {})

    def _collect_metrics(self) -> dict:
        """Raccoglie metriche correnti."""
        # CPU (media su 1 secondo)
        cpu = psutil.cpu_percent(interval=1)

        # RAM
        mem = psutil.virtual_memory()
        ram_percent = mem.percent
        ram_used_gb = mem.used / (1024 ** 3)
        ram_total_gb = mem.total / (1024 ** 3)

        # Disco (root partition)
        disk = psutil.disk_usage("/")
        disk_percent = disk.percent
        disk_used_gb = disk.used / (1024 ** 3)
        disk_total_gb = disk.total / (1024 ** 3)

        return {
            "cpu_percent": round(cpu, 1),
            "ram_percent": round(ram_percent, 1),
            "ram_used_gb": round(ram_used_gb, 2),
            "ram_total_gb": round(ram_total_gb, 2),
            "disk_percent": round(disk_percent, 1),
            "disk_used_gb": round(disk_used_gb, 2),
            "disk_total_gb": round(disk_total_gb, 2),
        }

    async def _check_thresholds(self, current: dict):
        """Controlla le soglie e genera alert/recovery."""
        checks = [
            ("cpu", current["cpu_percent"], "CPU"),
            ("ram", current["ram_percent"], "RAM"),
            ("disk", current["disk_percent"], "Disco"),
        ]

        for key, value, label in checks:
            threshold = self._thresholds[key]
            was_active = self._alert_active[key]

            if value >= threshold and not was_active:
                # Nuovo alert
                self._alert_active[key] = True

                detail = self._format_detail(key, current)
                await self._notify(
                    "error" if value >= 95 else "warning",
                    f"📈 {label} al {value}% — sopra soglia ({threshold}%)!\n{detail}",
                    {
                        "metric": key,
                        "value": value,
                        "threshold": threshold,
                        **current,
                    },
                )

            elif value < threshold * 0.9 and was_active:
                # Recovery (con isteresi al 90% della soglia per evitare flapping)
                self._alert_active[key] = False

                await self._notify(
                    "info",
                    f"📉 {label} tornato a {value}% — sotto soglia ({threshold}%)",
                    {"metric": key, "value": value},
                )

    @staticmethod
    def _format_detail(key: str, metrics: dict) -> str:
        if key == "cpu":
            return f"CPU: {metrics['cpu_percent']}%"
        elif key == "ram":
            return (
                f"RAM: {metrics['ram_used_gb']:.1f}GB / "
                f"{metrics['ram_total_gb']:.1f}GB ({metrics['ram_percent']}%)"
            )
        elif key == "disk":
            return (
                f"Disco: {metrics['disk_used_gb']:.1f}GB / "
                f"{metrics['disk_total_gb']:.1f}GB ({metrics['disk_percent']}%)"
            )
        return ""

    async def _loop(self):
        """Loop principale di raccolta metriche."""
        logger.info(
            f"HostMonitor: check ogni {self._interval}s — "
            f"soglie CPU={self._thresholds['cpu']}% "
            f"RAM={self._thresholds['ram']}% "
            f"Disk={self._thresholds['disk']}%"
        )

        while self._running:
            try:
                current = self._collect_metrics()
                current["last_check"] = datetime.now(timezone.utc).isoformat()

                self.metrics.update(current)
                await self._check_thresholds(current)

            except Exception as e:
                logger.error(f"HostMonitor errore: {e}", exc_info=True)

            await asyncio.sleep(self._interval)

    def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    def get_status(self) -> dict:
        return {
            "running": self._running,
            "thresholds": self._thresholds,
            "alerts_active": {**self._alert_active},
            **self.metrics,
        }
