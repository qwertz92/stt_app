from __future__ import annotations

import faulthandler
import logging
import os
import re
import sys
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TextIO

from PySide6 import QtCore

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
# Every one starts with `_CRASH_MARKER_PREFIX`; any other line is a trace.
_CRASH_MARKER_PREFIX = "=== stt_app "
_CRASH_SESSION_START = f"{_CRASH_MARKER_PREFIX}session started"
_CRASH_QUIT_STARTED = f"{_CRASH_MARKER_PREFIX}quit started"
_CRASH_SESSION_CLEAN_EXIT = f"{_CRASH_MARKER_PREFIX}session ended cleanly"
# A session whose faulthandler could not be enabled: nothing watched it, so
# the next start says nothing about how it ended.
_CRASH_SESSION_UNWATCHED = f"{_CRASH_MARKER_PREFIX}crash reporting unavailable"
# How faulthandler starts a dump: a fatal error, a Windows exception (also a
# handled one, which the process survives) or the quit watchdog's timeout.
_DUMP_HEADERS = ("Fatal Python error", "Windows fatal exception", "Timeout (")
# How many lines of a long dump's start the excerpt keeps: the header and
# what follows it. The rest of `CRASH_TRACE_MAX_LINES` goes to its end.
_TRACE_HEAD_LINES = 20
# Logged at start when the previous session did not quit cleanly or left a
# native trace. "Copy diagnostics" then also copies that previous session.
PREVIOUS_SESSION_END_LOG_MARKER = "previous_session_end"
# Logged instead when the previous session was ended while it quit and left
# no trace -- what a Windows shutdown or restart does. Not a crash, so the
# diagnostics copy does not reach back for it; the name must therefore not
# contain `PREVIOUS_SESSION_END_LOG_MARKER`.
PREVIOUS_SESSION_STOPPED_DURING_QUIT_LOG_MARKER = "previous_session_stopped_during_quit"
# Set on the hooks `install_exception_logging` installs, so a second call does
# not chain them twice and log every exception twice.
_HOOK_MARK = "_stt_app_exception_logging"
# Qt's message types and the log level each is written at. Debug messages
# fall below the app logger's INFO level and are dropped.
_QT_MESSAGE_LEVELS = {
    QtCore.QtMsgType.QtDebugMsg: logging.DEBUG,
    QtCore.QtMsgType.QtInfoMsg: logging.INFO,
    QtCore.QtMsgType.QtWarningMsg: logging.WARNING,
    QtCore.QtMsgType.QtCriticalMsg: logging.ERROR,
    QtCore.QtMsgType.QtFatalMsg: logging.CRITICAL,
}
# How often one Qt message is logged per session before it is dropped, so a
# warning Qt repeats on every window move cannot fill the log. Messages that
# differ only in numbers (coordinates, sizes, ids) count as one.
_QT_MESSAGE_REPEAT_LIMIT = 20
# The handler `install_qt_message_logging` installed; None before.
_qt_message_handler = None


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
    #: Its quit began (`CrashLog.mark_quit_started`), whether or not it ended.
    quit_started: bool
    #: What faulthandler wrote in that session, without blank lines.
    trace_lines: tuple[str, ...]


