"""The download paths must actually go through the coordinator.

Isolated tests of the coordinator cannot catch the defect these commits exist
to fix: a caller that never acquires the slot. Reverting the wiring must fail
here, so each test drives the real call site with a recording coordinator.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import pytest

from stt_app import model_download_coordinator as coordinator_module
from stt_app.model_download_coordinator import ACQUIRE_DOWNLOAD
from stt_app.settings_dialog_local import (
    _CLEANUP_KEPT,
    _CLEANUP_RAN,
    _CLEANUP_SKIPPED,
    _CleanupOutcome,
)


def _private_slot(monkeypatch, coordinator, *modules):
    """Point `modules` at their own download slot for one test.

    The real one is a process-wide singleton, so a test that parks it and then
    fails an assertion before its release wedges every later test in this file
    -- observed as a whole-file hang with no failure reported.
    """
    for module in modules:
        monkeypatch.setattr(module, "model_download_coordinator", lambda: coordinator)
    return coordinator


def _wait_until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    raise AssertionError("the condition never became true")


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
        status, _detail, cleanup = (
            _LocalModelsMixin._download_local_model_in_subprocess(dialog, "small", "")
        )
        assert status == "canceled"
        # It never held the slot, so it may not touch the partial files -- and
        # the drain must not report the cleanup it skipped as one that ran.
        assert cleanup == _CleanupOutcome(_CLEANUP_SKIPPED)
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
    # Left by a Cancel this drain consumed nothing of; the arm clears it.
    dialog._local_model_download_removed_by_cancel = ["stale"]
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
    assert dialog._local_model_download_removed_by_cancel == []
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


def test_a_finished_local_tab_download_lets_a_waiter_join(monkeypatch):
    """`succeeded=` decides whether a parked waiter re-downloads.

    It used to read a `result` tuple that was written once at the top of the
    method and never reassigned, because the one place that would have set it
    returned the worker call directly -- so the flag was a constant False. Its
    only consumer is the completion counter behind `ACQUIRE_JOINED`, so the
    app's main explicit download path never recorded completion and a waiting
    preload re-ran `snapshot_download` on an already-complete model.
    """
    import stt_app.settings_dialog_local as local_module
    from stt_app.model_download_coordinator import (
        ACQUIRE_JOINED,
        ModelDownloadCoordinator,
    )
    from stt_app.settings_dialog_local import _LocalModelsMixin

    coordinator = _private_slot(monkeypatch, ModelDownloadCoordinator(), local_module)
    dialog = _LocalModelsMixin.__new__(_LocalModelsMixin)
    dialog._local_model_download_lock = threading.RLock()
    dialog._local_model_download_active = None
    dialog._local_model_download_claimed = ("small", "")
    dialog._local_model_download_cancel_event = threading.Event()
    dialog._run_download_worker = lambda *_args: ("success", "", _CleanupOutcome())
    coordinator.register_explicit_interest("small", "")

    status, _detail, _cleanup = _LocalModelsMixin._download_local_model_in_subprocess(
        dialog, "small", ""
    )

    assert status == "success"
    # The waiter arrives after the fact, which is what the counter is for.
    assert coordinator.acquire("small", "", explicit=False) == ACQUIRE_DOWNLOAD
    coordinator.release("small", "", succeeded=False)

    outcome: list[str] = []
    coordinator.acquire("blocker", "", explicit=False)

    def _park():
        outcome.append(coordinator.acquire("small", "", explicit=False))

    waiter = threading.Thread(target=_park, daemon=True)
    waiter.start()
    _wait_until(lambda: coordinator.has_waiting_download("small", ""))
    dialog._local_model_download_claimed = ("small", "")
    coordinator.register_explicit_interest("small", "")
    coordinator.release("blocker", "", succeeded=False)
    _LocalModelsMixin._download_local_model_in_subprocess(dialog, "small", "")
    waiter.join(timeout=5)

    assert outcome == [ACQUIRE_JOINED], (
        f"the waiter downloaded again instead of joining: {outcome}"
    )


def test_cancelling_keeps_the_partials_a_parked_waiter_will_resume_from(monkeypatch):
    """The mirror of the preload guard, for the direction that had none.

    The preload path checks `has_explicit_interest` before deleting partials.
    The Local tab's cancel deleted them unconditionally -- and the waiter it
    robs is typically a *preload*, whose interest is implicit and therefore
    invisible to that check. Measured before this: cancel a 2.5 GB download
    with a preload parked on the same model, and the preload restarts from
    zero seconds later.
    """
    import stt_app.settings_dialog_local as local_module
    from stt_app.model_download_coordinator import ModelDownloadCoordinator
    from stt_app.settings_dialog_local import _LocalModelsMixin

    coordinator = _private_slot(monkeypatch, ModelDownloadCoordinator(), local_module)
    dialog = _LocalModelsMixin.__new__(_LocalModelsMixin)
    coordinator.acquire("large-v3", "", explicit=True)
    cleanups: list[tuple[str, str]] = []

    class _Facade:
        @staticmethod
        def cleanup_incomplete_model_download(model_name, model_dir):
            cleanups.append((model_name, model_dir))
            return 3, 4096

    # Recorded, not left to the real cleanup: with nothing on disk for this
    # model the real one also removes nothing, so an assertion on the counts
    # alone was satisfied whether the guard held or not -- a mutation that
    # deleted the guard outright still passed. The state is asserted too: a
    # kept set of partials is not the same fact as a disk that held none, and
    # the drain's closing sentence says which of the two happened.
    monkeypatch.setattr(local_module, "_facade", lambda: _Facade())

    def _park():
        try:
            coordinator.acquire("large-v3", "", explicit=False)
        except Exception:
            return
        coordinator.release("large-v3", "", succeeded=False)

    waiter = threading.Thread(target=_park, daemon=True)
    waiter.start()
    try:
        _wait_until(lambda: coordinator.has_waiting_download("large-v3", ""))

        removed = _LocalModelsMixin._cleanup_unless_awaited(dialog, "large-v3", "")

        assert cleanups == [], "the waiter's partial files were deleted"
        assert removed == _CleanupOutcome(_CLEANUP_KEPT), (
            f"the cancel reported {removed}"
        )
    finally:
        coordinator.release("large-v3", "", succeeded=False)
        waiter.join(timeout=5)


def test_the_waiter_registry_empties_once_nobody_is_parked():
    """A registration that outlives its waiter never lets the cleanup run.

    `has_waiting_download` gates the partial-file cleanup, so a count left
    behind by a waiter that has long since finished makes every later cancel
    keep unusable `*.incomplete` files forever -- the failure the cleanup
    exists to prevent, arrived at from the other side.
    """
    from stt_app.model_download_coordinator import ModelDownloadCoordinator

    coordinator = ModelDownloadCoordinator()
    coordinator.acquire("blocker", "", explicit=False)

    def _park() -> None:
        coordinator.acquire("m", "", explicit=False)
        coordinator.release("m", "", succeeded=False)

    waiter = threading.Thread(target=_park, daemon=True)
    waiter.start()
    _wait_until(lambda: coordinator.has_waiting_download("m", ""))
    coordinator.release("blocker", "", succeeded=False)
    waiter.join(timeout=5)
    assert not waiter.is_alive(), "the waiter never got the slot"

    assert coordinator.has_waiting_download("m", "") is False, (
        "the finished waiter is still registered"
    )
    # The uncontended path registers too, and must give it straight back.
    coordinator.acquire("m", "", explicit=False)
    try:
        assert coordinator.has_waiting_download("m", "") is False, (
            "the caller holding the slot is reported as waiting for it"
        )
    finally:
        coordinator.release("m", "", succeeded=False)


def test_cancelling_with_nobody_waiting_still_removes_the_partials():
    """The other direction: an abandoned partial download must be cleaned up.

    A guard that never lets the cleanup run would leave unusable
    `*.incomplete` files behind after every cancel.
    """
    from stt_app.settings_dialog_local import _LocalModelsMixin

    dialog = _LocalModelsMixin.__new__(_LocalModelsMixin)
    calls: list[tuple[str, str]] = []

    class _Facade:
        @staticmethod
        def cleanup_incomplete_model_download(model_name, model_dir):
            calls.append((model_name, model_dir))
            return 3, 4096

    import stt_app.settings_dialog_local as local_module

    original = local_module._facade
    local_module._facade = lambda: _Facade()
    try:
        removed = _LocalModelsMixin._cleanup_unless_awaited(dialog, "small", "")
    finally:
        local_module._facade = original

    assert removed == _CleanupOutcome(_CLEANUP_RAN, 3, 4096)
    assert calls == [("small", "")]


@pytest.mark.parametrize(
    ("label", "slot_holder", "expect_waiting"),
    [
        ("another model owns the slot", "large-v3", True),
        ("our own model owns the slot", "medium", False),
        ("the slot is free", None, False),
    ],
)
def test_the_overlay_never_invents_progress_for_a_queued_preload(
    label, slot_holder, expect_waiting, monkeypatch
):
    """Progress is directory growth, so a queued preload has none to show.

    Two gates for "I am not really downloading" existed -- queued behind
    another *preload*, and waiting for another *process* -- and neither
    covered waiting for the in-process download slot. The phase is already
    `download` there, because `_download_model_for_preload` sets it and then
    blocks inside `acquire`. Measured before this: a frozen "approx. 60%
    (919/1531 MB), measuring speed" for as long as the other download took,
    from partial bytes an earlier cancelled attempt had left behind.
    """
    import stt_app.controller as controller_module
    from stt_app.controller import _PRELOAD_PHASE_DOWNLOAD, DictationController
    from stt_app.model_download_coordinator import ModelDownloadCoordinator
    from stt_app.settings_store import AppSettings

    # A private slot, not the process-wide one: this test parks the slot for
    # the length of an assertion, and the singleton is shared with every other
    # test in this file.
    coordinator = ModelDownloadCoordinator()
    monkeypatch.setattr(
        controller_module, "model_download_coordinator", lambda: coordinator
    )
    controller = DictationController.__new__(DictationController)
    controller._preload_result_lock = threading.RLock()
    controller._preload_generation = 7
    controller._preload_phase = (7, _PRELOAD_PHASE_DOWNLOAD)
    controller._preload_target_model = "medium"
    controller._settings = AppSettings(model_size="medium")
    controller._insert_action_text = ""  # nothing pending: "Use Cancel"

    if slot_holder is not None:
        coordinator.acquire(slot_holder, "", explicit=False)
    try:
        waits = DictationController._preload_waits_for_another_model(
            controller, "medium", ""
        )
        word = DictationController._preload_phase_word(controller)
        # Only the waiting branch returns without measuring the cache, so it
        # is the only one this bare controller can render.
        detail = (
            DictationController._preload_progress_detail(controller)
            if waits
            else ""
        )
    finally:
        if slot_holder is not None:
            coordinator.release(slot_holder, "", succeeded=False)

    assert waits is expect_waiting, label
    if expect_waiting:
        assert "Waiting for another model download" in detail, label
        assert "%" not in detail, f"{label}: a percentage was invented: {detail}"
        assert word == "waiting for another model to finish", label
    else:
        assert word == "downloading", label


class _DrainLabel:
    def __init__(self):
        self.texts: list[str] = []

    def setStyleSheet(self, _style):
        pass

    def setText(self, text):
        self.texts.append(text)


class _DrainTimer:
    def start(self):
        pass

    def stop(self):
        pass


def _through_the_slot(name, model_dir, status):
    """What the real `_download_local_model_in_subprocess` does around a fetch:
    take the slot and give it back, which is what drops the explicit interest
    the enqueue registered."""
    from stt_app.model_download_coordinator import model_download_coordinator

    coordinator = model_download_coordinator()
    # The enqueue registered the interest; the real path says so, or the
    # release would drop one of two registrations and leave the other behind.
    coordinator.acquire(
        name, model_dir, explicit=True, interest_already_registered=True
    )
    coordinator.release(name, model_dir, succeeded=status == "success")
    # A canceled download always ran the cleanup in the real worker; only the
    # `ModelDownloadCanceled` arm returns without touching the disk.
    cleanup = (
        _CleanupOutcome(_CLEANUP_RAN) if status == "canceled" else _CleanupOutcome()
    )
    return (status, "", cleanup)


def _draining_dialog(monkeypatch):
    """A Local tab whose worker has not yet observed the Cancel it was sent."""
    from stt_app.settings_dialog_local import _LocalModelsMixin

    dialog = _LocalModelsMixin.__new__(_LocalModelsMixin)
    dialog._local_model_download_lock = threading.RLock()
    dialog._local_model_download_queue = []
    dialog._local_model_download_active = None
    dialog._local_model_download_claimed = None
    dialog._local_model_download_completed_names = set()
    dialog._local_model_download_worker_running = True
    dialog._local_model_download_cancel_event = threading.Event()
    dialog._local_model_download_cancel_event.set()
    dialog._local_model_download_removed_by_cancel = []
    dialog._local_model_download_worker_token = 1
    dialog._local_model_download_process = None
    dialog.model_dir_edit = type("Edit", (), {"text": lambda self: ""})()
    dialog.local_models_action_label = _DrainLabel()
    dialog._local_model_download_progress_timer = _DrainTimer()
    dialog._refresh_local_models_list = lambda: None
    dialog._update_local_model_actions = lambda: None
    dialog._refresh_local_model_download_progress = lambda: None
    emitted: list[tuple] = []
    monkeypatch.setattr(
        "stt_app.settings_dialog_local._emit_background_signal",
        lambda *args: emitted.append(args),
    )
    return dialog, emitted


def test_a_crashed_drain_drops_the_names_its_cancel_removed(monkeypatch):
    """`_local_model_download_removed_by_cancel` is drain-scoped and only
    `_consume_cancel_locked` empties it. The crash arm reset the queue, the
    active and claimed entries and the running flag, and left the list, so
    the next drain's first Cancel reported the crashed drain's removed models
    as its own: drain 2 queued only `medium` and `large-v3`, and its summary
    named `base` and `small` before them."""
    from stt_app import settings_dialog_local as local_module
    from stt_app.model_download_coordinator import ModelDownloadCoordinator
    from stt_app.settings_dialog_local import _LocalModelsMixin

    _private_slot(monkeypatch, ModelDownloadCoordinator(), local_module)
    monkeypatch.setattr(
        "stt_app.settings_dialog_local.terminate_model_download_process",
        lambda process: None,
    )
    dialog, emitted = _draining_dialog(monkeypatch)
    dialog._local_model_download_cancel_event.clear()

    def _download(self, name, model_dir):
        # Cancel lands while this entry downloads: the entries queued behind
        # it are removed and recorded for the summary.
        _LocalModelsMixin._cancel_local_model_downloads(self)
        if name == "tiny":
            raise ZeroDivisionError("the download queue crashed")
        return ("canceled", "", _CleanupOutcome(_CLEANUP_SKIPPED))

    monkeypatch.setattr(
        _LocalModelsMixin, "_download_local_model_in_subprocess", _download
    )

    _LocalModelsMixin._start_local_model_download(dialog, ["tiny", "base", "small"])
    with pytest.raises(ZeroDivisionError):
        _LocalModelsMixin._run_local_model_download_queue(dialog, 1)
    assert dialog._local_model_download_worker_running is False

    # What `_start_local_model_download` does for a fresh worker, minus the
    # thread: mark it running, clear the event, queue, and drain.
    dialog._local_model_download_worker_running = True
    dialog._local_model_download_cancel_event.clear()
    _LocalModelsMixin._start_local_model_download(dialog, ["medium", "large-v3"])
    _LocalModelsMixin._drive_local_model_download_queue(dialog, 2)

    finished = [e for e in emitted if e[1] == "local_model_download_finished"]
    summary = finished[-1][4]
    assert "medium" in summary
    assert "large-v3" in summary
    assert "base" not in summary, summary
    assert "small" not in summary, summary


def test_a_download_queued_during_a_cancel_drain_still_runs(monkeypatch):
    """Cancel, then Download: the tab said "Queued for download: small" and the
    old worker's drain then discarded it, silently, when it observed the cancel."""
    from stt_app.model_download_coordinator import model_download_coordinator
    from stt_app.settings_dialog_local import _LocalModelsMixin

    dialog, emitted = _draining_dialog(monkeypatch)
    downloads: list[str] = []
    monkeypatch.setattr(
        _LocalModelsMixin,
        "_download_local_model_in_subprocess",
        lambda self, name, model_dir: downloads.append(name)
        or _through_the_slot(name, model_dir, "success"),
    )

    _LocalModelsMixin._start_local_model_download(dialog, ["small"])
    assert dialog.local_models_action_label.texts[-1] == "Queued for download: small"
    _LocalModelsMixin._drive_local_model_download_queue(dialog, 1)

    assert downloads == ["small"]
    assert dialog._local_model_download_worker_running is False
    assert dialog._local_model_download_cancel_event.is_set() is False
    assert model_download_coordinator().has_explicit_interest("small", "") is False
    finished = [e for e in emitted if e[1] == "local_model_download_finished"]
    assert finished[-1][3] is True, finished
    assert "small" in finished[-1][4]


