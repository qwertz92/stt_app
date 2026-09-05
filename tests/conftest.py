"""Shared test fakes and fixtures for controller tests.

Both test_controller.py and test_controller_coverage.py use these helper
classes to avoid duplicating ~150 lines of boilerplate.
"""

from __future__ import annotations

import atexit
import hashlib
import logging
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from PySide6 import QtCore, QtGui, QtWidgets

from stt_app.config import (
    DEFAULT_CANCEL_HOTKEY,
    FALLBACK_HOTKEY,
    OVERLAY_INITIAL_DETAIL,
)
from stt_app.controller import DictationController
from stt_app.settings_store import AppSettings
from stt_app.text_inserter import TextInsertionError


@pytest.fixture(autouse=True)
def _isolate_appdata(monkeypatch, tmp_path):
    r"""Point the app's data directory -- and the fallback behind it -- at tmp.

    Setting `APPDATA` alone is not isolation. `app_paths._appdata_base_root`
    falls back to `Path.home() / "AppData" / "Roaming"` when the variable is
    missing, and on Windows that *is* `%APPDATA%`, so a test that does
    `monkeypatch.delenv("APPDATA")` escapes the sandbox entirely and reaches
    the developer's real data folder. `appdata_root` is not a lookup: it
    creates the directory and renames a legacy `tts_app` install onto the
    current name. Reproduced before this fixture was widened -- one test,
    `test_appdata_root_falls_back_to_home_when_APPDATA_unset`, moved a home
    directory's `settings.json`, `transcript_history.json` and recordings.

    It does not fire on a machine that already has `%APPDATA%\stt_app`, which
    is why it went unnoticed here; on a machine with only the legacy folder,
    running the test suite moves the user's data.

    `Path.home()` reads `USERPROFILE` on Windows and `HOME` elsewhere, so both
    are set. Three other `src/` call sites read it -- twice in
    `history_ui_actions.py` and once in `settings_dialog_benchmark.py` -- and
    all three only build a *suggested* path for `QFileDialog.getSaveFileName`,
    which `_forbid_blocking_modal_dialogs` refuses before it can open, so none
    of them can reach the real home either. The two `expanduser("~")` calls
    resolve the default Hugging Face cache, which `pytest_configure` already
    redirects with `HF_HOME`/`HF_HUB_CACHE`.
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))


@pytest.fixture(autouse=True)
def _forbid_handing_paths_to_the_desktop_shell(monkeypatch):
    """No test may open a real file-manager window or launch a process.

    Several dialogs reveal a file or directory in the system file manager. A
    test that reaches one of those paths without stubbing it opened a real
    Explorer window on the developer's desktop and left it there — usually
    pointing at an empty pytest ``tmp_path``, which then looks like a product
    bug ("my recordings folder is empty").

    Failing loudly is deliberate: a test that legitimately exercises such a
    path must stub it and assert on the call, which every current one does.
    A silently inert stub would let the next such test go unnoticed.
    """

    def _blocked_start_detached(*args, **kwargs):
        raise AssertionError(
            "QProcess.startDetached was called in a test. Patch it (and assert "
            "on the call) instead of launching a real process. Args: "
            f"{args!r}"
        )

    def _blocked_open_url(url, *args, **kwargs):
        raise AssertionError(
            "QDesktopServices.openUrl was called in a test. Patch it (and "
            "assert on the call) instead of handing the path to the shell. "
            f"URL: {url!r}"
        )

    monkeypatch.setattr(
        QtCore.QProcess,
        "startDetached",
        staticmethod(_blocked_start_detached),
        raising=False,
    )
    monkeypatch.setattr(
        QtGui.QDesktopServices,
        "openUrl",
        staticmethod(_blocked_open_url),
        raising=False,
    )


@pytest.fixture(autouse=True)
def _forbid_blocking_modal_dialogs(monkeypatch):
    """No test may open a modal dialog that waits for a human click.

    ``QMessageBox.information`` and the ``QFileDialog`` getters run their own
    event loop until someone clicks. A test that reaches one unstubbed does not
    fail — it hangs the whole run forever, with no output naming the cause.
    Turning that into an immediate, named failure is the difference between a
    five-second fix and bisecting a twenty-minute suite.

    Every test that legitimately drives one of these already patches it, so
    this only catches the ones that would otherwise hang.
    """

    def _blocker(kind: str):
        def _blocked(*args, **kwargs):
            raise AssertionError(
                f"{kind} was called in a test and would block until a human "
                "clicks it. Patch it (and assert on the call) instead."
            )

        return _blocked

    for name in ("information", "warning", "critical", "question", "about"):
        monkeypatch.setattr(
            QtWidgets.QMessageBox,
            name,
            staticmethod(_blocker(f"QMessageBox.{name}")),
            raising=False,
        )
    for name in (
        "getExistingDirectory",
        "getOpenFileName",
        "getOpenFileNames",
        "getSaveFileName",
    ):
        monkeypatch.setattr(
            QtWidgets.QFileDialog,
            name,
            staticmethod(_blocker(f"QFileDialog.{name}")),
            raising=False,
        )


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
        if hotkey not in {FALLBACK_HOTKEY, DEFAULT_CANCEL_HOTKEY}:
            raise ValueError("blocked")

    def unregister(self):
        pass


class FakeHotkeyManagerAllFail(FakeHotkeyManager):
    def register(self, hotkey):
        self.calls.append(hotkey)
        raise ValueError("blocked")


class FakeCancelHotkeyManager(FakeHotkeyManager):
    pass


class FakeOverlay:
    def __init__(self):
        self.states = []
        self.state_kwargs = []
        self.opacity_values = []
        self.compact_calls = 0
        self.always_on_top_values = []
        self.language_options = []
        self.reveal_calls = 0
        self.reveal_durations = []
        self.queue_updates = []
        # Mirrors `OverlayUI.detail_is_being_read`; a test sets it.
        self.detail_is_being_read = False

    def set_state(self, state, detail="", **kwargs):
        self.states.append((state, detail))
        self.state_kwargs.append(dict(kwargs))

    @property
    def state(self) -> str:
        """What the real overlay reports: the state name last written."""
        return self.states[-1][0] if self.states else "Idle"

    @property
    def detail(self) -> str:
        """The detail text last written, like the real overlay.

        Before any write the real overlay already shows the constructor's
        `OVERLAY_INITIAL_DETAIL`; answering "" here diverged from it.
        """
        return self.states[-1][1] if self.states else OVERLAY_INITIAL_DETAIL

    def set_transcription_queue(self, items):
        self.queue_updates.append([(int(t), str(label)) for t, label in items])

    def set_opacity_percent(self, value: int):
        self.opacity_values.append(int(value))

    def set_always_on_top(self, value: bool):
        self.always_on_top_values.append(bool(value))

    def set_language_options(self, modes, selected_mode):
        self.language_options.append((tuple(modes), str(selected_mode)))

    def reveal_temporarily(self, duration_ms=1800):
        self.reveal_calls += 1
        self.reveal_durations.append(int(duration_ms))

    def ensure_compact_size(self):
        self.compact_calls += 1
        return


class FakeTextInserter:
    def __init__(self, should_fail=False):
        self.should_fail = should_fail
        self.calls = []
        self.restore_flags = []

    def insert_text(self, text, target_hwnd=None):
        self.calls.append((text, target_hwnd))
        if self.should_fail:
            raise TextInsertionError("failed insert")
        return True

    def insert_text_with_options(
        self,
        text,
        target_hwnd=None,
        paste_mode="auto",
        restore_clipboard=True,
    ):
        self.calls.append((text, target_hwnd, paste_mode))
        self.restore_flags.append(restore_clipboard)
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
        self.current = hwnd
        if hwnd == self.captured:
            self.current_focus = self.captured_focus
            self.current_caret = self.captured_caret
        else:
            self.current_focus = hwnd
            self.current_caret = hwnd
        return True


class ImmediateExecutor:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)
        return

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
        self.on_error = None
        self._stop_raises = stop_raises
        self._push_raises = push_raises

    def transcribe_batch(self, audio_source):
        return "batch"

    def start_stream(self, on_partial=None, on_error=None):
        self.started = True
        self.on_partial = on_partial
        self.on_error = on_error

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
    instances: list[FakeCapture] = []

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
        from stt_app.audio_capture import AudioCaptureError

        raise AudioCaptureError("no mic")


class FakeLastRecordingStore:
    def __init__(self, path: str = "/tmp/last_recording.wav"):
        self.path = Path(path)
        self.saved: list[tuple[bytes, bool]] = []
        self.transcribing: list[tuple[str, str, str]] = []
        self.failed: list[str] = []
        self.canceled: list[str] = []
        self.completed = 0
        self._available = False

    def save_recording(self, wav_bytes: bytes, *, keep_after_success: bool):
        self.saved.append((bytes(wav_bytes), bool(keep_after_success)))
        self._available = bool(wav_bytes)
        return

    def mark_transcribing(self, *, engine: str, model: str, mode: str) -> None:
        self.transcribing.append((engine, model, mode))

    def mark_failed(self, error: str) -> None:
        self.failed.append(str(error))
        self._available = True

    def mark_canceled(self, detail: str = "") -> None:
        self.canceled.append(str(detail))
        self._available = True

    def mark_completed(self) -> None:
        self.completed += 1
        self._available = False

    def selectable_path(self, archived_recordings_dir=None):
        if not self._available:
            return None
        return self.path

    def is_managed_audio_path(self, path: str | Path) -> bool:
        return Path(path) == self.path

    def has_recoverable_recording(self) -> bool:
        return self._available


def make_controller(**kwargs):
    """Create a DictationController with sensible defaults for testing."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    defaults = {
        "settings_store": FakeSettingsStore(
            AppSettings(hotkey=FALLBACK_HOTKEY, keep_transcript_in_clipboard=False)
        ),
        "hotkey_manager": FakeHotkeyManager(),
        "cancel_hotkey_manager": FakeCancelHotkeyManager(),
        "overlay": FakeOverlay(),
        "text_inserter": FakeTextInserter(),
        "logger": logging.getLogger("test.controller"),
        "window_focus_helper": FakeWindowFocusHelper(),
    }
    defaults.update(kwargs)
    return DictationController(**defaults), app


