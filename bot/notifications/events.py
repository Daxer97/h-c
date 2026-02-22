"""
notifications/events.py — Modello eventi per il notification bus.

Ogni evento ha severity, categoria, messaggio e metadata opzionali.
Il bus smista gli eventi ai notifier in base alla severity minima configurata.
"""

from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from typing import Any


class Severity(IntEnum):
    """
    Livelli di severity — mappati 1:1 sui logging levels di Python.
    Usare IntEnum permette confronti diretti: Severity.ERROR > Severity.WARNING
    """
    DEBUG = logging.DEBUG        # 10
    INFO = logging.INFO          # 20
    WARNING = logging.WARNING    # 30
    ERROR = logging.ERROR        # 40
    CRITICAL = logging.CRITICAL  # 50

    @property
    def emoji(self) -> str:
        return {
            Severity.DEBUG: "🔍",
            Severity.INFO: "ℹ️",
            Severity.WARNING: "⚠️",
            Severity.ERROR: "❌",
            Severity.CRITICAL: "🔥",
        }[self]

    @property
    def label(self) -> str:
        return self.name


class EventCategory:
    """Categorie semantiche — stringhe libere, non enum, per estensibilità."""
    LIFECYCLE = "lifecycle"        # avvio, shutdown, restart
    REGISTRATION = "registration"  # flow registrazione Higgsfield
    MAIL = "mail"                  # errori mail.tm
    MONITOR = "monitor"            # cambiamenti struttura pagina
    CRASH = "crash"                # eccezioni non gestite
    SYSTEM = "system"              # generico / infrastruttura


@dataclass
class Event:
    """
    Evento che transita nel notification bus.

    Attributes:
        severity: Livello di gravità
        category: Categoria semantica (stringa libera)
        message: Messaggio leggibile
        timestamp: Quando è avvenuto
        metadata: Dati strutturati aggiuntivi (opzionale)
        exception: Eccezione associata (opzionale)
        traceback_str: Traceback formattato (opzionale)
        source: Chi ha generato l'evento (nome modulo/classe)
    """
    severity: Severity
    category: str
    message: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)
    exception: BaseException | None = None
    traceback_str: str = ""
    source: str = ""

    def __post_init__(self):
        # Auto-genera traceback se c'è un'eccezione
        if self.exception and not self.traceback_str:
            self.traceback_str = "".join(
                traceback.format_exception(
                    type(self.exception), self.exception, self.exception.__traceback__
                )
            )

    def format_plain(self) -> str:
        """Formato testuale semplice (per console/file)."""
        parts = [
            f"[{self.timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')}]",
            f"[{self.severity.label}]",
            f"[{self.category}]",
        ]
        if self.source:
            parts.append(f"[{self.source}]")
        parts.append(self.message)

        text = " ".join(parts)
        if self.traceback_str:
            text += f"\n{self.traceback_str}"
        return text

    def format_html(self) -> str:
        """Formato HTML per Telegram."""
        import html
        header = f"{self.severity.emoji} <b>[{self.severity.label}]</b> [{self.category}]"
        if self.source:
            header += f" <i>({html.escape(self.source)})</i>"

        body = html.escape(self.message)

        text = f"{header}\n{body}"

        if self.metadata:
            meta_lines = "\n".join(
                f"  • {html.escape(str(k))}: {html.escape(str(v))}"
                for k, v in self.metadata.items()
            )
            text += f"\n\n<b>Metadata:</b>\n{meta_lines}"

        if self.traceback_str:
            # Tronca traceback a 1000 chars per Telegram
            tb = html.escape(self.traceback_str[:1000])
            text += f"\n\n<pre>{tb}</pre>"

        ts = self.timestamp.strftime("%H:%M:%S UTC")
        text += f"\n\n<i>{ts}</i>"

        return text

    def format_json(self) -> dict:
        """Formato JSON per webhook."""
        data = {
            "severity": self.severity.label,
            "category": self.category,
            "message": self.message,
            "timestamp": self.timestamp.isoformat(),
            "source": self.source,
        }
        if self.metadata:
            data["metadata"] = self.metadata
        if self.traceback_str:
            data["traceback"] = self.traceback_str[:2000]
        return data
