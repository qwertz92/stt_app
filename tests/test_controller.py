import concurrent.futures
import logging
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import (
    FailSubmitExecutor,
    FakeCapture,
    FakeHotkeyManager,
    FakeHotkeyManagerAllFail,
    FakeLastRecordingStore,
    FakeOverlay,
    FakeSettingsStore,
    FakeStreamingTranscriber,
    FakeTextInserter,
    FakeWindowFocusHelper,
    ImmediateExecutor,
    make_controller,
)
from PySide6 import QtGui, QtWidgets

import stt_app.controller as controller_module
from stt_app.config import (
    DEFAULT_ENGINE,
    DEFAULT_HOTKEY,
    FALLBACK_HOTKEY,
    OVERLAY_ERROR_ACTION_INSERT,
    VALID_ENGINES,
)
from stt_app.controller import DictationController
from stt_app.settings_store import AppSettings
from stt_app.text_inserter import TextInsertionError
from stt_app.transcript_history import TranscriptHistoryStore


class DeferredExecutor:
    def __init__(self):
        self.calls = []

    def submit(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))
        return

    def shutdown(self, wait=False, cancel_futures=False):
        pass


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

    # The user's choice must survive. Another program holding the hotkey is
    # temporary; persisting the fallback made it permanent, so once that
    # program closed the app had already forgotten what the user wanted.
    assert controller.settings.hotkey == DEFAULT_HOTKEY
    assert store.saved is None or store.saved.hotkey == DEFAULT_HOTKEY
    assert controller._active_hotkey == FALLBACK_HOTKEY
    assert any("used by another program" in detail for _s, detail in overlay.states)
    # The idle line must name the key that actually works, not the stored one.
    assert any(
        f"Hotkey: {FALLBACK_HOTKEY}" in detail for _s, detail in overlay.states
    )

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
    assert "in use by other programs" in detail

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


def test_consecutive_transcripts_in_same_control_do_not_receive_separator():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    inserter = FakeTextInserter()
    focus = FakeWindowFocusHelper()
    controller = DictationController(
        settings_store=FakeSettingsStore(AppSettings()),
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=FakeOverlay(),
        text_inserter=inserter,
        logger=logging.getLogger("test.controller"),
        window_focus_helper=focus,
    )
    signature = focus.capture_target_signature()

    assert controller._insert_text_at_target(
        "first transcript",
        restore_focus=False,
        target_handle=focus.captured,
        target_signature=signature,
    )
    assert controller._insert_text_at_target(
        "second transcript",
        restore_focus=False,
        target_handle=focus.captured,
        target_signature=signature,
    )

    assert [call[0] for call in inserter.calls] == [
        "first transcript",
        "second transcript",
    ]
    controller.shutdown()
    _ = app


def test_separate_transcript_pastes_never_depend_on_target_control():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    inserter = FakeTextInserter()
    focus = FakeWindowFocusHelper()
    controller = DictationController(
        settings_store=FakeSettingsStore(AppSettings()),
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=FakeOverlay(),
        text_inserter=inserter,
        logger=logging.getLogger("test.controller"),
        window_focus_helper=focus,
    )

    assert controller._insert_text_at_target(
        "first",
        restore_focus=False,
        target_handle=100,
        target_signature=(100, 101, 102),
    )
    assert controller._insert_text_at_target(
        "second",
        restore_focus=False,
        target_handle=100,
        target_signature=(100, 101, 103),
    )

    assert [call[0] for call in inserter.calls] == ["first", "second"]
    controller.shutdown()
    _ = app


def test_controller_does_not_paste_when_target_focus_restore_fails(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, keep_transcript_in_clipboard=False)
    overlay = FakeOverlay()
    inserter = FakeTextInserter()
    focus_helper = FakeWindowFocusHelper()
    focus_helper.restore_target_window = lambda _hwnd: False
    clipboard = FakeClipboard()
    monkeypatch.setattr(QtGui.QGuiApplication, "clipboard", lambda: clipboard)
    controller = DictationController(
        settings_store=FakeSettingsStore(settings),
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=overlay,
        text_inserter=inserter,
        logger=logging.getLogger("test.controller"),
        window_focus_helper=focus_helper,
    )
    controller._target_window_handle = 555

    controller._on_transcription_ready("sensitive transcript")

    assert inserter.calls == []
    assert clipboard.text() == "sensitive transcript"
    assert overlay.states[-1][0] == "Error"
    assert "could not be restored" in overlay.states[-1][1]
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


def test_controller_preserves_clipboard_on_contention_error(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, keep_transcript_in_clipboard=False)
    overlay = FakeOverlay()

    class ContendedInserter:
        def insert_text_with_options(
            self,
            text,
            target_hwnd=None,
            paste_mode="auto",
            restore_clipboard=True,
        ):
            raise TextInsertionError(
                "Clipboard changed during paste.",
                allow_clipboard_fallback=False,
            )

    controller = DictationController(
        settings_store=FakeSettingsStore(settings),
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=overlay,
        text_inserter=ContendedInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
    )
    fake_clipboard = FakeClipboard()
    fake_clipboard.setText("user copy")
    monkeypatch.setattr(QtGui.QGuiApplication, "clipboard", lambda: fake_clipboard)

    controller._target_window_handle = 555
    controller._on_transcription_ready("do not overwrite")

    assert fake_clipboard.text() == "user copy"
    assert overlay.states[-1][0] == "Error"
    assert "current clipboard left untouched" in overlay.states[-1][1]

    controller.shutdown()
    _ = app