@pytest.fixture(scope="session")
def _download_lock_root(tmp_path_factory):
    """One lock root for the whole run; tests get a child of it.

    The root is session-scoped because `mktemp(numbered=True)` scans the
    parent directory to choose an index, which is O(n^2) across ~1000
    tests. Each test still gets its own child path so a lock one test
    leaves held cannot block the next -- acquiring waits with no timeout by
    design, so that would hang the run rather than fail it. The child
    directory is never created here; `CrossProcessLock` makes it only if a
    test actually takes a lock, so the tmp root stays clean.
    """
    return tmp_path_factory.mktemp("download-locks")


@pytest.fixture(autouse=True)
def _keep_download_locks_out_of_the_real_appdata(
    _download_lock_root, request, monkeypatch
):
    r"""Give every test its own directory for cross-process download locks.

    The coordinator takes a real OS lock before downloading, and its default
    location is the user's `%APPDATA%\stt_app\locks`. Left alone, the suite
    writes lock files into the real profile, and -- worse -- a test that leaves
    one held can block an unrelated later test forever, because acquiring the
    lock waits with no timeout by design.
    """
    from stt_app import model_download_coordinator

    lock_dir = _download_lock_root / hashlib.sha1(
        request.node.nodeid.encode("utf-8")
    ).hexdigest()[:16]
    monkeypatch.setattr(
        model_download_coordinator,
        "_download_lock_dir",
        lambda: lock_dir,
    )


