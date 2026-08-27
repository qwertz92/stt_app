"""The download paths must actually go through the coordinator.

Isolated tests of the coordinator cannot catch the defect these commits exist
to fix: a caller that never acquires the slot. Reverting the wiring must fail
here, so each test drives the real call site with a recording coordinator.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

import pytest

from stt_app import model_download_coordinator as coordinator_module
from stt_app.model_download_coordinator import ACQUIRE_DOWNLOAD


@pytest.fixture(autouse=True)
def _use_the_real_prefetch(real_model_prefetch):
    """This file exists to assert the pre-fetch happens, so it must run."""
    return real_model_prefetch


@dataclass
class _RecordingCoordinator:
    acquired: list[tuple[str, str, bool]] = field(default_factory=list)
    released: list[tuple[str, str, bool]] = field(default_factory=list)
    outcome: str = ACQUIRE_DOWNLOAD

    def acquire(self, model_name, model_dir, *, explicit, cancel_check=None):
        self.acquired.append((model_name, model_dir, explicit))
        return self.outcome

    def release(self, model_name, model_dir, *, succeeded):
        self.released.append((model_name, model_dir, succeeded))

    def has_explicit_interest(self, model_name, model_dir=""):
        return False

    def active(self):
        return None


@pytest.fixture
def recording(monkeypatch):
    recorder = _RecordingCoordinator()
    monkeypatch.setattr(
        coordinator_module, "model_download_coordinator", lambda: recorder
    )
    return recorder


def test_a_transcriber_cache_miss_takes_the_slot(recording, monkeypatch):
    """A transcriber downloading from its own load path is a real download.
    For the Cohere/Granite family with keep-loaded off this is the *only*
    download path, so leaving it uncoordinated left the original bug intact."""
    from stt_app.transcriber import local_webgpu_asr

    monkeypatch.setattr(
        local_webgpu_asr, "resolve_cached_webgpu_model_path", lambda *a, **k: None
    )
    downloads: list[str] = []
    monkeypatch.setattr(
        local_webgpu_asr,
        "download_webgpu_model_snapshot",
        lambda model, model_dir="": downloads.append(model),
    )

    transcriber = local_webgpu_asr.LocalOnnxWebGpuTranscriber(
        model_size="cohere-transcribe-03-2026"
    )
    with pytest.raises(Exception):
        # The snapshot is still missing afterwards; we only care that the
        # download was coordinated.
        transcriber._ensure_snapshot()

    assert downloads == ["cohere-transcribe-03-2026"]
    assert recording.acquired == [("cohere-transcribe-03-2026", "", False)]
    assert recording.released == [("cohere-transcribe-03-2026", "", True)]


def test_the_onnx_asr_load_path_takes_the_slot(recording, monkeypatch):
    from stt_app.transcriber import local_faster_whisper, local_webgpu_asr
    from stt_app.transcriber.local_onnx_asr import LocalOnnxAsrTranscriber

    monkeypatch.setattr(
        local_webgpu_asr, "resolve_cached_webgpu_model_path", lambda *a, **k: None
    )
    monkeypatch.setattr(
        local_faster_whisper, "download_model_snapshot", lambda *a, **k: None
    )

    transcriber = LocalOnnxAsrTranscriber("parakeet-tdt-0.6b-v3")
    with pytest.raises(Exception):
        transcriber._resolve_model_path()

    assert recording.acquired == [("parakeet-tdt-0.6b-v3", "", False)]
    assert recording.released == [("parakeet-tdt-0.6b-v3", "", True)]


def test_a_joined_download_is_not_repeated(recording, monkeypatch):
    """When another caller finished the same model while we waited, the caller
    must skip its own download instead of fetching it a second time."""
    from stt_app.transcriber import local_webgpu_asr

    recording.outcome = "joined"
    monkeypatch.setattr(
        local_webgpu_asr, "resolve_cached_webgpu_model_path", lambda *a, **k: None
    )
    downloads: list[str] = []
    monkeypatch.setattr(
        local_webgpu_asr,
        "download_webgpu_model_snapshot",
        lambda model, model_dir="": downloads.append(model),
    )

    transcriber = local_webgpu_asr.LocalOnnxWebGpuTranscriber(
        model_size="granite-speech-4.1-2b"
    )
    with pytest.raises(Exception):
        transcriber._ensure_snapshot()

    assert downloads == []
    assert recording.released == []


def test_no_two_downloads_overlap_across_the_real_call_sites(monkeypatch):
    """End to end against the real coordinator: hold the slot, then prove a
    transcriber load blocks instead of starting a rival download."""
    from stt_app.model_download_coordinator import model_download_coordinator
    from stt_app.transcriber import local_webgpu_asr

    real = model_download_coordinator()
    real.acquire("holder", "", explicit=False)
    started: list[str] = []
    monkeypatch.setattr(
        local_webgpu_asr, "resolve_cached_webgpu_model_path", lambda *a, **k: None
    )
    monkeypatch.setattr(
        local_webgpu_asr,
        "download_webgpu_model_snapshot",
        lambda model, model_dir="": started.append(model),
    )

    transcriber = local_webgpu_asr.LocalOnnxWebGpuTranscriber(
        model_size="granite-4.0-1b-speech"
    )
    thread = threading.Thread(target=lambda: _swallow(transcriber), daemon=True)
    thread.start()
    thread.join(timeout=1.0)
    try:
        assert started == [], "a transcriber downloaded while the slot was held"
        assert thread.is_alive(), "the transcriber should be waiting for the slot"
    finally:
        real.release("holder", "", succeeded=False)
        thread.join(timeout=10)
    assert started == ["granite-4.0-1b-speech"]


def _swallow(transcriber) -> None:
    try:
        transcriber._ensure_snapshot()
    except Exception:
        pass


def test_main_installs_the_selectable_message_filter(monkeypatch):
    """Without the install call every message box is unselectable again, and
    the dialog_style unit tests would not notice."""
    from stt_app import main as main_module

    installed: list[object] = []
    monkeypatch.setattr(
        main_module,
        "install_selectable_message_text",
        lambda app: installed.append(app),
    )
    source = main_module.run.__code__.co_names
    assert "install_selectable_message_text" in source, (
        "main.run must install the app-wide selectable-text filter"
    )


def test_faster_whisper_downloads_through_the_slot(recording, monkeypatch):
    """WhisperModel downloads inside its own constructor via huggingface_hub,
    which no grep of this repo reveals — so the default engine bypassed the slot
    entirely."""
    from stt_app.transcriber import local_faster_whisper

    # The gate is the *download destination*, not find_cached_models: the
    # latter also accepts the default cache and a flat layout, so with a custom
    # Model Dir it said "cached" for a model WhisperModel would still fetch.
    monkeypatch.setattr(
        local_faster_whisper, "_has_valid_model_snapshot", lambda *_a, **_k: False
    )
    downloaded: list[str] = []
    monkeypatch.setattr(
        local_faster_whisper,
        "download_model_snapshot",
        lambda model, model_dir="": downloaded.append(model),
    )
    constructed: list[str] = []

    def fake_factory(model_size, **_kwargs):
        # The download must already have happened by the time WhisperModel runs.
        assert downloaded == [model_size], "constructed before the coordinated fetch"
        constructed.append(model_size)
        return object()

    transcriber = local_faster_whisper.LocalFasterWhisperTranscriber(
        model_size="large-v3", model_factory=fake_factory
    )
    transcriber.preload_model()

    assert constructed == ["large-v3"]
    assert recording.acquired == [("large-v3", "", False)]
    assert recording.released == [("large-v3", "", True)]


def test_an_offline_transcriber_does_not_take_the_slot(recording, monkeypatch):
    from stt_app.transcriber import local_faster_whisper

    monkeypatch.setattr(
        local_faster_whisper, "_has_valid_model_snapshot", lambda *_a, **_k: False
    )
    transcriber = local_faster_whisper.LocalFasterWhisperTranscriber(
        model_size="small", offline_mode=True, model_factory=lambda *a, **k: object()
    )
    transcriber.preload_model()
    assert recording.acquired == []


def test_nemotron_does_not_load_on_the_calling_thread():
    """start_stream runs on the Qt main thread; loading there could block on the
    download slot with no progress and no way out, freezing the whole UI."""
    import inspect

    from stt_app.transcriber import local_nemotron

    source = inspect.getsource(local_nemotron.LocalNemotronTranscriber.start_stream)
    assert "_ensure_model" not in source, (
        "start_stream must not load the model on the caller's thread"
    )
    worker = inspect.getsource(local_nemotron.LocalNemotronTranscriber._stream_worker)
    assert "_ensure_model" in worker


def test_shutdown_stops_anyone_waiting_for_the_slot():
    """At quit the dialog shutdown releases the slot before the controller
    stops, so without this a waiter would start a fresh multi-gigabyte download
    on a non-daemon thread the interpreter then joins at exit."""
    import time

    from stt_app.model_download_coordinator import (
        ModelDownloadCanceled,
        ModelDownloadCoordinator,
        request_download_shutdown,
        reset_download_shutdown_for_tests,
    )

    reset_download_shutdown_for_tests()
    coordinator = ModelDownloadCoordinator()
    coordinator.acquire("holder", "", explicit=True)
    outcome: list[str] = []

    def waiter() -> None:
        try:
            outcome.append(coordinator.acquire("other", "", explicit=False))
        except ModelDownloadCanceled:
            outcome.append("canceled")

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    time.sleep(0.3)
    assert outcome == []
    try:
        request_download_shutdown()
        thread.join(timeout=5)
        assert outcome == ["canceled"]
    finally:
        reset_download_shutdown_for_tests()


def test_message_flags_are_added_not_replaced():
    """Replacing them stripped LinksAccessibleByMouse, which would have killed
    the links in the update dialogs."""
    from PySide6 import QtCore, QtWidgets

    from stt_app.dialog_style import make_message_text_selectable

    _ = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    box = QtWidgets.QMessageBox()
    box.setTextInteractionFlags(QtCore.Qt.LinksAccessibleByMouse)
    make_message_text_selectable(box)
    flags = box.textInteractionFlags()
    assert flags & QtCore.Qt.LinksAccessibleByMouse
    assert flags & QtCore.Qt.TextSelectableByMouse


def test_several_waiters_for_one_model_all_skip_their_download():
    """Only the first waiter used to consume the completion marker, so a second
    one re-downloaded a model that had just finished."""
    import time

    from stt_app.model_download_coordinator import (
        ACQUIRE_JOINED,
        ModelDownloadCoordinator,
    )

    coordinator = ModelDownloadCoordinator()
    coordinator.acquire("m", "", explicit=False)
    outcomes: list[str] = []
    threads = [
        threading.Thread(
            target=lambda: outcomes.append(coordinator.acquire("m", "", explicit=True)),
            daemon=True,
        )
        for _ in range(3)
    ]
    for thread in threads:
        thread.start()
    time.sleep(0.3)
    coordinator.release("m", "", succeeded=True)
    for thread in threads:
        thread.join(timeout=5)

    assert outcomes == [ACQUIRE_JOINED] * 3


def test_cancelling_while_queued_does_not_strand_the_entry(monkeypatch, tmp_path):
    """Cancelling while an entry waits for the slot used to leave it claimed:
    the tab then showed it as downloading forever and silently refused to queue
    it again for the rest of the session."""
    from stt_app.model_download_coordinator import model_download_coordinator
    from stt_app.settings_dialog_local import _LocalModelsMixin

    coordinator = model_download_coordinator()
    coordinator.acquire("someone-else", "", explicit=False)

    dialog = _LocalModelsMixin.__new__(_LocalModelsMixin)
    dialog._local_model_download_lock = threading.RLock()
    dialog._local_model_download_active = None
    dialog._local_model_download_claimed = ("small", "")
    dialog._local_model_download_cancel_event = threading.Event()
    dialog._local_model_download_cancel_event.set()
    coordinator.register_explicit_interest("small", "")

    try:
        status, _detail, _f, _b = _LocalModelsMixin._download_local_model_in_subprocess(
            dialog, "small", ""
        )
        assert status == "canceled"
        assert dialog._local_model_download_claimed is None, "entry stayed claimed"
        assert coordinator.has_explicit_interest("small", "") is False
    finally:
        coordinator.release("someone-else", "", succeeded=False)


def test_a_crashing_download_queue_does_not_wedge_the_tab(monkeypatch):
    """Without a finally the worker thread died holding the queue: interest
    stayed registered, `_worker_running` stayed True forever, and every later
    Download click appended to a queue nothing would ever run."""
    from stt_app.model_download_coordinator import model_download_coordinator
    from stt_app.settings_dialog_local import _LocalModelsMixin

    coordinator = model_download_coordinator()
    dialog = _LocalModelsMixin.__new__(_LocalModelsMixin)
    dialog._local_model_download_lock = threading.RLock()
    dialog._local_model_download_queue = [("medium", "")]
    dialog._local_model_download_active = None
    dialog._local_model_download_claimed = None
    dialog._local_model_download_worker_running = True
    coordinator.register_explicit_interest("medium", "")

    monkeypatch.setattr(
        _LocalModelsMixin,
        "_drive_local_model_download_queue",
        lambda self, token: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    emitted: list[tuple] = []
    monkeypatch.setattr(
        "stt_app.settings_dialog_local._emit_background_signal",
        lambda *args: emitted.append(args),
    )

    with pytest.raises(RuntimeError):
        _LocalModelsMixin._run_local_model_download_queue(dialog, 1)

    assert dialog._local_model_download_worker_running is False
    assert dialog._local_model_download_queue == []
    assert coordinator.has_explicit_interest("medium", "") is False
    assert emitted, "the user must be told the queue stopped"


def test_a_custom_model_dir_does_not_skip_the_slot(recording, monkeypatch, tmp_path):
    """`find_cached_models` also accepts the default cache and a flat layout, so
    gating on it answered "cached" for a model WhisperModel would still fetch
    into the configured Model Dir — uncoordinated. The gate must ask about the
    destination the constructor actually resolves."""
    from stt_app.transcriber import local_faster_whisper

    # The model exists somewhere find_cached_models looks...
    monkeypatch.setattr(
        local_faster_whisper, "find_cached_models", lambda *_a, **_k: ["small"]
    )
    # ...but not in the configured Model Dir, which is where it would land.
    downloaded: list[str] = []
    monkeypatch.setattr(
        local_faster_whisper,
        "download_model_snapshot",
        lambda model, model_dir="": downloaded.append(model),
    )

    transcriber = local_faster_whisper.LocalFasterWhisperTranscriber(
        model_size="small",
        model_dir=str(tmp_path),
        model_factory=lambda *a, **k: object(),
    )
    transcriber.preload_model()

    assert downloaded == ["small"], "the empty custom Model Dir must still download"
    assert recording.acquired == [("small", str(tmp_path), False)]


def test_a_model_present_in_the_custom_dir_is_not_refetched(
    recording, monkeypatch, tmp_path
):
    """The mirror case: over-narrowing would re-download a model the user has."""
    from stt_app.config import MODEL_REPO_MAP
    from stt_app.transcriber import local_faster_whisper

    repo = MODEL_REPO_MAP["small"].replace("/", "--")
    snapshot = tmp_path / f"models--{repo}" / "snapshots" / "abc123"
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}", encoding="utf-8")
    (snapshot / "model.bin").write_bytes(b"x")

    downloaded: list[str] = []
    monkeypatch.setattr(
        local_faster_whisper,
        "download_model_snapshot",
        lambda model, model_dir="": downloaded.append(model),
    )
    transcriber = local_faster_whisper.LocalFasterWhisperTranscriber(
        model_size="small",
        model_dir=str(tmp_path),
        model_factory=lambda *a, **k: object(),
    )
    transcriber.preload_model()

    assert downloaded == [], "a model already in the Model Dir must not be refetched"
    assert recording.acquired == []


class _BarStub:
    """Minimal stand-in for the Local tab's progress widgets."""

    def __init__(self):
        self.visible = False
        self.text = ""
        self.range = None
        self.value = None
        self.fmt = ""

    # progress bar
    def setVisible(self, value):
        self.visible = bool(value)

    def setRange(self, low, high):
        self.range = (low, high)

    def setValue(self, value):
        self.value = value

    def setFormat(self, fmt):
        self.fmt = fmt

    # label
    def setText(self, text):
        self.text = text

    def setStyleSheet(self, _sheet):
        pass


