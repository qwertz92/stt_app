"""Tests for AppLogger — initialization, log path, diagnostics, idempotency."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

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
    """`install_exception_logging` replaces three process-wide hooks, and
    `install_qt_message_logging` Qt's message handler."""
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(sys, "unraisablehook", sys.unraisablehook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    installed_before = logger_module._qt_message_handler
    yield
    if logger_module._qt_message_handler is not installed_before:
        QtCore.qInstallMessageHandler(installed_before)
        logger_module._qt_message_handler = installed_before


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
    quitting.mark_quit_started()
    quitting.mark_clean_exit()
    quitting.stream.close()
    logger_module.start_crash_reporting(tmp_path, log, version="1").stream.close()

    reports = [line for line in records.lines if line.startswith("previous_session")]
    expected = (
        "previous_session_end clean_quit=no quit_started=no native_trace_lines=0 "
        f"crash_log={tmp_path / 'crash.log'} session_started="
    )
    assert len(reports) == 1
    assert reports[0].startswith(expected)
    assert reports[0].endswith(f" pid={os.getpid()} version=1")
    assert [kwargs["all_threads"] for kwargs in enabled] == [True, True, True]


def test_a_report_repeated_after_an_unopenable_crash_log_names_the_same_session(
    monkeypatch, tmp_path, _restore_exception_hooks
):
    """A start that cannot open crash.log writes no start line of its own, so
    the next start reports the same crash again (accepted, see
    known-limitations.md); both reports name that session."""
    (tmp_path / "crash.log").write_text(
        "=== stt_app session started 2026-10-23T10:00:00+02:00 pid=42 "
        "version=0.13.0 ===\n"
        "Windows fatal exception: access violation\n",
        encoding="utf-8",
    )
    log, records = _capturing_logger("test.crash_repeated")

    def _refused(*_args, **_kwargs):
        raise PermissionError("held by a virus scanner")

    monkeypatch.setattr(logger_module, "open", _refused, raising=False)
    assert logger_module.start_crash_reporting(tmp_path, log, version="1") is None
    monkeypatch.delattr(logger_module, "open")
    monkeypatch.setattr(logger_module.faulthandler, "enable", lambda **kwargs: None)
    logger_module.start_crash_reporting(tmp_path, log, version="1").stream.close()

    reports = [line for line in records.lines if line.startswith("previous_session")]
    session = "session_started=2026-10-23T10:00:00+02:00 pid=42 version=0.13.0"
    assert len(reports) == 2
    assert all(report.splitlines()[0].endswith(session) for report in reports)


def test_a_session_ended_during_its_quit_is_not_reported_as_a_crash(
    monkeypatch, tmp_path, _restore_exception_hooks
):
    """A Windows shutdown ends the process inside WM_ENDSESSION, after Qt
    emitted `aboutToQuit` and before `app.exec()` returns, so the session
    never writes its end line. Reported as a crash, every restart of the PC
    put a warning into the log and doubled the next diagnostics copy."""
    monkeypatch.setattr(logger_module.faulthandler, "enable", lambda **kwargs: None)
    log, records = _capturing_logger("test.crash_shutdown")

    shut_down = logger_module.start_crash_reporting(tmp_path, log, version="1")
    shut_down.mark_quit_started()
    shut_down.stream.close()  # Windows ended it there
    hung = logger_module.start_crash_reporting(tmp_path, log, version="1")
    hung.mark_quit_started()
    hung.stream.write("Timeout (0:00:15)!\nThread 0x0001 (most recent call first):\n")
    hung.stream.close()  # the quit watchdog's dump, then its exit
    logger_module.start_crash_reporting(tmp_path, log, version="1").stream.close()

    reports = [line for line in records.lines if line.startswith("previous_session")]
    assert len(reports) == 2
    assert reports[0].startswith("previous_session_stopped_during_quit ")
    assert "Windows shutdown" in reports[0]
    assert logger_module.PREVIOUS_SESSION_END_LOG_MARKER not in reports[0], (
        "the diagnostics copy would reach back into a session that only shut down"
    )
    assert reports[1].startswith(
        "previous_session_end clean_quit=no quit_started=yes native_trace_lines=2 "
    )


def test_a_long_dump_keeps_its_header_and_the_crashing_thread(
    monkeypatch, tmp_path, _restore_exception_hooks
):
    """faulthandler writes the header first and the crashing thread last,
    after every other thread (measured). Keeping only the last lines of a
    long dump lost the header that names the fault."""
    monkeypatch.setattr(logger_module.faulthandler, "enable", lambda **kwargs: None)
    other_threads = [
        line
        for index in range(200)
        for line in (
            f"Thread 0x{index:04x} (most recent call first):",
            '  File "threading.py", line 359 in wait',
        )
    ]
    (tmp_path / "crash.log").write_text(
        "\n".join(
            [
                "=== stt_app session started 2026-10-23T10:00:00+02:00 ===",
                "Windows fatal exception: code 0xe06d7363",
                "",
                "Thread 0x9999 (most recent call first):",
                '  File "survived.py", line 1 in handled',
                "Windows fatal exception: access violation",
                "",
                *other_threads,
                "",
                "Current thread 0x5540 (most recent call first):",
                '  File "history_dialog.py", line 42 in crashing_frame',
                "",
                "Extension modules: PySide6.QtCore (total: 1)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    log, records = _capturing_logger("test.crash_excerpt")

    logger_module.start_crash_reporting(tmp_path, log, version="1").stream.close()

    (report,) = [line for line in records.lines if line.startswith("previous_session")]
    excerpt = report.splitlines()[1:]
    assert excerpt[0] == "(3 earlier trace lines are in crash.log)"
    assert excerpt[1] == "Windows fatal exception: access violation"
    assert '  File "history_dialog.py", line 42 in crashing_frame' in excerpt
    assert excerpt[-1] == "Extension modules: PySide6.QtCore (total: 1)"
    assert any("lines of this dump are left out here" in line for line in excerpt)
    # The excerpt is bounded: the cap plus the two notes.
    assert len(excerpt) == logger_module.CRASH_TRACE_MAX_LINES + 2


def test_the_whole_previous_section_is_read(
    monkeypatch, tmp_path, _restore_exception_hooks
):
    """Only the last 256 kB used to be read: a session whose dumps were
    longer had its start line outside, and its crash went unreported."""
    monkeypatch.setattr(logger_module.faulthandler, "enable", lambda **kwargs: None)
    trace = "\n".join(f'  File "deep.py", line {i} in frame' for i in range(12_000))
    (tmp_path / "crash.log").write_text(
        "=== stt_app session started 2026-10-23T10:00:00+02:00 ===\n"
        f"Fatal Python error: Aborted\n{trace}\n",
        encoding="utf-8",
    )
    assert (tmp_path / "crash.log").stat().st_size > 256_000
    log, records = _capturing_logger("test.crash_window")

    logger_module.start_crash_reporting(tmp_path, log, version="1").stream.close()

    (report,) = [line for line in records.lines if line.startswith("previous_session")]
    assert "clean_quit=no" in report
    assert "native_trace_lines=12001" in report
    assert report.splitlines()[1] == "Fatal Python error: Aborted"


def test_a_section_longer_than_the_crash_log_bound_is_still_reported(
    monkeypatch, tmp_path, _restore_exception_hooks
):
    monkeypatch.setattr(logger_module.faulthandler, "enable", lambda **kwargs: None)
    monkeypatch.setattr(logger_module, "CRASH_LOG_MAX_BYTES", 1_000)
    trace = "\n".join(f'  File "deep.py", line {i} in frame' for i in range(200))
    (tmp_path / "crash.log").write_text(
        "=== stt_app session started 2026-10-23T10:00:00+02:00 ===\n"
        f"Fatal Python error: Aborted\n{trace}\n",
        encoding="utf-8",
    )
    log, records = _capturing_logger("test.crash_window_bound")

    logger_module.start_crash_reporting(tmp_path, log, version="1").stream.close()

    (report,) = [line for line in records.lines if line.startswith("previous_session")]
    assert "clean_quit=no" in report
    assert report.splitlines()[-1] == '  File "deep.py", line 199 in frame'


def test_the_previous_session_is_reported_when_the_crash_log_cannot_be_opened(
    monkeypatch, tmp_path, _restore_exception_hooks
):
    (tmp_path / "crash.log").write_text(
        "=== stt_app session started 2026-10-23T10:00:00+02:00 ===\n"
        "Windows fatal exception: access violation\n",
        encoding="utf-8",
    )

    def _refused(*_args, **_kwargs):
        raise PermissionError("held by a virus scanner")

    monkeypatch.setattr(logger_module, "open", _refused, raising=False)
    log, records = _capturing_logger("test.crash_unopenable")

    assert logger_module.start_crash_reporting(tmp_path, log, version="1") is None

    assert any(
        line.startswith("previous_session_end clean_quit=no") for line in records.lines
    )
    assert any(line.startswith("crash_log_unavailable") for line in records.lines)


def test_a_session_faulthandler_could_not_watch_is_not_reported(
    monkeypatch, tmp_path, _restore_exception_hooks
):
    """Its start line was written, its end line never would be: the next
    start reported a crash that nothing had observed."""

    def _refused(**_kwargs):
        raise RuntimeError("cannot enable")

    monkeypatch.setattr(logger_module.faulthandler, "enable", _refused)
    log, records = _capturing_logger("test.crash_unwatched")
    assert logger_module.start_crash_reporting(tmp_path, log, version="1") is None
    monkeypatch.setattr(logger_module.faulthandler, "enable", lambda **kwargs: None)

    logger_module.start_crash_reporting(tmp_path, log, version="1").stream.close()

    assert not [line for line in records.lines if line.startswith("previous_session")]


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


def test_a_failing_log_handler_never_keeps_the_previous_hook_from_running(
    monkeypatch, _restore_exception_hooks
):
    handed_on = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: handed_on.append("sys"))
    monkeypatch.setattr(threading, "excepthook", lambda args: handed_on.append("thr"))
    monkeypatch.setattr(sys, "unraisablehook", lambda u: handed_on.append("unr"))

    class _BrokenHandler(logging.Handler):
        def emit(self, record):
            raise RuntimeError("the log file is gone")

    log = logging.getLogger("test.exception_logging_broken")
    log.handlers[:] = [_BrokenHandler()]
    log.propagate = False
    logger_module.install_exception_logging(log)
    error = ValueError("boom")

    sys.excepthook(ValueError, error, None)
    threading.excepthook(threading.ExceptHookArgs((ValueError, error, None, None)))
    sys.unraisablehook(
        SimpleNamespace(
            exc_type=ValueError,
            exc_value=error,
            exc_traceback=None,
            err_msg=None,
            object=None,
        )
    )

    assert handed_on == ["sys", "thr", "unr"]


def test_qt_messages_reach_the_log_once_per_kind_and_never_recurse(
    _restore_exception_hooks,
):
    """Qt's own warnings went to stderr, which the windowed build lacks: the
    "Timers cannot be stopped from another thread" before the crash of
    2026-10-23 was written nowhere."""
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    log, records = _capturing_logger("test.qt_messages")

    class _ReentrantHandler(logging.Handler):
        """A handler that makes Qt print while it logs."""

        def emit(self, record):
            QtCore.qWarning("raised while logging")

    log.addHandler(_ReentrantHandler())
    logger_module.install_qt_message_logging(log)
    logger_module.install_qt_message_logging(log)  # a second call adds nothing

    QtCore.qWarning("QObject::killTimer: Timers cannot be stopped from another thread")
    QtCore.qDebug("debug chatter")
    QtCore.qCritical("critical trouble")
    for index in range(30):
        QtCore.qWarning(f"QWindowsWindow::setGeometry: Unable to set {index}x{index}")

    assert records.lines[0] == (
        "qt_message category=default "
        "QObject::killTimer: Timers cannot be stopped from another thread"
    )
    assert records.lines[1] == "qt_message category=default critical trouble"
    assert not any("debug chatter" in line for line in records.lines)
    assert not any("raised while logging" in line for line in records.lines)
    geometry = [line for line in records.lines if "setGeometry" in line]
    assert len(geometry) == 20
    assert geometry[-1].endswith("(repeated too often; later ones are not logged)")


def test_qt_messages_with_other_pointers_count_as_one_and_the_count_is_bounded(
    monkeypatch, _restore_exception_hooks
):
    """Numbers alone did not merge hex pointers (0x1f3a), and every distinct
    text stayed in the count for the whole session."""
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    monkeypatch.setattr(logger_module, "_QT_MESSAGE_REPEAT_LIMIT", 2)
    monkeypatch.setattr(logger_module, "_QT_MESSAGE_KINDS_LIMIT", 3)
    log, records = _capturing_logger("test.qt_message_kinds")
    logger_module.install_qt_message_logging(log)

    for pointer in ("0x1f3a", "0xbeef", "0xCAFE"):
        QtCore.qWarning(f"QWidget({pointer}) has no parent")
    QtCore.qWarning("first other text")
    QtCore.qWarning("second other text")
    QtCore.qWarning("third other text")  # a fourth kind: the count starts over
    QtCore.qWarning("QWidget(0x1f3a) has no parent")

    pointer_lines = [line for line in records.lines if "has no parent" in line]
    assert len(pointer_lines) == 3, "two before the cap, one after the restart"
    assert pointer_lines[1].endswith("(repeated too often; later ones are not logged)")


_LATE_QT_MESSAGE = """
import logging
import sys

from PySide6 import QtCore

from stt_app.logger import install_qt_message_logging

log = logging.getLogger("late")
log.addHandler(logging.StreamHandler(sys.stderr))
install_qt_message_logging(log)


class Late:
    def __del__(self):
        QtCore.qWarning("from a finalizer at exit")


late = Late()
late.cycle = late  # only the interpreter's last collection frees it
del late
"""


def test_a_qt_message_from_the_last_collection_at_exit_does_not_crash(tmp_path):
    """The handler is Python code; Qt calling it from a finalizer while the
    interpreter shuts down crashed the process (reviewer, 6 of 6 runs)."""
    import stt_app

    env = {
        **os.environ,
        "PYTHONPATH": str(Path(stt_app.__file__).resolve().parents[1]),
        "APPDATA": str(tmp_path / "appdata"),
        "LOCALAPPDATA": str(tmp_path / "localappdata"),
    }
    for _run in range(3):
        finished = subprocess.run(
            [sys.executable, "-c", _LATE_QT_MESSAGE],
            env=env,
            capture_output=True,
            timeout=60,
        )
        assert finished.returncode == 0, finished.stderr
