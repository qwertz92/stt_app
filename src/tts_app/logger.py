from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .app_paths import logs_dir
from .config import (
    APP_LOGGER_NAME,
    DIAGNOSTICS_MAX_LINES,
    LOG_BACKUP_COUNT,
    LOG_FILE_NAME,
    LOG_MAX_BYTES,
)


class AppLogger:
    def __init__(self, root_dir: Path | None = None, file_name: str = LOG_FILE_NAME) -> None:
        self._root_dir = root_dir or logs_dir()
        self._root_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = self._root_dir / file_name

        self._configured = False
        self._configure()

    @property
    def log_path(self) -> Path:
        return self._log_path

    def get_logger(self, name: str = APP_LOGGER_NAME) -> logging.Logger:
        return logging.getLogger(name)

    def diagnostics_text(self, max_lines: int = DIAGNOSTICS_MAX_LINES) -> str:
        if not self._log_path.exists():
            return "No diagnostics available yet."

        lines = self._log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = lines[-max_lines:]
        return "\n".join(tail)

    def _configure(self) -> None:
        if self._configured:
            return

        root_logger = logging.getLogger(APP_LOGGER_NAME)
        root_logger.setLevel(logging.INFO)

        if not any(
            isinstance(handler, RotatingFileHandler) and handler.baseFilename == str(self._log_path)
            for handler in root_logger.handlers
        ):
            handler = RotatingFileHandler(
                self._log_path,
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8",
                delay=True,
            )
            formatter = logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
            )
            handler.setFormatter(formatter)
            root_logger.addHandler(handler)

        self._configured = True


Logger = AppLogger
