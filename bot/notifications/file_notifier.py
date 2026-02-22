"""
notifications/file_notifier.py — Notifier su file e console.

Fallback locale — funziona sempre, anche senza rete.
Scrive su:
  - Console (stderr) per visibilità immediata in docker logs
  - File di log rotativo per persistenza

Questo notifier è il "safety net" — se Telegram e webhook falliscono,
gli eventi sono comunque catturati qui.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from notifications.base import BaseNotifier
from notifications.events import Event, Severity

logger = logging.getLogger(__name__)


class FileNotifier(BaseNotifier):
    """
    Scrive eventi su file e/o console.

    Args:
        log_dir: Directory per i file di log
        filename: Nome del file di log
        max_bytes: Dimensione max file prima della rotazione
        backup_count: Numero di file di backup da tenere
        console_output: Se True, scrive anche su stderr
    """

    def __init__(
        self,
        log_dir: str = "/app/logs",
        filename: str = "notifications.log",
        max_bytes: int = 5 * 1024 * 1024,  # 5MB
        backup_count: int = 3,
        console_output: bool = True,
        min_severity: Severity = Severity.DEBUG,
        enabled: bool = True,
        name: str = "file",
    ):
        super().__init__(name=name, min_severity=min_severity, enabled=enabled)
        self._console_output = console_output
        self._file_logger: logging.Logger | None = None

        # Setup file logging
        try:
            Path(log_dir).mkdir(parents=True, exist_ok=True)
            filepath = os.path.join(log_dir, filename)

            self._file_logger = logging.getLogger(f"notifications.file.{name}")
            self._file_logger.setLevel(logging.DEBUG)
            self._file_logger.propagate = False

            # Evita handler duplicati
            if not self._file_logger.handlers:
                handler = RotatingFileHandler(
                    filepath,
                    maxBytes=max_bytes,
                    backupCount=backup_count,
                    encoding="utf-8",
                )
                handler.setFormatter(
                    logging.Formatter("%(message)s")
                )
                self._file_logger.addHandler(handler)

            logger.info(f"FileNotifier: logging su {filepath}")

        except Exception as e:
            logger.error(f"FileNotifier: impossibile creare file log: {e}")
            self._file_logger = None

    async def send(self, event: Event) -> bool:
        text = event.format_plain()

        try:
            # File
            if self._file_logger:
                self._file_logger.info(text)

            # Console (stderr via print per visibilità in docker logs)
            if self._console_output:
                severity_colors = {
                    Severity.DEBUG: "\033[90m",     # grigio
                    Severity.INFO: "\033[36m",      # cyan
                    Severity.WARNING: "\033[33m",   # giallo
                    Severity.ERROR: "\033[31m",     # rosso
                    Severity.CRITICAL: "\033[41m",  # sfondo rosso
                }
                reset = "\033[0m"
                color = severity_colors.get(event.severity, "")
                print(f"{color}{text}{reset}", flush=True)

            return True

        except Exception as e:
            # Ultimo resort — se neanche il file funziona, logga su stderr
            print(f"FileNotifier ERRORE: {e} — evento: {text}", flush=True)
            return False

    async def close(self):
        if self._file_logger:
            for handler in self._file_logger.handlers[:]:
                handler.close()
                self._file_logger.removeHandler(handler)
