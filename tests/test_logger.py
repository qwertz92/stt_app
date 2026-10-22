"""Tests for AppLogger — initialization, log path, diagnostics, idempotency."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from PySide6 import QtCore, QtWidgets

from stt_app import logger as logger_module
from stt_app.config import APP_LOGGER_NAME
from stt_app.logger import AppLogger


@pytest.fixture(autouse=True)
def _clean_logger_handlers():
    """Remove handlers added during tests to prevent cross-test pollution."""
    yield
    root = logging.getLogger(APP_LOGGER_NAME)
    root.handlers.clear()


def test_creates_log_dir_and_file_handler(tmp_path):
    al = AppLogger(root_dir=tmp_path)
    assert al.log_path.parent.is_dir()
    root = logging.getLogger(APP_LOGGER_NAME)
    rh = [
        h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(rh) >= 1


def test_log_path_property(tmp_path):
    al = AppLogger(root_dir=tmp_path)
    assert al.log_path == tmp_path / "dictation.log"


def test_custom_file_name(tmp_path):
    al = AppLogger(root_dir=tmp_path, file_name="custom.log")
    assert al.log_path.name == "custom.log"


def test_get_logger_returns_named_logger(tmp_path):
    al = AppLogger(root_dir=tmp_path)
    lg = al.get_logger("test.logger")
    assert lg.name == "test.logger"


def test_diagnostics_text_when_no_file(tmp_path):
    al = AppLogger(root_dir=tmp_path, file_name="missing.log")
    al._log_path.unlink(missing_ok=True)
    assert al.diagnostics_text() == "No diagnostics available yet."


def test_diagnostics_text_returns_tail(tmp_path):
    al = AppLogger(root_dir=tmp_path)
    al.log_path.write_text("line1\nline2\nline3\nline4\nline5\n", encoding="utf-8")
    tail = al.diagnostics_text(max_lines=3)
    lines = tail.strip().splitlines()
    # A header naming the log file, then at most max_lines log lines.
    assert lines[0].startswith("Log file:")
    assert lines[1:] == ["line3", "line4", "line5"]


def test_diagnostics_text_starts_at_the_current_session(tmp_path):
    """A copied diagnostic should cover this run, not an arbitrary tail."""
    from stt_app.config import SESSION_START_LOG_MARKER

    al = AppLogger(root_dir=tmp_path)
    al.log_path.write_text(
        "\n".join(
            [
                "old session line",
                f"2026-01-01 10:00:00 [INFO] {SESSION_START_LOG_MARKER} version=1",
                "first line of this run",
                f"2026-01-01 11:00:00 [INFO] {SESSION_START_LOG_MARKER} version=1",
                "restarted",
                "and this",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    lines = al.diagnostics_text().strip().splitlines()

    assert "current session" in lines[0]
    assert SESSION_START_LOG_MARKER in lines[1]
    assert lines[2:] == ["restarted", "and this"]


def test_diagnostics_text_includes_rotated_backups(tmp_path):
    """The interesting part (start, preload, first failure) is often rotated."""
    al = AppLogger(root_dir=tmp_path)
    al.log_path.with_name(al.log_path.name + ".2").write_text(
        "oldest\n", encoding="utf-8"
    )
    al.log_path.with_name(al.log_path.name + ".1").write_text(
        "older\n", encoding="utf-8"
    )
    al.log_path.write_text("current\n", encoding="utf-8")

    lines = al.diagnostics_text().strip().splitlines()

    assert lines[1:] == ["oldest", "older", "current"]


def test_configure_is_idempotent(tmp_path):
    al = AppLogger(root_dir=tmp_path)
    # Force second configure attempt
    al._configured = False
    al._configure()

    root = logging.getLogger(APP_LOGGER_NAME)
    rh = [
        h
        for h in root.handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
        and h.baseFilename == str(al.log_path)
    ]
    # Only one handler for the same path
    assert len(rh) == 1


def _end_process_logging() -> None:
    """What a process exit does to the log: every handler closed and gone."""
    root = logging.getLogger(APP_LOGGER_NAME)
    for handler in list(root.handlers):
        handler.close()
        root.removeHandler(handler)


@pytest.fixture
def _restore_exception_hooks(monkeypatch):
    """`install_exception_logging` replaces three process-wide hooks."""
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(sys, "unraisablehook", sys.unraisablehook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(logging.Formatter().format(record))


def _capturing_logger(name: str) -> tuple[logging.Logger, _Records]:
    records = _Records()
    log = logging.getLogger(name)
    log.handlers[:] = [records]
    log.propagate = False
    log.setLevel(logging.INFO)
    return log, records


def test_a_restart_keeps_the_previous_sessions_log(tmp_path):
    """The owner found only post-restart lines after a crash (2026-10-23).

    The cause was not the file: a restart appends. This pins that, so a
    rotation at every start or a truncating open cannot slip in.
    """
    first = AppLogger(root_dir=tmp_path)
    first.get_logger().info("last line of the crashed session")
    _end_process_logging()

    second = AppLogger(root_dir=tmp_path)
    second.get_logger().info("first line after the restart")
    _end_process_logging()

    text = second.log_path.read_text(encoding="utf-8")
    assert "last line of the crashed session" in text
    assert "first line after the restart" in text


_CRASHING_SESSION = """
import faulthandler
import sys
from pathlib import Path

from stt_app.logger import AppLogger, start_crash_reporting

