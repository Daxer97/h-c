"""
main.py — TempMail + Higgsfield Auto-Registration Telegram Bot

Comandi:
  /start              - Intro e istruzioni
  /newemail           - Crea una nuova email temporanea
  /check              - Controlla inbox
  /wait               - Polling automatico (2 min)
  /read <n>           - Leggi messaggio completo
  /links              - Estrai link dall'ultimo messaggio
  /info               - Mostra email attiva
  /register           - Auto-registrazione Higgsfield
  /monitor_status     - Status del page monitor
  /monitor_check      - Forza check immediato del monitor
  /notif_status       - Status del notification bus
"""

import asyncio
import logging
import time
import html as html_module

from aiogram import Bot, Dispatcher, Router
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.enums import ParseMode

from config import (
    BOT_TOKEN,
    ALLOWED_USER_IDS,
    ADMIN_CHAT_ID,
    WEBHOOK_URL,
    WEBHOOK_FORMAT,
    LOG_DIR,
)
from mail_service import MailTMService, TempMailAccount
from higgsfield_service import HiggsFieldService, RegistrationResult
from monitor_service import PageMonitor
from notifications import (
    setup_notifications,
    NotificationBus,
    LifecycleEmitter,
    EventCategory,
)
from health import start_health_server, set_started, set_healthy

# ── Logging ─────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ── Init ────────────────────────────────────────────────────

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

mail_service = MailTMService()
higgs_service = HiggsFieldService(mail_service)

# Notification bus e lifecycle — inizializzati in on_startup
bus: NotificationBus | None = None
lifecycle: LifecycleEmitter | None = None
health_runner = None  # aiohttp runner per health server


# Monitor — alert via notification bus
async def monitor_alert(text: str):
    if bus:
        await bus.warning(
            text,
            category=EventCategory.MONITOR,
            source="page_monitor",
        )


page_monitor = PageMonitor(alert_callback=monitor_alert)

# ── State (in-memory, per-user) ─────────────────────────────

user_accounts: dict[int, TempMailAccount] = {}
user_known_ids: dict[int, set[str]] = {}
user_last_message: dict[int, object] = {}
user_registrations: dict[int, list[RegistrationResult]] = {}

# ── Helpers ─────────────────────────────────────────────────

# Parse once at module level instead of re-parsing on every command
_ALLOWED_IDS: set[int] | None = None
if ALLOWED_USER_IDS.strip():
    _ALLOWED_IDS = {int(x.strip()) for x in ALLOWED_USER_IDS.split(",") if x.strip()}


def is_allowed(user_id: int) -> bool:
    if _ALLOWED_IDS is None:
        return True
    return user_id in _ALLOWED_IDS


def escape(text: str) -> str:
    return html_module.escape(str(text))


def truncate(text: str, max_len: int = 3500) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "\n\n… [troncato]"


# ── Rate limiting ──────────────────────────────────────────────
# Per-user cooldowns (seconds) for resource-intensive commands
_COMMAND_COOLDOWNS: dict[str, int] = {
    "register": 60,       # Chromium-heavy
    "monitor_check": 30,  # Chromium-heavy
    "newemail": 10,       # API call
}
_user_last_command: dict[tuple[int, str], float] = {}


def check_rate_limit(user_id: int, command: str) -> str | None:
    """Returns an error message if rate-limited, None if allowed."""
    cooldown = _COMMAND_COOLDOWNS.get(command)
    if cooldown is None:
        return None
    key = (user_id, command)
    now = time.monotonic()
    last = _user_last_command.get(key, 0)
    remaining = cooldown - (now - last)
    if remaining > 0:
        return f"⏳ Attendi {int(remaining)}s prima di riusare /{command}."
    _user_last_command[key] = now
    return None


# ═══════════════════════════════════════════════════════════
# COMANDI EMAIL
# ═══════════════════════════════════════════════════════════


@router.message(Command("start"))
async def cmd_start(message: Message):
    if not is_allowed(message.from_user.id):
        await message.reply("⛔ Non sei autorizzato.")
        return

    await message.reply(
        "<b>📬 TempMail + Higgsfield Bot</b>\n\n"
        "Email temporanee e auto-registrazione Higgsfield.\n\n"
        "<b>📧 Email:</b>\n"
        "/newemail — Nuova email temporanea\n"
        "/check — Controlla inbox\n"
        "/wait — Attendi messaggio (polling 2 min)\n"
        "/read &lt;n&gt; — Leggi messaggio\n"
        "/links — Estrai link\n"
        "/info — Email attiva\n\n"
        "<b>🚀 Higgsfield:</b>\n"
        "/register — Auto-registrazione completa\n\n"
        "<b>🔍 Monitor &amp; Diagnostica:</b>\n"
        "/monitor_status — Status monitor pagina\n"
        "/monitor_check — Check struttura immediato\n"
        "/notif_status — Status notification bus\n",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("newemail"))
