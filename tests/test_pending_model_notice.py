"""The retired-model notice a start still owes the user, and the text for a
selected model that is not on this PC.

`pending_model_moves` lives in the settings file until the tray has shown the
balloon. Anything that saves a stale in-memory copy of the settings (the
overlay's own controls do) must neither bring a shown notice back nor drop one
that was not shown yet.
"""

import json
import logging
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from conftest import (
    FakeHotkeyManager,
    FakeOverlay,
    FakeTextInserter,
    FakeWindowFocusHelper,
)
from PySide6 import QtWidgets

from stt_app import settings_store as settings_store_module
from stt_app.config import FALLBACK_HOTKEY
from stt_app.controller import DictationController
from stt_app.persistence import store_unavailable_error
from stt_app.settings_store import AppSettings, SettingsStore


def _write(path, **fields):
    payload = AppSettings(hotkey=FALLBACK_HOTKEY).to_dict()
    payload.update(fields)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _pending(path):
    return SettingsStore(path).load().pending_model_moves


def _controller(store):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    controller = DictationController(
        settings_store=store,
        hotkey_manager=FakeHotkeyManager(),
        cancel_hotkey_manager=FakeHotkeyManager(),
        overlay=FakeOverlay(),
        text_inserter=FakeTextInserter(),
        logger=logging.getLogger("test.pending"),
        window_focus_helper=FakeWindowFocusHelper(),
    )
    controller._start_local_model_preload = lambda *a, **k: None
    return controller, app


def _a_controller_holding_a_stale_notice(tmp_path, **fields):
    path = tmp_path / "settings.json"
    _write(path, engine="local", model_size="tiny", **fields)
    store = SettingsStore(path)
    store.load()
    controller, app = _controller(store)
    controller.reload_settings(re_register_hotkey=False)
    # The controller's own copy still carries the notice ...
    assert controller.settings.pending_model_moves
    # ... and the tray shows it and clears it in the file.
    store.clear_model_notice()
    assert _pending(path) == []
    return controller, app, path


def test_a_plain_save_of_a_stale_copy_never_brings_a_shown_notice_back(tmp_path):
    path = tmp_path / "settings.json"
    _write(path, engine="local", model_size="tiny")
    store = SettingsStore(path)
    stale = store.load()
    assert stale.pending_model_moves
    store.clear_model_notice()

    store.save(stale)

    assert _pending(path) == []


def test_a_plain_save_never_drops_a_notice_that_was_not_shown_yet(tmp_path):
    path = tmp_path / "settings.json"
    _write(path, engine="local", model_size="tiny")
    store = SettingsStore(path)
    fresh = store.load()

    store.save(replace(fresh, pending_model_moves=[]))

    assert [m["from"] for m in _pending(path)] == ["tiny"]


def test_the_language_menu_does_not_bring_a_shown_notice_back(tmp_path):
    controller, app, path = _a_controller_holding_a_stale_notice(
        tmp_path, language_mode="auto"
    )

    controller.set_language_mode("de")

    assert _pending(path) == []
    controller.shutdown()
    _ = app


def test_the_opacity_slider_does_not_bring_a_shown_notice_back(tmp_path):
    controller, app, path = _a_controller_holding_a_stale_notice(tmp_path)

    controller.set_overlay_opacity_percent(
        int(controller.settings.overlay_opacity_percent) - 10
    )

    assert _pending(path) == []
    controller.shutdown()
    _ = app


def test_the_pin_button_does_not_bring_a_shown_notice_back(tmp_path):
    controller, app, path = _a_controller_holding_a_stale_notice(tmp_path)

    controller.set_overlay_always_on_top(not controller.settings.overlay_always_on_top)

    assert _pending(path) == []
    controller.shutdown()
    _ = app


