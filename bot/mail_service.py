"""
mail_service.py — Wrapper asincrono per le API di mail.tm
Gestisce creazione account, autenticazione e polling messaggi.
"""

import asyncio
import logging
import random
import string
import re
from dataclasses import dataclass

import aiohttp

from config import get_random_proxy

logger = logging.getLogger(__name__)

BASE_URL = "https://api.mail.tm"


@dataclass
class TempMailAccount:
    address: str
    password: str
    account_id: str
    token: str = ""


@dataclass
class MailMessage:
    id: str
    from_address: str
    subject: str
    text: str
    html: str
    intro: str


class MailTMService:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(self, method: str, url: str, max_retries: int = 3, **kwargs):
        session = await self._get_session()
        # Fresh proxy per-request for proper rotation
        proxy = get_random_proxy()
        if proxy:
            kwargs["proxy"] = proxy

        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                async with session.request(method, url, **kwargs) as resp:
                    if resp.status == 429:
                        retry_after = int(resp.headers.get("Retry-After", 2))
                        logger.warning(
                            "mail.tm rate limited, retry in %ds (attempt %d/%d)",
                            retry_after, attempt, max_retries,
                        )
                        await asyncio.sleep(retry_after)
                        continue
                    if resp.status >= 500:
                        logger.warning(
                            "mail.tm server error %d (attempt %d/%d)",
                            resp.status, attempt, max_retries,
                        )
                        await asyncio.sleep(2 ** attempt)
                        continue
                    resp.raise_for_status()
                    return await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_error = e
                if attempt < max_retries:
                    logger.warning(
                        "mail.tm request error: %s (attempt %d/%d)",
                        e, attempt, max_retries,
                    )
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise

        raise last_error or RuntimeError("mail.tm request failed after retries")

    # ── Domain ──────────────────────────────────────────────

    async def get_domains(self) -> list[str]:
        data = await self._request("GET", f"{BASE_URL}/domains")
        members = data.get("hydra:member", data) if isinstance(data, dict) else data
        return [d["domain"] for d in members if d.get("isActive", True)]

    # ── Account ─────────────────────────────────────────────

    @staticmethod
    def _random_string(length: int = 12) -> str:
        return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))

    async def create_account(self, username: str | None = None) -> TempMailAccount:
        domains = await self.get_domains()
        if not domains:
            raise RuntimeError("Nessun dominio disponibile su mail.tm")

        domain = random.choice(domains)
        username = username or self._random_string()
        address = f"{username}@{domain}"
        password = self._random_string(16)

        session = await self._get_session()
        proxy = get_random_proxy()
        proxy_kw = {"proxy": proxy} if proxy else {}

        # Crea account
        async with session.post(
            f"{BASE_URL}/accounts",
            json={"address": address, "password": password},
            **proxy_kw,
        ) as resp:
            if resp.status not in (200, 201):
                body = await resp.text()
                raise RuntimeError(f"Errore creazione account ({resp.status}): {body}")
            acc_data = await resp.json()

        account = TempMailAccount(
            address=address, password=password, account_id=acc_data["id"]
        )

        # Ottieni JWT
        async with session.post(
            f"{BASE_URL}/token",
            json={"address": address, "password": password},
            **proxy_kw,
        ) as resp:
            resp.raise_for_status()
            token_data = await resp.json()
            account.token = token_data["token"]

        return account

    # ── Messages ────────────────────────────────────────────

    async def get_messages(self, account: TempMailAccount) -> list[MailMessage]:
        session = await self._get_session()
        headers = {"Authorization": f"Bearer {account.token}"}
        proxy = get_random_proxy()
        proxy_kw = {"proxy": proxy} if proxy else {}

        async with session.get(
            f"{BASE_URL}/messages", headers=headers, **proxy_kw
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            members = data.get("hydra:member", data) if isinstance(data, dict) else data

        return [
            MailMessage(
                id=m["id"],
                from_address=m.get("from", {}).get("address", "unknown"),
                subject=m.get("subject", "(no subject)"),
                text=m.get("text", ""),
                html=(
                    m.get("html", [""])[0]
                    if isinstance(m.get("html"), list)
                    else m.get("html", "")
                ),
                intro=m.get("intro", ""),
            )
            for m in members
        ]

    async def get_message_detail(
        self, account: TempMailAccount, message_id: str
    ) -> MailMessage:
        session = await self._get_session()
        headers = {"Authorization": f"Bearer {account.token}"}
        proxy = get_random_proxy()
        proxy_kw = {"proxy": proxy} if proxy else {}

        async with session.get(
            f"{BASE_URL}/messages/{message_id}", headers=headers, **proxy_kw
        ) as resp:
            resp.raise_for_status()
            m = await resp.json()

        html_content = m.get("html", "")
        if isinstance(html_content, list):
            html_content = html_content[0] if html_content else ""

        return MailMessage(
            id=m["id"],
            from_address=m.get("from", {}).get("address", "unknown"),
            subject=m.get("subject", "(no subject)"),
            text=m.get("text", ""),
            html=html_content,
            intro=m.get("intro", ""),
        )

    async def wait_for_message(
        self,
        account: TempMailAccount,
        timeout: int = 120,
        interval: int = 5,
        known_ids: set[str] | None = None,
    ) -> MailMessage | None:
        known_ids = known_ids or set()
        elapsed = 0
        consecutive_errors = 0
        max_consecutive_errors = 5

        while elapsed < timeout:
            try:
                messages = await self.get_messages(account)
                consecutive_errors = 0  # Reset on success
                for msg in messages:
                    if msg.id not in known_ids:
                        return await self.get_message_detail(account, msg.id)
            except Exception as e:
                consecutive_errors += 1
                logger.warning(
                    "Errore polling inbox (tentativo %d/%d): %s",
                    consecutive_errors, max_consecutive_errors, e,
                )
                if consecutive_errors >= max_consecutive_errors:
                    logger.error(
                        "Troppi errori consecutivi nel polling inbox, interruzione"
                    )
                    return None

            await asyncio.sleep(interval)
            elapsed += interval
        return None

    @staticmethod
    def extract_links(text: str) -> list[str]:
        return re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE).findall(text)
