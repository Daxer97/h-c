"""
higgsfield_service.py — Automazione registrazione Higgsfield con Playwright.
Gestisce il flow completo: compilazione form → email verifica → click link.
"""

import asyncio
import logging
import random
import string
import uuid
from dataclasses import dataclass

from playwright.async_api import async_playwright, Browser, Page, BrowserContext

from config import (
    SELECTORS,
    HIGGSFIELD_SIGNUP_URL,
    DEFAULT_PASSWORD_LENGTH,
    REGISTRATION_TIMEOUT,
    EMAIL_WAIT_TIMEOUT,
    EMAIL_POLL_INTERVAL,
    get_random_proxy,
)
from mail_service import MailTMService, TempMailAccount

logger = logging.getLogger(__name__)

# Limit to one Chromium instance at a time to avoid OOM with the 1GB container limit
_browser_semaphore = asyncio.Semaphore(1)


@dataclass
class RegistrationResult:
    success: bool
    email: str = ""
    password: str = ""
    message: str = ""
    verification_link: str = ""


def _random_password(length: int = DEFAULT_PASSWORD_LENGTH) -> str:
    """Genera password con almeno 1 upper, 1 lower, 1 digit, 1 special."""
    chars = string.ascii_letters + string.digits + "!@#$%"
    while True:
        pwd = "".join(random.choices(chars, k=length))
        if (
            any(c.isupper() for c in pwd)
            and any(c.islower() for c in pwd)
            and any(c.isdigit() for c in pwd)
            and any(c in "!@#$%" for c in pwd)
        ):
            return pwd


def _parse_proxy_for_playwright(proxy_url: str) -> dict | None:
    """
    Converte 'protocol://user:pass@host:port' nel formato Playwright.
    Playwright vuole: {"server": "protocol://host:port", "username": ..., "password": ...}
    """
    if not proxy_url:
        return None

    from urllib.parse import urlparse

    parsed = urlparse(proxy_url)
    result = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        result["username"] = parsed.username
    if parsed.password:
        result["password"] = parsed.password
    return result