def test_clearing_the_notice_never_raises_into_qt_and_keeps_it_pending(
    tmp_path, monkeypatch, caplog
):
    """With a primary settings file that cannot be opened the save refuses; the
    timer callback that clears the notice must log it, not raise."""
    path = tmp_path / "settings.json"
    _write(path, engine="local", model_size="tiny")
    store = SettingsStore(path)
    store.load()

    def refuse(file_path):
        raise store_unavailable_error(file_path)

    monkeypatch.setattr(settings_store_module, "refuse_to_overwrite_unreadable", refuse)
    with caplog.at_level(logging.WARNING):
        store.clear_model_notice()
    monkeypatch.undo()

    assert [m["from"] for m in _pending(path)] == ["tiny"]
    assert any("pending" in record.getMessage().lower() for record in caplog.records)


@pytest.mark.parametrize("which", ["one", "all"])
def test_clearing_named_moves_leaves_the_others_pending(tmp_path, which):
    path = tmp_path / "settings.json"
    _write(
        path,
        engine="local",
        model_size="tiny",
        openai_model="whisper-1",
        has_openai_key=True,
    )
    store = SettingsStore(path)
    moves = store.load().pending_model_moves
    assert {m["engine"] for m in moves} == {"local", "openai"}

    if which == "one":
        store.clear_model_notice([m for m in moves if m["engine"] == "local"])
        assert [m["engine"] for m in _pending(path)] == ["openai"]
    else:
        store.clear_model_notice()
        assert _pending(path) == []


# ---------------------------------------------------------------------------
# The notice says the successor "is downloading now": it has to be.


def _a_moved_granite_user(tmp_path, monkeypatch, **fields):
    import stt_app.controller as controller_module

    path = tmp_path / "settings.json"
    _write(
        path,
        engine="local",
        model_size="granite-speech-4.1-2b",
        keep_onnx_model_loaded=False,
        model_dir=str(tmp_path / "models"),
        **fields,
    )
    started: list[tuple] = []

    def fake_start(model_name, model_dir=""):
        started.append((model_name, model_dir))
        raise RuntimeError("stop here")

    monkeypatch.setattr(controller_module, "start_model_download_process", fake_start)
    controller, app = _controller(SettingsStore(path))
    controller.reload_settings(re_register_hotkey=False)
    return controller, app, started


def test_a_moved_user_with_keep_onnx_off_gets_the_successor_downloaded(
    tmp_path, monkeypatch
):
    controller, app, started = _a_moved_granite_user(tmp_path, monkeypatch)

    future = controller._start_retirement_download()

    assert future is not None
    future.result(timeout=30)
    assert started == [("granite-speech-5.0-2b", str(tmp_path / "models"))]
    controller.shutdown()
    _ = app


