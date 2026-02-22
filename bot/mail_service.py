"""
mail_service.py — Wrapper asincrono per le API di mail.tm
Gestisce creazione account, autenticazione e polling messaggi.
"""

import asyncio
import random
import string
import re
from dataclasses import dataclass

import aiohttp

from config import get_random_proxy

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
            # Proxy opzionale anche per le chiamate API mail.tm
            proxy = get_random_proxy()
            self._session = aiohttp.ClientSession()
            self._proxy = proxy
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(self, method: str, url: str, **kwargs):
        session = await self._get_session()
        if self._proxy:
            kwargs["proxy"] = self._proxy
        async with session.request(method, url, **kwargs) as resp:
            resp.raise_for_status()
            return await resp.json()

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
        proxy_kw = {"proxy": self._proxy} if self._proxy else {}

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
        proxy_kw = {"proxy": self._proxy} if self._proxy else {}

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
        proxy_kw = {"proxy": self._proxy} if self._proxy else {}

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
        while elapsed < timeout:
            messages = await self.get_messages(account)
            for msg in messages:
                if msg.id not in known_ids:
                    return await self.get_message_detail(account, msg.id)
            await asyncio.sleep(interval)
            elapsed += interval
        return None

    @staticmethod
    def extract_links(text: str) -> list[str]:
        return re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE).findall(text)
