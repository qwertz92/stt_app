import logging

from PySide6 import QtCore, QtGui, QtWidgets

from tts_app.config import DEFAULT_HOTKEY, FALLBACK_HOTKEY
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
        _ = wait
        _ = cancel_futures


class FakeStreamingTranscriber:
    def __init__(self):
        self.started = False
        self.stopped = False
        self.aborted = False
        self.chunks = []
        self.on_partial = None

    def transcribe_batch(self, audio_source):
        return "batch"

    def start_stream(self, on_partial=None):
        self.started = True
        self.on_partial = on_partial

    def push_audio_chunk(self, chunk: bytes):
        self.chunks.append(chunk)
        if self.on_partial is not None:
            self.on_partial("stream")

    def stop_stream(self):
        self.stopped = True
        return "stream final"

    def abort_stream(self):
        self.aborted = True


class FakeCapture:
    instances = []

    def __init__(self, *args, **kwargs):
        self.chunk_callback = kwargs.get("chunk_callback")
        self.started = False
        self.stopped = False
        FakeCapture.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        return b"RIFF"

    def save_wav(self, path, wav_bytes):
        return None


def test_controller_falls_back_to_safe_hotkey():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(hotkey=DEFAULT_HOTKEY, keep_transcript_in_clipboard=False)
    store = FakeSettingsStore(settings)
    hotkey_manager = FakeHotkeyManager()
    overlay = FakeOverlay()

    controller = DictationController(
        settings_store=store,
        hotkey_manager=hotkey_manager,
        overlay=overlay,
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
    )

    controller.reload_settings(re_register_hotkey=True)
    controller.show_idle_status()

    assert hotkey_manager.calls[0] == DEFAULT_HOTKEY
    assert hotkey_manager.calls[1] == FALLBACK_HOTKEY
    assert store.saved is not None
    assert store.saved.hotkey == FALLBACK_HOTKEY
    assert any("Using fallback" in detail for _state, detail in overlay.states)

    controller.shutdown()
    _ = app


def test_controller_shows_error_when_all_hotkey_registration_fails():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(hotkey=DEFAULT_HOTKEY, keep_transcript_in_clipboard=False)
    store = FakeSettingsStore(settings)
    hotkey_manager = FakeHotkeyManagerAllFail()
    overlay = FakeOverlay()

    controller = DictationController(
        settings_store=store,
        hotkey_manager=hotkey_manager,
        overlay=overlay,
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
    )

    controller.reload_settings(re_register_hotkey=True)
    controller.show_idle_status()

    assert overlay.states
    state, detail = overlay.states[-1]
    assert state == "Error"
    assert "Hotkey registration failed" in detail

    controller.shutdown()
    _ = app


def test_controller_restores_target_focus_before_insert():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, keep_transcript_in_clipboard=False)
    store = FakeSettingsStore(settings)
    hotkey_manager = FakeHotkeyManager()
    overlay = FakeOverlay()
    inserter = FakeTextInserter()
    focus_helper = FakeWindowFocusHelper()

    controller = DictationController(
        settings_store=store,
        hotkey_manager=hotkey_manager,
        overlay=overlay,
        text_inserter=inserter,
        logger=logging.getLogger("test.controller"),
        window_focus_helper=focus_helper,
    )

    controller._target_window_handle = 555
    controller._on_transcription_ready("hello world")

    assert focus_helper.restore_calls == [555]
    assert inserter.calls == [("hello world", 555, settings.paste_mode)]

    controller.shutdown()
    _ = app


class FakeClipboard:
    def __init__(self):
        self.value = ""

    def setText(self, text):
        self.value = text

    def text(self):
        return self.value


