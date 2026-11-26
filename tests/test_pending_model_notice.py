"""The retired-model notice a start still owes the user, and the text for a
selected model that is not on this PC.

`pending_model_moves` lives in the settings file until the tray has shown the
balloon. Anything that saves a stale in-memory copy of the settings (the
overlay's own controls do) must neither bring a shown notice back nor drop one
that was not shown yet.
"""

import json
import logging
from dataclasses import replace

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
    controller._cancel_preload_generation(controller._preload_generation)
    controller._preload_generation += 1
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


def test_a_canceled_preload_generation_is_not_restarted_by_the_queued_download(
    tmp_path, monkeypatch
):
    """The user pressed Cancel on the overlay while the work was queued."""
    controller, app, started = _a_moved_granite_user(tmp_path, monkeypatch)
    gate = _occupy_the_preload_worker(controller)
    future = controller._start_retirement_download()
    controller._cancel_preload_generation(controller._preload_generation)
    gate.set()

    future.result(timeout=30)

    assert started == []
    controller.shutdown()
    _ = app