async def cmd_new_email(message: Message):
    if not is_allowed(message.from_user.id):
        return

    uid = message.from_user.id
    rate_msg = check_rate_limit(uid, "newemail")
    if rate_msg:
        await message.reply(rate_msg)
        return

    status = await message.reply("⏳ Creo email temporanea...")

    try:
        account = await mail_service.create_account()
        user_accounts[uid] = account
        user_known_ids[uid] = set()
        user_last_message[uid] = None

        await status.edit_text(
            f"✅ <b>Email creata!</b>\n\n"
            f"📧 <code>{escape(account.address)}</code>\n\n"
            f"Usa /check o /wait per ricevere messaggi.",
            parse_mode=ParseMode.HTML,
        )

    except Exception as e:
        await status.edit_text(f"❌ Errore: {escape(e)}", parse_mode=ParseMode.HTML)
        if bus:
            await bus.error(
                f"Errore creazione email per user {uid}: {e}",
                category=EventCategory.MAIL,
                exception=e,
                source="cmd_newemail",
            )


@router.message(Command("info"))
async def cmd_info(message: Message):
    if not is_allowed(message.from_user.id):
        return

    uid = message.from_user.id
    account = user_accounts.get(uid)
    if not account:
        await message.reply("⚠️ Nessuna email attiva. Usa /newemail.")
        return

    text = f"📧 Email attiva: <code>{escape(account.address)}</code>"
    regs = user_registrations.get(uid, [])
    if regs:
        last = regs[-1]
        text += (
            f"\n\n🚀 Ultima registrazione:\n"
            f"Email: <code>{escape(last.email)}</code>\n"
            f"Stato: {'✅' if last.success else '❌'} {escape(last.message)}"
        )

    await message.reply(text, parse_mode=ParseMode.HTML)


@router.message(Command("check"))
async def cmd_check(message: Message):
    if not is_allowed(message.from_user.id):
        return

    uid = message.from_user.id
    account = user_accounts.get(uid)
    if not account:
        await message.reply("⚠️ Nessuna email attiva. Usa /newemail prima.")
        return

    try:
        messages = await mail_service.get_messages(account)
        if not messages:
            await message.reply("📭 Inbox vuota.")
            return

        user_known_ids.setdefault(uid, set())
        lines = [f"📬 <b>{len(messages)} messaggio/i:</b>\n"]

        for i, msg in enumerate(messages, 1):
            flag = "🆕 " if msg.id not in user_known_ids[uid] else ""
            lines.append(
                f"{flag}<b>{i}.</b> {escape(msg.from_address)}\n"
                f"   📌 {escape(msg.subject)}\n"
                f"   💬 {escape(msg.intro[:100])}\n"
            )
            user_known_ids[uid].add(msg.id)

        if messages:
            detail = await mail_service.get_message_detail(account, messages[0].id)
            user_last_message[uid] = detail

        lines.append("/read &lt;n&gt; per leggere un messaggio.")
        await message.reply("\n".join(lines), parse_mode=ParseMode.HTML)

    except Exception as e:
        await message.reply(f"❌ Errore: {escape(e)}", parse_mode=ParseMode.HTML)


@router.message(Command("wait"))
async def cmd_wait(message: Message):
    if not is_allowed(message.from_user.id):
        return

    uid = message.from_user.id
    account = user_accounts.get(uid)
    if not account:
        await message.reply("⚠️ Nessuna email attiva. Usa /newemail prima.")
        return

    status = await message.reply("⏳ Attendo nuovo messaggio (max 2 min)...")

    try:
        known = user_known_ids.get(uid, set())
        msg = await mail_service.wait_for_message(
            account, timeout=120, interval=5, known_ids=known
        )

        if not msg:
            await status.edit_text("⏰ Timeout — nessun messaggio.")
            return

        user_known_ids.setdefault(uid, set()).add(msg.id)
        user_last_message[uid] = msg

        content = msg.text or msg.html or ""
        links = mail_service.extract_links(content)
        links_text = (
            "\n".join(f"🔗 {escape(link)}" for link in links[:5])
            if links
            else "Nessun link."
        )

        await status.edit_text(
            f"📩 <b>Nuovo messaggio!</b>\n\n"
            f"Da: {escape(msg.from_address)}\n"
            f"Oggetto: {escape(msg.subject)}\n\n"
            f"<b>Preview:</b>\n{escape(truncate(msg.intro or msg.text or '(vuoto)', 500))}\n\n"
            f"<b>Link:</b>\n{links_text}",
            parse_mode=ParseMode.HTML,
        )

    except Exception as e:
        await status.edit_text(f"❌ Errore: {escape(e)}", parse_mode=ParseMode.HTML)