def test_controller_copies_transcript_on_insert_error(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, keep_transcript_in_clipboard=False)
    store = FakeSettingsStore(settings)
    hotkey_manager = FakeHotkeyManager()
    overlay = FakeOverlay()
    inserter = FakeTextInserter(should_fail=True)
    focus_helper = FakeWindowFocusHelper()

    controller = DictationController(
        settings_store=store,
        hotkey_manager=hotkey_manager,
        overlay=overlay,
        text_inserter=inserter,
        logger=logging.getLogger("test.controller"),
        window_focus_helper=focus_helper,
    )
    fake_clipboard = FakeClipboard()
    monkeypatch.setattr(QtGui.QGuiApplication, "clipboard", lambda: fake_clipboard)

    controller._target_window_handle = 555
    controller._on_transcription_ready("copy me")

    assert fake_clipboard.text() == "copy me"
    assert overlay.states[-1][0] == "Error"
    assert "Transcript copied to clipboard." in overlay.states[-1][1]

    controller.shutdown()
    _ = app


def test_controller_keeps_transcript_in_clipboard_on_success(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        keep_transcript_in_clipboard=True,
    )
    controller = DictationController(
        settings_store=FakeSettingsStore(settings),
        hotkey_manager=FakeHotkeyManager(),
        overlay=FakeOverlay(),
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
    )
    fake_clipboard = FakeClipboard()
    monkeypatch.setattr(QtGui.QGuiApplication, "clipboard", lambda: fake_clipboard)

    controller._target_window_handle = 123
    controller._on_transcription_ready("persist me")

    assert fake_clipboard.text() == "persist me"
    assert controller._overlay.states[-1][0] == "Done"

    controller.shutdown()
    _ = app


def test_copy_last_transcript_returns_false_when_empty(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, keep_transcript_in_clipboard=False)
    controller = DictationController(
        settings_store=FakeSettingsStore(settings),
        hotkey_manager=FakeHotkeyManager(),
        overlay=FakeOverlay(),
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
    )
    fake_clipboard = FakeClipboard()
    monkeypatch.setattr(QtGui.QGuiApplication, "clipboard", lambda: fake_clipboard)

    assert controller.copy_last_transcript_to_clipboard() is False

    controller._last_transcript = "latest text"
    assert controller.copy_last_transcript_to_clipboard() is True
    assert fake_clipboard.text() == "latest text"

    controller.shutdown()
    _ = app


def test_controller_streaming_mode_uses_transcriber_streaming(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        mode="streaming",
        keep_transcript_in_clipboard=False,
    )
    store = FakeSettingsStore(settings)
    overlay = FakeOverlay()
    inserter = FakeTextInserter()
    transcriber = FakeStreamingTranscriber()
    focus_helper = FakeWindowFocusHelper()
    FakeCapture.instances = []

    monkeypatch.setattr("tts_app.controller.AudioCapture", FakeCapture)
    monkeypatch.setattr("tts_app.controller.create_transcriber", lambda _s: transcriber)

    controller = DictationController(
        settings_store=store,
        hotkey_manager=FakeHotkeyManager(),
        overlay=overlay,
        text_inserter=inserter,
        logger=logging.getLogger("test.controller"),
        window_focus_helper=focus_helper,
    )
    controller._executor = ImmediateExecutor()

    controller.start_recording()
    assert transcriber.started is True
    assert FakeCapture.instances
    capture = FakeCapture.instances[-1]
    assert capture.started is True

    capture.chunk_callback(b"\x00\x01")
    controller.stop_recording()

    assert transcriber.chunks == [b"\x00\x01"]
    assert transcriber.stopped is True
    assert inserter.calls == [("stream final", focus_helper.captured_caret, settings.paste_mode)]
    assert overlay.states[-1][0] == "Done"

    controller.shutdown()
    _ = app