def test_a_download_queued_while_the_active_one_is_being_canceled_runs_next(
    monkeypatch,
):
    """The Download click lands while the canceled subprocess is still dying."""
    from stt_app.settings_dialog_local import _LocalModelsMixin

    dialog, emitted = _draining_dialog(monkeypatch)
    dialog._local_model_download_cancel_event.clear()
    dialog._local_model_download_queue = [("medium", "")]
    downloads: list[str] = []

    def _download(self, name, model_dir):
        downloads.append(name)
        if name == "medium":
            # The user presses Cancel, then Download on another model, while
            # this one's process is being terminated.
            _LocalModelsMixin._cancel_local_model_downloads(self)
            _LocalModelsMixin._start_local_model_download(self, ["small"])
            return ("canceled", "", _CleanupOutcome(_CLEANUP_RAN))
        return _through_the_slot(name, model_dir, "success")

    monkeypatch.setattr(
        _LocalModelsMixin, "_download_local_model_in_subprocess", _download
    )
    monkeypatch.setattr(
        "stt_app.settings_dialog_local.terminate_model_download_process",
        lambda process: None,
    )

    _LocalModelsMixin._drive_local_model_download_queue(dialog, 1)

    assert downloads == ["medium", "small"]
    finished = [e for e in emitted if e[1] == "local_model_download_finished"]
    # A download was killed, so the Cancel is the headline; what the user
    # queued afterwards is listed after it, not reported as a plain success.
    assert finished[-1][3] is False
    assert finished[-1][4] == (
        "Download canceled. No incomplete files remained. "
        "Canceled: medium. Then downloaded: small."
    )