root = Path(sys.argv[1])
log = AppLogger(root_dir=root).get_logger()
log.info("app_session_started version=test")
start_crash_reporting(root, log, version="test")
if sys.argv[2] == "crash":
    log.info("last line before the crash")
    faulthandler._sigsegv()
"""


def test_a_native_crash_leaves_its_stack_and_the_next_start_reports_it(tmp_path):
    """A crash in native code bypasses Python logging entirely.

    Measured before the change: the crashed session's log simply stopped,
    nothing anywhere named the crash, and "Copy diagnostics" after the
    restart held only the lines written since. Now faulthandler writes the
    stack into crash.log, the next start copies it into the log, and the
    diagnostics copy reaches back into the crashed session.
    """
    import stt_app

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "dictation.log").write_text(
        "2026-10-01 10:00:00,000 [INFO] stt_app: app_session_started version=old\n"
        "an older session that quit cleanly\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(stt_app.__file__).resolve().parents[1]),
        "APPDATA": str(tmp_path / "appdata"),
        "LOCALAPPDATA": str(tmp_path / "localappdata"),
    }
    crashed = subprocess.run(
        [sys.executable, "-c", _CRASHING_SESSION, str(log_dir), "crash"],
        env=env,
        capture_output=True,
        timeout=60,
    )
    restarted = subprocess.run(
        [sys.executable, "-c", _CRASHING_SESSION, str(log_dir), "restart"],
        env=env,
        capture_output=True,
        timeout=60,
    )

    assert crashed.returncode != 0
    assert restarted.returncode == 0, restarted.stderr
    crash_text = (log_dir / "crash.log").read_text(encoding="utf-8")
    assert "Fatal Python error" in crash_text or "fatal exception" in crash_text
    log_text = (log_dir / "dictation.log").read_text(encoding="utf-8")
    assert "previous_session_end clean_quit=no" in log_text
    assert "most recent call first" in log_text, "the trace is not in the log"
    diagnostics = AppLogger(root_dir=log_dir).diagnostics_text()
    assert "last line before the crash" in diagnostics
    assert "an older session" not in diagnostics
    assert "current and previous session" in diagnostics.splitlines()[0]


def test_only_a_session_that_did_not_quit_is_reported(
    monkeypatch, tmp_path, _restore_exception_hooks
):
    enabled = []
    monkeypatch.setattr(
        logger_module.faulthandler, "enable", lambda **kwargs: enabled.append(kwargs)
    )
    log, records = _capturing_logger("test.crash_reporting")

    killed = logger_module.start_crash_reporting(tmp_path, log, version="1")
    killed.stream.close()  # ended without `mark_clean_exit`, as a kill does
    quitting = logger_module.start_crash_reporting(tmp_path, log, version="1")
    quitting.mark_clean_exit()
    quitting.stream.close()
    logger_module.start_crash_reporting(tmp_path, log, version="1").stream.close()

    reports = [
        line for line in records.lines if line.startswith("previous_session_end")
    ]
    expected = (
        "previous_session_end clean_quit=no native_trace_lines=0 "
        f"crash_log={tmp_path / 'crash.log'}"
    )
    assert reports == [expected]
    assert [kwargs["all_threads"] for kwargs in enabled] == [True, True, True]


def test_a_large_crash_log_is_set_aside_at_start(
    monkeypatch, tmp_path, _restore_exception_hooks
):
    monkeypatch.setattr(logger_module.faulthandler, "enable", lambda **kwargs: None)
    monkeypatch.setattr(logger_module, "CRASH_LOG_MAX_BYTES", 100)
    crash_log = tmp_path / "crash.log"
    crash_log.write_text("x" * 101, encoding="utf-8")
    log, _records = _capturing_logger("test.crash_rotation")

    logger_module.start_crash_reporting(tmp_path, log, version="1").stream.close()

    assert (tmp_path / "crash.log.1").read_text(encoding="utf-8") == "x" * 101
    assert crash_log.stat().st_size < 101


def test_uncaught_exceptions_are_logged_with_their_traceback(
    monkeypatch, _restore_exception_hooks
):
    """Without a console, these went nowhere: a dead button, a worker gone."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    handed_on = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: handed_on.append("sys"))
    monkeypatch.setattr(threading, "excepthook", lambda args: handed_on.append("thr"))
    monkeypatch.setattr(sys, "unraisablehook", lambda u: handed_on.append("unr"))
    log, records = _capturing_logger("test.exception_logging")
    logger_module.install_exception_logging(log)
    logger_module.install_exception_logging(log)  # a second call adds nothing

    def _thread_target():
        raise ValueError("worker boom")

    worker = threading.Thread(target=_thread_target, name="boom_worker")
    worker.start()
    worker.join(5)

    class _Emitter(QtCore.QObject):
        fired = QtCore.Signal()

    def _slot():
        raise RuntimeError("slot boom")

    emitter = _Emitter()
    emitter.fired.connect(_slot)
    emitter.fired.emit()
    app.processEvents()

    class _Finalizer:
        def __del__(self):
            raise OSError("del boom")

    _Finalizer()

    text = "\n".join(records.lines)
    assert "unhandled_thread_exception thread=boom_worker" in text
    assert "ValueError: worker boom" in text
    assert "unhandled_exception" in text and "RuntimeError: slot boom" in text
    assert "unraisable_exception" in text and "OSError: del boom" in text
    assert sorted(handed_on) == ["sys", "thr", "unr"]
