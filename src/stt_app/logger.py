from __future__ import annotations

import faulthandler
import logging
import os
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TextIO

from .app_paths import logs_dir
from .config import (
    APP_LOGGER_NAME,
    CRASH_LOG_FILE_NAME,
    CRASH_LOG_MAX_BYTES,
    CRASH_TRACE_MAX_LINES,
    DIAGNOSTICS_MAX_LINES,
    LOG_BACKUP_COUNT,
    LOG_FILE_NAME,
    LOG_MAX_BYTES,
    SESSION_START_LOG_MARKER,
)

# The lines this module writes into the crash log around faulthandler's
# output, so that the next start can tell how the previous session ended.
_CRASH_SESSION_START = "=== stt_app session started"
_CRASH_SESSION_CLEAN_EXIT = "=== stt_app session ended cleanly"
# How much of the crash log the next start reads back. One session's section
# is a header, at most a few dumps and the end line -- far less than this.
_CRASH_LOG_TAIL_BYTES = 256_000
# Logged at start when the previous session did not quit cleanly or left a
# native trace. "Copy diagnostics" then also copies that previous session.
PREVIOUS_SESSION_END_LOG_MARKER = "previous_session_end"
# Set on the hooks `install_exception_logging` installs, so a second call does
# not chain them twice and log every exception twice.
_HOOK_MARK = "_stt_app_exception_logging"