def test_a_second_cancel_during_the_drain_still_stops_everything(monkeypatch):
    from stt_app.settings_dialog_local import _LocalModelsMixin

    dialog, emitted = _draining_dialog(monkeypatch)
    dialog._local_model_download_cancel_event.clear()
    dialog._local_model_download_queue = [("medium", "")]
    downloads: list[str] = []

    def _download(self, name, model_dir):
        downloads.append(name)
        _LocalModelsMixin._cancel_local_model_downloads(self)
        _LocalModelsMixin._start_local_model_download(self, ["small"])
        _LocalModelsMixin._cancel_local_model_downloads(self)
        return ("canceled", "", _CleanupOutcome(_CLEANUP_RAN))

    monkeypatch.setattr(
        _LocalModelsMixin, "_download_local_model_in_subprocess", _download
    )
    monkeypatch.setattr(
        "stt_app.settings_dialog_local.terminate_model_download_process",
        lambda process: None,
    )

    _LocalModelsMixin._drive_local_model_download_queue(dialog, 1)

    assert downloads == ["medium"]
    assert dialog._local_model_download_worker_running is False
    # The worker consumed the cancel on its way out; a set event left behind
    # would make the next worker start with a phantom cancel and report the
    # user's fresh download as resumed after "an earlier download".
    assert dialog._local_model_download_cancel_event.is_set() is False
    finished = [e for e in emitted if e[1] == "local_model_download_finished"]
    assert finished[-1][3] is False
    assert finished[-1][4].startswith("Download canceled.")


