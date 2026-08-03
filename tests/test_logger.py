"""Tests for AppLogger — initialization, log path, diagnostics, idempotency."""

from __future__ import annotations

import logging

import pytest

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
    rh = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
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
        h for h in root.handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
        and h.baseFilename == str(al.log_path)
    ]
    # Only one handler for the same path
    assert len(rh) == 1