def _isolate_the_hugging_face_environment() -> None:
    """Point Hugging Face at a throwaway cache, before anything reads it.

    This has to run in `pytest_configure`, not in a fixture. `huggingface_hub`
    computes `constants.HF_HUB_CACHE` and `constants.HF_HUB_OFFLINE` **at
    import**, and `test_modelscope_mirror.py` imports it at module scope --
    which pytest does during collection, before any fixture has run. Setting
    these from a function-scoped fixture therefore changed nothing at all:
    measured after collection, `constants.HF_HUB_CACHE` was still the
    developer's real `~/.cache/huggingface/hub` and `HF_HUB_OFFLINE` was False.

    That mattered because `download_model_snapshot` passes no `cache_dir` when
    `model_dir` is empty, so those frozen constants decide where a download
    lands. Any path that escapes the `_coordinated_download_if_missing` stub
    was writing into the real cache over the network.

    One directory for the whole session, not one per test: `tmp_path_factory`
    scans its base directory on every `mktemp`, so a per-test directory cost
    the suite ~1750 extra directories and several seconds of pure scanning for
    isolation that nothing needs to be per-test.
    """
    cache_root = Path(tempfile.mkdtemp(prefix="stt-hf-cache-"))
    os.environ["HF_HOME"] = str(cache_root)
    os.environ["HF_HUB_CACHE"] = str(cache_root / "hub")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["STT_APP_DISABLE_MODELSCOPE"] = "1"
    atexit.register(shutil.rmtree, cache_root, True)