@router.message(Command("read"))
async def cmd_read(message: Message):
    if not is_allowed(message.from_user.id):
        return

    uid = message.from_user.id
    account = user_accounts.get(uid)
    if not account:
        await message.reply("⚠️ Nessuna email attiva.")
        return

    args = message.text.split()
    idx = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1

    try:
        messages = await mail_service.get_messages(account)
        if not messages:
            await message.reply("📭 Inbox vuota.")
            return
        if idx < 1 or idx > len(messages):
            await message.reply(f"⚠️ Numero non valido (hai {len(messages)} msg).")
            return

        msg = await mail_service.get_message_detail(account, messages[idx - 1].id)
        user_last_message[uid] = msg

        content = msg.text or msg.intro or "(vuoto)"
        links = mail_service.extract_links(msg.text or msg.html or "")
        links_text = "\n".join(f"🔗 {escape(link)}" for link in links[:10]) if links else ""

        text = (
            f"📧 <b>Messaggio #{idx}</b>\n\n"
            f"<b>Da:</b> {escape(msg.from_address)}\n"
            f"<b>Oggetto:</b> {escape(msg.subject)}\n\n"
            f"{escape(truncate(content))}"
        )
        if links_text:
            text += f"\n\n<b>Link:</b>\n{links_text}"

        await message.reply(text, parse_mode=ParseMode.HTML)

    except Exception as e:
        await message.reply(f"❌ Errore: {escape(e)}", parse_mode=ParseMode.HTML)


@router.message(Command("links"))
async def cmd_links(message: Message):
    if not is_allowed(message.from_user.id):
        return

    last = user_last_message.get(message.from_user.id)
    if not last:
        await message.reply("⚠️ Nessun messaggio letto. Usa /check o /wait prima.")
        return

    content = last.text or last.html or ""
    links = mail_service.extract_links(content)

    if not links:
        await message.reply("🔍 Nessun link trovato.")
        return

    lines = ["🔗 <b>Link trovati:</b>\n"]
    for i, link in enumerate(links[:15], 1):
        lines.append(f"{i}. {escape(link)}")
    await message.reply("\n".join(lines), parse_mode=ParseMode.HTML)


# ═══════════════════════════════════════════════════════════
# HIGGSFIELD AUTO-REGISTRATION
# ═══════════════════════════════════════════════════════════


