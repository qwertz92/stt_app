"""A wake from sleep loads the kept ONNX model again in the background.

`handle_system_resume` closes a cached Cohere/Granite runtime so the GPU
backends are recreated, and nothing loaded it again: with "Keep ONNX model
loaded" on, the first dictation after every wake paid the whole load inside
its transcription (owner's log 2026-10-25 to 2026-11-24: 12 resumes, the next
transcription 7.8-13.1 s for short clips against a median of 0.74 s).
"""

from __future__ import annotations

import logging
import threading
import time

from conftest import FakeOverlay, FakeSettingsStore
from conftest import make_controller as _make_controller

from stt_app import controller as controller_module
from stt_app.config import FALLBACK_HOTKEY, RESUME_PRELOAD_MAX_ATTEMPTS
from stt_app.settings_store import AppSettings

_MODEL = "cohere-transcribe-03-2026"


class _GpuRuntime:
    """A Cohere runtime whose load can fail while the GPU is not back yet."""

    model_size = _MODEL
    runtime_device = "webgpu"

    def __init__(self, plan: _LoadPlan) -> None:
        self._plan = plan
        self.closed = False
        self.loaded = False

    def set_cancel_check(self, _check) -> None:
        return None

    def set_progress_callback(self, _callback) -> None:
        return None

    def set_language_mode(self, _mode) -> None:
        return None

    def preload_model(self) -> None:
        self._plan.load()
        self.loaded = True

    def close(self) -> None:
        self.closed = True


class _LoadPlan:
    def __init__(self, failures: int = 0, hold: threading.Event | None = None):
        self.failures = failures
        self.loads = 0
        self.created: list[_GpuRuntime] = []
        self.hold = hold
        self.entered = threading.Event()

    def load(self) -> None:
        self.loads += 1
        self.entered.set()
        if self.hold is not None:
            assert self.hold.wait(timeout=8.0), "the test never released the load"
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("WebGPU adapter not available")

    def create(self, _settings, **_kwargs) -> _GpuRuntime:
        runtime = _GpuRuntime(self)
        self.created.append(runtime)
        return runtime


def _settings(**changes) -> AppSettings:
    values = {
        "hotkey": FALLBACK_HOTKEY,
        "engine": "local",
        "mode": "batch",
        "model_size": _MODEL,
        "keep_onnx_model_loaded": True,
        "keep_transcript_in_clipboard": False,
    }
    values.update(changes)
    return AppSettings(**values)


def _controller_after_startup(monkeypatch, plan: _LoadPlan, settings: AppSettings):
    """A controller whose startup preload loaded ``settings``' model."""
    monkeypatch.setattr(controller_module, "create_transcriber", plan.create)
    overlay = FakeOverlay()
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        overlay=overlay,
        logger=logging.getLogger("test.controller.resume_preload"),
    )
    monkeypatch.setattr(controller, "_download_model_for_preload", lambda *_a: None)
    loaded = _GpuRuntime(plan)
    loaded.loaded = True
    controller._transcriber_cache = loaded
    controller._transcriber_cache_key = controller._transcriber_identity(settings)
    with controller._preload_result_lock:
        controller._preload_results[controller._model_preload_key(settings)] = (
            controller._preload_generation,
            None,
        )
    return controller, app, overlay, loaded


