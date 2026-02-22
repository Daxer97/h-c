"""
watchdog/health_checker.py — Health check poller per il container app.

Pinga periodicamente un endpoint HTTP del container principale.
Se N check consecutivi falliscono → alert.
Quando torna healthy dopo un downtime → alert di recovery.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

import aiohttp

logger = logging.getLogger(__name__)


class HealthChecker:
    """
    Polling HTTP health check.

    Il container app espone un endpoint /health.
    Se non risponde per N volte consecutive → notifica errore.
    Quando torna su → notifica recovery.
    """

    def __init__(
        self,
        url: str,
        interval: int = 30,
        threshold: int = 3,
        event_callback=None,
    ):
        self._url = url
        self._interval = interval
        self._threshold = threshold
        self._callback = event_callback
        self._running = False
        self._task: asyncio.Task | None = None

        # State
        self._consecutive_failures = 0
        self._is_healthy = True
        self._alerted = False  # True se abbiamo già mandato alert per questo downtime
        self._last_check: datetime | None = None
        self._last_response_ms: float | None = None
        self._downtime_start: float | None = None

        # Stats
        self.stats = {
            "total_checks": 0,
            "total_failures": 0,
            "current_streak_failures": 0,
            "is_healthy": True,
            "last_check": None,
            "last_response_ms": None,
            "uptime_percent": 100.0,
        }

    async def _notify(self, severity: str, message: str, metadata: dict | None = None):
        if self._callback:
            await self._callback(severity, message, metadata or {})

    async def _check_once(self) -> tuple[bool, float, str]:
        """
        Esegue un singolo health check.
        Returns: (is_ok, response_time_ms, detail)
        """
        start = time.monotonic()
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as session:
                async with session.get(self._url) as resp:
                    elapsed_ms = (time.monotonic() - start) * 1000
                    if resp.status == 200:
                        return True, elapsed_ms, "OK"
                    else:
                        return False, elapsed_ms, f"HTTP {resp.status}"
        except aiohttp.ClientConnectorError:
            elapsed_ms = (time.monotonic() - start) * 1000
            return False, elapsed_ms, "Connection refused"
        except asyncio.TimeoutError:
            elapsed_ms = (time.monotonic() - start) * 1000
            return False, elapsed_ms, "Timeout"
        except Exception as e:
            elapsed_ms = (time.monotonic() - start) * 1000
            return False, elapsed_ms, str(e)

    async def _loop(self):
        """Loop principale di health checking."""
        logger.info(
            f"HealthChecker: polling {self._url} ogni {self._interval}s "
            f"(threshold: {self._threshold})"
        )

        while self._running:
            is_ok, response_ms, detail = await self._check_once()

            self.stats["total_checks"] += 1
            self._last_check = datetime.now(timezone.utc)
            self._last_response_ms = response_ms
            self.stats["last_check"] = self._last_check.isoformat()
            self.stats["last_response_ms"] = round(response_ms, 1)

            if is_ok:
                # Recovery da stato unhealthy
                if not self._is_healthy and self._alerted:
                    downtime = ""
                    if self._downtime_start:
                        dt = time.monotonic() - self._downtime_start
                        mins, secs = divmod(int(dt), 60)
                        downtime = f"{mins}m {secs}s"

                    await self._notify(
                        "info",
                        f"💚 Health check RECOVERED — '{self._url}' risponde "
                        f"({response_ms:.0f}ms)",
                        {
                            "downtime": downtime,
                            "failed_checks": self._consecutive_failures,
                        },
                    )

                self._consecutive_failures = 0
                self._is_healthy = True
                self._alerted = False
                self._downtime_start = None

            else:
                self._consecutive_failures += 1
                self.stats["total_failures"] += 1

                if self._consecutive_failures == 1:
                    self._downtime_start = time.monotonic()

                # Supera soglia → alert (una sola volta per downtime)
                if (
                    self._consecutive_failures >= self._threshold
                    and not self._alerted
                ):
                    self._is_healthy = False
                    self._alerted = True

                    await self._notify(
                        "error",
                        f"🏥 Health check FAILED — '{self._url}' non risponde "
                        f"da {self._consecutive_failures} check consecutivi.\n"
                        f"Ultimo errore: {detail}",
                        {
                            "consecutive_failures": self._consecutive_failures,
                            "last_error": detail,
                            "response_ms": round(response_ms, 1),
                        },
                    )

            self.stats["current_streak_failures"] = self._consecutive_failures
            self.stats["is_healthy"] = self._is_healthy

            # Calcola uptime %
            if self.stats["total_checks"] > 0:
                self.stats["uptime_percent"] = round(
                    (1 - self.stats["total_failures"] / self.stats["total_checks"])
                    * 100,
                    2,
                )

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
        return {**self.stats, "running": self._running, "url": self._url}
