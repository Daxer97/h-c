# 📬 TempMail + Higgsfield Auto-Registration Bot

Bot Telegram con:
- **Email temporanee** via [mail.tm](https://mail.tm) API
- **Auto-registrazione Higgsfield** via Playwright headless
- **Page monitor** che ti avvisa su Telegram se la struttura della pagina cambia
- **Proxy rotation** per IP rotation

## Quick Start (Docker)

```bash
# 1. Clona e configura
cp .env.example .env
# Modifica .env → inserisci TELEGRAM_BOT_TOKEN e ADMIN_CHAT_ID

# 2. Build e run
docker compose up -d

# 3. Verifica logs
docker compose logs -f
```

## Quick Start (senza Docker)

Richiede Python 3.12+ e le dipendenze di sistema per Chromium.

```bash
# 1. Installa dipendenze di sistema (Debian/Ubuntu)
sudo apt-get update && sudo apt-get install -y \
    wget ca-certificates fonts-liberation libasound2t64 \
    libatk-bridge2.0-0 libatk1.0-0 libcups2 libdbus-1-3 \
    libdrm2 libgbm1 libgtk-3-0 libnspr4 libnss3 \
    libx11-xcb1 libxcomposite1 libxdamage1 libxrandr2 xdg-utils

# 2. Crea virtual environment e installa dipendenze Python
python3 -m venv .venv
source .venv/bin/activate
pip install -r bot/requirements.txt

# 3. Installa Chromium per Playwright
playwright install chromium

# 4. Configura environment
cp .env.example .env
# Modifica .env → inserisci TELEGRAM_BOT_TOKEN e ADMIN_CHAT_ID
# Cambia LOG_DIR se vuoi (default: /app/logs)
#   LOG_DIR=./logs

# 5. Avvia il bot
cd bot
python main.py
```

> **Nota:** il watchdog (`watchdog/main.py`) monitora container Docker, quindi
> ha senso solo dentro Docker. Senza Docker il bot gira comunque —
> perdi solo il monitoraggio infrastruttura.

## Comandi Bot

### 📧 Email Temporanee
| Comando | Descrizione |
|---------|-------------|
| `/newemail` | Crea email temporanea |
| `/check` | Controlla inbox |
| `/wait` | Attendi messaggio (polling 2 min) |
| `/read <n>` | Leggi messaggio n |
| `/links` | Estrai link dall'ultimo messaggio |
| `/info` | Mostra email attiva + ultima registrazione |

### 🚀 Higgsfield
| Comando | Descrizione |
|---------|-------------|
| `/register` | Auto-registrazione completa (email → form → verifica) |

### 🔍 Monitor & Diagnostica
| Comando | Descrizione |
|---------|-------------|
| `/monitor_status` | Stato del monitor |
| `/monitor_check` | Forza check immediato della struttura pagina |
| `/notif_status` | Status del notification bus |

## Architettura

```
┌─────────────────────────────────────────────────────┐
│ OVERLAY — tempmail-bot (container)                   │
│  ┌──────────┬──────────────┬──────────────────┐     │
│  │ Mail     │ Higgsfield   │ Page Monitor     │     │
│  │ Service  │ Service      │ (Playwright)     │     │
│  │ (aiohttp)│ (Playwright) │                  │     │
│  └──────────┴──────────────┴──────────────────┘     │
│  Health endpoint :8080/health                        │
├─────────────────────────────────────────────────────┤
│ UNDERLAY — watchdog (sidecar container)              │
│  ┌──────────────┬──────────────┬──────────────┐     │
│  │ Docker Events│ Health Check │ Host Metrics │     │
│  │ (socket)     │ (HTTP poll)  │ (psutil)     │     │
│  └──────────────┴──────────────┴──────────────┘     │
├─────────────────────────────────────────────────────┤
│ NOTIFICATION BUS (shared da entrambi i container)    │
│  ┌──────────┐ ┌──────────┐ ┌──────────────┐         │
│  │ Telegram │ │ Webhook  │ │ File/Console │         │
│  └──────────┘ └──────────┘ └──────────────┘         │
├─────────────────────────────────────────────────────┤
│              Proxy Layer (opzionale)                  │
└─────────────────────────────────────────────────────┘
```

**Flusso `/register`:**
1. Crea email su mail.tm
2. Lancia Chromium headless → naviga sign-up
3. Compila form con digitazione simulata
4. Polling mail.tm per email di verifica
5. Estrae link → naviga per conferma
6. Restituisce credenziali su Telegram

**Flusso Monitor:**
1. All'avvio, acquisisce baseline (fingerprint struttura pagina)
2. Ogni N secondi (`MONITOR_INTERVAL`), ricalcola fingerprint
3. Se l'hash cambia → alert su Telegram via `ADMIN_CHAT_ID`
4. Il fingerprint traccia: input fields, buttons, iframes (CAPTCHA), forms

## Proxy

Supporta HTTP e SOCKS5. Configura in `.env`:

```env
# Singolo proxy
PROXY_LIST=socks5://user:pass@proxy1:1080

# Rotazione (random ad ogni richiesta)
PROXY_LIST=socks5://user:pass@proxy1:1080,http://proxy2:8080,socks5://proxy3:1080
```

Il proxy è usato sia da Playwright (browser) che da aiohttp (API mail.tm).

## Selectors

Se il monitor rileva un cambiamento, aggiorna i selectors in `bot/config.py`:

```python
SELECTORS = {
    "email_input": 'input[type="email"], input[name="email"]',
    "password_input": 'input[type="password"]',
    "submit_button": 'button[type="submit"]',
    ...
}
```

I selectors usano una catena di fallback (priorità da sinistra a destra).

## Watchdog (System Monitor)

Container sidecar che monitora l'infrastruttura — quello che il bot non può monitorare da solo.

### Cosa monitora

| Monitor | Cosa rileva | Severity |
|---------|-------------|----------|
| **Docker Events** | die, OOM kill, restart, stop, kill signal | CRITICAL/WARNING |
| **Restart Loop** | 3+ restart in 5 minuti | CRITICAL |
| **Health Check** | Bot non risponde su /health | ERROR |
| **CPU** | Sopra soglia (default 90%) | WARNING/ERROR |
| **RAM** | Sopra soglia (default 85%) | WARNING/ERROR |
| **Disco** | Sopra soglia (default 90%) | WARNING/ERROR |

### Come funziona

Il watchdog si aggancia al Docker socket (`/var/run/docker.sock`, read-only) e ascolta eventi in tempo reale. Per le metriche host usa `psutil`. Per la salute del bot, pinga `http://tempmail-bot:8080/health` via rete interna Docker.

Tutte le notifiche passano per lo stesso `NotificationBus` del bot — stessi endpoint (Telegram, webhook, file).

### Perché un container separato?

Un processo non può monitorare la propria morte. Se il bot crasha per OOM, il suo codice Python non gira — non può mandarti nulla. Il watchdog sopravvive e ti avvisa.

## Notification System

Architettura pub/sub pluggabile per instradare errori, crash e lifecycle events verso qualsiasi endpoint.

```
Python Logging → LoggingBridge → NotificationBus → [Telegram, Webhook, File]
sys.excepthook ──────────────────┘
asyncio.exception_handler ───────┘
```

### Notifier inclusi

| Notifier | Trigger | Config |
|----------|---------|--------|
| **File/Console** | Sempre attivo (safety net) | `LOG_DIR` |
| **Telegram** | Se `ADMIN_CHAT_ID` presente | `TELEGRAM_BOT_TOKEN` + `ADMIN_CHAT_ID` |
| **Webhook** | Se `WEBHOOK_URL` presente | `WEBHOOK_URL` + `WEBHOOK_FORMAT` |

### Aggiungere un notifier custom

```python
from notifications import BaseNotifier, Event, NotificationBus

class SlackNotifier(BaseNotifier):
    async def send(self, event: Event) -> bool:
        # la tua logica qui
        return True

bus = NotificationBus.get_bus()
bus.register(SlackNotifier(name="slack", min_severity=Severity.ERROR))
```

### Severity levels

`DEBUG` → `INFO` → `WARNING` → `ERROR` → `CRITICAL`

Ogni notifier ha un `min_severity` — riceve solo eventi >= quel livello.

## Rate Limiting

I comandi più pesanti hanno un cooldown per-utente per evitare abusi e OOM:

| Comando | Cooldown |
|---------|----------|
| `/register` | 60s |
| `/monitor_check` | 30s |
| `/newemail` | 10s |

Le istanze Chromium sono serializzate (max 1 alla volta) per restare nel limite di memoria del container.

## Limiti e Note

- **Stato in-memory** — si resetta al riavvio del container
- **mail.tm rate limit** — 8 req/sec per IP (gestito con retry automatico)
- **Immagine Docker** — ~800MB (Chromium incluso)
- **Memoria** — il container usa fino a ~500MB durante registrazione (Chromium)
- **ToS** — l'automazione viola i termini di servizio di Higgsfield
