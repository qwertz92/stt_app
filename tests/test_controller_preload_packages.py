"""The startup preload says so while it updates the JavaScript packages.

With "Keep ONNX model loaded" on (the default) the first Cohere/Granite
runner starts in the startup preload, so a source checkout's package update
(`transcriber._node_packages`) runs there -- 11 s to over a minute -- while
the overlay said only "Loading selected model...". A dictation start already
showed the update's own line; the preload now shows the same line.
"""

from __future__ import annotations

from conftest import FakeCapture, FakeOverlay, FakeSettingsStore
from conftest import make_controller as _make_controller

from stt_app import controller as controller_module
from stt_app.config import FALLBACK_HOTKEY, NODE_PACKAGES_UPDATING_TEXT
from stt_app.settings_store import AppSettings
from stt_app.transcriber.base import ProgressReporter

_MODEL = "cohere-transcribe-03-2026"


class _RunningFuture:
    def done(self):
        return False


class _UpdatingTranscriber(ProgressReporter):
    """Reports what `_ensure_js_runtime_available` and the runner report.

    `while_updating` and `after_update` run where the real preload is busy:
    inside npm, and once the runner is starting.
    """

    def __init__(self, while_updating, after_update) -> None:
        super().__init__()
        self._while_updating = while_updating
        self._after_update = after_update

    def set_cancel_check(self, _check) -> None:
        return None

    def preload_model(self) -> None:
        self._emit_progress(NODE_PACKAGES_UPDATING_TEXT)
        self._while_updating()
        self._emit_progress("JavaScript packages for Cohere/Granite updated.")
        self._emit_progress(f"Starting ONNX runtime for {_MODEL}: GPU only.")
        self._after_update()


def test_the_startup_preload_shows_the_package_update_while_npm_runs(monkeypatch):
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY,
        engine="local",
        mode="batch",
        model_size=_MODEL,
        keep_onnx_model_loaded=True,
    )
    overlay = FakeOverlay()
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
    )
    controller._preload_future = _RunningFuture()
    controller._preload_target_model = _MODEL
    generation = controller._preload_generation
    shown: list[str] = []

    def poll() -> None:
        # The 600 ms timer's own slot: the only writer of the preload line.
        controller._on_preload_progress_poll()
        shown.append(overlay.detail)

    transcriber = _UpdatingTranscriber(poll, poll)

    class Lease:
        def __init__(self) -> None:
            self.transcriber = transcriber

        def release(self) -> None:
            return None

    monkeypatch.setattr(controller, "_download_model_for_preload", lambda *_a: None)
    monkeypatch.setattr(
        controller,
        "_acquire_transcriber_runtime",
        lambda _settings, allow_isolated=True: Lease(),
    )

    controller._preload_model_worker(
        settings, generation, controller._model_preload_key(settings)
    )

    updating, loading = shown
    assert updating.startswith(NODE_PACKAGES_UPDATING_TEXT)
    assert "You can start recording now" in updating
    assert f"Loading '{_MODEL}' into memory." in loading
    # The runtime is shared and cached: its next owner must not report into
    # this finished preload.
    assert transcriber._progress_callback is None
    controller.shutdown()
    _ = app


def test_a_retired_preloads_update_line_never_describes_the_next_one(monkeypatch):
    """A settings change retires a preload whose worker can still be inside
    npm; its lines, before or after the change, are not the new preload's."""
    settings = AppSettings(
        hotkey=FALLBACK_HOTKEY, engine="local", mode="batch", model_size=_MODEL
    )
    monkeypatch.setattr("stt_app.controller.AudioCapture", FakeCapture)
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings), overlay=FakeOverlay()
    )
    controller._preload_target_model = _MODEL
    retired = controller._preload_generation
    controller._note_preload_progress(retired, NODE_PACKAGES_UPDATING_TEXT)
    controller._preload_generation = retired + 1
    controller._set_preload_phase(retired + 1, controller_module._PRELOAD_PHASE_LOAD)

    retired_line_shown = controller._preload_progress_detail()
    # The current preload updates; the retired worker moves on meanwhile.
    controller._note_preload_progress(retired + 1, NODE_PACKAGES_UPDATING_TEXT)
    controller._note_preload_progress(retired, f"Starting ONNX runtime for {_MODEL}.")
    current_line_shown = controller._preload_progress_detail()

    assert f"Loading '{_MODEL}' into memory." in retired_line_shown
    assert current_line_shown.startswith(NODE_PACKAGES_UPDATING_TEXT)
    controller.shutdown()
    _ = app
