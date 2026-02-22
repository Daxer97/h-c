"""
watchdog/docker_monitor.py — Monitor eventi Docker via socket.

Ascolta il Docker daemon per eventi relativi al container monitorato:
  - die: container morto (crash, OOM, stop)
  - oom: Out of Memory killer attivato
  - start/restart: container riavviato
  - health_status: cambiamenti health check

Rileva anche restart loop (troppi restart in poco tempo).
"""

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timezone

import docker
from docker.errors import DockerException

logger = logging.getLogger(__name__)

# Restart loop: se N restart in M secondi → alert
RESTART_LOOP_COUNT = 3
RESTART_LOOP_WINDOW = 300  # 5 minuti


class DockerMonitor:
    """
    Ascolta eventi Docker per un container specifico.

    Usa il Docker SDK (via socket) per ricevere eventi in tempo reale.
    Quando rileva un evento significativo, chiama il callback.
    """

    def __init__(
        self,
        container_name: str,
        event_callback=None,
    ):
        self._container_name = container_name
        self._callback = event_callback
        self._running = False
        self._task: asyncio.Task | None = None
        self._client: docker.DockerClient | None = None

        # Tracking restart loop
        self._restart_times: deque[float] = deque(maxlen=RESTART_LOOP_COUNT * 2)
        self._last_status: str = "unknown"

        # Statistiche
        self.stats = {
            "events_received": 0,
            "last_event": None,
            "last_event_time": None,
            "restart_count": 0,
            "oom_count": 0,
            "current_status": "unknown",
        }

    async def _notify(self, severity: str, message: str, metadata: dict | None = None):
        if self._callback:
            await self._callback(severity, message, metadata or {})

    def _get_client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.DockerClient(
                base_url="unix:///var/run/docker.sock", timeout=10
            )
        return self._client

    def _get_container_status(self) -> dict | None:
        """Recupera lo stato corrente del container."""
        try:
            client = self._get_client()
            container = client.containers.get(self._container_name)
            return {
                "status": container.status,
                "restart_count": container.attrs.get("RestartCount", 0),
                "started_at": container.attrs.get("State", {}).get("StartedAt", ""),
                "finished_at": container.attrs.get("State", {}).get("FinishedAt", ""),
                "exit_code": container.attrs.get("State", {}).get("ExitCode", -1),
                "oom_killed": container.attrs.get("State", {}).get("OOMKilled", False),
            }
        except docker.errors.NotFound:
            return None
        except Exception as e:
            logger.error(f"Errore lettura stato container: {e}")
            return None

    def _check_restart_loop(self) -> bool:
        """Controlla se siamo in un restart loop."""
        now = time.time()
        self._restart_times.append(now)

        # Conta restart nella finestra temporale
        recent = [t for t in self._restart_times if now - t < RESTART_LOOP_WINDOW]
        return len(recent) >= RESTART_LOOP_COUNT

    async def _process_event(self, event: dict):
        """Processa un singolo evento Docker."""
        action = event.get("Action", "")
        actor = event.get("Actor", {})
        attributes = actor.get("Attributes", {})
        container_name = attributes.get("name", "")

        # Filtra solo il container che ci interessa
        if container_name != self._container_name:
            return

        self.stats["events_received"] += 1
        self.stats["last_event"] = action
        self.stats["last_event_time"] = datetime.now(timezone.utc).isoformat()

        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        base_meta = {
            "container": container_name,
            "timestamp": timestamp,
        }

        if action == "die":
            exit_code = attributes.get("exitCode", "?")
            base_meta["exit_code"] = exit_code

            # Controlla se è OOM
            status = self._get_container_status()
            if status and status.get("oom_killed"):
                self.stats["oom_count"] += 1
                await self._notify(
                    "critical",
                    f"💀 Container '{container_name}' ucciso da OOM Killer!\n"
                    f"Il container ha esaurito la memoria allocata.",
                    {**base_meta, "reason": "OOM Killed"},
                )
            else:
                severity = "critical" if exit_code != "0" else "warning"
                await self._notify(
                    severity,
                    f"💀 Container '{container_name}' terminato con exit code {exit_code}",
                    base_meta,
                )

        elif action == "start":
            self.stats["restart_count"] += 1

            # Check restart loop
            if self._check_restart_loop():
                await self._notify(
                    "critical",
                    f"🔄 RESTART LOOP rilevato! '{container_name}' ha fatto "
                    f"{RESTART_LOOP_COUNT}+ restart in {RESTART_LOOP_WINDOW}s.\n"
                    f"Probabile crash ripetuto.",
                    {**base_meta, "restart_count": self.stats["restart_count"]},
                )
            elif self.stats["restart_count"] > 1:
                # Non il primo start (che è normale)
                await self._notify(
                    "warning",
                    f"🔄 Container '{container_name}' riavviato "
                    f"(restart #{self.stats['restart_count']})",
                    base_meta,
                )

        elif action == "oom":
            self.stats["oom_count"] += 1
            await self._notify(
                "critical",
                f"🧠 OOM event su '{container_name}'! "
                f"Il sistema ha esaurito la memoria.",
                base_meta,
            )

        elif action == "stop":
            await self._notify(
                "info",
                f"⏹️ Container '{container_name}' fermato",
                base_meta,
            )

        elif action == "kill":
            signal = attributes.get("signal", "?")
            await self._notify(
                "warning",
                f"⚡ Container '{container_name}' killato con signal {signal}",
                {**base_meta, "signal": signal},
            )

        elif action.startswith("health_status"):
            health = action.split(": ", 1)[-1] if ": " in action else action
            prev = self._last_status

            if health == "unhealthy" and prev != "unhealthy":
                await self._notify(
                    "error",
                    f"🏥 Container '{container_name}' è diventato UNHEALTHY",
                    {**base_meta, "health_status": health, "previous": prev},
                )
            elif health == "healthy" and prev == "unhealthy":
                await self._notify(
                    "info",
                    f"💚 Container '{container_name}' tornato HEALTHY",
                    {**base_meta, "health_status": health},
                )

            self._last_status = health

        # Aggiorna status
        status = self._get_container_status()
        if status:
            self.stats["current_status"] = status["status"]

    def _iter_events_blocking(self):
        """Blocking generator that yields Docker events. Runs in executor."""
        client = self._get_client()
        events = client.events(
            decode=True,
            filters={
                "type": "container",
                "event": [
                    "die", "start", "stop", "kill", "oom",
                    "health_status",
                ],
            },
        )
        for event in events:
            if not self._running:
                break
            yield event

    async def _event_loop(self):
        """Loop principale — ascolta eventi Docker."""
        logger.info(f"DockerMonitor: ascolto eventi per '{self._container_name}'")

        while self._running:
            try:
                # Verifica che il container esista
                status = self._get_container_status()
                if status:
                    self.stats["current_status"] = status["status"]
                    logger.info(
                        f"Container '{self._container_name}' trovato, "
                        f"status: {status['status']}"
                    )
                else:
                    await self._notify(
                        "warning",
                        f"⚠️ Container '{self._container_name}' non trovato! "
                        f"Attendo che venga creato...",
                        {},
                    )

                # Read events in a thread to avoid blocking the asyncio loop
                loop = asyncio.get_running_loop()
                queue: asyncio.Queue = asyncio.Queue()

                def _reader():
                    try:
                        for event in self._iter_events_blocking():
                            loop.call_soon_threadsafe(queue.put_nowait, event)
                    except Exception as e:
                        loop.call_soon_threadsafe(queue.put_nowait, e)

                reader_future = loop.run_in_executor(None, _reader)

                while self._running:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    if isinstance(item, Exception):
                        raise item
                    await self._process_event(item)

                reader_future.cancel()

            except DockerException as e:
                logger.error(f"DockerMonitor: errore Docker: {e}")
                await self._notify(
                    "error",
                    f"DockerMonitor: impossibile connettersi al Docker daemon: {e}",
                    {},
                )
                await asyncio.sleep(10)

            except Exception as e:
                logger.error(f"DockerMonitor: errore imprevisto: {e}", exc_info=True)
                await asyncio.sleep(5)

    def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._event_loop())

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
        if self._client:
            self._client.close()

    def get_status(self) -> dict:
        return {**self.stats, "running": self._running}
