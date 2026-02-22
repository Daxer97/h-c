"""
monitor_service.py — Monitor struttura pagina Higgsfield.
Controlla periodicamente se la pagina di sign-up è cambiata
e invia alert su Telegram se rileva modifiche.
"""

import asyncio
import hashlib
import json
import logging
from collections import deque
from datetime import datetime, timezone

from playwright.async_api import async_playwright

from config import (
    HIGGSFIELD_SIGNUP_URL,
    HIGGSFIELD_SIGNIN_URL,
    MONITOR_INTERVAL,
    SELECTORS,
    get_random_proxy,
)
from higgsfield_service import _parse_proxy_for_playwright, _browser_semaphore

logger = logging.getLogger(__name__)


class PageMonitor:
    """
    Monitora la struttura HTML delle pagine auth di Higgsfield.
    Confronta un hash della struttura del form ad ogni check.
    """

    def __init__(self, alert_callback=None):
        """
        Args:
            alert_callback: async callable(str) — chiamata quando rileva un cambiamento.
                           Tipicamente invia un messaggio Telegram all'admin.
        """
        self._alert = alert_callback
        self._running = False
        self._task: asyncio.Task | None = None

        # Hash dell'ultima struttura nota
        self._last_hashes: dict[str, str] = {}
        self._last_check: datetime | None = None
        self._change_log: deque[dict] = deque(maxlen=20)

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def last_check(self) -> datetime | None:
        return self._last_check

    @property
    def change_log(self) -> list[dict]:
        return list(self._change_log)

    async def _extract_page_fingerprint(self, url: str) -> dict:
        """
        Naviga alla pagina e estrae un fingerprint della struttura:
        - Lista di tag input con attributi
        - Lista di button con testo
        - Presenza di iframe (CAPTCHA)
        - Struttura del form
        """
        proxy = get_random_proxy()
        pw_proxy = _parse_proxy_for_playwright(proxy) if proxy else None

        async with _browser_semaphore, async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            ctx_opts = {
                "viewport": {"width": 1920, "height": 1080},
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            }
            if pw_proxy:
                ctx_opts["proxy"] = pw_proxy

            context = await browser.new_context(**ctx_opts)
            page = await context.new_page()

            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
                await page.wait_for_timeout(2000)

                # Estrai struttura
                fingerprint = await page.evaluate("""() => {
                    const inputs = Array.from(document.querySelectorAll('input')).map(el => ({
                        type: el.type,
                        name: el.name,
                        placeholder: el.placeholder,
                        id: el.id,
                        required: el.required,
                    }));

                    const buttons = Array.from(document.querySelectorAll('button')).map(el => ({
                        type: el.type,
                        text: el.innerText.trim().substring(0, 50),
                        id: el.id,
                    }));

                    const iframes = Array.from(document.querySelectorAll('iframe')).map(el => ({
                        src: (el.src || '').substring(0, 100),
                    }));

                    const forms = Array.from(document.querySelectorAll('form')).map(el => ({
                        action: el.action,
                        method: el.method,
                        id: el.id,
                    }));

                    const links = Array.from(document.querySelectorAll('a')).map(el => ({
                        href: (el.href || '').substring(0, 100),
                        text: el.innerText.trim().substring(0, 50),
                    })).filter(l => l.href.includes('auth') || l.href.includes('sign'));

                    return { inputs, buttons, iframes, forms, links };
                }""")

                return fingerprint

            except Exception as e:
                logger.error(f"Errore estrazione fingerprint da {url}: {e}")
                return {"error": str(e)}
            finally:
                await browser.close()

    @staticmethod
    def _hash_fingerprint(fp: dict) -> str:
        """Crea un hash deterministico del fingerprint."""
        serialized = json.dumps(fp, sort_keys=True, ensure_ascii=True)
        return hashlib.sha256(serialized.encode()).hexdigest()[:16]

    async def check_now(self) -> dict[str, dict]:
        """
        Esegue un check immediato su tutte le pagine monitorate.
        Returns: dict con risultati per ogni URL.
        """
        urls = {
            "sign-up": HIGGSFIELD_SIGNUP_URL,
            "sign-in": HIGGSFIELD_SIGNIN_URL,
        }

        results = {}
        changes_detected = []

        for name, url in urls.items():
            fp = await self._extract_page_fingerprint(url)
            current_hash = self._hash_fingerprint(fp)
            previous_hash = self._last_hashes.get(name)

            changed = previous_hash is not None and previous_hash != current_hash
            is_first = previous_hash is None

            results[name] = {
                "url": url,
                "hash": current_hash,
                "previous_hash": previous_hash,
                "changed": changed,
                "is_first_check": is_first,
                "fingerprint": fp,
            }

            if changed:
                changes_detected.append(name)
                self._change_log.append({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "page": name,
                    "old_hash": previous_hash,
                    "new_hash": current_hash,
                    "fingerprint": fp,
                })

            self._last_hashes[name] = current_hash

        self._last_check = datetime.now(timezone.utc)

        # Alert se ci sono cambiamenti
        if changes_detected and self._alert:
            pages_str = ", ".join(changes_detected)
            detail_lines = []
            for name in changes_detected:
                r = results[name]
                fp = r["fingerprint"]
                n_inputs = len(fp.get("inputs", []))
                n_buttons = len(fp.get("buttons", []))
                n_iframes = len(fp.get("iframes", []))
                detail_lines.append(
                    f"  • <b>{name}</b>: {n_inputs} input, {n_buttons} button, "
                    f"{n_iframes} iframe | hash {r['previous_hash']} → {r['hash']}"
                )

            alert_msg = (
                f"🚨 <b>ALERT: Struttura pagina cambiata!</b>\n\n"
                f"Pagine modificate: {pages_str}\n\n"
                + "\n".join(detail_lines)
                + "\n\n⚠️ I selectors in config.py potrebbero dover essere aggiornati.\n"
                f"Usa /monitor_status per dettagli."
            )
            await self._alert(alert_msg)

        return results

    async def _loop(self):
        """Loop principale del monitor."""
        logger.info("Monitor avviato — intervallo: %ds", MONITOR_INTERVAL)

        # Primo check per baseline
        await self.check_now()
        logger.info("Monitor — baseline acquisita: %s", self._last_hashes)

        while self._running:
            await asyncio.sleep(MONITOR_INTERVAL)
            if not self._running:
                break
            try:
                await self.check_now()
                logger.info(
                    "Monitor check completato — hashes: %s", self._last_hashes
                )
            except Exception as e:
                logger.error(f"Monitor errore nel check: {e}", exc_info=True)
                if self._alert:
                    await self._alert(
                        f"⚠️ Monitor errore: {e}\nIl monitor continua a funzionare."
                    )

    def start(self):
        """Avvia il monitor in background."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    def stop(self):
        """Ferma il monitor."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    def get_status(self) -> str:
        """Restituisce lo status formattato del monitor."""
        if not self._last_check:
            return "Monitor non ha ancora eseguito check."

        lines = [
            f"🔍 <b>Monitor Status</b>",
            f"Ultimo check: {self._last_check.strftime('%Y-%m-%d %H:%M:%S UTC')}",
            f"Intervallo: {MONITOR_INTERVAL}s",
            f"Running: {'✅' if self._running else '❌'}",
            "",
        ]
        for name, h in self._last_hashes.items():
            lines.append(f"• {name}: <code>{h}</code>")

        if self._change_log:
            lines.append(f"\n📋 Ultimi cambiamenti ({len(self._change_log)}):")
            for entry in self._change_log[-5:]:
                lines.append(
                    f"  {entry['timestamp'][:19]} — {entry['page']} "
                    f"({entry['old_hash']} → {entry['new_hash']})"
                )

        return "\n".join(lines)
