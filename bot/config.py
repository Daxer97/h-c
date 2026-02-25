"""
config.py — Configurazione centralizzata.
Selectors, URL, proxy, e parametri di monitoraggio.

NOTA: Se il monitor rileva un cambiamento nella struttura della pagina,
aggiorna i selectors qui sotto.
"""

import os
import random
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# ── Telegram ────────────────────────────────────────────────

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_IDS = os.environ.get("ALLOWED_USER_IDS", "")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")  # Chat per alert del monitor

# ── Higgsfield URLs ─────────────────────────────────────────

HIGGSFIELD_SIGNUP_URL = "https://higgsfield.ai/auth/email/sign-up"
HIGGSFIELD_SIGNIN_URL = "https://higgsfield.ai/auth/email/sign-in"
HIGGSFIELD_VERIFY_URL = "https://higgsfield.ai/auth/email/verify"

# ── Higgsfield Selectors ───────────────────────────────────
# Next.js app — selectors basati su attributi semantici.
# Aggiornali se il monitor rileva cambiamenti.

SELECTORS = {
    # Sign-up form
    "email_input": 'input[type="email"], input[name="email"], input[placeholder*="mail" i]',
    "password_input": 'input[type="password"], input[name="password"]',
    "confirm_password_input": 'input[name="confirmPassword"], input[placeholder*="confirm" i]',
    "submit_button": 'button[type="submit"], button:has-text("Sign up"), button:has-text("Create")',

    # Verification page
    "verify_success": 'text=/verified|success|welcome/i',

    # Possibili CAPTCHA
    "captcha_frame": 'iframe[src*="captcha"], iframe[src*="recaptcha"], iframe[src*="hcaptcha"]',

    # Elementi per fingerprint struttura pagina (monitor)
    "form_container": "form, [class*='auth'], [class*='sign']",
}

# ── Proxy ───────────────────────────────────────────────────
# Formato: protocollo://user:pass@host:port
# Se multipli, separati da virgola per rotation.
# Supporta http, https, socks5.
# Es: "socks5://user:pass@proxy1:1080,http://proxy2:8080"

PROXY_LIST_RAW = os.environ.get("PROXY_LIST", "")


def get_proxy_list() -> list[str]:
    """Parsa la lista proxy dall'env var."""
    if not PROXY_LIST_RAW.strip():
        return []
    return [p.strip() for p in PROXY_LIST_RAW.split(",") if p.strip()]


def get_random_proxy() -> str | None:
    """Restituisce un proxy random dalla lista, o None."""
    proxies = get_proxy_list()
    return random.choice(proxies) if proxies else None


# ── Webhook (notifiche esterne opzionali) ───────────────────

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
# Formato: None (raw JSON), "slack", "discord", o callable
WEBHOOK_FORMAT = os.environ.get("WEBHOOK_FORMAT", "") or None

# ── Logging ─────────────────────────────────────────────────

LOG_DIR = os.environ.get("LOG_DIR", "/app/logs")

# ── Monitor ─────────────────────────────────────────────────

# Intervallo check struttura pagina (secondi)
MONITOR_INTERVAL = int(os.environ.get("MONITOR_INTERVAL", "3600"))  # default 1h

# ── Registration defaults ───────────────────────────────────

DEFAULT_PASSWORD_LENGTH = 16
REGISTRATION_TIMEOUT = 60  # secondi max per il flow Playwright
EMAIL_WAIT_TIMEOUT = 120   # secondi max per attendere email verifica
EMAIL_POLL_INTERVAL = 5    # secondi tra ogni check inbox