def test_controller_prefers_caret_handle_for_insertion_target():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, keep_transcript_in_clipboard=False)
    store = FakeSettingsStore(settings)
    hotkey_manager = FakeHotkeyManager()
    overlay = FakeOverlay()
    inserter = FakeTextInserter()
    focus_helper = FakeWindowFocusHelper()

    controller = DictationController(
        settings_store=store,
        hotkey_manager=hotkey_manager,
        overlay=overlay,
        text_inserter=inserter,
        logger=logging.getLogger("test.controller"),
        window_focus_helper=focus_helper,
    )

    controller._target_window_handle = 555
    controller._target_focus_signature = (555, 556, 557)
    controller._on_transcription_ready("hello world")

    assert focus_helper.restore_calls == [555]
    assert inserter.calls == [("hello world", 557, settings.paste_mode)]

    controller.shutdown()
    _ = app


def test_controller_streaming_aborts_when_focus_changes(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        mode="streaming",
        keep_transcript_in_clipboard=False,
    )
    overlay = FakeOverlay()
    transcriber = FakeStreamingTranscriber()
    focus_helper = FakeWindowFocusHelper()
    FakeCapture.instances = []

    monkeypatch.setattr("tts_app.controller.AudioCapture", FakeCapture)
    monkeypatch.setattr("tts_app.controller.create_transcriber", lambda _s: transcriber)

    controller = DictationController(
        settings_store=FakeSettingsStore(settings),
        hotkey_manager=FakeHotkeyManager(),
        overlay=overlay,
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=focus_helper,
    )
    controller._executor = ImmediateExecutor()

    beep_calls = {"count": 0}
    monkeypatch.setattr(controller, "_play_abort_beep", lambda: beep_calls.__setitem__("count", beep_calls["count"] + 1))

    controller.start_recording()
    capture = FakeCapture.instances[-1]
    focus_helper.current = 123456  # simulate user focus switch away from target
    capture.chunk_callback(b"\x00\x01")

    assert transcriber.aborted is True
    assert transcriber.stopped is False
    assert capture.stopped is True
    assert controller._audio_capture is None
    assert beep_calls["count"] == 1
    assert overlay.states[-1][0] == "Error"
    assert "focus changed" in overlay.states[-1][1].lower()

    controller.shutdown()
    _ = app


def test_controller_streaming_aborts_when_focus_control_changes(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        mode="streaming",
        keep_transcript_in_clipboard=False,
    )
    overlay = FakeOverlay()
    transcriber = FakeStreamingTranscriber()
    focus_helper = FakeWindowFocusHelper()
    FakeCapture.instances = []

    monkeypatch.setattr("tts_app.controller.AudioCapture", FakeCapture)
    monkeypatch.setattr("tts_app.controller.create_transcriber", lambda _s: transcriber)

    controller = DictationController(
        settings_store=FakeSettingsStore(settings),
        hotkey_manager=FakeHotkeyManager(),
        overlay=overlay,
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=focus_helper,
    )
    controller._executor = ImmediateExecutor()

    controller.start_recording()
    focus_helper.current = focus_helper.captured  # same top-level window
    focus_helper.current_focus = focus_helper.captured_focus
    focus_helper.current_caret = 999999  # changed caret owner
    controller._on_stream_focus_poll()

    assert transcriber.aborted is True
    assert controller._audio_capture is None
    assert overlay.states[-1][0] == "Error"
    assert "focus changed" in overlay.states[-1][1].lower()

    controller.shutdown()
    _ = app


def test_stream_tail_uses_word_overlap_for_append():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    controller = DictationController(
        settings_store=FakeSettingsStore(AppSettings(hotkey=FALLBACK_HOTKEY)),
        hotkey_manager=FakeHotkeyManager(),
        overlay=FakeOverlay(),
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
    )

    assert controller._stream_tail("hello world", "world again now") == "again now"
    assert controller._stream_tail("alpha beta", "gamma delta") == ""

    controller.shutdown()
    _ = app


