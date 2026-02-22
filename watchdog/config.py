"""
watchdog/config.py — Configurazione del watchdog sidecar.
"""

import os

# ── Telegram ────────────────────────────────────────────────

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")

# ── Webhook ─────────────────────────────────────────────────

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
WEBHOOK_FORMAT = os.environ.get("WEBHOOK_FORMAT", "") or None

# ── Container da monitorare ─────────────────────────────────

# Nome del container principale (deve matchare container_name nel compose)
WATCHED_CONTAINER = os.environ.get("WATCHED_CONTAINER", "tempmail-bot")

# ── Health check ────────────────────────────────────────────

# URL health del container app (interno alla rete Docker)
HEALTH_CHECK_URL = os.environ.get(
    "HEALTH_CHECK_URL", "http://tempmail-bot:8080/health"
)
HEALTH_CHECK_INTERVAL = int(os.environ.get("HEALTH_CHECK_INTERVAL", "30"))
# Quanti check falliti consecutivi prima di alertare
HEALTH_CHECK_THRESHOLD = int(os.environ.get("HEALTH_CHECK_THRESHOLD", "3"))

# ── Host metrics ────────────────────────────────────────────

HOST_METRICS_INTERVAL = int(os.environ.get("HOST_METRICS_INTERVAL", "60"))

# Soglie per alert (percentuali)
CPU_THRESHOLD = float(os.environ.get("CPU_THRESHOLD", "90"))
RAM_THRESHOLD = float(os.environ.get("RAM_THRESHOLD", "85"))
DISK_THRESHOLD = float(os.environ.get("DISK_THRESHOLD", "90"))

# ── Logging ─────────────────────────────────────────────────

LOG_DIR = os.environ.get("LOG_DIR", "/app/logs")