def _progress_dialog(measured):
    from stt_app.model_download_progress import ModelDownloadSpeedTracker
    from stt_app.settings_dialog_local import _LocalModelsMixin

    dialog = _LocalModelsMixin.__new__(_LocalModelsMixin)
    dialog._local_model_download_lock = threading.RLock()
    dialog._local_model_download_queue = []
    dialog._local_model_download_worker_running = True
    dialog._local_model_download_speed_tracker = ModelDownloadSpeedTracker()
    dialog.local_model_download_progress_bar = _BarStub()
    dialog.local_models_action_label = _BarStub()
    dialog.model_dir_edit = _BarStub()
    dialog.model_dir_edit.text = lambda: ""
    dialog._preload_downloading_model = lambda: None
    dialog._local_model_download_bar_shown = False
    return dialog


def test_the_progress_bar_never_measures_a_merely_queued_model(monkeypatch):
    """A claimed entry is folded into the snapshot for the list and the
    duplicate check. Measuring it reports another model's directory growth as
    its progress and invents a percentage for a download that has not started."""
    measured: list[str] = []
    monkeypatch.setattr(
        "stt_app.settings_dialog_local._facade",
        lambda: type(
            "F",
            (),
            {
                "estimate_cached_model_bytes": staticmethod(
                    lambda name, model_dir: measured.append(name) or 0
                )
            },
        ),
    )
    from stt_app.settings_dialog_local import _LocalModelsMixin

    dialog = _progress_dialog(measured)
    dialog._local_model_download_active = None
    dialog._local_model_download_claimed = ("queued-model", "")

    _LocalModelsMixin._refresh_local_model_download_progress(dialog)

    assert measured == [], "measured a model that is only waiting for the slot"
    assert dialog.local_model_download_progress_bar.visible is False


def test_the_progress_bar_hides_even_while_settings_is_closed():
    """Qt reports a child of a hidden dialog as not visible, and this dialog
    persists hidden for the app lifetime, so asking the widget meant a download
    ending while Settings was closed never cleared its bar."""
    from stt_app.settings_dialog_local import _LocalModelsMixin

    dialog = _progress_dialog([])
    dialog._local_model_download_active = None
    dialog._local_model_download_claimed = None
    dialog._local_model_download_bar_shown = True
    # The widget reports itself invisible, exactly as Qt would here.
    dialog.local_model_download_progress_bar.visible = False
    dialog.local_models_action_label.text = "Downloading 'x': approx. 27%"

    _LocalModelsMixin._hide_local_model_download_progress(dialog)

    assert dialog._local_model_download_bar_shown is False
    assert dialog.local_models_action_label.text == "", "stale status line kept"