def test_a_cancel_after_the_first_model_finished_is_reported_as_a_cancel(
    monkeypatch,
):
    """Queue [medium, small]; medium finishes; the user cancels before the
    worker looks at the queue again. `resumed = canceled and bool(successes)`
    read medium's success as the drain having resumed after the cancel and
    reported "Downloaded: medium (an earlier download was canceled)" in the
    success colour, while small -- the model the user was waiting for -- was
    dropped without a word."""
    from stt_app.settings_dialog_local import _LocalModelsMixin

    dialog, emitted = _draining_dialog(monkeypatch)
    dialog._local_model_download_cancel_event.clear()
    dialog._local_model_download_queue = [("medium", ""), ("small", "")]
    downloads: list[str] = []

    def _download(self, name, model_dir):
        downloads.append(name)
        result = _through_the_slot(name, model_dir, "success")
        # Lands after the subprocess exited 0 and before the loop top.
        _LocalModelsMixin._cancel_local_model_downloads(self)
        return result

    monkeypatch.setattr(
        _LocalModelsMixin, "_download_local_model_in_subprocess", _download
    )
    monkeypatch.setattr(
        "stt_app.settings_dialog_local.terminate_model_download_process",
        lambda process: None,
    )

    _LocalModelsMixin._drive_local_model_download_queue(dialog, 1)

    assert downloads == ["medium"]
    finished = [e for e in emitted if e[1] == "local_model_download_finished"]
    assert finished[-1][3] is False
    # The Cancel removed `small` from the queue; the summary says so, where
    # it used to name only the download a Cancel killed. No download was
    # interrupted, so there is nothing to say about incomplete files -- "No
    # incomplete files remained." was a claim about a disk nobody read.
    assert finished[-1][4] == (
        "Download canceled. Downloaded: medium. Then removed from the queue: small."
    )
    assert dialog._local_model_download_cancel_event.is_set() is False