@router.message(Command("register"))
async def cmd_register(message: Message):
    if not is_allowed(message.from_user.id):
        await message.reply("⛔ Non sei autorizzato.")
        return

    uid = message.from_user.id
    rate_msg = check_rate_limit(uid, "register")
    if rate_msg:
        await message.reply(rate_msg)
        return

    status = await message.reply(
        "🚀 <b>Avvio auto-registrazione Higgsfield...</b>\n\n"
        "Il processo richiede circa 1-3 minuti.",
        parse_mode=ParseMode.HTML,
    )

    steps: list[str] = []

    async def progress(msg: str):
        steps.append(msg)
        display = "\n".join(steps[-5:])
        try:
            await status.edit_text(
                f"🚀 <b>Registrazione in corso...</b>\n\n{display}",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    try:
        result = await higgs_service.register(progress_callback=progress)
        user_registrations.setdefault(uid, []).append(result)

        if result.success:
            await status.edit_text(
                f"✅ <b>Registrazione completata!</b>\n\n"
                f"📧 Email: <code>{escape(result.email)}</code>\n"
                f"🔑 Password: <code>{escape(result.password)}</code>\n"
                f"🔗 Link verifica: {escape(result.verification_link[:80])}\n\n"
                f"⚠️ Salva queste credenziali.",
                parse_mode=ParseMode.HTML,
            )
            if bus:
                await bus.info(
                    f"Registrazione Higgsfield riuscita: {result.email}",
                    category=EventCategory.REGISTRATION,
                    source="cmd_register",
                    metadata={"user_id": uid, "email": result.email},
                )
        else:
            await status.edit_text(
                f"❌ <b>Registrazione fallita</b>\n\n"
                f"📧 Email: <code>{escape(result.email)}</code>\n"
                f"🔑 Password: <code>{escape(result.password)}</code>\n"
                f"💬 {escape(result.message)}\n\n"
                f"Riprova con /register o completa manualmente.",
                parse_mode=ParseMode.HTML,
            )
            if bus:
                await bus.warning(
                    f"Registrazione Higgsfield fallita: {result.message}",
                    category=EventCategory.REGISTRATION,
                    source="cmd_register",
                    metadata={"user_id": uid, "email": result.email},
                )

    except Exception as e:
        logger.error(f"Errore registrazione per user {uid}: {e}", exc_info=True)
        await status.edit_text(
            f"❌ Errore imprevisto: {escape(e)}", parse_mode=ParseMode.HTML
        )


# ═══════════════════════════════════════════════════════════
# MONITOR COMMANDS
# ═══════════════════════════════════════════════════════════


@router.message(Command("monitor_status"))
async def cmd_monitor_status(message: Message):
    if not is_allowed(message.from_user.id):
        return
    await message.reply(page_monitor.get_status(), parse_mode=ParseMode.HTML)


@router.message(Command("monitor_check"))
async def cmd_monitor_check(message: Message):
    if not is_allowed(message.from_user.id):
        return

    rate_msg = check_rate_limit(message.from_user.id, "monitor_check")
    if rate_msg:
        await message.reply(rate_msg)
        return

    status = await message.reply("🔍 Check struttura in corso...")

    try:
        results = await page_monitor.check_now()
        lines = ["🔍 <b>Risultati check:</b>\n"]

        for name, r in results.items():
            icon = "🔴" if r["changed"] else ("🟡" if r["is_first_check"] else "🟢")
            state = (
                "CAMBIATO!" if r["changed"]
                else ("Baseline" if r["is_first_check"] else "OK")
            )
            fp = r["fingerprint"]
            n_inputs = len(fp.get("inputs", []))
            n_buttons = len(fp.get("buttons", []))
            n_iframes = len(fp.get("iframes", []))

            lines.append(
                f"{icon} <b>{name}</b>: {state}\n"
                f"   Hash: <code>{r['hash']}</code>\n"
                f"   Struttura: {n_inputs} input, {n_buttons} button, {n_iframes} iframe"
            )

        await status.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)

    except Exception as e:
        await status.edit_text(f"❌ Errore: {escape(e)}", parse_mode=ParseMode.HTML)


# ═══════════════════════════════════════════════════════════
# NOTIFICATION STATUS
# ═══════════════════════════════════════════════════════════


@router.message(Command("notif_status"))
async def cmd_notif_status(message: Message):
    if not is_allowed(message.from_user.id):
        return

    if not bus:
        await message.reply("⚠️ Notification bus non inizializzato.")
        return

    status = bus.get_status()
    notifiers = status["notifiers"]

    lines = [
        "📡 <b>Notification Bus Status</b>\n",
        f"Notifier registrati: {len(notifiers)}",
        f"Eventi in log: {status['event_log_size']}\n",
    ]

    for n in notifiers:
        icon = "✅" if n["enabled"] else "❌"
        lines.append(
            f"{icon} <b>{n['name']}</b> ({n['type']})\n"
            f"   Min severity: {n['min_severity']}"
        )

    if status["last_event"]:
        lines.append(
            f"\n<b>Ultimo evento:</b>\n"
            f"<code>{escape(status['last_event'][:200])}</code>"
        )

    recent = bus.recent_events[-5:]
    if recent:
        lines.append(f"\n<b>Ultimi {len(recent)} eventi:</b>")
        for evt in recent:
            lines.append(
                f"  {evt.severity.emoji} [{evt.category}] "
                f"{escape(evt.message[:60])}"
            )

    await message.reply("\n".join(lines), parse_mode=ParseMode.HTML)


# ═══════════════════════════════════════════════════════════
# LIFECYCLE
# ═══════════════════════════════════════════════════════════


async def on_startup():
    global bus, lifecycle, health_runner

    # Health endpoint (per watchdog + Docker HEALTHCHECK)
    health_runner = await start_health_server(port=8080)
    set_started()

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

    page_monitor.start()
    set_healthy(True, monitors="active")
    logger.info("Bot + notification bus + page monitor + health server avviati")


async def on_shutdown():
    set_healthy(False, reason="shutting_down")

    if lifecycle:
        await lifecycle.shutdown(reason="Shutdown richiesto")

    page_monitor.stop()
    await mail_service.close()

    if health_runner:
        await health_runner.cleanup()

    if bus:
        await bus.close()

    logger.info("Shutdown completato")


async def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    logger.info("Avvio bot...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