def pytest_configure(config):
    _isolate_the_hugging_face_environment()
    config.addinivalue_line(
        "markers",
        "pixel_exact: asserts exact widget geometry; skipped on the offscreen "
        "Qt platform, where metrics are 1-4 px off and failures are artifacts",
    )
    config.addinivalue_line(
        "markers",
        "platform_dependent: asserts behaviour of the real Qt platform plugin "
        "(defaults, native styling); skipped on the offscreen platform",
    )


@pytest.fixture(autouse=True)
def _skip_pixel_exact_tests_on_the_offscreen_platform(request):
    """Turn the known offscreen metric drift into a skip, not a false failure.

    AGENTS.md already says an offscreen run's layout failures are artifacts, but
    saying so did not stop it: both CI workflows set `QT_QPA_PLATFORM=offscreen`
    and the release gate ran the whole suite before the build step, so v0.8.0
    was tagged and never published. `quality.yml` no longer sets it (verified:
    the only mention there is a comment saying why), while
    `windows-release.yml` still does -- deliberately, since a headless build
    runner has no real platform plugin. This fixture is what makes that safe:
    the metric-dependent assertions skip there instead of failing the gate.
    """
    markers = ("pixel_exact", "platform_dependent")
    if all(request.node.get_closest_marker(name) is None for name in markers):
        return
    if os.environ.get("QT_QPA_PLATFORM", "").strip().lower() == "offscreen":
        pytest.skip(
            "This assertion is meaningless on the offscreen Qt platform "
            "(metrics shift 1-4 px and widget defaults differ); run it on a "
            "real platform plugin."
        )


# Captured at import, before any test can stub it.
from stt_app.transcriber.local_faster_whisper import (  # noqa: E402
    LocalFasterWhisperTranscriber as _LocalFasterWhisperTranscriber,
)

_REAL_COORDINATED_DOWNLOAD = (
    _LocalFasterWhisperTranscriber._coordinated_download_if_missing
)


@pytest.fixture(autouse=True)
def _isolate_the_hugging_face_cache(monkeypatch):
    """Point the default model cache at an empty directory for every test.

    `find_cached_models` and the ONNX inventory search the default Hugging
    Face cache as well as a configured Model Dir, so on a developer machine
    they see whatever models are really downloaded there. A test that writes
    one model into `tmp_path` and asserts the inventory equals `[that model]`
    then passes or fails depending on the machine -- and the same tests are
    what a release build runs on a clean CI image, where the cache is empty.

    `_default_hf_cache_dir` reads these variables at call time, so setting
    them is enough.

    The rest of this fixture is what keeps an empty cache from being worse
    than a shared one. An empty cache is not inert: `_ensure_model`
    pre-fetches through the download slot whenever the destination holds no
    valid snapshot, so pointing the cache at an empty directory turned an
    offline unit test into a real `snapshot_download` against huggingface.co
    -- measured at 42 s for one test, and it would have written 486 MB if the
    request had succeeded. That is also what a clean CI runner was doing
    before this fixture existed, silently, on every run.

    So the pre-fetch itself is disabled by default. Deliberately that method
    and not the `_has_valid_model_snapshot` predicate underneath it: the
    predicate is also what `find_cached_models` detects with, so stubbing it
    made every Whisper model look installed. `test_download_coordination_
    wiring.py` restores the real method, which is the file that exists to
    assert the pre-fetch happens.

    The environment half of the isolation lives in
    `_isolate_the_hugging_face_environment`, which runs in `pytest_configure`
    because `huggingface_hub` freezes those constants at import -- see its
    docstring. This fixture is the half that has to be per-test.
    """
    from stt_app.transcriber import local_faster_whisper

    monkeypatch.setattr(
        local_faster_whisper.LocalFasterWhisperTranscriber,
        "_coordinated_download_if_missing",
        lambda self: None,
    )


