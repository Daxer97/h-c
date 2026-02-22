"""
notifications/base.py — Interfaccia base per tutti i notifier.

Ogni notifier deve implementare:
  - send(event) → invia l'evento all'endpoint
  - close() → cleanup risorse

Il bus chiama send() su ogni notifier registrato che accetta la severity dell'evento.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from notifications.events import Event, Severity

logger = logging.getLogger(__name__)


class BaseNotifier(ABC):
    """
    Interfaccia base per i notifier.

    Attributes:
        name: Identificativo del notifier (per logging/debug)
        min_severity: Severity minima per ricevere eventi.
                      Se un evento ha severity < min_severity, viene ignorato.
        enabled: Se False, il notifier è disattivato.
    """

    def __init__(
        self,
        name: str,
        min_severity: Severity = Severity.INFO,
        enabled: bool = True,
    ):
        self.name = name
        self.min_severity = min_severity
        self.enabled = enabled

    def accepts(self, event: Event) -> bool:
        """Controlla se questo notifier deve gestire l'evento."""
        return self.enabled and event.severity >= self.min_severity

    @abstractmethod
    async def send(self, event: Event) -> bool:
        """
        Invia l'evento all'endpoint.

        Returns:
            True se l'invio è riuscito, False altrimenti.
            Il bus NON fa retry — è responsabilità del notifier gestirli internamente.
        """
        ...

    async def close(self):
        """Cleanup risorse (connessioni, file handle, ecc.)."""
        pass

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"name={self.name!r}, "
            f"min_severity={self.min_severity.label}, "
            f"enabled={self.enabled})"
        )