def test_failed_insert_shows_the_transcript_and_copies_only_it(monkeypatch):
    """The transcript must be readable while the insertion error is shown."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, keep_transcript_in_clipboard=False)
    overlay = FakeOverlay()

    class FailingInserter:
        def insert_text_with_options(
            self,
            text,
            target_hwnd=None,
            paste_mode="auto",
            restore_clipboard=True,
        ):
            raise TextInsertionError("Target window did not accept the paste.")

    controller = DictationController(
        settings_store=FakeSettingsStore(settings),
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=overlay,
        text_inserter=FailingInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
    )
    monkeypatch.setattr(QtGui.QGuiApplication, "clipboard", lambda: FakeClipboard())

    controller._target_window_handle = 555
    controller._on_transcription_ready("the transcribed sentence")

    state, detail = overlay.states[-1]
    assert state == "Error"
    assert "Target window did not accept the paste." in detail
    assert "the transcribed sentence" in detail
    # Copy must yield the transcript, not the error message around it.
    assert overlay.state_kwargs[-1]["copy_text"] == "the transcribed sentence"
    # Retry would re-transcribe; the transcription succeeded, so the overlay
    # must offer inserting the transcript again instead.
    assert overlay.state_kwargs[-1]["error_action"] == OVERLAY_ERROR_ACTION_INSERT

    controller.shutdown()
    _ = app


def test_controller_records_history_when_insert_fails(monkeypatch, tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, keep_transcript_in_clipboard=False)
    history_store = TranscriptHistoryStore(tmp_path / "history.json")
    controller = DictationController(
        settings_store=FakeSettingsStore(settings),
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=FakeOverlay(),
        text_inserter=FakeTextInserter(should_fail=True),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
        history_store=history_store,
    )
    fake_clipboard = FakeClipboard()
    monkeypatch.setattr(QtGui.QGuiApplication, "clipboard", lambda: fake_clipboard)

    controller._last_transcribe_settings = settings
    controller._on_transcription_ready("saved despite paste failure")

    entries = history_store.load()
    assert [entry.text for entry in entries] == ["saved despite paste failure"]
    assert entries[0].model == settings.model_size
    assert controller._last_transcribe_settings is None

    controller.shutdown()
    _ = app


def test_controller_history_uses_transcription_settings_snapshot(tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    current_settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        model_size="tiny",
        keep_transcript_in_clipboard=False,
    )
    transcribe_settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        model_size="base",
        keep_transcript_in_clipboard=False,
    )
    history_store = TranscriptHistoryStore(tmp_path / "history.json")
    controller = DictationController(
        settings_store=FakeSettingsStore(current_settings),
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=FakeOverlay(),
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
        history_store=history_store,
    )

    controller._last_transcribe_settings = transcribe_settings
    controller._on_transcription_ready("model snapshot")

    entries = history_store.load()
    assert entries[0].model == "base"

    controller.shutdown()
    _ = app


def test_stream_finalize_keeps_settings_snapshot_for_queued_result(tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    current_settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        mode="streaming",
        model_size="tiny",
        keep_transcript_in_clipboard=False,
    )
    stream_settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        mode="streaming",
        model_size="base",
        keep_transcript_in_clipboard=False,
    )
    history_store = TranscriptHistoryStore(tmp_path / "history.json")
    last_recording_store = FakeLastRecordingStore()
    controller = DictationController(
        settings_store=FakeSettingsStore(current_settings),
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=FakeOverlay(),
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
        history_store=history_store,
        last_recording_store=last_recording_store,
    )
    controller._executor = DeferredExecutor()
    controller._active_session_mode = "streaming"
    controller._active_stream_settings = stream_settings

    controller._submit_stream_finalize()
    request_token = controller._active_request_token
    controller._active_stream_settings = None
    controller._on_transcription_ready(
        "stream settings snapshot", request_token=request_token
    )

    entries = history_store.load()
    assert entries[0].model == "base"
    assert last_recording_store.transcribing == [("local", "base", "streaming")]

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


def test_controller_reveals_overlay_briefly_after_successful_result():
    from stt_app.config import OVERLAY_RESULT_REVEAL_MS

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = FakeOverlay()
    controller = DictationController(
        settings_store=FakeSettingsStore(AppSettings(hotkey=FALLBACK_HOTKEY)),
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=overlay,
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
    )
    controller._target_window_handle = 123

    controller._on_transcription_ready("hello world")

    assert overlay.states[-1][0] == "Done"
    assert overlay.reveal_durations[-1:] == [OVERLAY_RESULT_REVEAL_MS]

    controller.shutdown()
    _ = app


def test_controller_reveals_overlay_longer_when_insertion_fails(monkeypatch):
    from stt_app.config import OVERLAY_ERROR_REVEAL_MS

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = FakeOverlay()
    controller = DictationController(
        settings_store=FakeSettingsStore(AppSettings(hotkey=FALLBACK_HOTKEY)),
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=overlay,
        text_inserter=FakeTextInserter(should_fail=True),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
    )
    controller._target_window_handle = 123
    fake_clipboard = FakeClipboard()
    monkeypatch.setattr(QtGui.QGuiApplication, "clipboard", lambda: fake_clipboard)

    controller._on_transcription_ready("insertion will fail")

    # Insertion failed -> overlay shows an error and is revealed for longer so
    # the transcript can still be copied from it.
    assert overlay.states[-1][0] == "Error"
    assert overlay.reveal_durations[-1:] == [OVERLAY_ERROR_REVEAL_MS]
    assert fake_clipboard.text() == "insertion will fail"

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


def test_controller_edits_last_transcript_history_entry(monkeypatch, tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(hotkey=FALLBACK_HOTKEY, keep_transcript_in_clipboard=False)
    history_store = TranscriptHistoryStore(tmp_path / "history.json")
    overlay = FakeOverlay()
    controller = DictationController(
        settings_store=FakeSettingsStore(settings),
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=overlay,
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
        history_store=history_store,
    )
    monkeypatch.setattr(
        "stt_app.transcript_edit_dialog.TranscriptEditDialog.get_text",
        lambda _parent, _text: "corrected text",
    )

    controller._on_transcription_ready("original text")
    edited = controller.edit_last_transcript()

    assert edited is True
    assert controller._last_transcript == "corrected text"
    assert [entry.text for entry in history_store.load()] == ["corrected text"]
    assert overlay.states[-1] == ("Done", "corrected text")
    controller.shutdown()
    _ = app


def test_background_and_import_history_do_not_replace_edit_target(
    monkeypatch,
    tmp_path,
):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        keep_transcript_in_clipboard=False,
    )
    history_store = TranscriptHistoryStore(tmp_path / "history.json")
    controller = DictationController(
        settings_store=FakeSettingsStore(settings),
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=FakeOverlay(),
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
        history_store=history_store,
    )
    controller._last_transcript = "foreground transcript"
    foreground_entry = controller._append_transcript_history(
        "foreground transcript",
        settings,
        "batch",
    )
    assert foreground_entry is not None

    background_job = controller._register_transcription_job(77, settings, "batch")
    background_job.background_delivery = "history"
    controller._handle_background_transcription_ready(
        background_job,
        "background transcript",
    )

    class _FakeTranscriber:
        def transcribe_batch(self, _source):
            return "import transcript"

    monkeypatch.setattr(
        "stt_app.controller.create_transcriber",
        lambda _settings, **_kwargs: _FakeTranscriber(),
    )
    audio_path = tmp_path / "external.wav"
    audio_path.write_bytes(b"RIFF")
    assert controller.transcribe_audio_file(str(audio_path)) == (
        True,
        "import transcript",
    )

    assert controller._last_transcript == "foreground transcript"
    assert controller._last_history_entry == foreground_entry
    monkeypatch.setattr(
        "stt_app.transcript_edit_dialog.TranscriptEditDialog.get_text",
        lambda _parent, _text: "corrected foreground",
    )
    assert controller.edit_last_transcript() is True
    history_entries = history_store.load()
    assert [entry.text for entry in history_entries] == [
        "corrected foreground",
        "background transcript",
        "import transcript",
    ]
    assert history_entries[-1].source_audio_path == str(audio_path.resolve())
    controller.shutdown()
    _ = app


def test_controller_streaming_mode_uses_transcriber_streaming(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        # Explicit: the default local model is the batch-only Parakeet.
        model_size="small",
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
        ("stream final", focus_helper.captured_caret, settings.paste_mode),
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
    """Pins the opt-in hard abort.

    The default no longer ends the session on a focus change -- it
    suspends insertion and delivers the rest at stop -- so this has to
    ask for the old behaviour explicitly.
    """
    monkeypatch.setattr(
        "stt_app.controller.STREAMING_ABORT_ON_FOCUS_CHANGE", True
    )
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        # Explicit: the default local model is the batch-only Parakeet.
        model_size="small",
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
    """Pins the opt-in hard abort.

    The default no longer ends the session on a focus change -- it
    suspends insertion and delivers the rest at stop -- so this has to
    ask for the old behaviour explicitly.
    """
    monkeypatch.setattr(
        "stt_app.controller.STREAMING_ABORT_ON_FOCUS_CHANGE", True
    )
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        # Explicit: the default local model is the batch-only Parakeet.
        model_size="small",
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


def test_streaming_partial_insertions_continue_after_revisions():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        mode="streaming",
        # A streaming-capable model explicitly. These tests call the partial
        # handlers directly, so they never reach the controller's refusal --
        # but a settings object describing a combination the app rejects is
        # not what any of them means to set up.
        model_size="small",
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

    assert [call for call in inserter.calls if call[0] == "replace"] == []
    assert inserter.calls == [
        ("hello there", focus_helper.captured_caret, settings.paste_mode),
        (" this", focus_helper.captured_caret, settings.paste_mode),
    ]
    assert overlay.states[-1][0] == "Listening"
    assert controller._stream_committed_text == "hello there this"
    assert controller._stream_live_text == "hello there this is working now"

    controller.shutdown()
    _ = app


def test_streaming_partial_revision_can_shrink_live_tail():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        mode="streaming",
        # A streaming-capable model explicitly. These tests call the partial
        # handlers directly, so they never reach the controller's refusal --
        # but a settings object describing a combination the app rejects is
        # not what any of them means to set up.
        model_size="small",
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

    assert inserter.calls == []
    assert controller._stream_live_text == "hello world"

    controller.shutdown()
    _ = app


def test_streaming_partial_insertions_handle_rolling_local_windows():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        mode="streaming",
        # A streaming-capable model explicitly. These tests call the partial
        # handlers directly, so they never reach the controller's refusal --
        # but a settings object describing a combination the app rejects is
        # not what any of them means to set up.
        model_size="small",
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

    controller._on_transcription_partial("hello world this is")
    controller._on_transcription_partial("world this is working now")
    controller._on_transcription_partial("this is working now today")

    assert inserter.calls == [
        ("hello world", focus_helper.captured_caret, settings.paste_mode),
        (" this is", focus_helper.captured_caret, settings.paste_mode),
    ]
    assert controller._stream_committed_text == "hello world this is"
    assert controller._stream_live_text == "hello world this is working now today"
    assert controller._overlay.states[-1] == (
        "Listening",
        "Live: hello world this is working now today",
    )

    controller.shutdown()
    _ = app


def test_streaming_finalize_appends_without_copying_revision(monkeypatch):
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
    assert [call for call in inserter.calls if call[0] == "replace"] == []
    assert inserter.calls[-1] == (" there now", 557, settings.paste_mode)
    assert overlay.states[-1][0] == "Done"

    controller.shutdown()
    _ = app


def test_streaming_finalize_never_removes_live_tail(monkeypatch):
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
    assert inserter.calls == []

    controller.shutdown()
    _ = app


def test_streaming_finalize_inserts_final_text_when_live_insertion_disabled(
    monkeypatch,
):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    monkeypatch.setattr(controller_module, "STREAMING_LIVE_INSERT_ENABLED", False)
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
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
    fake_clipboard = FakeClipboard()
    monkeypatch.setattr(QtGui.QGuiApplication, "clipboard", lambda: fake_clipboard)

    controller._active_session_mode = "streaming"
    controller._target_window_handle = focus_helper.captured
    controller._target_focus_signature = focus_helper.capture_target_signature()
    controller._on_transcription_partial("ignored live text")
    controller._on_transcription_ready("final transcript")

    assert fake_clipboard.text() == ""
    assert inserter.calls[-1] == (
        "final transcript",
        focus_helper.captured_caret,
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

    def mock_preload(_settings, generation, _key):
        preload_called.append(True)
        # Emit success signal directly.
        controller.model_preload_done.emit(generation, True, "Model loaded: small")

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
    controller._preload_model_worker = lambda *_args: preload_called.append(True)
    controller.initialize()

    assert len(preload_called) == 0
    # Should show idle (or error from hotkey) but not "Loading model..."
    assert any(s[0] in ("Idle", "Error") for s in overlay.states)
    controller.shutdown()
    _ = app


def test_controller_initialize_skips_preload_for_webgpu_local_model():
    """Without keep-loaded there is nothing to keep, so nothing is preloaded."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(
        engine="local",
        model_size="cohere-transcribe-03-2026",
        hotkey=FALLBACK_HOTKEY,
        keep_onnx_model_loaded=False,
    )
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
    controller._preload_model_worker = lambda *_args: preload_called.append(True)
    controller.initialize()

    assert preload_called == []
    assert any(state == "Idle" for state, _detail in overlay.states)
    controller.shutdown()
    _ = app