def test_stream_live_delta_waits_for_partial_stability():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    controller = DictationController(
        settings_store=FakeSettingsStore(AppSettings(hotkey=FALLBACK_HOTKEY)),
        hotkey_manager=FakeHotkeyManager(),
        overlay=FakeOverlay(),
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
    )

    delta, committed = controller._compute_stream_live_delta("", "", "hello world")
    assert delta == ""
    assert committed == ""

    delta, committed = controller._compute_stream_live_delta("", "hello world", "hello world now")
    assert delta == "hello"
    assert committed == "hello"

    delta, committed = controller._compute_stream_live_delta(
        "hello",
        "hello world now",
        "hello world now again",
    )
    assert delta == "world"
    assert committed == "hello world"

    controller.shutdown()
    _ = app


def test_stream_live_delta_recovers_after_partial_revision():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    controller = DictationController(
        settings_store=FakeSettingsStore(AppSettings(hotkey=FALLBACK_HOTKEY)),
        hotkey_manager=FakeHotkeyManager(),
        overlay=FakeOverlay(),
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
    )

    delta, committed = controller._compute_stream_live_delta(
        "hello world",
        "hello there foo bar",
        "hello there foo bar baz",
    )
    assert delta == "there foo"
    assert committed == "hello world there foo"

    delta2, committed2 = controller._compute_stream_live_delta(
        committed,
        "hello there foo bar baz",
        "hello there foo bar baz qux",
    )
    assert delta2 == "bar"
    assert committed2.endswith("there foo bar")

    controller.shutdown()
    _ = app


def test_stream_finalize_tail_uses_last_partial_when_final_diverges():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    controller = DictationController(
        settings_store=FakeSettingsStore(AppSettings(hotkey=FALLBACK_HOTKEY)),
        hotkey_manager=FakeHotkeyManager(),
        overlay=FakeOverlay(),
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
    )
    controller._stream_last_partial_text = "hello world plus"

    tail = controller._best_stream_finalize_tail("hello world", "hello word")

    assert tail == "plus"

    controller.shutdown()
    _ = app


def test_streaming_partial_insertions_continue_after_revisions():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        mode="streaming",
        keep_transcript_in_clipboard=False,
    )
    inserter = FakeTextInserter()
    focus_helper = FakeWindowFocusHelper()
    overlay = FakeOverlay()
    controller = DictationController(
        settings_store=FakeSettingsStore(settings),
        hotkey_manager=FakeHotkeyManager(),
        overlay=overlay,
        text_inserter=inserter,
        logger=logging.getLogger("test.controller"),
        window_focus_helper=focus_helper,
    )
    controller._streaming_recording = True
    controller._audio_capture = object()
    controller._target_window_handle = focus_helper.captured
    controller._target_focus_signature = focus_helper.capture_target_signature()

    partials = [
        "hello world",
        "hello world this",
        "hello there this is",
        "hello there this is working",
        "hello there this is working now",
    ]
    for partial in partials:
        controller._on_transcription_partial(partial)

    inserted_texts = [call[0] for call in inserter.calls]
    assert len(inserted_texts) >= 3
    assert any("there this" in text for text in inserted_texts)
    assert any("is" in text for text in inserted_texts)
    assert overlay.states[-1][0] == "Listening"

    controller.shutdown()
    _ = app


def test_streaming_finalize_does_not_copy_revision_to_clipboard(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        keep_transcript_in_clipboard=False,
    )
    overlay = FakeOverlay()
    inserter = FakeTextInserter()
    controller = DictationController(
        settings_store=FakeSettingsStore(settings),
        hotkey_manager=FakeHotkeyManager(),
        overlay=overlay,
        text_inserter=inserter,
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
    )
    fake_clipboard = FakeClipboard()
    monkeypatch.setattr(QtGui.QGuiApplication, "clipboard", lambda: fake_clipboard)

    controller._active_session_mode = "streaming"
    controller._stream_committed_text = "hello world"
    controller._target_window_handle = 555
    controller._on_transcription_ready("world plus")

    assert fake_clipboard.text() == ""
    assert inserter.calls[-1][0] == " plus"
    assert overlay.states[-1][0] == "Done"

    controller.shutdown()
    _ = app