@pytest.fixture
def real_model_prefetch(monkeypatch):
    """Undo the suite-wide pre-fetch stub, for files that test the pre-fetch.

    `_isolate_the_hugging_face_cache` disables
    `_coordinated_download_if_missing` because an empty cache otherwise turns
    every faster-whisper unit test into a real `snapshot_download`. The two
    files that exist to assert that call happens -- the coordination wiring
    and the download-cancel suite -- request this and get the real method back.

    Autouse fixtures are set up before explicitly requested ones at the same
    scope, so the stub is always in place by the time this restores it.
    """
    from stt_app.transcriber import local_faster_whisper

    monkeypatch.setattr(
        local_faster_whisper.LocalFasterWhisperTranscriber,
        "_coordinated_download_if_missing",
        _REAL_COORDINATED_DOWNLOAD,
    )


class RealTranscriberRefused(BaseException):
    """Deliberately not an `Exception`, and that is the whole point.

    Every controller error path catches `Exception`, so an `AssertionError`
    raised here is caught by the very arm under test: the test then passes
    without executing one line of what it names. Two tests for the streaming
    capture-failure arms did exactly that, and three mutations of those arms
    survived a full run because of it.
    """


@pytest.fixture(autouse=True)
def _forbid_building_a_real_transcriber(monkeypatch):
    """No test may construct a real provider or local runtime by accident.

    `_acquire_transcriber_runtime` has two arms. The shared one goes through
    `_get_or_create_transcriber`, which 27 patch sites in this suite replace.
    The isolated one -- taken whenever `_transcriber_runtime_lock` is already
    held -- calls the module-level `create_transcriber` instead, so those
    patches do not apply to it. That fallback is silent by design, and the
    consequence in a test is not a clean failure: it builds a real OpenAI
    client, or a real Parakeet transcriber that tries to download its model.
    (Observed once in a contended full-suite run, then not reproducible; the
    mechanism is real whether or not that particular run was.)

    So the module-level name is blocked unless a test replaces it, which 64
    of them already do. `monkeypatch` applies the test's own patch after this
    fixture's, so opting in needs no change.

    It raises rather than returning a stub: a stub would let the test pass
    while exercising something other than what it names. For the same reason
    it raises a `BaseException`, not an `AssertionError` -- see
    `RealTranscriberRefused`.
    """
    from stt_app import controller as controller_module

    def _refuse(*_args, **_kwargs):
        raise RealTranscriberRefused(
            "A test reached the real `create_transcriber`. This is the "
            "isolated arm of `_acquire_transcriber_runtime`, which does not "
            "go through `_get_or_create_transcriber` -- patch "
            "`stt_app.controller.create_transcriber` instead, or find out why "
            "the shared runtime lock was already held."
        )

    monkeypatch.setattr(controller_module, "create_transcriber", _refuse)


@pytest.fixture(autouse=True)
def _reset_the_transcription_shutdown_flag():
    """`DictationController.shutdown()` sets a process-wide flag that every
    remote wait reads (`transcriber.base.request_transcription_shutdown`), and
    nearly every controller test ends in `shutdown()`. The flag is once-per-
    process by design -- a quit is -- so nothing in `src/` clears it, and it
    leaked into every later test's provider loop, which then gave up at once:
    26 failures in the full suite that no single-file run could show, because
    a single file never runs a controller test before a provider test."""
    from stt_app.transcriber.base import reset_transcription_shutdown_for_tests

    reset_transcription_shutdown_for_tests()
    yield
    reset_transcription_shutdown_for_tests()