class CrashLog:
    """The file faulthandler writes a native crash's stacks into.

    It stays open for the life of the process: faulthandler writes through
    the file descriptor from inside the crash, where nothing can be opened.
    faulthandler holds its own reference to the stream as well.
    """

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    @property
    def stream(self) -> TextIO:
        return self._stream

    def mark_quit_started(self) -> None:
        """Record that this session began to quit (read by the next start).

        A Windows shutdown, restart or sign-out ends the process while it
        quits: Qt emits `aboutToQuit` from inside WM_ENDSESSION (measured on
        2026-10-23), and Windows may end the process once that returns, before
        `app.exec()` returns and `mark_clean_exit` runs. With this line the
        next start tells that apart from a crash.
        """
        _write_marker(self._stream, _CRASH_QUIT_STARTED)

    def mark_clean_exit(self) -> None:
        """Record that this session quit cleanly (read by the next start)."""
        _write_marker(self._stream, _CRASH_SESSION_CLEAN_EXIT)


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
    not have; so do Qt's own warnings. So after a crash the log simply
    stopped. Three things change that: faulthandler writes every thread's
    Python stack into `crash.log` for the life of the process,
    `install_exception_logging` logs uncaught exceptions from any thread, and
    `install_qt_message_logging` logs Qt's messages.

    Each session writes a start line into the crash log, a line when its quit
    begins and, from `main`, an end line once `app.exec()` returned. A
    section without the end line is a session that did not finish quitting.
    With the quit line and no trace, something ended it during the quit,
    which is what a Windows shutdown does; without the quit line it did not
    quit at all: a crash, a kill or a power loss. The next start says which,
    with whatever faulthandler wrote.

    Returns None when the crash log cannot be used; the previous session is
    reported and the exception and Qt message hooks are installed either way.
    """
    path = log_dir / CRASH_LOG_FILE_NAME
    previous = _read_previous_session_end(path)
    if previous is not None:
        _log_previous_session_end(logger, previous, path)
    _rotate_large_crash_log(path)
    install_exception_logging(logger)
    install_qt_message_logging(logger)
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
        # Nothing watches this session and nothing will write its end line:
        # without this one the next start would report it as a crash.
        _write_marker(stream, _CRASH_SESSION_UNWATCHED)
        stream.close()
        logger.warning("crash_log_unavailable path=%s error=%s", path, exc)
        return None
    logger.info("crash_log_enabled path=%s", path)
    return CrashLog(stream)


def _log_previous_session_end(
    logger: logging.Logger,
    previous: PreviousSessionEnd,
    path: Path,
) -> None:
    if previous.clean_quit and not previous.trace_lines:
        return
    if previous.quit_started and not previous.trace_lines:
        logger.info(
            "%s crash_log=%s (ended while it quit, for example by a Windows "
            "shutdown or restart; no crash trace)",
            PREVIOUS_SESSION_STOPPED_DURING_QUIT_LOG_MARKER,
            path,
        )
        return
    logger.warning(
        "%s clean_quit=%s quit_started=%s native_trace_lines=%d crash_log=%s%s",
        PREVIOUS_SESSION_END_LOG_MARKER,
        "yes" if previous.clean_quit else "no",
        "yes" if previous.quit_started else "no",
        len(previous.trace_lines),
        path,
        "".join(f"\n{line}" for line in _trace_excerpt(previous.trace_lines, path)),
    )


def _trace_excerpt(trace_lines: tuple[str, ...], path: Path) -> list[str]:
    """The part of the previous session's trace that goes into the log.

    That is the last dump, the one that ended the session; earlier ones are
    handled exceptions faulthandler reported too. A dump starts with its
    header ("Fatal Python error: ...") and ends with the thread that crashed
    ("Current thread ...", written after every other thread, measured on
    2026-10-23), so a dump longer than `CRASH_TRACE_MAX_LINES` keeps both
    ends and leaves out the middle.
    """
    starts = [
        index
        for index, line in enumerate(trace_lines)
        if line.startswith(_DUMP_HEADERS)
    ]
    first = starts[-1] if starts else 0
    dump = list(trace_lines[first:])
    excerpt: list[str] = []
    if first:
        excerpt.append(f"({first} earlier trace lines are in {path.name})")
    if len(dump) <= CRASH_TRACE_MAX_LINES:
        return [*excerpt, *dump]
    tail_lines = CRASH_TRACE_MAX_LINES - _TRACE_HEAD_LINES
    omitted = len(dump) - CRASH_TRACE_MAX_LINES
    return [
        *excerpt,
        *dump[:_TRACE_HEAD_LINES],
        f"({omitted} lines of this dump are left out here; all are in {path.name})",
        *dump[-tail_lines:],
    ]


def _read_previous_session_end(path: Path) -> PreviousSessionEnd | None:
    """The last session section of the crash log, or None without one.

    Reads at most `CRASH_LOG_MAX_BYTES` from the end, the size past which the
    file is set aside at a start, so a file below it is read whole. A
    section longer than that is read from its end: its start line lies
    before the window.
    """
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            window_start = max(0, size - CRASH_LOG_MAX_BYTES)
            handle.seek(window_start)
            window = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    lines = window.splitlines()
    if window_start:
        lines = lines[1:]  # the window cut this one
    starts = [
        index
        for index, line in enumerate(lines)
        if line.startswith(_CRASH_SESSION_START)
    ]
    if starts:
        section = lines[starts[-1] + 1 :]
    elif window_start:
        section = lines
    else:
        return None
    if any(line.startswith(_CRASH_SESSION_UNWATCHED) for line in section):
        return None
    return PreviousSessionEnd(
        clean_quit=any(line.startswith(_CRASH_SESSION_CLEAN_EXIT) for line in section),
        quit_started=any(line.startswith(_CRASH_QUIT_STARTED) for line in section),
        trace_lines=tuple(
            line
            for line in section
            if line.strip() and not line.startswith(_CRASH_MARKER_PREFIX)
        ),
    )


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


def _write_marker(stream: TextIO, marker: str) -> None:
    try:
        stream.write(f"{marker} {_now_text()} ===\n")
        stream.flush()
    except (OSError, ValueError):
        pass


def install_exception_logging(logger: logging.Logger) -> None:
    """Log every exception nothing else catches, then hand it on.

    Covers the main thread and Qt slots (PySide6 reports a slot's exception
    through `sys.excepthook`), every `threading.Thread`, and exceptions Python
    can only report, such as one raised inside a ctypes callback or a
    `__del__` (`sys.unraisablehook`). Each hook logs with the traceback and
    then calls the hook it replaced, so a terminal still shows the exception;
    a logging failure never keeps it from doing so.
    """
    if getattr(sys.excepthook, _HOOK_MARK, False):
        return
    previous_excepthook = sys.excepthook
    previous_thread_hook = threading.excepthook
    previous_unraisable_hook = sys.unraisablehook

    def excepthook(exc_type, exc_value, exc_traceback) -> None:
        if not issubclass(exc_type, KeyboardInterrupt):
            _log_exception(
                logger,
                "unhandled_exception thread=%s",
                threading.current_thread().name,
                exc_info=(exc_type, exc_value, exc_traceback),
            )
        previous_excepthook(exc_type, exc_value, exc_traceback)

    def thread_excepthook(args) -> None:
        if args.exc_type is not SystemExit:
            _log_exception(
                logger,
                "unhandled_thread_exception thread=%s",
                args.thread.name if args.thread is not None else "unknown",
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
        previous_thread_hook(args)

    def unraisablehook(unraisable) -> None:
        _log_exception(
            logger,
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


def _log_exception(logger: logging.Logger, message: str, *args, exc_info) -> None:
    try:
        logger.error(message, *args, exc_info=exc_info)
    except Exception:
        # A handler or a formatter that fails here must not cost the hook
        # it replaced; logging already reports its own handler errors.
        pass


def install_qt_message_logging(logger: logging.Logger) -> None:
    """Write Qt's own messages into the log.

    Qt reports problems it survives -- "QObject::killTimer: Timers cannot be
    stopped from another thread" came right before the retranscription crash
    of 2026-10-23 -- through its message handler, which writes to stderr: in
    the windowed build, nowhere. Each message is logged at the level of its
    type, with Qt's category, and at most `_QT_MESSAGE_REPEAT_LIMIT` times
    per session. A message Qt raises while this handler logs (none is known)
    is dropped instead of recursing.
    """
    global _qt_message_handler
    if _qt_message_handler is not None:
        return
    repeats: Counter[str] = Counter()
    lock = threading.Lock()
    inside = threading.local()

    def handler(message_type, context, message) -> None:
        level = _QT_MESSAGE_LEVELS.get(message_type, logging.WARNING)
        if getattr(inside, "active", False) or not logger.isEnabledFor(level):
            return
        inside.active = True
        try:
            key = re.sub(r"\d+", "#", message)
            with lock:
                repeats[key] += 1
                count = repeats[key]
            if count > _QT_MESSAGE_REPEAT_LIMIT:
                return
            logger.log(
                level,
                "qt_message category=%s %s%s",
                context.category,
                message,
                " (repeated too often; later ones are not logged)"
                if count == _QT_MESSAGE_REPEAT_LIMIT
                else "",
            )
        except Exception:
            # Raised into Qt's C++ caller it would only be printed to stderr.
            pass
        finally:
            inside.active = False

    QtCore.qInstallMessageHandler(handler)
    _qt_message_handler = handler


def _safe_repr(value: object) -> str:
    try:
        text = repr(value)
    except Exception:
        return f"<unrepresentable {type(value).__name__}>"
    return text if len(text) <= 200 else f"{text[:200]}..."


def _now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