class AppLogger:
    def __init__(
        self, root_dir: Path | None = None, file_name: str = LOG_FILE_NAME
    ) -> None:
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

        After a crash the interesting part is the session before: when this
        one started by reporting that the previous one ended without a clean
        quit (`PREVIOUS_SESSION_END_LOG_MARKER`), the copy starts at the
        previous session's marker. Copying only the current session was why,
        after a crash and a restart, the copied log showed nothing but the
        lines written since the restart (owner's report, 2026-10-23).
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

        session, with_previous = self._current_session_lines(lines)
        truncated = len(session) > max_lines
        if len(session) == len(lines):
            scope = "full log"
        elif with_previous:
            scope = "current and previous session"
        else:
            scope = "current session"
        if truncated:
            scope = f"{scope}, last {max_lines} lines"
        header = f"Log file: {self._log_path} ({scope})"
        return "\n".join([header, *session[-max_lines:]])

    @staticmethod
    def _current_session_lines(lines: list[str]) -> tuple[list[str], bool]:
        """The lines from the last session marker on, and whether they start
        one session earlier because the last one reported an unclean end."""
        starts = [
            index
            for index, line in enumerate(lines)
            if SESSION_START_LOG_MARKER in line
        ]
        if not starts:
            return lines, False
        start = starts[-1]
        if len(starts) > 1 and any(
            PREVIOUS_SESSION_END_LOG_MARKER in line for line in lines[start:]
        ):
            return lines[starts[-2] :], True
        return lines[start:], False

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
            isinstance(handler, RotatingFileHandler)
            and handler.baseFilename == str(self._log_path)
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


@dataclass(frozen=True)
class PreviousSessionEnd:
    """How the previous session's section of the crash log ended."""

    clean_quit: bool
    #: What faulthandler wrote in that session, without blank lines.
    trace_lines: tuple[str, ...]


class CrashLog:
    """The file faulthandler writes a native crash's stacks into.

    It stays open for the life of the process: faulthandler writes through
    the file descriptor from inside the crash, where nothing can be opened.
    faulthandler holds its own reference to the stream as well.
    """

    def __init__(self, path: Path, stream: TextIO) -> None:
        self._path = path
        self._stream = stream

    @property
    def path(self) -> Path:
        return self._path

    @property
    def stream(self) -> TextIO:
        return self._stream

    def mark_clean_exit(self) -> None:
        """Record that this session quit cleanly (read by the next start)."""
        try:
            self._stream.write(f"{_CRASH_SESSION_CLEAN_EXIT} {_now_text()} ===\n")
            self._stream.flush()
        except (OSError, ValueError):
            pass


def start_crash_reporting(
    log_dir: Path,
    logger: logging.Logger,
    *,
    version: str,
) -> CrashLog | None:
    """Make every crash leave a trace, and report the previous session's end.

    Python logging never sees a crash in native code -- an access violation
    in Qt or a C extension ends the process before any handler runs -- and an
    exception nothing catches goes to stderr, which the windowed build does
    not have. So after a crash the log simply stopped. Two things change that:
    faulthandler writes every thread's Python stack into `crash.log` for the
    life of the process, and `install_exception_logging` logs uncaught
    exceptions from any thread.

    Each session writes a start line into the crash log and, from `main`, an
    end line once `app.exec()` returned. A section without the end line is a
    session that did not quit: a crash, a kill, a Windows restart or a power
    loss. The next start says so in the log, with whatever faulthandler wrote.

    Returns None when the crash log cannot be opened; the exception hooks are
    installed either way.
    """
    path = log_dir / CRASH_LOG_FILE_NAME
    previous = _read_previous_session_end(path)
    _rotate_large_crash_log(path)
    install_exception_logging(logger)
    try:
        stream = open(path, "a", encoding="utf-8", errors="replace")  # noqa: SIM115 - held to exit
    except OSError as exc:
        logger.warning("crash_log_unavailable path=%s error=%s", path, exc)
        return None
    try:
        stream.write(
            f"{_CRASH_SESSION_START} {_now_text()} pid={os.getpid()} "
            f"version={version} ===\n"
        )
        stream.flush()
        faulthandler.enable(file=stream, all_threads=True)
    except (OSError, ValueError, RuntimeError) as exc:
        stream.close()
        logger.warning("crash_log_unavailable path=%s error=%s", path, exc)
        return None
    logger.info("crash_log_enabled path=%s", path)
    if previous is not None and (not previous.clean_quit or previous.trace_lines):
        _log_previous_session_end(logger, previous, path)
    return CrashLog(path, stream)


def _log_previous_session_end(
    logger: logging.Logger,
    previous: PreviousSessionEnd,
    path: Path,
) -> None:
    trace = previous.trace_lines[-CRASH_TRACE_MAX_LINES:]
    omitted = len(previous.trace_lines) - len(trace)
    details = []
    if omitted:
        details.append(f"({omitted} earlier trace lines are in {path.name})")
    details.extend(trace)
    logger.warning(
        "%s clean_quit=%s native_trace_lines=%d crash_log=%s%s",
        PREVIOUS_SESSION_END_LOG_MARKER,
        "yes" if previous.clean_quit else "no",
        len(previous.trace_lines),
        path,
        "".join(f"\n{line}" for line in details),
    )


def _read_previous_session_end(path: Path) -> PreviousSessionEnd | None:
    """The last session section of the crash log, or None without one."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _CRASH_LOG_TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    lines = tail.splitlines()
    starts = [
        index
        for index, line in enumerate(lines)
        if line.startswith(_CRASH_SESSION_START)
    ]
    if not starts:
        return None
    section = lines[starts[-1] + 1 :]
    clean_quit = any(line.startswith(_CRASH_SESSION_CLEAN_EXIT) for line in section)
    trace = tuple(
        line
        for line in section
        if line.strip() and not line.startswith(_CRASH_SESSION_CLEAN_EXIT)
    )
    return PreviousSessionEnd(clean_quit=clean_quit, trace_lines=trace)


def _rotate_large_crash_log(path: Path) -> None:
    """Keep the crash log bounded: past the limit it becomes `crash.log.1`."""
    try:
        if path.stat().st_size <= CRASH_LOG_MAX_BYTES:
            return
        os.replace(path, path.with_name(f"{path.name}.1"))
    except OSError:
        # Missing (nothing to bound) or held open elsewhere: appending to the
        # large file is better than losing this session's trace.
        return


def install_exception_logging(logger: logging.Logger) -> None:
    """Log every exception nothing else catches, then hand it on.

    Covers the main thread and Qt slots (PySide6 reports a slot's exception
    through `sys.excepthook`), every `threading.Thread`, and exceptions Python
    can only report, such as one raised inside a ctypes callback or a
    `__del__` (`sys.unraisablehook`). Each hook logs with the traceback and
    then calls the hook it replaced, so a terminal still shows the exception.
    """
    if getattr(sys.excepthook, _HOOK_MARK, False):
        return
    previous_excepthook = sys.excepthook
    previous_thread_hook = threading.excepthook
    previous_unraisable_hook = sys.unraisablehook

    def excepthook(exc_type, exc_value, exc_traceback) -> None:
        if not issubclass(exc_type, KeyboardInterrupt):
            logger.error(
                "unhandled_exception thread=%s",
                threading.current_thread().name,
                exc_info=(exc_type, exc_value, exc_traceback),
            )
        previous_excepthook(exc_type, exc_value, exc_traceback)

    def thread_excepthook(args) -> None:
        if args.exc_type is not SystemExit:
            name = args.thread.name if args.thread is not None else "unknown"
            logger.error(
                "unhandled_thread_exception thread=%s",
                name,
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
        previous_thread_hook(args)

    def unraisablehook(unraisable) -> None:
        logger.error(
            "unraisable_exception %s: %s",
            unraisable.err_msg or "Exception ignored in",
            _safe_repr(unraisable.object),
            exc_info=(
                unraisable.exc_type,
                unraisable.exc_value,
                unraisable.exc_traceback,
            ),
        )
        previous_unraisable_hook(unraisable)

    for hook in (excepthook, thread_excepthook, unraisablehook):
        setattr(hook, _HOOK_MARK, True)
    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
    sys.unraisablehook = unraisablehook


def _safe_repr(value: object) -> str:
    try:
        text = repr(value)
    except Exception:
        return f"<unrepresentable {type(value).__name__}>"
    return text if len(text) <= 200 else f"{text[:200]}..."


def _now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