def test_a_cancel_that_kills_the_second_model_names_it_and_keeps_the_cleanup(
    monkeypatch,
):
    """The commonest Cancel there is: two models queued, the first done, the
    second killed. The cleanup numbers and the killed model's name were lost
    with the success-coloured report."""
    from stt_app.settings_dialog_local import _LocalModelsMixin

    dialog, emitted = _draining_dialog(monkeypatch)
    dialog._local_model_download_cancel_event.clear()
    dialog._local_model_download_queue = [("medium", ""), ("small", "")]

    def _download(self, name, model_dir):
        if name == "medium":
            return _through_the_slot(name, model_dir, "success")
        _LocalModelsMixin._cancel_local_model_downloads(self)
        _through_the_slot(name, model_dir, "canceled")
        return ("canceled", "", _CleanupOutcome(_CLEANUP_RAN, 3, 1_500_000))

    monkeypatch.setattr(
        _LocalModelsMixin, "_download_local_model_in_subprocess", _download
    )
    monkeypatch.setattr(
        "stt_app.settings_dialog_local.terminate_model_download_process",
        lambda process: None,
    )

    _LocalModelsMixin._drive_local_model_download_queue(dialog, 1)

    finished = [e for e in emitted if e[1] == "local_model_download_finished"]
    assert finished[-1][3] is False
    assert finished[-1][4] == (
        "Download canceled. Removed 3 incomplete files (1.5 MB). "
        "Downloaded: medium. Then canceled: small."
    )


def test_a_cancel_that_kills_a_download_also_names_the_queue_it_emptied(monkeypatch):
    """One Cancel, three models: the running one is killed and the two
    behind it are removed from the queue. The killed model is reported by
    the "canceled" status its download returns, the removed ones by the
    same consumption of the cancel event -- both, in that order."""
    from stt_app.settings_dialog_local import _LocalModelsMixin

    dialog, emitted = _draining_dialog(monkeypatch)
    dialog._local_model_download_cancel_event.clear()
    dialog._local_model_download_queue = [("medium", ""), ("small", ""), ("tiny", "")]

    def _download(self, name, model_dir):
        _LocalModelsMixin._cancel_local_model_downloads(self)
        _through_the_slot(name, model_dir, "canceled")
        return ("canceled", "", _CleanupOutcome(_CLEANUP_RAN))

    monkeypatch.setattr(
        _LocalModelsMixin, "_download_local_model_in_subprocess", _download
    )
    monkeypatch.setattr(
        "stt_app.settings_dialog_local.terminate_model_download_process",
        lambda process: None,
    )

    _LocalModelsMixin._drive_local_model_download_queue(dialog, 1)

    finished = [e for e in emitted if e[1] == "local_model_download_finished"]
    assert finished[-1][3] is False
    assert finished[-1][4] == (
        "Download canceled. No incomplete files remained. "
        "Canceled: medium. Then removed from the queue: small, tiny."
    )