def test_controller_initialize_preloads_nemotron_for_prompt_streaming_start():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(
        engine="local",
        model_size="nemotron-3.5-asr-streaming-0.6b-int4",
        hotkey=FALLBACK_HOTKEY,
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
    controller._preload_executor = ImmediateExecutor()

    preload_called = []
    controller._preload_model_worker = lambda *_args: preload_called.append(True)
    controller.initialize()

    assert preload_called == [True]
    controller.shutdown()
    _ = app


def test_controller_initialize_preloads_webgpu_when_keep_loaded_enabled():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(
        engine="local",
        model_size="cohere-transcribe-03-2026",
        hotkey=FALLBACK_HOTKEY,
        keep_onnx_model_loaded=True,
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
    controller._preload_executor = ImmediateExecutor()

    preload_called = []
    controller._preload_model_worker = lambda *_args: preload_called.append(True)
    controller.initialize()

    assert preload_called == [True]
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

    def mock_preload(_settings, generation, _key):
        preload_called.append(True)
        controller.model_preload_done.emit(generation, True, "Model loaded: small")

    controller._preload_model_worker = mock_preload
    controller.initialize()

    assert preload_called == [True]
    controller.shutdown()
    _ = app


def test_controller_preload_failure_is_reported_without_fallback():
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
    controller._on_model_preload_done(0, True, "Model loaded: small")
    assert overlay.states[-1][0] != "Error"

    controller._on_model_preload_done(0, False, "No models found")
    assert overlay.states[-1][0] == "Error"

    controller.shutdown()
    _ = app


def test_keep_loaded_onnx_model_is_preloaded():
    """With keep-loaded on, the ONNX runtime is warmed like every other local
    model instead of being loaded again for each dictation."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(
        engine="local",
        model_size="cohere-transcribe-03-2026",
        hotkey=FALLBACK_HOTKEY,
        keep_onnx_model_loaded=True,
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
    controller._preload_executor = ImmediateExecutor()
    preload_called: list[bool] = []
    controller._preload_model_worker = lambda *_args: preload_called.append(True)

    controller.initialize()

    assert preload_called == [True]
    controller.shutdown()
    _ = app


def test_webgpu_batch_transcriber_is_closed_after_worker(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(
        engine="local",
        model_size="cohere-transcribe-03-2026",
        hotkey=FALLBACK_HOTKEY,
        keep_onnx_model_loaded=False,
    )

    class FakeWebGpuTranscriber:
        def __init__(self) -> None:
            self.closed = False

        def transcribe_batch(self, _audio_source):
            return "hello"

        def close(self):
            self.closed = True

    transcriber = FakeWebGpuTranscriber()
    monkeypatch.setattr(
        "stt_app.controller.create_transcriber",
        lambda _settings, **_kwargs: transcriber,
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
    emitted = []
    controller.transcription_ready.connect(
        lambda token, text: emitted.append((token, text))
    )

    controller._transcribe_worker(7, b"RIFF", settings)

    assert emitted == [(7, "hello")]
    assert transcriber.closed is True
    assert controller._transcriber_cache is None
    controller.shutdown()
    _ = app


def test_webgpu_batch_transcriber_is_cached_when_keep_loaded(monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(
        engine="local",
        model_size="cohere-transcribe-03-2026",
        hotkey=FALLBACK_HOTKEY,
        keep_onnx_model_loaded=True,
    )

    class FakeWebGpuTranscriber:
        def __init__(self) -> None:
            self.closed = False
            self.calls = 0

        def transcribe_batch(self, _audio_source):
            self.calls += 1
            return "hello"

        def close(self):
            self.closed = True

    transcriber = FakeWebGpuTranscriber()
    monkeypatch.setattr(
        "stt_app.controller.create_transcriber",
        lambda _settings, **_kwargs: transcriber,
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
    emitted = []
    controller.transcription_ready.connect(
        lambda token, text: emitted.append((token, text))
    )

    controller._transcribe_worker(7, b"RIFF", settings)

    assert emitted == [(7, "hello")]
    assert transcriber.closed is False
    assert controller._transcriber_cache is transcriber
    controller.shutdown()
    assert transcriber.closed is True
    _ = app


def test_preload_worker_failure_never_changes_selected_model(monkeypatch):
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
    monkeypatch.setattr(
        "stt_app.transcriber.local_faster_whisper.LocalFasterWhisperTranscriber",
        DummyLocalTranscriber,
    )

    def fake_get_or_create(s: AppSettings):
        if s.model_size == "medium":
            return mediums
        raise AssertionError("unexpected model size")

    controller._get_or_create_transcriber = fake_get_or_create  # type: ignore[method-assign]
    emitted = []
    controller.model_preload_done.connect(
        lambda generation, ok, msg: emitted.append((generation, ok, msg))
    )

    key = controller._model_preload_key(settings)
    controller._preload_model_worker(settings, 1, key)

    assert emitted
    assert emitted[-1][1] is False
    assert "No fallback model was used" in emitted[-1][2]
    assert controller.settings.model_size == "medium"
    assert store.saved is None

    controller.shutdown()
    _ = app


def test_stale_preload_completion_does_not_override_active_progress():
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

    controller._preload_generation = 2
    controller._preload_target_model = "medium"

    controller._on_model_preload_done(1, False, "old model failed")

    assert controller._preload_target_model == "medium"
    assert controller._overlay.states == []
    controller.shutdown()
    _ = app


def test_preload_completion_does_not_overwrite_active_transcription_status():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = FakeOverlay()
    controller = DictationController(
        settings_store=FakeSettingsStore(AppSettings(hotkey=FALLBACK_HOTKEY)),
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=overlay,
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
    )
    controller._preload_generation = 3
    controller._active_request_token = 42
    overlay.set_state("Processing", "Transcribing audio...")

    controller._on_model_preload_done(3, True, "Model loaded: small")

    assert overlay.states[-1] == ("Processing", "Transcribing audio...")
    controller.shutdown()
    _ = app


def test_matching_preload_failure_blocks_selected_model_transcription():
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

    settings = controller.settings
    key = controller._model_preload_key(settings)
    controller._record_model_preload_result(
        key,
        1,
        "Selected model 'small' could not be loaded. No fallback model was used.",
    )

    assert "No fallback model" in controller._model_preload_failure(settings)
    controller.shutdown()
    _ = app


def test_batch_worker_waits_for_matching_preload_before_runtime_acquisition():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(engine="local", model_size="small", hotkey=FALLBACK_HOTKEY)
    controller = DictationController(
        settings_store=FakeSettingsStore(settings),
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=FakeOverlay(),
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
    )
    order = []

    class WaitingFuture:
        def done(self):
            return False

        def result(self, timeout=None):
            assert timeout == 0.1
            order.append("preload-finished")

    class Transcriber:
        def transcribe_batch(self, _wav_bytes):
            order.append("transcribed")
            return "selected model result"

    class Lease:
        transcriber = Transcriber()

        def release(self):
            order.append("released")

    key = controller._model_preload_key(settings)
    controller._preload_target_key = key
    controller._preload_future = WaitingFuture()

    def acquire(acquired_settings, *, allow_isolated=True):
        assert acquired_settings.model_size == "small"
        assert allow_isolated is True
        order.append("runtime-acquired")
        return Lease()

    controller._acquire_transcriber_runtime = acquire  # type: ignore[method-assign]
    emitted = []
    controller.transcription_ready.connect(
        lambda token, text: emitted.append((token, text))
    )

    controller._transcribe_worker(4, b"RIFF", settings)

    assert order == [
        "preload-finished",
        "runtime-acquired",
        "transcribed",
        "released",
    ]
    assert emitted == [(4, "selected model result")]
    controller.shutdown()
    _ = app


def test_batch_worker_cancels_while_waiting_without_acquiring_runtime():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(engine="local", model_size="small", hotkey=FALLBACK_HOTKEY)
    controller = DictationController(
        settings_store=FakeSettingsStore(settings),
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=FakeOverlay(),
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
    )
    job = controller._register_transcription_job(6, settings, "batch")

    class CancelingFuture:
        def done(self):
            return False

        def result(self, timeout=None):
            assert timeout == 0.1
            job.aborting = True
            raise concurrent.futures.TimeoutError()

    key = controller._model_preload_key(settings)
    controller._preload_target_key = key
    controller._preload_future = CancelingFuture()
    controller._acquire_transcriber_runtime = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a canceled wait must not acquire a runtime")
        )
    )
    canceled = []
    controller.transcription_canceled.connect(canceled.append)

    controller._transcribe_worker(6, b"RIFF", settings, job)

    assert canceled == [6]
    controller.shutdown()
    _ = app


def test_batch_worker_reports_preload_failure_without_creating_fallback():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(engine="local", model_size="small", hotkey=FALLBACK_HOTKEY)
    controller = DictationController(
        settings_store=FakeSettingsStore(settings),
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=FakeOverlay(),
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
    )
    key = controller._model_preload_key(settings)
    failure = "Selected model 'small' could not be loaded. No fallback model was used."
    controller._record_model_preload_result(key, 1, failure)
    controller._acquire_transcriber_runtime = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a failed selected model must not create a fallback")
        )
    )
    emitted = []
    controller.transcription_failed.connect(
        lambda token, message: emitted.append((token, message))
    )

    controller._transcribe_worker(5, b"RIFF", settings)

    assert emitted == [(5, failure)]
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

    def mock_preload(_settings, generation, _key):
        preload_called.append(True)
        controller.model_preload_done.emit(generation, True, "Model loaded: small")

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
    controller._preload_model_worker = lambda *_args: preload_called.append(True)
    controller.on_settings_changed()

    assert len(preload_called) == 0
    # Should show idle (or error from hotkey fallback) — NOT "Processing"
    last_state = overlay.states[-1][0]
    assert last_state in ("Idle", "Error")
    controller.shutdown()
    _ = app


def test_on_settings_changed_skips_preload_for_webgpu_local_model():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(
        engine="local",
        model_size="granite-4.0-1b-speech",
        hotkey=FALLBACK_HOTKEY,
        keep_onnx_model_loaded=False,
    )
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
    controller._preload_model_worker = lambda *_args: preload_called.append(True)
    controller.on_settings_changed()

    assert preload_called == []
    assert any(state == "Idle" for state, _detail in overlay.states)
    controller.shutdown()
    _ = app


def test_idle_status_never_overwrites_an_active_recording():
    """A delayed preload timer must not report Idle while dictation runs.

    The overlay showing "Idle" made it look as if nothing was being recorded;
    pressing the hotkey again to "start" then stopped the running capture.
    """
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    overlay = FakeOverlay()
    controller = DictationController(
        settings_store=FakeSettingsStore(AppSettings(hotkey=FALLBACK_HOTKEY)),
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=overlay,
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
    )
    controller._hotkey_registration_ok = True
    controller._cancel_hotkey_registration_ok = True
    controller._show_overlay_hotkey_registration_ok = True
    controller._repaste_hotkey_registration_ok = True

    controller.show_idle_status()
    assert overlay.states[-1][0] == "Idle"

    # A recording started after the timer was armed.
    controller._audio_capture = object()
    overlay.set_state("Listening", "Speak now.")

    controller.show_idle_status()

    assert overlay.states[-1][0] == "Listening"
    controller._audio_capture = None
    controller.shutdown()
    _ = app


def test_overlay_language_change_keeps_the_loaded_model():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(
        engine="local",
        model_size="small",
        language_mode="auto",
        hotkey=FALLBACK_HOTKEY,
    )
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
    preload_calls = []
    controller._start_local_model_preload = lambda: preload_calls.append(True)
    cached = object()
    controller._transcriber_cache = cached

    controller.set_language_mode("de")

    assert controller.settings.language_mode == "de"
    assert store.saved is not None
    assert store.saved.language_mode == "de"
    # The language is a per-request parameter, so switching it must neither
    # tear down the loaded runtime nor schedule a reload.
    assert controller._transcriber_cache is cached
    assert preload_calls == []
    assert overlay.language_options[-1][1] == "de"
    controller.shutdown()
    _ = app


def test_language_change_reuses_the_cached_transcriber(monkeypatch):
    """A language switch must apply to the cached runtime, not rebuild it."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    class FakeCachedTranscriber:
        def __init__(self, language_mode: str):
            self.language_mode = language_mode

        def set_language_mode(self, mode: str) -> None:
            self.language_mode = mode

    created: list[FakeCachedTranscriber] = []

    def fake_create(settings, secret_store=None):
        transcriber = FakeCachedTranscriber(settings.language_mode)
        created.append(transcriber)
        return transcriber

    monkeypatch.setattr(controller_module, "create_transcriber", fake_create)
    settings = AppSettings(
        engine="local",
        model_size="small",
        language_mode="auto",
        hotkey=FALLBACK_HOTKEY,
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
    controller._start_local_model_preload = lambda: None

    first = controller._get_or_create_transcriber(controller.settings)
    controller.set_language_mode("de")
    second = controller._get_or_create_transcriber(controller.settings)

    assert second is first
    assert len(created) == 1
    assert first.language_mode == "de"
    controller.shutdown()
    _ = app


def test_overlay_language_change_rejects_unsupported_cohere_auto():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(
        engine="local",
        model_size="cohere-transcribe-03-2026",
        language_mode="de",
        hotkey=FALLBACK_HOTKEY,
    )
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

    controller.set_language_mode("auto")

    assert controller.settings.language_mode == "de"
    assert store.saved is None
    assert overlay.language_options[-1] == (
        (
            "de",
            "en",
            "fr",
            "it",
            "es",
            "pt",
            "el",
            "nl",
            "pl",
            "ar",
            "vi",
            "zh",
            "ja",
            "ko",
        ),
        "de",
    )
    controller.shutdown()
    _ = app


def test_system_resume_closes_cached_webgpu_runtime():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(
        engine="local",
        model_size="cohere-transcribe-03-2026",
        hotkey=FALLBACK_HOTKEY,
        keep_onnx_model_loaded=True,
    )
    controller, _app = make_controller(
        settings_store=FakeSettingsStore(settings),
        logger=logging.getLogger("test.controller"),
    )

    class CachedWebGpuTranscriber:
        model_size = "cohere-transcribe-03-2026"
        runtime_device = "webgpu"

        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    cached = CachedWebGpuTranscriber()
    controller._transcriber_cache = cached
    controller._transcriber_cache_key = controller._transcriber_identity(settings)

    controller.handle_system_resume()

    assert cached.closed is True
    assert controller._transcriber_cache is None
    assert controller._transcriber_cache_key is None
    controller.shutdown()
    _ = app


def test_system_resume_reads_the_onnx_model_from_the_cache_key_by_name():
    """The cache key alone must be able to trigger the resume teardown.

    A runtime that does not expose ``model_size`` leaves the key as the only
    evidence that an ONNX/WebGPU model is loaded. The key is read by name:
    ``model_size`` sits at index 1 today, so a positional read happens to
    agree -- which is exactly why the field order is pinned separately below.
    """
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(
        engine="local",
        model_size="cohere-transcribe-03-2026",
        hotkey=FALLBACK_HOTKEY,
        keep_onnx_model_loaded=True,
    )
    controller, _app = make_controller(
        settings_store=FakeSettingsStore(settings),
        logger=logging.getLogger("test.controller"),
    )

    class OpaqueRuntime:
        """No ``model_size``/``runtime_device`` attributes at all."""

        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    cached = OpaqueRuntime()
    controller._transcriber_cache = cached
    key = controller._transcriber_identity(settings)
    assert key.model_size == "cohere-transcribe-03-2026"

    controller._transcriber_cache_key = key

    controller.handle_system_resume()

    assert cached.closed is True
    assert controller._transcriber_cache is None
    controller.shutdown()
    _ = app


# ---------------------------------------------------------------------------
# A settings save reloads the model only when the model actually changed
# ---------------------------------------------------------------------------


_RUNTIME_BASE_SETTINGS = AppSettings(
    engine="local",
    model_size="small",
    hotkey=FALLBACK_HOTKEY,
    vad_enabled=False,
    silence_gate_enabled=True,
    silence_gate_threshold=0.004,
    custom_vocabulary="",
    local_onnx_device="auto",
    streaming_full_final_transcript=False,
    model_dir="",
    keep_onnx_model_loaded=True,
)


def _controller_with_loaded_model(settings):
    """Controller that already holds a preloaded runtime for ``settings``."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    controller = DictationController(
        settings_store=FakeSettingsStore(settings),
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=FakeOverlay(),
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=FakeWindowFocusHelper(),
    )
    preloads: list[bool] = []
    controller._start_local_model_preload = lambda: preloads.append(True)
    closed: list[object] = []
    controller._close_cached_transcriber = closed.append
    cached = object()
    controller._transcriber_cache = cached
    controller._transcriber_cache_key = controller._transcriber_identity(settings)
    with controller._preload_result_lock:
        controller._preload_results[controller._model_preload_key(settings)] = (
            controller._preload_generation,
            None,
        )
    return controller, app, preloads, closed, cached


def _assert_reload_outcome(controller, reloads, *, preloads, closed, cached):
    """Assert what a save actually did to the loaded runtime.

    `reloads=True` means the save must have dropped the cached runtime;
    `False` means it must have left it alone. Only a *local* engine also
    preloads -- a remote provider has no model to load, so its runtime is
    rebuilt on the next request instead.
    """
    engine = controller._settings_store._settings.engine
    expected_preloads = [True] if reloads and engine == "local" else []
    if reloads:
        assert closed == [cached], "the changed runtime was not closed"
    else:
        assert closed == [], "an unchanged runtime was closed"
        assert controller._transcriber_cache is cached
        assert controller._pending_transcriber_cache_reset is False
    assert preloads == expected_preloads, (
        f"engine {engine!r} with reloads={reloads} produced {preloads}"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"overlay_opacity_percent": 55},
        {"completion_beep_enabled": True},
        {"history_max_items": 123},
        {"insert_target": "current_window"},
        {"language_mode": "de"},
        {"overlay_corner": "top-left"},
        {"keep_transcript_in_clipboard": True},
        # A remote provider's model/endpoint is not part of a *local* runtime,
        # so changing one must not close the loaded local model.
        {"groq_model": "whisper-large-v3"},
        {"azure_endpoint": "https://example.cognitiveservices.azure.com"},
        # The base settings select faster-whisper, which reads neither of
        # these -- they belong to the ONNX runtimes.
        {"local_onnx_device": "cpu"},
        {"keep_onnx_model_loaded": False},
    ],
    ids=lambda change: next(iter(change)),
)
def test_a_save_that_does_not_change_the_runtime_keeps_the_loaded_model(change):
    """Saving an unrelated setting must not close and reload the model.

    Every save used to reset the transcriber cache, so changing the overlay
    opacity or a beep tone threw away a multi-gigabyte local model and preloaded
    the identical one again.
    """
    settings = _RUNTIME_BASE_SETTINGS
    controller, app, preloads, closed, cached = _controller_with_loaded_model(settings)

    saved = replace(settings, **change)
    # Guard against a parameter that happens to repeat the default: it would
    # make this test pass without changing anything at all.
    assert saved != settings
    controller._settings_store._settings = saved
    controller.on_settings_changed()

    assert closed == []
    assert controller._transcriber_cache is cached
    assert controller._pending_transcriber_cache_reset is False
    assert preloads == []
    controller.shutdown()
    _ = app


@pytest.mark.parametrize(
    "change",
    [
        {"model_size": "medium"},
        {"engine": "groq"},
        {"custom_vocabulary": "Kubernetes, Nemotron"},
        {"silence_gate_enabled": False},
        {"silence_gate_threshold": 0.02},
        {"vad_enabled": True},
        {"model_dir": "D:/models"},
        {"streaming_full_final_transcript": True},
        {"offline_mode": True},
    ],
    ids=lambda change: next(iter(change)),
)
def test_a_save_that_changes_the_runtime_drops_the_loaded_model(change):
    """Anything a transcriber is constructed from must still invalidate it."""
    settings = _RUNTIME_BASE_SETTINGS
    controller, app, _preloads, closed, cached = _controller_with_loaded_model(settings)

    saved = replace(settings, **change)
    assert controller._transcriber_identity(
        saved
    ) != controller._transcriber_identity(settings)
    controller._settings_store._settings = saved
    controller.on_settings_changed()

    assert closed == [cached]
    assert controller._transcriber_cache is None
    controller.shutdown()
    _ = app


# Which settings each *local* runtime is actually built from. "local" is four
# different transcriber classes, so scoping the identity by engine alone still
# reloaded a 670 MB Parakeet model when a faster-whisper-only field changed.
_LOCAL_RUNTIME_FIELDS = [
    # (model_size, change, must_reload)
    ("small", {"custom_vocabulary": "Kubernetes"}, True),
    ("small", {"silence_gate_threshold": 0.02}, True),
    ("small", {"streaming_full_final_transcript": True}, True),
    ("small", {"vad_enabled": True}, True),
    ("small", {"local_onnx_device": "cpu"}, False),
    ("small", {"keep_onnx_model_loaded": False}, False),
    ("parakeet-tdt-0.6b-v3", {"offline_mode": True}, True),
    ("parakeet-tdt-0.6b-v3", {"model_dir": "D:/models"}, True),
    ("parakeet-tdt-0.6b-v3", {"custom_vocabulary": "Kubernetes"}, False),
    ("parakeet-tdt-0.6b-v3", {"vad_enabled": True}, False),
    ("parakeet-tdt-0.6b-v3", {"silence_gate_threshold": 0.02}, False),
    ("parakeet-tdt-0.6b-v3", {"local_onnx_device": "cpu"}, False),
    ("parakeet-tdt-0.6b-v3", {"keep_onnx_model_loaded": False}, False),
    ("nemotron-3.5-asr-streaming-0.6b-int4", {"vad_enabled": True}, True),
    ("nemotron-3.5-asr-streaming-0.6b-int4", {"local_onnx_device": "cpu"}, True),
    ("nemotron-3.5-asr-streaming-0.6b-int4", {"offline_mode": True}, True),
    ("nemotron-3.5-asr-streaming-0.6b-int4", {"model_dir": "D:/models"}, True),
    ("nemotron-3.5-asr-streaming-0.6b-int4", {"custom_vocabulary": "K8s"}, False),
    ("nemotron-3.5-asr-streaming-0.6b-int4", {"keep_onnx_model_loaded": False}, False),
    (
        "nemotron-3.5-asr-streaming-0.6b-int4",
        {"streaming_full_final_transcript": True},
        False,
    ),
    (
        "nemotron-3.5-asr-streaming-0.6b-int4",
        {"silence_gate_threshold": 0.02},
        False,
    ),
    ("nemotron-3.5-asr-streaming-0.6b-int4", {"silence_gate_enabled": False}, False),
    ("cohere-transcribe-03-2026", {"local_onnx_device": "cpu"}, True),
    ("cohere-transcribe-03-2026", {"keep_onnx_model_loaded": False}, True),
    ("cohere-transcribe-03-2026", {"offline_mode": True}, True),
    ("cohere-transcribe-03-2026", {"model_dir": "D:/models"}, True),
    ("cohere-transcribe-03-2026", {"custom_vocabulary": "Kubernetes"}, False),
    ("cohere-transcribe-03-2026", {"vad_enabled": True}, False),
    ("cohere-transcribe-03-2026", {"streaming_full_final_transcript": True}, False),
    ("cohere-transcribe-03-2026", {"silence_gate_threshold": 0.02}, False),
    ("cohere-transcribe-03-2026", {"silence_gate_enabled": False}, False),
    ("parakeet-tdt-0.6b-v3", {"streaming_full_final_transcript": True}, False),
    ("parakeet-tdt-0.6b-v3", {"silence_gate_enabled": False}, False),
]


@pytest.mark.parametrize(
    ("model_size", "change", "reloads"),
    _LOCAL_RUNTIME_FIELDS,
    ids=[
        f"{model}-{next(iter(change))}" for model, change, _ in _LOCAL_RUNTIME_FIELDS
    ],
)
def test_a_local_identity_reads_only_what_its_own_runtime_takes(
    model_size, change, reloads
):
    """`_create_local_transcriber` picks one of four classes, each with its own
    constructor arguments. The identity has to follow that split, or a setting
    one runtime never receives still costs the others a full reload."""
    settings = replace(_RUNTIME_BASE_SETTINGS, model_size=model_size)
    saved = replace(settings, **change)
    # Guard against a parameter that repeats the current value, which would
    # make the "no reload" half pass without testing anything.
    assert saved != settings

    controller, app, preloads, closed, cached = _controller_with_loaded_model(
        settings
    )
    controller._settings_store._settings = saved
    controller.on_settings_changed()

    # Asserted on the observable outcome rather than on the identity tuple:
    # an identity that changed but did not reach the cache reset, or a reset
    # that ran without a preload, would both pass a bare identity comparison.
    _assert_reload_outcome(
        controller, reloads, preloads=preloads, closed=closed, cached=cached
    )
    controller.shutdown()
    _ = app


@pytest.mark.parametrize(
    ("first", "second", "reloads"),
    [
        # ORT GenAI has no WebGPU provider, so all three GPU flavours resolve
        # to the same `("dml",)` and build a byte-identical runtime. The
        # General tab's own note tells the user that verbatim, so switching
        # between them is a realistic thing to do -- and it closed the loaded
        # 793 MB model and preloaded the identical one again.
        ("gpu", "dml", False),
        ("gpu", "webgpu", False),
        ("dml", "webgpu", False),
        # These really are different provider orders.
        ("auto", "cpu", True),
        ("gpu", "cpu", True),
    ],
    ids=lambda value: value if isinstance(value, str) else str(value),
)
def test_nemotron_reloads_only_when_the_resolved_provider_order_changes(
    first, second, reloads
):
    """The identity must hold what the constructor receives, not the picker
    value: the factory passes `nemotron_provider_order(...)`, which collapses
    every GPU policy onto DirectML."""
    settings = replace(
        _RUNTIME_BASE_SETTINGS,
        model_size="nemotron-3.5-asr-streaming-0.6b-int4",
        local_onnx_device=first,
    )
    saved = replace(settings, local_onnx_device=second)
    assert saved != settings

    controller, app, preloads, closed, cached = _controller_with_loaded_model(
        settings
    )
    controller._settings_store._settings = saved
    controller.on_settings_changed()

    # Asserted on the observable outcome rather than on the identity tuple:
    # an identity that changed but did not reach the cache reset, or a reset
    # that ran without a preload, would both pass a bare identity comparison.
    _assert_reload_outcome(
        controller, reloads, preloads=preloads, closed=closed, cached=cached
    )
    controller.shutdown()
    _ = app


def test_a_save_that_changes_the_model_starts_a_preload():
    settings = _RUNTIME_BASE_SETTINGS
    controller, app, preloads, _closed, _cached = _controller_with_loaded_model(
        settings
    )

    controller._settings_store._settings = replace(settings, model_size="medium")
    controller.on_settings_changed()

    assert preloads == [True]
    controller.shutdown()
    _ = app


def test_a_save_retries_a_preload_that_previously_failed():
    """A failed model is retried on the next save even if nothing changed.

    A save is exactly when the user expects a fix (a freed disk, a repaired
    download) to be picked up.
    """
    settings = _RUNTIME_BASE_SETTINGS
    controller, app, preloads, _closed, _cached = _controller_with_loaded_model(
        settings
    )
    with controller._preload_result_lock:
        controller._preload_results[controller._model_preload_key(settings)] = (
            controller._preload_generation,
            "Model failed to load.",
        )

    controller.on_settings_changed()

    assert preloads == [True]
    controller.shutdown()
    _ = app


def test_invalidate_transcriber_credentials_drops_the_runtime_using_that_key():
    """A replaced API key is invisible in AppSettings, so it needs its own path.

    ``has_*_key`` only flips when a key is added or removed; overwriting one
    with a different value leaves the settings snapshot byte-identical.
    """
    settings = replace(_RUNTIME_BASE_SETTINGS, engine="groq", has_groq_key=True)
    controller, app, _preloads, closed, cached = _controller_with_loaded_model(settings)

    controller.invalidate_transcriber_credentials(["groq"])

    assert closed == [cached]
    assert controller._transcriber_cache is None
    controller.shutdown()
    _ = app


def test_an_api_key_change_never_unloads_a_local_model():
    """A local model reads no API key, so a key change must not cost a reload.

    Selecting the remote provider later changes ``settings.engine``, which the
    transcriber identity does see; until then the loaded model is untouched.
    """
    settings = _RUNTIME_BASE_SETTINGS
    controller, app, preloads, closed, cached = _controller_with_loaded_model(settings)

    controller.invalidate_transcriber_credentials(["openai"])

    assert closed == []
    assert controller._transcriber_cache is cached
    assert controller._pending_transcriber_cache_reset is False
    assert preloads == []
    controller.shutdown()
    _ = app


def test_a_key_change_for_another_provider_keeps_the_loaded_runtime():
    """A Groq runtime does not care about an OpenAI key."""
    settings = replace(_RUNTIME_BASE_SETTINGS, engine="groq", has_groq_key=True)
    controller, app, _preloads, closed, cached = _controller_with_loaded_model(settings)

    controller.invalidate_transcriber_credentials(["openai", "deepgram"])

    assert closed == []
    assert controller._transcriber_cache is cached
    controller.shutdown()
    _ = app


def test_every_remote_engine_is_in_both_identity_maps():
    """A new engine must appear in both maps, or its identity is silently wrong.

    `_transcriber_identity` falls back to the *local* branch for an engine it
    does not recognize, which produces an identity with no model and no key
    flag -- so changing that provider's model would never rebuild its runtime,
    and two settings that differ only in the custom vocabulary would compare
    equal while `create_transcriber` still passes it through.
    """
    remote = set(VALID_ENGINES) - {DEFAULT_ENGINE}

    assert set(controller_module._ENGINE_MODEL_FIELDS) == remote
    assert set(controller_module._ENGINE_KEY_FLAGS) == remote
    # The names must resolve, too: a typo would read a default forever.
    defaults = AppSettings()
    for engine in remote:
        assert hasattr(defaults, controller_module._ENGINE_MODEL_FIELDS[engine])
        assert hasattr(defaults, controller_module._ENGINE_KEY_FLAGS[engine])


@pytest.mark.parametrize("engine", sorted(set(VALID_ENGINES) - {DEFAULT_ENGINE}))
def test_each_remote_engine_reads_its_own_model_field_and_key_flag(engine):
    """Every provider, not just the three that happened to be parametrized."""
    controller, app, _preloads, _closed, _cached = _controller_with_loaded_model(
        replace(_RUNTIME_BASE_SETTINGS, engine=engine)
    )
    settings = replace(_RUNTIME_BASE_SETTINGS, engine=engine)
    base = controller._transcriber_identity(settings)

    model_field = controller_module._ENGINE_MODEL_FIELDS[engine]
    key_flag = controller_module._ENGINE_KEY_FLAGS[engine]
    own_model = replace(settings, **{model_field: "some-other-model"})
    own_key = replace(settings, **{key_flag: not getattr(settings, key_flag)})

    assert controller._transcriber_identity(own_model) != base
    assert controller._transcriber_identity(own_key) != base
    for other, other_flag in controller_module._ENGINE_KEY_FLAGS.items():
        if other == engine:
            continue
        foreign = replace(settings, **{other_flag: not getattr(settings, other_flag)})
        assert controller._transcriber_identity(foreign) == base, other
    controller.shutdown()
    _ = app


def test_the_identity_field_order_is_not_load_bearing():
    """Nothing may read ``_TranscriberIdentity`` positionally.

    It is a NamedTuple, so ``key[1]`` compiles and returns *a* value; the
    resume teardown used to read the model that way. Inserting a field would
    have moved it onto ``vad_enabled`` and silently stopped closing the GPU
    runtime on resume. This asserts the current order so that a future insert
    fails here -- next to the reminder -- instead of in that teardown.
    """
    assert controller_module._TranscriberIdentity._fields[:2] == (
        "engine",
        "model_size",
    )
    # And that the readers use names, not indices.
    source = Path(controller_module.__file__).read_text(encoding="utf-8")
    assert "cache_key[" not in source
    assert "cached_key[" not in source


def test_a_bare_provider_string_is_not_iterated_character_by_character():
    """``providers="groq"`` must name one provider, not four letters.

    A string is iterable, so the membership test compared ``"groq"`` against
    ``{"g", "r", "o", "q"}``: the runtime whose key had just been replaced was
    the one case that silently kept running.
    """
    settings = replace(_RUNTIME_BASE_SETTINGS, engine="groq", has_groq_key=True)
    controller, app, _preloads, closed, cached = _controller_with_loaded_model(settings)

    controller.invalidate_transcriber_credentials("groq")

    assert closed == [cached]
    assert controller._transcriber_cache is None
    controller.shutdown()
    _ = app


def test_a_cache_key_that_is_not_an_identity_invalidates_unconditionally():
    """An unrecognized key must fail towards dropping a stale credential.

    The engine is read off the key, so a key of another shape has no engine to
    compare and would otherwise leave a runtime holding a revoked API key.
    """
    settings = replace(_RUNTIME_BASE_SETTINGS, engine="groq", has_groq_key=True)
    controller, app, _preloads, closed, cached = _controller_with_loaded_model(settings)
    controller._transcriber_cache_key = ("groq", "whisper-large-v3-turbo")

    controller.invalidate_transcriber_credentials(["openai"])

    assert closed == [cached]
    assert controller._transcriber_cache is None
    controller.shutdown()
    _ = app


@pytest.mark.parametrize(
    ("engine", "change", "reloads"),
    [
        ("groq", {"groq_model": "whisper-large-v3"}, True),
        ("groq", {"openai_model": "gpt-4o-transcribe"}, False),
        ("groq", {"model_size": "medium"}, False),
        ("groq", {"has_groq_key": True}, True),
        ("azure", {"azure_endpoint": "https://other.cognitiveservices.azure.com"}, True),
        ("azure", {"azure_speech_model": "mai-transcribe-1"}, True),
        ("azure", {"groq_model": "whisper-large-v3"}, False),
        ("azure", {"allow_insecure_key_storage": True}, True),
        ("openai", {"openai_model": "gpt-4o-transcribe"}, True),
        ("openai", {"custom_vocabulary": "Kubernetes"}, True),
        # ElevenLabs exposes no biasing input, so the term list never reaches it.
        ("elevenlabs", {"custom_vocabulary": "Kubernetes"}, False),
        ("elevenlabs", {"model_size": "medium"}, False),
    ],
    ids=lambda value: value if isinstance(value, str) else str(value),
)
def test_a_remote_identity_reads_only_the_fields_that_engine_uses(
    engine, change, reloads
):
    """Each engine's identity must cover its own constructor arguments only.

    Listing every provider's model field for every engine would reload a Groq
    runtime because an unrelated Azure endpoint was typed in; omitting one
    would keep a runtime built from the previous value.
    """
    settings = replace(_RUNTIME_BASE_SETTINGS, engine=engine)
    saved = replace(settings, **change)
    # Guard against a parameter that repeats the current value: it would make
    # the "no reload" half pass without testing anything.
    assert saved != settings

    controller, app, preloads, closed, cached = _controller_with_loaded_model(
        settings
    )
    controller._settings_store._settings = saved
    controller.on_settings_changed()

    # Asserted on the observable outcome rather than on the identity tuple:
    # an identity that changed but did not reach the cache reset, or a reset
    # that ran without a preload, would both pass a bare identity comparison.
    _assert_reload_outcome(
        controller, reloads, preloads=preloads, closed=closed, cached=cached
    )
    controller.shutdown()
    _ = app


def test_a_condemned_runtime_is_preloaded_again_even_though_the_key_matches():
    """The cache key alone is not enough to call a runtime ready.

    A runtime that is still in use but already condemned
    (`_pending_transcriber_cache_reset`) is closed the moment its last owner
    releases it, so the matching cache key describes a model that is about to
    disappear. Without this branch the save that condemned it would skip the
    preload and the next dictation would load the model on the hotkey press.
    """
    settings = _RUNTIME_BASE_SETTINGS
    controller, app, _preloads, _closed, _cached = _controller_with_loaded_model(
        settings
    )
    # Exactly the state that makes every other branch say "no preload needed":
    # a successful result for this key and a cache key that matches it.
    assert controller._local_model_preload_needed(settings) is False

    with controller._transcriber_runtime_state_lock:
        controller._pending_transcriber_cache_reset = True

    assert controller._local_model_preload_needed(settings) is True
    controller.shutdown()
    _ = app


def test_a_remote_engine_never_asks_for_a_local_preload():
    """Both call sites check the engine too, so this is the helper's own
    guard: it must not be dropped on the assumption a caller always checks."""
    settings = replace(_RUNTIME_BASE_SETTINGS, engine="groq")
    controller, app, _preloads, _closed, _cached = _controller_with_loaded_model(
        settings
    )
    # Without this the stored "already preloaded" result answers first and the
    # engine guard is never reached.
    with controller._preload_result_lock:
        controller._preload_results.clear()

    assert controller._local_model_preload_needed(settings) is False
    controller.shutdown()
    _ = app


def test_a_failed_live_insert_offers_the_same_words_again():
    """`apply_partial_append_only` commits the words as it hands them over.

    So a paste that failed loses them for good unless the commit is taken
    back: the locked prefix can only move forward, and it would never offer
    that text again. The usual cause is a modifier key still held down, which
    turns the injected Ctrl+V into Ctrl+Alt+V -- transient, and fatal only
    because the words are gone. Nothing tested the call site; the unit
    `rollback_commit` was covered on its own.
    """
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        mode="streaming",
        model_size="small",
        keep_transcript_in_clipboard=False,
    )
    inserter = FakeTextInserter(should_fail=True)
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

    # The locked prefix needs two partials to agree before anything is
    # inserted, so the failing paste is the one the second partial triggers.
    controller._on_transcription_partial("hello world this is")
    controller._on_transcription_partial("world this is working now")
    assert [call[0] for call in inserter.calls] == ["hello world"], inserter.calls

    inserter.should_fail = False
    controller._on_transcription_partial("this is working now today")

    assert [call[0] for call in inserter.calls] == [
        "hello world",
        "hello world this is",
    ], (
        "the words from the failed paste were never offered again, so they "
        f"exist only in history: {inserter.calls}"
    )
    controller.shutdown()
    _ = app


@pytest.mark.parametrize(
    ("label", "helper_kind"),
    [
        ("a helper that records it", "records"),
        ("a helper that raises", "raises"),
        ("a helper without the method", "absent"),
    ],
)
def test_note_foreground_window_is_forwarded_and_never_raises(label, helper_kind):
    """The tray calls this on the way into a menu; it must not be able to fail.

    Its whole job is to catch the last moment the user's own window is still
    in front. It runs from a Qt slot on a path that has nothing to report to,
    so a helper that raises must be swallowed rather than take the menu with
    it -- while a helper that works must actually be called, or the tray fix
    is decoration.
    """
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    focus = FakeWindowFocusHelper()
    calls: list[int] = []
    if helper_kind == "records":
        focus.note_foreground_window = lambda: calls.append(1)
    elif helper_kind == "raises":

        def _boom():
            calls.append(1)
            raise OSError("GetForegroundWindow failed")

        focus.note_foreground_window = _boom

    controller = DictationController(
        settings_store=FakeSettingsStore(AppSettings(hotkey=FALLBACK_HOTKEY)),
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=FakeOverlay(),
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.controller"),
        window_focus_helper=focus,
    )
    try:
        controller.note_foreground_window()
    finally:
        controller.shutdown()

    expected = 0 if helper_kind == "absent" else 1
    assert len(calls) == expected, label
    _ = app