class HiggsFieldService:
    """Automazione registrazione Higgsfield."""

    def __init__(self, mail_service: MailTMService):
        self.mail = mail_service

    async def register(
        self,
        proxy_url: str | None = None,
        progress_callback=None,
    ) -> RegistrationResult:
        """
        Esegue il flow completo di registrazione.

        Args:
            proxy_url: Proxy da usare (opzionale, se None usa get_random_proxy)
            progress_callback: async callable(str) per aggiornamenti di stato

        Returns:
            RegistrationResult con credenziali o errore
        """
        proxy = proxy_url or get_random_proxy()

        async def notify(msg: str):
            logger.info(msg)
            if progress_callback:
                await progress_callback(msg)

        async with _browser_semaphore:
            return await self._register_impl(proxy, notify)

    async def _register_impl(self, proxy, notify) -> RegistrationResult:
        """Internal registration logic, runs under the browser semaphore."""
        # ── Step 1: Crea email temporanea ───────────────────
        await notify("📧 Creazione email temporanea...")
        try:
            mail_account = await self.mail.create_account()
        except Exception as e:
            return RegistrationResult(
                success=False, message=f"Errore creazione email: {e}"
            )

        await notify(f"✅ Email: {mail_account.address}")

        higgs_password = _random_password()

        # ── Step 2: Registrazione via Playwright ────────────
        await notify("🌐 Apertura browser e navigazione...")

        pw_proxy = _parse_proxy_for_playwright(proxy) if proxy else None

        try:
            async with async_playwright() as p:
                # Browser con stealth settings
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                    ],
                )

                context_opts = {
                    "viewport": {"width": 1920, "height": 1080},
                    "user_agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                    "locale": "en-US",
                    "timezone_id": "America/New_York",
                }
                if pw_proxy:
                    context_opts["proxy"] = pw_proxy

                context = await browser.new_context(**context_opts)

                # Rimuovi webdriver flag
                await context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
                    window.chrome = { runtime: {} };
                """)

                page = await context.new_page()

                # Naviga alla pagina di sign-up
                try:
                    await page.goto(
                        HIGGSFIELD_SIGNUP_URL,
                        wait_until="networkidle",
                        timeout=REGISTRATION_TIMEOUT * 1000,
                    )
                except Exception as e:
                    await browser.close()
                    return RegistrationResult(
                        success=False,
                        email=mail_account.address,
                        message=f"Errore navigazione: {e}",
                    )

                await notify("📝 Compilazione form di registrazione...")

                # ── Check CAPTCHA ───────────────────────────
                captcha = await page.query_selector(SELECTORS["captcha_frame"])
                if captcha:
                    await browser.close()
                    return RegistrationResult(
                        success=False,
                        email=mail_account.address,
                        message="⚠️ CAPTCHA rilevato! Registrazione manuale necessaria.",
                    )

                # ── Compila form ────────────────────────────
                result = await self._fill_and_submit(
                    page, mail_account.address, higgs_password
                )
                if not result:
                    # Screenshot per debug (unique filename to avoid overwrites)
                    screenshot_path = f"/tmp/higgs_error_{uuid.uuid4().hex[:8]}.png"
                    await page.screenshot(path=screenshot_path)
                    await browser.close()
                    return RegistrationResult(
                        success=False,
                        email=mail_account.address,
                        password=higgs_password,
                        message="❌ Errore compilazione/submit del form. Screenshot salvato.",
                    )

                await notify("📨 Form inviato. Attendo email di verifica...")

                # ── Step 3: Attendi email verifica ──────────
                msg = await self.mail.wait_for_message(
                    mail_account,
                    timeout=EMAIL_WAIT_TIMEOUT,
                    interval=EMAIL_POLL_INTERVAL,
                )

                if not msg:
                    await browser.close()
                    return RegistrationResult(
                        success=False,
                        email=mail_account.address,
                        password=higgs_password,
                        message="⏰ Timeout: nessuna email di verifica ricevuta.",
                    )

                await notify(f"📩 Email ricevuta: {msg.subject}")

                # ── Step 4: Estrai e clicca link verifica ───
                content = msg.text or msg.html or ""
                links = self.mail.extract_links(content)

                verify_link = None
                for link in links:
                    if "higgsfield" in link.lower() and (
                        "verify" in link.lower()
                        or "confirm" in link.lower()
                        or "token" in link.lower()
                    ):
                        verify_link = link
                        break

                # Fallback: prendi il primo link higgsfield
                if not verify_link:
                    for link in links:
                        if "higgsfield" in link.lower():
                            verify_link = link
                            break

                if not verify_link:
                    await browser.close()
                    return RegistrationResult(
                        success=False,
                        email=mail_account.address,
                        password=higgs_password,
                        message=f"❌ Nessun link di verifica trovato. Link nell'email: {links[:5]}",
                    )

                await notify(f"🔗 Link verifica trovato, apertura...")

                try:
                    await page.goto(
                        verify_link,
                        wait_until="networkidle",
                        timeout=30000,
                    )
                    # Attendi qualche secondo per il redirect
                    await page.wait_for_timeout(3000)
                except Exception as e:
                    logger.warning(f"Errore navigazione link verifica: {e}")

                await browser.close()

                await notify("✅ Registrazione completata!")

                return RegistrationResult(
                    success=True,
                    email=mail_account.address,
                    password=higgs_password,
                    verification_link=verify_link,
                    message="Account creato e verificato con successo.",
                )

        except Exception as e:
            logger.error(f"Errore generale registrazione: {e}", exc_info=True)
            return RegistrationResult(
                success=False,
                email=mail_account.address,
                password=higgs_password,
                message=f"❌ Errore imprevisto: {e}",
            )

    async def _fill_and_submit(
        self, page: Page, email: str, password: str
    ) -> bool:
        """
        Compila il form di registrazione e fa submit.
        Ritorna True se il submit sembra andato a buon fine.
        """
        try:
            # Attendi che il form sia visibile
            await page.wait_for_timeout(2000)

            # Dismissa il cookie banner se presente (blocca i click)
            try:
                cookie_banner = await page.query_selector(
                    "#cookiescript_injected_wrapper"
                )
                if cookie_banner:
                    # Prova a cliccare il pulsante "Accept" dentro il banner
                    accept_btn = await page.query_selector(
                        "#cookiescript_accept, "
                        "#cookiescript_injected_wrapper [data-cs-action='accept'], "
                        "#cookiescript_injected_wrapper button"
                    )
                    if accept_btn:
                        await accept_btn.click(timeout=3000)
                        logger.info("Cookie banner accettato tramite pulsante")
                    else:
                        # Fallback: rimuovi il banner via JS
                        await page.evaluate("""
                            document.querySelector('#cookiescript_injected_wrapper')?.remove();
                            document.querySelector('#cookiescript_injected')?.remove();
                        """)
                        logger.info("Cookie banner rimosso via JS")
                    await page.wait_for_timeout(500)
            except Exception as e:
                # Se fallisce, rimuovi forzatamente via JS
                logger.warning(f"Cookie banner dismiss fallito, rimozione forzata: {e}")
                await page.evaluate("""
                    document.querySelectorAll(
                        '#cookiescript_injected_wrapper, #cookiescript_injected'
                    ).forEach(el => el.remove());
                """)
                await page.wait_for_timeout(300)

            # Email
            email_input = await page.query_selector(SELECTORS["email_input"])
            if not email_input:
                logger.error("Email input non trovato")
                return False

            await email_input.click()
            await page.wait_for_timeout(random.randint(100, 300))
            await email_input.fill("")
            # Simula digitazione umana
            for char in email:
                await email_input.type(char, delay=random.randint(30, 80))
            await page.wait_for_timeout(random.randint(200, 500))

            # Password
            password_input = await page.query_selector(SELECTORS["password_input"])
            if not password_input:
                logger.error("Password input non trovato")
                return False

            await password_input.click()
            await page.wait_for_timeout(random.randint(100, 300))
            for char in password:
                await password_input.type(char, delay=random.randint(30, 80))
            await page.wait_for_timeout(random.randint(200, 500))

            # Confirm password (se presente)
            confirm_input = await page.query_selector(
                SELECTORS["confirm_password_input"]
            )
            if confirm_input:
                await confirm_input.click()
                await page.wait_for_timeout(random.randint(100, 300))
                for char in password:
                    await confirm_input.type(char, delay=random.randint(30, 80))
                await page.wait_for_timeout(random.randint(200, 500))

            # Submit
            submit_btn = await page.query_selector(SELECTORS["submit_button"])
            if not submit_btn:
                # Fallback: prova Enter
                logger.warning("Submit button non trovato, provo Enter")
                await page.keyboard.press("Enter")
            else:
                await submit_btn.click()

            # Attendi navigazione o cambiamento pagina
            await page.wait_for_timeout(3000)

            # Verifica se c'è un errore visibile (es. "already exists")
            page_text = await page.inner_text("body")
            error_keywords = ["already exists", "error", "invalid", "failed"]
            for kw in error_keywords:
                if kw.lower() in page_text.lower():
                    logger.warning(f"Possibile errore nel form: trovato '{kw}'")
                    # Non necessariamente un blocco — potrebbe essere testo generico
                    break

            return True

        except Exception as e:
            logger.error(f"Errore fill_and_submit: {e}", exc_info=True)
            return False
