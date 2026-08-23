"""Shared test fakes and fixtures for controller tests.

Both test_controller.py and test_controller_coverage.py use these helper
classes to avoid duplicating ~150 lines of boilerplate.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
from PySide6 import QtCore, QtGui, QtWidgets

from stt_app.config import DEFAULT_CANCEL_HOTKEY, FALLBACK_HOTKEY
from stt_app.controller import DictationController
from stt_app.settings_store import AppSettings
from stt_app.text_inserter import TextInsertionError


@pytest.fixture(autouse=True)
def _isolate_appdata(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))


@pytest.fixture(autouse=True)
def _forbid_handing_paths_to_the_desktop_shell(monkeypatch):
    """No test may open a real file-manager window or launch a process.

    Several dialogs reveal a file or directory in the system file manager. A
    test that reaches one of those paths without stubbing it opened a real
    Explorer window on the developer's desktop and left it there — usually
    pointing at an empty pytest ``tmp_path``, which then looks like a product
    bug ("my recordings folder is empty").

    Failing loudly is deliberate: a test that legitimately exercises such a
    path must stub it and assert on the call, which every current one does.
    A silently inert stub would let the next such test go unnoticed.
    """

    def _blocked_start_detached(*args, **kwargs):
        raise AssertionError(
            "QProcess.startDetached was called in a test. Patch it (and assert "
            "on the call) instead of launching a real process. Args: "
            f"{args!r}"
        )

    def _blocked_open_url(url, *args, **kwargs):
        raise AssertionError(
            "QDesktopServices.openUrl was called in a test. Patch it (and "
            "assert on the call) instead of handing the path to the shell. "
            f"URL: {url!r}"
        )

    monkeypatch.setattr(
        QtCore.QProcess,
        "startDetached",
        staticmethod(_blocked_start_detached),
        raising=False,
    )
    monkeypatch.setattr(
        QtGui.QDesktopServices,
        "openUrl",
        staticmethod(_blocked_open_url),
        raising=False,
    )


@pytest.fixture(autouse=True)
def _forbid_blocking_modal_dialogs(monkeypatch):
    """No test may open a modal dialog that waits for a human click.

    ``QMessageBox.information`` and the ``QFileDialog`` getters run their own
    event loop until someone clicks. A test that reaches one unstubbed does not
    fail — it hangs the whole run forever, with no output naming the cause.
    Turning that into an immediate, named failure is the difference between a
    five-second fix and bisecting a twenty-minute suite.

    Every test that legitimately drives one of these already patches it, so
    this only catches the ones that would otherwise hang.
    """

    def _blocker(kind: str):
        def _blocked(*args, **kwargs):
            raise AssertionError(
                f"{kind} was called in a test and would block until a human "
                "clicks it. Patch it (and assert on the call) instead."
            )

        return _blocked

    for name in ("information", "warning", "critical", "question", "about"):
        monkeypatch.setattr(
            QtWidgets.QMessageBox,
            name,
            staticmethod(_blocker(f"QMessageBox.{name}")),
            raising=False,
        )
    for name in (
        "getExistingDirectory",
        "getOpenFileName",
        "getOpenFileNames",
        "getSaveFileName",
    ):
        monkeypatch.setattr(
            QtWidgets.QFileDialog,
            name,
            staticmethod(_blocker(f"QFileDialog.{name}")),
            raising=False,
        )


class FakeSettingsStore:
    def __init__(self, settings):
        self._settings = settings
        self.saved = None

    def load(self):
        return self._settings

    def save(self, settings):
        self.saved = settings


class FakeHotkeyManager:
    def __init__(self):
        self.calls = []

    def register(self, hotkey):
        self.calls.append(hotkey)
        if hotkey not in {FALLBACK_HOTKEY, DEFAULT_CANCEL_HOTKEY}:
            raise ValueError("blocked")

    def unregister(self):
        pass


class FakeHotkeyManagerAllFail(FakeHotkeyManager):
    def register(self, hotkey):
        self.calls.append(hotkey)
        raise ValueError("blocked")


class FakeCancelHotkeyManager(FakeHotkeyManager):
    pass


class FakeOverlay:
    def __init__(self):
        self.states = []
        self.state_kwargs = []
        self.opacity_values = []
        self.compact_calls = 0
        self.always_on_top_values = []
        self.language_options = []
        self.reveal_calls = 0
        self.reveal_durations = []
        self.queue_updates = []

    def set_state(self, state, detail="", **kwargs):
        self.states.append((state, detail))
        self.state_kwargs.append(dict(kwargs))

    def set_transcription_queue(self, items):
        self.queue_updates.append([(int(t), str(label)) for t, label in items])

    def set_opacity_percent(self, value: int):
        self.opacity_values.append(int(value))

    def set_always_on_top(self, value: bool):
        self.always_on_top_values.append(bool(value))

    def set_language_options(self, modes, selected_mode):
        self.language_options.append((tuple(modes), str(selected_mode)))

    def reveal_temporarily(self, duration_ms=1800):
        self.reveal_calls += 1
        self.reveal_durations.append(int(duration_ms))

    def ensure_compact_size(self):
        self.compact_calls += 1
        return None


class FakeTextInserter:
    def __init__(self, should_fail=False):
        self.should_fail = should_fail
        self.calls = []
        self.restore_flags = []

    def insert_text(self, text, target_hwnd=None):
        self.calls.append((text, target_hwnd))
        if self.should_fail:
            raise TextInsertionError("failed insert")
        return True

    def insert_text_with_options(
        self,
        text,
        target_hwnd=None,
        paste_mode="auto",
        restore_clipboard=True,
    ):
        self.calls.append((text, target_hwnd, paste_mode))
        self.restore_flags.append(restore_clipboard)
        if self.should_fail:
            raise TextInsertionError("failed insert")
        return True

class FakeWindowFocusHelper:
    def __init__(self):
        self.captured = 987
        self.captured_focus = 654
        self.captured_caret = 321
        self.current = 987
        self.current_focus = 654
        self.current_caret = 321
        self.restore_calls = []

    def capture_target_window(self):
        return self.captured

    def capture_target_signature(self):
        focus = self.captured_focus or self.captured
        caret = self.captured_caret or focus
        return (self.captured, focus, caret)

    def get_foreground_window(self):
        return self.current

    def get_focus_signature(self):
        focus = self.current_focus or self.current
        caret = self.current_caret or focus
        return (self.current, focus, caret)

    def restore_target_window(self, hwnd):
        self.restore_calls.append(hwnd)
        self.current = hwnd
        if hwnd == self.captured:
            self.current_focus = self.captured_focus
            self.current_caret = self.captured_caret
        else:
            self.current_focus = hwnd
            self.current_caret = hwnd
        return True


class ImmediateExecutor:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)
        return None

    def shutdown(self, wait=False, cancel_futures=False):
        pass


class FailSubmitExecutor:
    def submit(self, fn, *args, **kwargs):
        raise AssertionError("submit() should not be called on this executor")

    def shutdown(self, wait=False, cancel_futures=False):
        pass


class FakeStreamingTranscriber:
    def __init__(self, *, stop_raises=None, push_raises=None):
        self.started = False
        self.stopped = False
        self.aborted = False
        self.chunks = []
        self.on_partial = None
        self.on_error = None
        self._stop_raises = stop_raises
        self._push_raises = push_raises

    def transcribe_batch(self, audio_source):
        return "batch"

    def start_stream(self, on_partial=None, on_error=None):
        self.started = True
        self.on_partial = on_partial
        self.on_error = on_error

    def push_audio_chunk(self, chunk: bytes):
        if self._push_raises:
            raise self._push_raises
        self.chunks.append(chunk)
        if self.on_partial is not None:
            self.on_partial("stream")

    def stop_stream(self):
        self.stopped = True
        if self._stop_raises:
            raise self._stop_raises
        return "stream final"

    def abort_stream(self):
        self.aborted = True


class FakeCapture:
    instances: list["FakeCapture"] = []

    def __init__(self, *args, **kwargs):
        self.chunk_callback = kwargs.get("chunk_callback")
        self.started = False
        self.stopped = False
        self._wav_bytes = b"RIFF"
        self.last_saved_path = None
        self.last_saved_bytes = None
        FakeCapture.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        return self._wav_bytes

    def save_wav(self, path, wav_bytes):
        self.last_saved_path = path
        self.last_saved_bytes = wav_bytes


class FakeCaptureFails(FakeCapture):
    def start(self):
        from stt_app.audio_capture import AudioCaptureError

        raise AudioCaptureError("no mic")


class FakeLastRecordingStore:
    def __init__(self, path: str = "/tmp/last_recording.wav"):
        self.path = Path(path)
        self.saved: list[tuple[bytes, bool]] = []
        self.transcribing: list[tuple[str, str, str]] = []
        self.failed: list[str] = []
        self.canceled: list[str] = []
        self.completed = 0
        self._available = False

    def save_recording(self, wav_bytes: bytes, *, keep_after_success: bool):
        self.saved.append((bytes(wav_bytes), bool(keep_after_success)))
        self._available = bool(wav_bytes)
        return None

    def mark_transcribing(self, *, engine: str, model: str, mode: str) -> None:
        self.transcribing.append((engine, model, mode))

    def mark_failed(self, error: str) -> None:
        self.failed.append(str(error))
        self._available = True

    def mark_canceled(self, detail: str = "") -> None:
        self.canceled.append(str(detail))
        self._available = True

    def mark_completed(self) -> None:
        self.completed += 1
        self._available = False

    def selectable_path(self, archived_recordings_dir=None):
        if not self._available:
            return None
        return self.path

    def is_managed_audio_path(self, path: str | Path) -> bool:
        return Path(path) == self.path

    def has_recoverable_recording(self) -> bool:
        return self._available


def make_controller(**kwargs):
    """Create a DictationController with sensible defaults for testing."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    defaults = dict(
        settings_store=FakeSettingsStore(
            AppSettings(hotkey=FALLBACK_HOTKEY, keep_transcript_in_clipboard=False)
        ),
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeCancelHotkeyManager(),
        overlay=FakeOverlay(),
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
    )
    defaults.update(kwargs)
    return DictationController(**defaults), app


