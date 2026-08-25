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
    SESSION_START_LOG_MARKER,
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
        """Return the current session's log, across already rotated files.

        Reading only the live file made a diagnostics copy stop at the last
        rotation, which can be minutes of runtime: the interesting part (app
        start, model preload, the first failure) had usually rolled into a
        backup by the time the user copied anything. A plain line count is a
        poor substitute — too small and it cuts the session, too large and the
        clipboard is unusable — so the text starts at the last session marker
        and only falls back to the tail when there is none.
        """
        lines: list[str] = []
        # Oldest backup first so the copied text stays chronological.
        for path in [*self._rotated_log_paths(), self._log_path]:
            if not path.exists():
                continue
            lines.extend(
                path.read_text(encoding="utf-8", errors="replace").splitlines()
            )
        if not lines:
            return "No diagnostics available yet."

        session = self._current_session_lines(lines)
        truncated = len(session) > max_lines
        scope = "current session" if len(session) < len(lines) else "full log"
        if truncated:
            scope = f"{scope}, last {max_lines} lines"
        header = f"Log file: {self._log_path} ({scope})"
        return "\n".join([header, *session[-max_lines:]])

    @staticmethod
    def _current_session_lines(lines: list[str]) -> list[str]:
        for index in range(len(lines) - 1, -1, -1):
            if SESSION_START_LOG_MARKER in lines[index]:
                return lines[index:]
        return lines

    def _rotated_log_paths(self) -> list[Path]:
        """Existing ``dictation.log.N`` backups, oldest first."""
        paths = [
            self._log_path.with_name(f"{self._log_path.name}.{index}")
            for index in range(LOG_BACKUP_COUNT, 0, -1)
        ]
        return [path for path in paths if path.exists()]

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