def test_initialize_starts_the_retirement_download(tmp_path, monkeypatch):
    controller, app, _started = _a_moved_granite_user(tmp_path, monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(
        controller, "_start_retirement_download", lambda: calls.append("download")
    )

    controller.initialize()

    assert calls == ["download"]
    controller.shutdown()
    _ = app


@pytest.mark.parametrize(
    "fields",
    [
        {"offline_mode": True},
        {"engine": "openai"},
        {"model_size": "small"},
    ],
    ids=["offline mode", "another engine", "another model selected"],
)
def test_no_download_starts_when_the_notice_would_not_promise_one(
    tmp_path, monkeypatch, fields
):
    controller, app, started = _a_moved_granite_user(tmp_path, monkeypatch)
    controller._settings = replace(controller._settings, **fields)

    assert controller._start_retirement_download() is None
    assert started == []
    controller.shutdown()
    _ = app


def test_a_successor_already_on_disk_is_not_downloaded_again(tmp_path, monkeypatch):
    import stt_app.transcriber.local_faster_whisper as whisper_module

    controller, app, started = _a_moved_granite_user(tmp_path, monkeypatch)
    monkeypatch.setattr(
        whisper_module, "find_cached_models", lambda _dir="": ["granite-speech-5.0-2b"]
    )

    future = controller._start_retirement_download()

    assert future is not None
    future.result(timeout=30)
    assert started == []
    controller.shutdown()
    _ = app


# ---------------------------------------------------------------------------
# A selected model that is not on this PC and cannot be fetched (offline mode)


_RAW_OFFLINE_LOAD_ERROR = (
    "Cannot find an appropriate cached snapshot folder for the specified "
    "revision on the local disk and outgoing traffic has been disabled. To "
    "enable repo look-ups and downloads online, pass 'local_files_only=False' "
    "as input."
)


@pytest.mark.parametrize(
    ("model", "name", "size"),
    [
        ("large-v3-turbo", "large-v3-turbo", "~1.62 GB"),
        ("small", "small", "~486 MB"),
        ("granite-speech-5.0-2b", "IBM Granite Speech 5.0 2B", "~1.73 GB"),
        ("fluid-speech-pro", "FluidVoice Fluid Speech Pro", "~801 MB"),
    ],
)
def test_a_model_missing_in_offline_mode_gets_the_readable_text(
    tmp_path, model, name, size
):
    from stt_app.controller import _preload_failure_text

    settings = AppSettings(
        engine="local",
        model_size=model,
        offline_mode=True,
        model_dir=str(tmp_path / "an-empty-model-dir"),
    )

    text = _preload_failure_text(settings, OSError(_RAW_OFFLINE_LOAD_ERROR), None)

    assert f"{name} ({size}) is not on this PC" in text
    assert "Offline mode is on" in text
    assert "Settings > Models" in text
    assert "Settings > Transcription" in text
    # Library wording, the raw id and the docs path are for the log.
    assert "local_files_only" not in text
    assert f"'{model}'" not in text
    assert "docs/" not in text


def test_a_model_on_disk_that_fails_to_load_in_offline_mode_keeps_its_own_text(
    tmp_path, monkeypatch
):
    import stt_app.transcriber.local_faster_whisper as whisper_module
    from stt_app.controller import _preload_failure_text

    monkeypatch.setattr(whisper_module, "find_cached_models", lambda _dir="": ["small"])
    settings = AppSettings(
        engine="local", model_size="small", offline_mode=True, model_dir=str(tmp_path)
    )

    text = _preload_failure_text(settings, OSError("corrupt snapshot"), None)

    assert "could not be loaded: corrupt snapshot" in text


def test_the_offline_preload_reaches_the_overlay_as_the_readable_text(
    tmp_path, monkeypatch
):
    """The worker path: offline mode skips the download without an error, the
    load then fails with the library's own text."""
    path = tmp_path / "settings.json"
    _write(
        path,
        engine="local",
        model_size="large-v3-turbo",
        offline_mode=True,
        model_dir=str(tmp_path / "empty"),
    )
    controller, app = _controller(SettingsStore(path))
    controller.reload_settings(re_register_hotkey=False)
    done: list[tuple[int, bool, str]] = []
    controller.model_preload_done.connect(
        lambda generation, ok, message: done.append((generation, ok, message))
    )

    def failing_load(*_args, **_kwargs):
        raise OSError(_RAW_OFFLINE_LOAD_ERROR)

    monkeypatch.setattr(controller, "_acquire_transcriber_runtime", failing_load)
    settings = controller.settings
    controller._preload_model_worker(
        settings, 1, controller._model_preload_key(settings)
    )

    assert done and done[-1][1] is False
    assert "Offline mode is on" in done[-1][2]
    assert "local_files_only" not in done[-1][2]
    controller.shutdown()
    _ = app


# ---------------------------------------------------------------------------
# A queued successor download must not outlive the choice it was queued for.


def _occupy_the_preload_worker(controller):
    """Stand-in for the start's own preload (Keep ONNX loaded on, or a Whisper
    successor): it holds the single preload worker until `gate` is set."""
    import threading

    gate = threading.Event()
    controller._preload_executor.submit(gate.wait, 30)
    return gate


def test_a_queued_retirement_download_does_not_fetch_a_model_the_user_left(
    tmp_path, monkeypatch
):
    controller, app, started = _a_moved_granite_user(tmp_path, monkeypatch)
    gate = _occupy_the_preload_worker(controller)
    future = controller._start_retirement_download()
    assert future is not None

    # The user picks another model in Settings; the save cancels the generation
    # the work was queued under and starts the next one.
    controller._settings = replace(controller._settings, model_size="small")
    gate.set()
    future.result(timeout=30)

    assert started == []
    controller.shutdown()
    _ = app


@pytest.mark.parametrize(
    "fields",
    [{"engine": "openai"}, {"model_size": "small"}, {"offline_mode": True}],
    ids=["another engine", "another model", "offline mode"],
)
def test_a_queued_retirement_download_gives_up_when_only_the_settings_changed(
    tmp_path, monkeypatch, fields
):
    """No new generation at all: the work still looks at what is selected now
    when it starts."""
    controller, app, started = _a_moved_granite_user(tmp_path, monkeypatch)
    gate = _occupy_the_preload_worker(controller)
    future = controller._start_retirement_download()
    controller._settings = replace(controller._settings, **fields)
    gate.set()

    future.result(timeout=30)

    assert started == []
    controller.shutdown()
    _ = app


def test_a_queued_retirement_download_runs_once_when_the_user_keeps_the_model(
    tmp_path, monkeypatch
):
    controller, app, started = _a_moved_granite_user(tmp_path, monkeypatch)
    gate = _occupy_the_preload_worker(controller)
    future = controller._start_retirement_download()
    gate.set()

    future.result(timeout=30)

    assert started == [("granite-speech-5.0-2b", str(tmp_path / "models"))]
    controller.shutdown()
    _ = app


def test_a_queued_retirement_download_does_not_repeat_what_the_preload_fetched(
    tmp_path, monkeypatch
):
    """The preload ahead of it downloads the successor; once that finished the
    queued work finds it on disk and starts no second download."""
    import stt_app.transcriber.local_faster_whisper as whisper_module

    controller, app, started = _a_moved_granite_user(tmp_path, monkeypatch)
    cached: list[str] = []
    monkeypatch.setattr(
        whisper_module, "find_cached_models", lambda _dir="": list(cached)
    )
    import threading

    gate = threading.Event()

    def the_preload_downloading_the_successor():
        gate.wait(30)
        cached.append("granite-speech-5.0-2b")

    controller._preload_executor.submit(the_preload_downloading_the_successor)
    future = controller._start_retirement_download()
    gate.set()

    future.result(timeout=30)

    assert started == []
    controller.shutdown()
    _ = app


def test_a_new_preload_generation_does_not_end_the_queued_download(
    tmp_path, monkeypatch
):
    """The queued work belongs to the choice, not to a preload generation: a
    save that restarts the preload leaves it alone."""
    controller, app, started = _a_moved_granite_user(tmp_path, monkeypatch)
    gate = _occupy_the_preload_worker(controller)
    future = controller._start_retirement_download()
    controller._cancel_preload_generation(controller._preload_generation)
    controller._preload_generation += 1
    gate.set()

    future.result(timeout=30)

    assert started == [("granite-speech-5.0-2b", str(tmp_path / "models"))]
    controller.shutdown()
    _ = app


# ---------------------------------------------------------------------------
# The same readable text without a preload (Keep ONNX model loaded off).


@pytest.mark.parametrize(
    "model",
    [
        "granite-speech-5.0-2b",
        "fluid-speech-pro",
        "nemotron-3.5-asr-streaming-0.6b-int4",
    ],
)
def test_a_dictation_without_a_preload_names_the_missing_model_offline(tmp_path, model):
    """With offline mode on and no preload, the transcriber's own refusal reaches
    the overlay as the readable text; its raw wording (a docs path, a raw id) is
    for the log."""
    from stt_app.transcriber.base import ModelNotCachedError

    controller, app = _controller(SettingsStore(tmp_path / "settings.json"))
    settings = AppSettings(
        engine="local",
        model_size=model,
        offline_mode=True,
        model_dir=str(tmp_path / "empty"),
        keep_onnx_model_loaded=False,
    )
    controller._settings = settings

    class Refusing:
        def transcribe_batch(self, *_args, **_kwargs):
            raise ModelNotCachedError(
                f"ONNX/WebGPU model '{model}' is not cached locally. Disable Offline "
                "mode or download it first. See docs/models.md."
            )

    class Lease:
        transcriber = Refusing()

        def release(self):
            return None

    controller._acquire_transcriber_runtime = lambda *a, **k: Lease()
    controller._wait_for_selected_model_preload = lambda *a, **k: None
    emitted: list[tuple] = []
    controller.transcription_failed.connect(lambda *args: emitted.append(args))

    controller._transcribe_worker(7, b"RIFF", settings)

    assert len(emitted) == 1
    text = emitted[0][-1]
    assert "is not on this PC" in text and "Offline mode is on" in text
    assert "Settings > Models" in text
    assert f"'{model}'" not in text and "docs/" not in text
    controller.shutdown()
    _ = app


def test_the_transcriber_refuses_a_missing_offline_model_with_the_typed_error(
    tmp_path,
):
    from stt_app.transcriber.base import ModelNotCachedError, TranscriptionError
    from stt_app.transcriber.local_webgpu_asr import LocalOnnxWebGpuTranscriber

    transcriber = LocalOnnxWebGpuTranscriber(
        model_size="granite-speech-5.0-2b",
        offline_mode=True,
        model_dir=str(tmp_path / "empty"),
    )

    with pytest.raises(ModelNotCachedError) as raised:
        transcriber._ensure_snapshot()

    assert isinstance(raised.value, TranscriptionError)
    assert "not cached locally" in str(raised.value)


# ---------------------------------------------------------------------------
# While the settings still select the successor, a running download is only
# ended by the quit: a Settings save that changes none of engine, model, model
# directory and offline mode leaves it alone (no restart from zero).


class _DownloadProcess:
    def __init__(self):
        self.returncode = None
        self.release = threading.Event()

    def poll(self):
        if self.returncode is None and self.release.is_set():
            self.returncode = 0
        return self.returncode


def _wait_until(condition, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.02)
    return condition()


def _a_successor_download_in_progress(tmp_path, monkeypatch, keep):
    """A moved user whose successor download is running, as the start leaves it:
    with `keep` False only the download, with `keep` True the preload's own
    download (then its load) with the queued download behind it."""
    import stt_app.controller as controller_module
    import stt_app.transcriber.local_faster_whisper as whisper_module

    controller, app, _unused = _a_moved_granite_user(tmp_path, monkeypatch)
    started: list[str] = []
    procs: list[_DownloadProcess] = []
    loads: list[str] = []

    def fake_start(model_name, model_dir=""):
        started.append(model_name)
        process = _DownloadProcess()
        procs.append(process)
        return process

    def fake_terminate(process):
        process.returncode = -1

    monkeypatch.setattr(controller_module, "start_model_download_process", fake_start)
    monkeypatch.setattr(
        controller_module, "terminate_model_download_process", fake_terminate
    )
    monkeypatch.setattr(
        controller_module, "release_model_download_process", lambda process: None
    )
    monkeypatch.setattr(
        controller_module, "model_download_process_error", lambda process: ""
    )
    monkeypatch.setattr(
        whisper_module,
        "find_cached_models",
        lambda _dir="": [
            name
            for name, process in zip(started, procs, strict=False)
            if process.poll() == 0
        ],
    )

    class Lease:
        transcriber = SimpleNamespace()

        def release(self):
            return None

    def acquire(settings, **_kwargs):
        loads.append(settings.model_size)
        return Lease()

    monkeypatch.setattr(controller, "_acquire_transcriber_runtime", acquire)
    controller._start_local_model_preload = lambda *a, **k: (
        DictationController._start_local_model_preload(controller, *a, **k)
    )
    controller._settings = replace(controller._settings, keep_onnx_model_loaded=keep)
    if keep:
        controller._start_local_model_preload()
    controller._start_retirement_download()
    assert _wait_until(lambda: procs), "the download did not start"
    return controller, app, started, procs, loads


def _save_in_settings(controller, **changes):
    """What the dialog does: write the file, then tell the controller."""
    store = controller._settings_store
    store.save(replace(store.load(), **changes))
    controller.on_settings_changed()


def _finish_downloads(controller, procs):
    for process in procs:
        process.release.set()
    controller.shutdown()


@pytest.mark.parametrize("keep", [False, True], ids=["keep off", "keep on"])
@pytest.mark.parametrize("saves", [1, 2], ids=["one save", "two saves"])
def test_an_unrelated_save_leaves_the_successor_download_running(
    tmp_path, monkeypatch, keep, saves
):
    controller, app, started, procs, loads = _a_successor_download_in_progress(
        tmp_path, monkeypatch, keep
    )

    for opacity in range(55, 55 + saves):
        _save_in_settings(
            controller, overlay_opacity_percent=opacity, keep_onnx_model_loaded=keep
        )
    time.sleep(0.6)

    assert [process.returncode for process in procs] == [None]
    procs[0].release.set()
    assert _wait_until(lambda: procs[0].returncode == 0)
    assert started == ["granite-speech-5.0-2b"]
    if keep:
        assert _wait_until(lambda: loads), "the model was never loaded"
    else:
        assert loads == []
    _finish_downloads(controller, procs)
    _ = app


@pytest.mark.parametrize("keep", [False, True], ids=["off to on", "on to off"])
def test_toggling_keep_onnx_loaded_leaves_the_successor_download_running(
    tmp_path, monkeypatch, keep
):
    controller, app, started, procs, loads = _a_successor_download_in_progress(
        tmp_path, monkeypatch, keep
    )

    _save_in_settings(controller, keep_onnx_model_loaded=not keep)
    time.sleep(0.6)

    assert [process.returncode for process in procs] == [None]
    procs[0].release.set()
    assert _wait_until(lambda: procs[0].returncode == 0)
    if not keep:
        # Turned on: the load this save asked for follows the download.
        assert _wait_until(lambda: loads), "the model was never loaded"
    time.sleep(0.3)
    assert started == ["granite-speech-5.0-2b"]
    if keep:
        # Turned off: the download finished, nothing is loaded.
        assert loads == []
    _finish_downloads(controller, procs)
    _ = app


@pytest.mark.parametrize(
    "changes",
    [
        {"model_size": "small"},
        {"offline_mode": True},
        {"engine": "openai"},
        {"model_dir": "other-models"},
    ],
    ids=["another model", "offline mode", "another engine", "another model dir"],
)
@pytest.mark.parametrize("keep", [False, True], ids=["keep off", "keep on"])
def test_a_save_that_changes_the_selection_ends_the_successor_download(
    tmp_path, monkeypatch, keep, changes
):
    controller, app, _started, procs, _loads = _a_successor_download_in_progress(
        tmp_path, monkeypatch, keep
    )
    if "model_dir" in changes:
        changes = {"model_dir": str(tmp_path / changes["model_dir"])}

    _save_in_settings(controller, keep_onnx_model_loaded=keep, **changes)

    assert _wait_until(lambda: procs[0].returncode == -1)
    _finish_downloads(controller, procs)
    _ = app


def test_quitting_ends_the_successor_download(tmp_path, monkeypatch):
    controller, app, _started, procs, _loads = _a_successor_download_in_progress(
        tmp_path, monkeypatch, keep=False
    )

    controller.shutdown()

    assert _wait_until(lambda: procs[0].returncode == -1)
    _ = app