@pytest.fixture(autouse=True)
def _keep_download_locks_out_of_the_real_appdata(tmp_path_factory, monkeypatch):
    r"""Give every test its own directory for cross-process download locks.

    The coordinator takes a real OS lock before downloading, and its default
    location is the user's `%APPDATA%\stt_app\locks`. Left alone, the suite
    writes lock files into the real profile, and -- worse -- a test that leaves
    one held can block an unrelated later test forever, because acquiring the
    lock waits with no timeout by design.
    """
    from stt_app import model_download_coordinator

    lock_dir = tmp_path_factory.mktemp("download-locks")
    monkeypatch.setattr(
        model_download_coordinator, "_download_lock_dir", lambda: lock_dir
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "pixel_exact: asserts exact widget geometry; skipped on the offscreen "
        "Qt platform, where metrics are 1-4 px off and failures are artifacts",
    )


@pytest.fixture(autouse=True)
def _skip_pixel_exact_tests_on_the_offscreen_platform(request):
    """Turn the known offscreen metric drift into a skip, not a false failure.

    AGENTS.md already says an offscreen run's layout failures are artifacts, but
    saying so did not stop it: both CI workflows set `QT_QPA_PLATFORM=offscreen`
    and the release gate ran the whole suite before the build step, so v0.8.0
    was tagged and never published. The workflows no longer set it; this makes
    the failure mode impossible to reintroduce by accident.
    """
    if request.node.get_closest_marker("pixel_exact") is None:
        return
    if os.environ.get("QT_QPA_PLATFORM", "").strip().lower() == "offscreen":
        pytest.skip(
            "Pixel-exact layout assertions are meaningless on the offscreen Qt "
            "platform (metrics shift 1-4 px); run on a real platform plugin."
        )