def test_work_before_and_after_a_cancel_is_listed_on_its_side_of_it(monkeypatch):
    from stt_app.settings_dialog_local import _LocalModelsMixin

    dialog, emitted = _draining_dialog(monkeypatch)
    dialog._local_model_download_cancel_event.clear()
    dialog._local_model_download_queue = [
        ("tiny", ""),
        ("medium", ""),
        ("small", ""),
    ]

    def _download(self, name, model_dir):
        if name == "tiny":
            _through_the_slot(name, model_dir, "error")
            return ("error", "disk full", _CleanupOutcome())
        if name == "medium":
            return _through_the_slot(name, model_dir, "success")
        if name == "small":
            _LocalModelsMixin._cancel_local_model_downloads(self)
            _LocalModelsMixin._start_local_model_download(self, ["large-v3"])
            _through_the_slot(name, model_dir, "canceled")
            return ("canceled", "", _CleanupOutcome(_CLEANUP_RAN, 1, 200_000))
        _through_the_slot(name, model_dir, "error")
        return ("error", "network error", _CleanupOutcome())

    monkeypatch.setattr(
        _LocalModelsMixin, "_download_local_model_in_subprocess", _download
    )
    monkeypatch.setattr(
        "stt_app.settings_dialog_local.terminate_model_download_process",
        lambda process: None,
    )

    _LocalModelsMixin._drive_local_model_download_queue(dialog, 1)

    finished = [e for e in emitted if e[1] == "local_model_download_finished"]
    assert finished[-1][3] is False
    assert finished[-1][4] == (
        "Download canceled. Removed 1 incomplete file (0.2 MB). "
        "Failed: tiny: disk full. "
        "Then downloaded: medium. "
        "Then canceled: small. "
        "Then failed: large-v3: network error."
    )


def test_a_cancel_that_only_emptied_the_queue_is_reported_as_one(monkeypatch):
    """The Cancel lands before the worker's first iteration: it killed nothing
    and nothing had finished, so the old headline condition dropped it and the
    drain reported "Downloaded: " -- a success line naming no model, right
    after the user cancelled two queued models. Whether the user queues more
    afterwards or not, the removed entries are named."""
    from stt_app.settings_dialog_local import _LocalModelsMixin

    dialog, emitted = _draining_dialog(monkeypatch)
    dialog._local_model_download_cancel_event.clear()
    dialog._local_model_download_worker_running = True
    dialog._local_model_download_queue = [("medium", ""), ("small", "")]
    _LocalModelsMixin._cancel_local_model_downloads(dialog)
    assert dialog._local_model_download_queue == []

    _LocalModelsMixin._drive_local_model_download_queue(dialog, 1)

    finished = [e for e in emitted if e[1] == "local_model_download_finished"]
    assert finished[-1][3] is False
    # Nothing ever started downloading, so the summary says nothing about
    # incomplete files rather than reporting a cleanup that never ran.
    assert finished[-1][4] == (
        "Download canceled. Removed from the queue: medium, small."
    )


def test_a_download_queued_after_a_queue_only_cancel_is_listed_after_it(
    monkeypatch,
):
    from stt_app.settings_dialog_local import _LocalModelsMixin

    dialog, emitted = _draining_dialog(monkeypatch)
    dialog._local_model_download_cancel_event.clear()
    dialog._local_model_download_worker_running = True
    dialog._local_model_download_queue = [("medium", ""), ("small", "")]
    _LocalModelsMixin._cancel_local_model_downloads(dialog)
    _LocalModelsMixin._start_local_model_download(dialog, ["tiny"])
    monkeypatch.setattr(
        _LocalModelsMixin,
        "_download_local_model_in_subprocess",
        lambda self, name, model_dir: _through_the_slot(name, model_dir, "success"),
    )

    _LocalModelsMixin._drive_local_model_download_queue(dialog, 1)

    finished = [e for e in emitted if e[1] == "local_model_download_finished"]
    assert finished[-1][3] is False
    assert finished[-1][4] == (
        "Download canceled. Removed from the queue: medium, small. "
        "Then downloaded: tiny."
    )


def test_two_cancels_in_one_drain_keep_their_order(monkeypatch):
    """A canceled, B downloaded, C canceled: one snapshot of "how much came
    before the cancel" could only describe one of the two, and read B as
    finished before *any* cancellation."""
    from stt_app.settings_dialog_local import _LocalModelsMixin

    dialog, emitted = _draining_dialog(monkeypatch)
    dialog._local_model_download_cancel_event.clear()
    dialog._local_model_download_queue = [("a", "")]

    def _download(self, name, model_dir):
        if name in ("a", "c"):
            _LocalModelsMixin._cancel_local_model_downloads(self)
            _LocalModelsMixin._start_local_model_download(
                self, ["b"] if name == "a" else ["d"]
            )
            _through_the_slot(name, model_dir, "canceled")
            return ("canceled", "", _CleanupOutcome(_CLEANUP_RAN))
        if name == "b":
            _LocalModelsMixin._start_local_model_download(self, ["c"])
        return _through_the_slot(name, model_dir, "success")

    monkeypatch.setattr(
        _LocalModelsMixin, "_download_local_model_in_subprocess", _download
    )
    monkeypatch.setattr(
        "stt_app.settings_dialog_local.terminate_model_download_process",
        lambda process: None,
    )

    _LocalModelsMixin._drive_local_model_download_queue(dialog, 1)

    finished = [e for e in emitted if e[1] == "local_model_download_finished"]
    assert finished[-1][3] is False
    assert finished[-1][4] == (
        "Download canceled. No incomplete files remained. "
        "Canceled: a. Then downloaded: b. Then canceled: c. Then downloaded: d."
    )


