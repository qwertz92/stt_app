"""Shared test fakes and fixtures for controller tests.

Both test_controller.py and test_controller_coverage.py use these helper
classes to avoid duplicating ~150 lines of boilerplate.
"""

from __future__ import annotations

import logging

from PySide6 import QtWidgets

from tts_app.config import FALLBACK_HOTKEY
from tts_app.controller import DictationController
from tts_app.settings_store import AppSettings
from tts_app.text_inserter import TextInsertionError


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
        if hotkey != FALLBACK_HOTKEY:
            raise ValueError("blocked")

    def unregister(self):
        pass


class FakeHotkeyManagerAllFail(FakeHotkeyManager):
    def register(self, hotkey):
        self.calls.append(hotkey)
        raise ValueError("blocked")


class FakeOverlay:
    def __init__(self):
        self.states = []

    def set_state(self, state, detail=""):
        self.states.append((state, detail))


class FakeTextInserter:
    def __init__(self, should_fail=False):
        self.should_fail = should_fail
        self.calls = []

    def insert_text(self, text, target_hwnd=None):
        self.calls.append((text, target_hwnd))
        if self.should_fail:
            raise TextInsertionError("failed insert")
        return True

    def insert_text_with_options(self, text, target_hwnd=None, paste_mode="auto"):
        self.calls.append((text, target_hwnd, paste_mode))
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
        self._stop_raises = stop_raises
        self._push_raises = push_raises

    def transcribe_batch(self, audio_source):
        return "batch"

    def start_stream(self, on_partial=None):
        self.started = True
        self.on_partial = on_partial

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
        from tts_app.audio_capture import AudioCaptureError

        raise AudioCaptureError("no mic")


def make_controller(**kwargs):
    """Create a DictationController with sensible defaults for testing."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    defaults = dict(
        settings_store=FakeSettingsStore(
            AppSettings(hotkey=FALLBACK_HOTKEY, keep_transcript_in_clipboard=False)
        ),
        hotkey_manager=FakeHotkeyManager(),
        overlay=FakeOverlay(),
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
    )
    defaults.update(kwargs)
    return DictationController(**defaults), app
