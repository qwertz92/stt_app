import logging

from PySide6 import QtGui, QtWidgets

from stt_app.config import DEFAULT_HOTKEY, FALLBACK_HOTKEY
from stt_app.controller import DictationController
from stt_app.settings_store import AppSettings

from conftest import (
    FakeCapture,
    FakeHotkeyManager,
    FakeHotkeyManagerAllFail,
    FakeOverlay,
    FakeSettingsStore,
    FakeStreamingTranscriber,
    FakeTextInserter,
    FakeWindowFocusHelper,
    FailSubmitExecutor,
    ImmediateExecutor,
)


def test_controller_falls_back_to_safe_hotkey():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(hotkey=DEFAULT_HOTKEY, keep_transcript_in_clipboard=False)
    store = FakeSettingsStore(settings)
    hotkey_manager = FakeHotkeyManager()
    overlay = FakeOverlay()

    controller = DictationController(
        settings_store=store,
        hotkey_manager=hotkey_manager,
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=overlay,
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
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
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=overlay,
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
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
        cancel_hotkey_manager=FakeHotkeyManager(),
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
        cancel_hotkey_manager=FakeHotkeyManager(),
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
        cancel_hotkey_manager=FakeHotkeyManager(),
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
        cancel_hotkey_manager=FakeHotkeyManager(),
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

    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    monkeypatch.setattr(
        "stt_app.controller.create_transcriber", lambda _s, **kw: transcriber
    )

    controller = DictationController(
        settings_store=store,
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
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
    assert inserter.calls == [
        ("stream", focus_helper.captured_caret, settings.paste_mode),
        (
            "replace",
            "stream",
            "stream final",
            focus_helper.captured_caret,
            settings.paste_mode,
        ),
    ]
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
        cancel_hotkey_manager=FakeHotkeyManager(),
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

    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    monkeypatch.setattr(
        "stt_app.controller.create_transcriber", lambda _s, **kw: transcriber
    )

    controller = DictationController(
        settings_store=FakeSettingsStore(settings),
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=overlay,
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=focus_helper,
    )
    controller._executor = ImmediateExecutor()

    beep_calls = {"count": 0}
    monkeypatch.setattr(
        controller,
        "_play_abort_beep",
        lambda: beep_calls.__setitem__("count", beep_calls["count"] + 1),
    )

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

    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    monkeypatch.setattr(
        "stt_app.controller.create_transcriber", lambda _s, **kw: transcriber
    )

    controller = DictationController(
        settings_store=FakeSettingsStore(settings),
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
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


def test_stream_live_delta_keeps_first_partial_revisable():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    controller = DictationController(
        settings_store=FakeSettingsStore(AppSettings(hotkey=FALLBACK_HOTKEY)),
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=FakeOverlay(),
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
    )

    delta, committed = controller._compute_stream_live_delta("", "", "hello world")
    assert delta == "hello world"
    assert committed == ""

    delta, committed = controller._compute_stream_live_delta(
        "", "hello world", "hello world now"
    )
    assert delta == "hello world now"
    assert committed == ""

    delta, committed = controller._compute_stream_live_delta(
        "",
        "hello world now",
        "hello world now again",
    )
    assert delta == "world now again"
    assert committed == "hello"

    controller.shutdown()
    _ = app


def test_stream_live_delta_extends_locked_prefix_only_when_prefix_stays_consistent():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    controller = DictationController(
        settings_store=FakeSettingsStore(AppSettings(hotkey=FALLBACK_HOTKEY)),
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=FakeOverlay(),
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
    )

    delta, committed = controller._compute_stream_live_delta(
        "hello",
        "hello there foo bar",
        "hello there foo bar baz",
    )
    assert delta == "foo bar baz"
    assert committed == "hello there"

    delta2, committed2 = controller._compute_stream_live_delta(
        committed,
        "hello there foo bar baz",
        "hello there foo bar baz qux",
    )
    assert delta2 == "bar baz qux"
    assert committed2 == "hello there foo"

    controller.shutdown()
    _ = app


def test_stream_finalize_tail_uses_last_partial_when_final_diverges():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    controller = DictationController(
        settings_store=FakeSettingsStore(AppSettings(hotkey=FALLBACK_HOTKEY)),
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
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
        cancel_hotkey_manager=FakeHotkeyManager(),
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

    assert inserter.calls[0] == ("hello world", focus_helper.captured_caret, settings.paste_mode)
    replace_calls = [call for call in inserter.calls if call[0] == "replace"]
    assert replace_calls
    assert any(call[1] == "hello world" and call[2] == "hello world this" for call in replace_calls)
    assert any(call[1] == "hello world this" and call[2] == "hello there this is" for call in replace_calls)
    assert any(call[1] == " this is" and call[2] == " this is working" for call in replace_calls)
    assert any(call[1] == " is working" and call[2] == " is working now" for call in replace_calls)
    assert overlay.states[-1][0] == "Listening"
    assert controller._stream_live_text == "hello there this is working now"

    controller.shutdown()
    _ = app


def test_streaming_partial_revision_can_shrink_live_tail():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        mode="streaming",
        keep_transcript_in_clipboard=False,
    )
    inserter = FakeTextInserter()
    focus_helper = FakeWindowFocusHelper()
    controller = DictationController(
        settings_store=FakeSettingsStore(settings),
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=FakeOverlay(),
        text_inserter=inserter,
        logger=logging.getLogger("test.controller"),
        window_focus_helper=focus_helper,
    )
    controller._streaming_recording = True
    controller._audio_capture = object()
    controller._target_window_handle = focus_helper.captured
    controller._target_focus_signature = focus_helper.capture_target_signature()

    controller._on_transcription_partial("hello world this")
    controller._on_transcription_partial("hello world")

    assert inserter.calls == [
        ("hello world this", focus_helper.captured_caret, settings.paste_mode),
        (
            "replace",
            "hello world this",
            "hello world",
            focus_helper.captured_caret,
            settings.paste_mode,
        ),
    ]
    assert controller._stream_live_text == "hello world"

    controller.shutdown()
    _ = app


def test_streaming_finalize_replaces_live_tail_without_copying_revision(monkeypatch):
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
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=overlay,
        text_inserter=inserter,
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
    )
    fake_clipboard = FakeClipboard()
    monkeypatch.setattr(QtGui.QGuiApplication, "clipboard", lambda: fake_clipboard)

    controller._active_session_mode = "streaming"
    controller._stream_committed_text = "hello"
    controller._stream_live_text = "hello world now"
    controller._stream_last_partial_text = "hello world now"
    controller._target_window_handle = 555
    controller._target_focus_signature = (555, 556, 557)
    controller._on_transcription_ready("hello there now")

    assert fake_clipboard.text() == ""
    assert inserter.calls[-1] == (
        "replace",
        " world now",
        " there now",
        557,
        settings.paste_mode,
    )
    assert overlay.states[-1][0] == "Done"

    controller.shutdown()
    _ = app


def test_streaming_finalize_can_remove_live_tail(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        keep_transcript_in_clipboard=False,
    )
    inserter = FakeTextInserter()
    controller = DictationController(
        settings_store=FakeSettingsStore(settings),
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=FakeOverlay(),
        text_inserter=inserter,
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
    )
    fake_clipboard = FakeClipboard()
    monkeypatch.setattr(QtGui.QGuiApplication, "clipboard", lambda: fake_clipboard)

    controller._active_session_mode = "streaming"
    controller._stream_committed_text = "hello world"
    controller._stream_live_text = "hello world extra"
    controller._stream_last_partial_text = "hello world extra"
    controller._target_window_handle = 555
    controller._target_focus_signature = (555, 556, 557)
    controller._on_transcription_ready("hello world")

    assert fake_clipboard.text() == ""
    assert inserter.calls[-1] == (
        "replace",
        " extra",
        "",
        557,
        settings.paste_mode,
    )

    controller.shutdown()
    _ = app


# ---------------------------------------------------------------------------
# Model preloading tests
# ---------------------------------------------------------------------------


def test_controller_initialize_triggers_preload_for_local_engine():
    """When engine is local, initialize() should submit preload worker."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(engine="local", hotkey=FALLBACK_HOTKEY)
    overlay = FakeOverlay()
    controller = DictationController(
        settings_store=FakeSettingsStore(settings),
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=overlay,
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
    )
    controller._executor = ImmediateExecutor()
    controller._preload_executor = ImmediateExecutor()

    # Mock out the preload worker to verify it gets called.
    preload_called = []

    def mock_preload():
        preload_called.append(True)
        # Emit success signal directly.
        controller.model_preload_done.emit(True, "Model loaded: small")

    controller._preload_model_worker = mock_preload
    controller.initialize()

    assert len(preload_called) == 1
    controller.shutdown()
    _ = app


def test_controller_initialize_skips_preload_for_remote_engine():
    """When engine is remote (e.g. assemblyai), no preload should happen."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(engine="assemblyai", hotkey=FALLBACK_HOTKEY)
    overlay = FakeOverlay()
    controller = DictationController(
        settings_store=FakeSettingsStore(settings),
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=overlay,
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
    )
    controller._preload_executor = ImmediateExecutor()

    preload_called = []
    controller._preload_model_worker = lambda: preload_called.append(True)
    controller.initialize()

    assert len(preload_called) == 0
    # Should show idle (or error from hotkey) but not "Loading model..."
    assert any(s[0] in ("Idle", "Error") for s in overlay.states)
    controller.shutdown()
    _ = app


def test_controller_initialize_local_uses_preload_executor_only():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(engine="local", hotkey=FALLBACK_HOTKEY)
    overlay = FakeOverlay()
    controller = DictationController(
        settings_store=FakeSettingsStore(settings),
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=overlay,
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
    )
    controller._executor = FailSubmitExecutor()
    controller._preload_executor = ImmediateExecutor()

    preload_called = []

    def mock_preload():
        preload_called.append(True)
        controller.model_preload_done.emit(True, "Model loaded: small")

    controller._preload_model_worker = mock_preload
    controller.initialize()

    assert preload_called == [True]
    controller.shutdown()
    _ = app


def test_controller_preload_fallback_on_failure():
    """Preload failure should trigger fallback to available cached model."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(engine="local", model_size="medium", hotkey=FALLBACK_HOTKEY)
    overlay = FakeOverlay()
    controller = DictationController(
        settings_store=FakeSettingsStore(settings),
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=overlay,
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
    )

    # Test the on_model_preload_done handler directly.
    controller._hotkey_registration_ok = (
        True  # Simulate successful hotkey registration.
    )
    controller._on_model_preload_done(True, "Model loaded: small")
    assert overlay.states[-1][0] != "Error"

    controller._on_model_preload_done(False, "No models found")
    assert overlay.states[-1][0] == "Error"

    controller._on_model_preload_done(True, "Fallback: using 'tiny'")
    assert overlay.states[-1][0] == "Error"  # Fallback still shows warning

    controller.shutdown()
    _ = app


def test_preload_worker_persists_fallback_model(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(engine="local", model_size="medium", hotkey=FALLBACK_HOTKEY)
    store = FakeSettingsStore(settings)
    controller = DictationController(
        settings_store=store,
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=FakeOverlay(),
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
    )

    class DummyLocalTranscriber:
        def __init__(self, should_fail: bool) -> None:
            self.should_fail = should_fail

        def preload_model(self):
            if self.should_fail:
                raise RuntimeError("load failed")

    mediums = DummyLocalTranscriber(should_fail=True)
    tiny = DummyLocalTranscriber(should_fail=False)

    monkeypatch.setattr(
        "stt_app.transcriber.local_faster_whisper.LocalFasterWhisperTranscriber",
        DummyLocalTranscriber,
    )
    monkeypatch.setattr(
        "stt_app.transcriber.local_faster_whisper.find_cached_models",
        lambda _model_dir="": ["tiny"],
    )

    def fake_get_or_create(s: AppSettings):
        if s.model_size == "medium":
            return mediums
        if s.model_size == "tiny":
            return tiny
        raise AssertionError("unexpected model size")

    controller._get_or_create_transcriber = fake_get_or_create  # type: ignore[method-assign]
    emitted = []
    controller.model_preload_done.connect(lambda ok, msg: emitted.append((ok, msg)))

    controller._preload_model_worker()

    assert emitted
    assert emitted[-1][0] is True
    assert "Fallback" in emitted[-1][1]
    assert controller.settings.model_size == "tiny"
    assert store.saved is not None
    assert store.saved.model_size == "tiny"

    controller.shutdown()
    _ = app


def test_select_cached_fallback_model_prefers_closest_smaller():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    controller = DictationController(
        settings_store=FakeSettingsStore(AppSettings(hotkey=FALLBACK_HOTKEY)),
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=FakeOverlay(),
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
    )

    result = controller._select_cached_fallback_model(
        "large-v3-turbo", ["tiny", "small", "medium", "large-v3"]
    )

    # large-v3-turbo is 809 MB, so "small" (484 MB) is the closest smaller.
    # "medium" (1400 MB) is actually bigger than the turbo variant.
    assert result == "small"
    controller.shutdown()
    _ = app


def test_select_cached_fallback_model_uses_best_available_when_no_smaller():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    controller = DictationController(
        settings_store=FakeSettingsStore(AppSettings(hotkey=FALLBACK_HOTKEY)),
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=FakeOverlay(),
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
    )

    result = controller._select_cached_fallback_model("tiny", ["base", "small"])

    assert result == "small"
    controller.shutdown()
    _ = app


# ---------------------------------------------------------------------------
# on_settings_changed tests
# ---------------------------------------------------------------------------


def test_on_settings_changed_preloads_for_local_engine():
    """on_settings_changed() should trigger preload when switching to local."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(engine="local", hotkey=FALLBACK_HOTKEY)
    store = FakeSettingsStore(settings)
    overlay = FakeOverlay()
    controller = DictationController(
        settings_store=store,
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=overlay,
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
    )
    controller._preload_executor = ImmediateExecutor()

    preload_called = []

    def mock_preload():
        preload_called.append(True)
        controller.model_preload_done.emit(True, "Model loaded: small")

    controller._preload_model_worker = mock_preload
    controller.on_settings_changed()

    assert len(preload_called) == 1
    # Should have set "Processing" before preloading
    assert any(s[0] == "Processing" for s in overlay.states)
    controller.shutdown()
    _ = app


def test_on_settings_changed_skips_preload_for_remote_engine():
    """on_settings_changed() should show idle for remote engines."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(engine="groq", hotkey=FALLBACK_HOTKEY)
    store = FakeSettingsStore(settings)
    overlay = FakeOverlay()
    controller = DictationController(
        settings_store=store,
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=overlay,
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
    )
    controller._preload_executor = ImmediateExecutor()

    preload_called = []
    controller._preload_model_worker = lambda: preload_called.append(True)
    controller.on_settings_changed()

    assert len(preload_called) == 0
    # Should show idle (or error from hotkey fallback) — NOT "Processing"
    last_state = overlay.states[-1][0]
    assert last_state in ("Idle", "Error")
    controller.shutdown()
    _ = app