class _CleanupCoordinator:
    """A download slot scripted for one entry's cleanup decision.

    The real coordinator cannot produce these states in a drain test: the
    kept case needs a caller genuinely parked on the slot, which the drain
    would then block behind, and the skipped case needs `acquire` to raise
    while the drain holds nothing.
    """

    def __init__(self, *, waiting: bool = False, cancel_on_acquire: bool = False):
        self._waiting = waiting
        self._cancel_on_acquire = cancel_on_acquire

    def acquire(self, name, model_dir, *, explicit, cancel_check=None,
                interest_already_registered=False):
        if self._cancel_on_acquire:
            from stt_app.model_download_coordinator import ModelDownloadCanceled

            raise ModelDownloadCanceled(name)
        return ACQUIRE_DOWNLOAD

    def release(self, name, model_dir, *, succeeded):
        pass

    def register_explicit_interest(self, name, model_dir=""):
        pass

    def drop_explicit_interest(self, name, model_dir=""):
        pass

    def has_waiting_download(self, name, model_dir=""):
        return self._waiting

    def has_explicit_interest(self, name, model_dir=""):
        return False

    def active(self):
        return None


class _CanceledProcess:
    """A download subprocess the user cancels while it is still running."""

    def __init__(self, cancel_event):
        self._cancel_event = cancel_event
        self._polls = 0
        self.returncode = 1

    def poll(self):
        self._polls += 1
        self._cancel_event.set()
        return None if self._polls < 2 else 1


def _drain_one_canceled_download(monkeypatch, coordinator):
    """Run the real worker for one model that is canceled mid-download."""
    import stt_app.settings_dialog_local as local_module
    from stt_app.settings_dialog_local import _LocalModelsMixin

    dialog, emitted = _draining_dialog(monkeypatch)
    dialog._local_model_download_cancel_event.clear()
    dialog._local_model_download_queue = [("large-v3", "")]
    _private_slot(monkeypatch, coordinator, local_module)
    cleanups: list[str] = []

    class _Facade:
        @staticmethod
        def start_model_download_process(model_name, model_dir):
            return _CanceledProcess(dialog._local_model_download_cancel_event)

        @staticmethod
        def cleanup_incomplete_model_download(model_name, model_dir):
            cleanups.append(model_name)
            return 4, 3_100_000_000

    monkeypatch.setattr(local_module, "_facade", lambda: _Facade)
    monkeypatch.setattr(local_module, "model_download_process_error", lambda p: "")
    monkeypatch.setattr(
        local_module, "terminate_model_download_process", lambda process: None
    )

    _LocalModelsMixin._drive_local_model_download_queue(dialog, 1)

    finished = [e for e in emitted if e[1] == "local_model_download_finished"]
    return finished[-1][3], finished[-1][4], cleanups


def test_partials_kept_for_a_waiting_download_are_not_reported_as_absent(
    monkeypatch,
):
    """`_cleanup_unless_awaited` keeps the partial files on purpose when
    another caller is parked to resume them, and the drain then read its zero
    as "No incomplete files remained." -- a statement about a disk nobody
    looked at, and the opposite of what happened to the gigabytes still
    sitting there."""
    success, summary, cleanups = _drain_one_canceled_download(
        monkeypatch, _CleanupCoordinator(waiting=True)
    )

    assert cleanups == [], "the waiting download's partial files were deleted"
    assert success is False
    assert summary == (
        "Download canceled. Incomplete files of large-v3 were kept: another "
        "download is waiting to resume them. Canceled: large-v3."
    )


def test_a_cancel_before_the_slot_says_the_download_had_not_started(monkeypatch):
    """The `ModelDownloadCanceled` arm returns without ever reaching the
    disk: the entry was still waiting for the download slot. Reported as "No
    incomplete files remained." that was a cleanup the drain never ran."""
    success, summary, cleanups = _drain_one_canceled_download(
        monkeypatch, _CleanupCoordinator(cancel_on_acquire=True)
    )

    assert cleanups == []
    assert success is False
    assert summary == (
        "Download canceled. Incomplete files of large-v3 were left in place: "
        "its download had not started yet. Canceled: large-v3."
    )


