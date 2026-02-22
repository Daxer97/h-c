"""
health.py — Endpoint HTTP /health per il container del bot.

Server aiohttp minimale su porta 8080.
Risponde 200 se il bot è operativo, 503 altrimenti.
Usato dal watchdog HealthChecker e dal Docker HEALTHCHECK.
"""

import asyncio
import logging
import time

from aiohttp import web

logger = logging.getLogger(__name__)

# Stato globale — aggiornato dal bot
_health_state = {
    "healthy": False,
    "started_at": None,
    "last_activity": None,
    "details": {},
}


def set_healthy(healthy: bool = True, **details):
    """Chiamato dal bot per aggiornare lo stato di salute."""
    _health_state["healthy"] = healthy
    _health_state["last_activity"] = time.time()
    if details:
        _health_state["details"].update(details)


def set_started():
    """Chiamato all'avvio del bot."""
    _health_state["started_at"] = time.time()
    _health_state["healthy"] = True


async def health_handler(request: web.Request) -> web.Response:
    """Handler GET /health."""
    if _health_state["healthy"]:
        uptime = ""
        if _health_state["started_at"]:
            secs = int(time.time() - _health_state["started_at"])
            hours, rem = divmod(secs, 3600)
            mins, s = divmod(rem, 60)
            uptime = f"{hours}h {mins}m {s}s"

        return web.json_response(
            {
                "status": "healthy",
                "uptime": uptime,
                **_health_state["details"],
            },
            status=200,
        )
    else:
        return web.json_response(
            {"status": "unhealthy", **_health_state["details"]},
            status=503,
        )


async def start_health_server(port: int = 8080):
    """Avvia il server health in background."""
    app = web.Application()
    app.router.add_get("/health", health_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Health server avviato su :{port}/health")
    return runner  # Tienilo per cleanup