def _pump_until(app, predicate, timeout_s: float = 8.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()
    assert predicate(), "the preload never finished"


def _finish_preload(controller, app) -> None:
    future = controller._preload_future
    assert future is not None, "no preload was started"
    future.result(timeout=8.0)
    _pump_until(app, lambda: controller._current_preload_phase() == "")


def test_a_resume_loads_the_kept_model_again_in_the_background(monkeypatch):
    plan = _LoadPlan()
    settings = _settings()
    controller, app, _overlay, loaded = _controller_after_startup(
        monkeypatch, plan, settings
    )
    try:
        controller.handle_system_resume()

        assert loaded.closed is True
        assert controller._transcriber_cache is None
        # Not at once: the GPU may not be back in the first moment after
        # wake, so the load waits a little, on a timer.
        assert controller._preload_future is None
        assert controller._resume_preload_timer.isActive()

        controller._resume_preload_timer.stop()
        controller._run_resume_preload()
        _finish_preload(controller, app)

        reloaded = controller._transcriber_cache
        assert reloaded is plan.created[-1] and reloaded.loaded
        assert controller._transcriber_cache_key == controller._transcriber_identity(
            settings
        )
        assert not controller._local_model_preload_needed(settings)
    finally:
        controller.shutdown()
    _ = app


def test_a_failed_load_after_resume_is_retried_and_never_blocks_dictation(
    monkeypatch,
):
    plan = _LoadPlan(failures=1)
    settings = _settings()
    controller, app, overlay, _loaded = _controller_after_startup(
        monkeypatch, plan, settings
    )
    try:
        controller.handle_system_resume()
        controller._resume_preload_timer.stop()
        controller._run_resume_preload()
        _finish_preload(controller, app)

        # The first load failed. A failure recorded for this key would be
        # raised to the next dictation ("could not be loaded ... Open
        # Settings"), so the app could not transcribe until a save.
        assert controller._model_preload_failure(settings) is None
        assert overlay.state != "Error"
        assert controller._resume_preload_timer.isActive(), "no retry was planned"

        controller._resume_preload_timer.stop()
        controller._run_resume_preload()
        _finish_preload(controller, app)

        assert plan.loads == 2
        assert controller._transcriber_cache is plan.created[-1]
        assert controller._transcriber_cache.loaded
        assert not controller._resume_preload_timer.isActive()
    finally:
        controller.shutdown()
    _ = app


def test_resume_preload_gives_up_after_its_attempts_and_leaves_the_first_use_load(
    monkeypatch,
):
    plan = _LoadPlan(failures=RESUME_PRELOAD_MAX_ATTEMPTS + 5)
    settings = _settings()
    controller, app, overlay, _loaded = _controller_after_startup(
        monkeypatch, plan, settings
    )
    try:
        controller.handle_system_resume()
        for _attempt in range(RESUME_PRELOAD_MAX_ATTEMPTS):
            assert controller._resume_preload_timer.isActive()
            controller._resume_preload_timer.stop()
            controller._run_resume_preload()
            _finish_preload(controller, app)

        assert plan.loads == RESUME_PRELOAD_MAX_ATTEMPTS
        assert not controller._resume_preload_timer.isActive()
        # The next dictation is not refused: it loads at first use.
        assert controller._model_preload_failure(settings) is None
        controller._wait_for_selected_model_preload(settings)
        assert overlay.state != "Error"
    finally:
        controller.shutdown()
    _ = app


def test_a_second_resume_during_the_load_starts_no_second_load(monkeypatch):
    hold = threading.Event()
    plan = _LoadPlan(hold=hold)
    settings = _settings()
    controller, app, _overlay, _loaded = _controller_after_startup(
        monkeypatch, plan, settings
    )
    try:
        controller.handle_system_resume()
        controller._resume_preload_timer.stop()
        controller._run_resume_preload()
        assert plan.entered.wait(timeout=8.0)
        running = controller._preload_future

        # Windows often sends several resume notifications for one wake. The
        # runtime is in use by the preload, so it is neither closed nor
        # loaded a second time.
        controller.handle_system_resume()
        controller._resume_preload_timer.stop()
        controller._run_resume_preload()

        assert controller._preload_future is running
        hold.set()
        _finish_preload(controller, app)
        assert plan.loads == 1
        assert len(plan.created) == 1
        assert controller._transcriber_cache is plan.created[0]
        assert not plan.created[0].closed
    finally:
        hold.set()
        controller.shutdown()
    _ = app


def test_resume_does_not_preload_without_keep_loaded_or_with_a_remote_engine(
    monkeypatch,
):
    for settings in (
        _settings(keep_onnx_model_loaded=False),
        _settings(engine="groq"),
    ):
        plan = _LoadPlan()
        controller, app, _overlay, _loaded = _controller_after_startup(
            monkeypatch, plan, settings
        )
        try:
            controller.handle_system_resume()
            assert not controller._resume_preload_timer.isActive()
            controller._run_resume_preload()
            assert controller._preload_future is None
            assert plan.loads == 0
        finally:
            controller.shutdown()
        _ = app


def test_the_resume_preload_leaves_a_finished_result_on_screen(monkeypatch):
    """The load runs seconds after the wake, when the overlay may show the
    last result -- a Done or an Error with Retry. Like every delayed writer
    it must not paint over it."""
    plan = _LoadPlan()
    settings = _settings()
    controller, app, overlay, _loaded = _controller_after_startup(
        monkeypatch, plan, settings
    )
    try:
        controller.handle_system_resume()
        overlay.set_state("Error", "Transcription failed. Retry?")
        painted_before = len(overlay.states)
        controller._resume_preload_timer.stop()
        controller._run_resume_preload()
        _finish_preload(controller, app)

        assert overlay.states[painted_before:] == []
        assert controller._transcriber_cache is plan.created[-1]
    finally:
        controller.shutdown()
    _ = app


def test_a_load_inside_a_transcription_is_logged_as_load_time(monkeypatch, caplog):
    """The first dictation after a wake ran the model load inside
    `transcribe_batch`, and the log called all of it `transcribe_ms` (7.8 to
    13.1 s, `init_ms` 1-2), so the load looked like slow transcription."""

    class _ColdRuntime(_GpuRuntime):
        runtime_start_ms_total = 1_000

        def transcribe_batch(self, _audio) -> str:
            self.runtime_start_ms_total += 9_000
            return "hello"

    settings = _settings()
    plan = _LoadPlan()
    monkeypatch.setattr(
        controller_module,
        "create_transcriber",
        lambda _settings, **_kwargs: _ColdRuntime(plan),
    )
    controller, app = _make_controller(
        settings_store=FakeSettingsStore(settings),
        logger=logging.getLogger("test.controller.resume_preload"),
    )
    try:
        with caplog.at_level(logging.INFO, logger="test.controller.resume_preload"):
            controller._transcribe_worker(1, b"RIFF", settings)
        line = next(
            record.getMessage()
            for record in caplog.records
            if record.getMessage().startswith("transcription_timing")
        )
        fields = dict(part.split("=", 1) for part in line.split() if "=" in part)
        assert fields["load_ms"] == "9000"
        assert int(fields["transcribe_ms"]) < 9_000
    finally:
        controller.shutdown()
    _ = app


def test_a_wake_loads_nothing_that_was_not_loaded_before_it(monkeypatch):
    """A model whose download was canceled or failed is not fetched because
    the PC woke up: the post-resume load only undoes the resume's close."""
    plan = _LoadPlan()
    settings = _settings()
    controller, app, _overlay, _loaded = _controller_after_startup(
        monkeypatch, plan, settings
    )
    try:
        controller._transcriber_cache = None
        controller._transcriber_cache_key = None

        controller.handle_system_resume()

        assert not controller._resume_preload_timer.isActive()
        assert controller._preload_future is None
    finally:
        controller.shutdown()
    _ = app


def test_a_failed_attempt_returns_the_overlay_to_idle_before_its_worker_ends(
    monkeypatch,
):
    """The worker reports before its `finally` releases the lease, and a failed
    load's release closes the runtime, which takes seconds for the Node runner.
    The idle line refused to paint while that future was not done, and the
    progress poll had already stopped: "Loading selected model..." stayed."""

    class _StillReleasing:
        def done(self) -> bool:
            return False

        def cancel(self) -> bool:
            return False

    plan = _LoadPlan()
    settings = _settings()
    controller, app, overlay, _loaded = _controller_after_startup(
        monkeypatch, plan, settings
    )
    try:
        controller._resume_preload_attempts_left = 1
        controller._start_local_model_preload = lambda **_kwargs: None
        generation = controller._preload_generation
        controller._resume_preload_generation = generation
        controller._preload_future = _StillReleasing()
        overlay.set_state("Processing", "Loading selected model...")

        controller._on_model_preload_done(
            generation, False, "Selected model could not be loaded: no adapter"
        )

        assert overlay.state != "Processing"
        assert controller._resume_preload_timer.isActive()
    finally:
        controller.shutdown()
    _ = app