def test_a_cleanup_that_ran_and_removed_files_still_reports_them(monkeypatch):
    """The other side of the same change: a cleanup that did run must keep
    saying what it removed, through the real worker rather than a fake
    tuple."""
    success, summary, cleanups = _drain_one_canceled_download(
        monkeypatch, _CleanupCoordinator()
    )

    assert cleanups == ["large-v3"]
    assert success is False
    assert summary == (
        "Download canceled. Removed 4 incomplete files (3100.0 MB). "
        "Canceled: large-v3."
    )


def test_a_drain_that_downloaded_nothing_does_not_report_an_empty_success(
    monkeypatch,
):
    """`_draining_dialog`'s state -- worker running, cancel event set, queue
    empty -- consumes a Cancel that removed nothing, so the drain falls past
    the cancel headline with no successes and no failures. That reached the
    user as "Downloaded: " naming no model, in the success colour. Not
    reachable through the UI at HEAD; guarded because the report was the
    worst possible answer for a state nobody can explain."""
    from stt_app.settings_dialog_local import _LocalModelsMixin

    dialog, emitted = _draining_dialog(monkeypatch)

    _LocalModelsMixin._drive_local_model_download_queue(dialog, 1)

    finished = [e for e in emitted if e[1] == "local_model_download_finished"]
    assert finished[-1][3] is False
    assert finished[-1][4] == "No downloads ran."


def test_nothing_is_queued_after_the_dialog_shut_down(monkeypatch):
    """The worker now continues past a Cancel with whatever is queued after
    it, so an enqueue that slipped in after `shutdown()`'s cancel would be a
    download the user's quit then waits on."""
    from stt_app.model_download_coordinator import model_download_coordinator
    from stt_app.settings_dialog_local import _LocalModelsMixin

    dialog, _emitted = _draining_dialog(monkeypatch)
    dialog._local_model_download_worker_running = False
    dialog._shutdown_started = True

    _LocalModelsMixin._start_local_model_download(dialog, ["small"])

    assert dialog._local_model_download_queue == []
    assert dialog._local_model_download_worker_running is False
    assert model_download_coordinator().has_explicit_interest("small", "") is False
    assert dialog.local_models_action_label.texts == []


def test_two_cancels_in_one_drain_name_the_model_each_cleanup_sentence_is_about(
    monkeypatch,
):
    """Two Cancels in one drain: the first killed a download and removed its
    partials, the second hit an entry still waiting for the slot. Unnamed,
    the two sentences read as a contradiction -- "Removed 3 incomplete
    files" beside "Incomplete files were left in place"."""
    from stt_app.settings_dialog_local import _LocalModelsMixin

    dialog, emitted = _draining_dialog(monkeypatch)
    dialog._local_model_download_cancel_event.clear()
    dialog._local_model_download_queue = [("medium", "")]

    def _download(self, name, model_dir):
        _LocalModelsMixin._cancel_local_model_downloads(self)
        if name == "medium":
            _LocalModelsMixin._start_local_model_download(self, ["large-v3"])
            _through_the_slot(name, model_dir, "canceled")
            return ("canceled", "", _CleanupOutcome(_CLEANUP_RAN, 3, 2_400_000))
        return ("canceled", "", _CleanupOutcome(_CLEANUP_SKIPPED))

    monkeypatch.setattr(
        _LocalModelsMixin, "_download_local_model_in_subprocess", _download
    )
    monkeypatch.setattr(
        "stt_app.settings_dialog_local.terminate_model_download_process",
        lambda process: None,
    )

    _LocalModelsMixin._drive_local_model_download_queue(dialog, 1)

    finished = [e for e in emitted if e[1] == "local_model_download_finished"]
    assert finished[-1][3] is False
    assert finished[-1][4] == (
        "Download canceled. Removed 3 incomplete files (2.4 MB). Incomplete "
        "files of large-v3 were left in place: its download had not started "
        "yet. Canceled: medium, large-v3."
    )


def test_several_models_left_alone_are_named_together():
    from stt_app.settings_dialog_local import _LocalModelsMixin

    summary = _LocalModelsMixin._canceled_drain_summary(
        [("canceled", "a"), ("canceled", "b"), ("canceled", "c")],
        0,
        0,
        cleanups=[
            ("a", _CLEANUP_SKIPPED),
            ("b", _CLEANUP_KEPT),
            ("c", _CLEANUP_SKIPPED),
        ],
    )

    assert summary == (
        "Download canceled. Incomplete files of b were kept: another download "
        "is waiting to resume them. Incomplete files of a and c were left in "
        "place: their downloads had not started yet. Canceled: a, b, c."
    )


def test_no_downloads_ran_is_reported_in_the_warning_colour():
    """"No downloads ran." is a Cancel-shaped outcome, not a failure; the
    colour rule softened only the two texts it knew and painted it red."""
    from unittest.mock import MagicMock

    from stt_app.settings_dialog_local import _LocalModelsMixin

    dialog = MagicMock()
    dialog._local_model_download_worker_token = 3
    dialog._local_model_download_is_running.return_value = False

    _LocalModelsMixin._on_local_model_download_finished(
        dialog, 3, False, "No downloads ran."
    )

    dialog._set_local_models_action_text.assert_called_once_with(
        "No downloads ran.", "#b26a00", allow_growth=True
    )
