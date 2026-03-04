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
    assert len(lines) == 3
    assert lines[0] == "line3"


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
