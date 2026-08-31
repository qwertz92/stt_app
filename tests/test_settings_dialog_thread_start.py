"""A worker thread that refuses to start must not leave Settings busy.

Every one of these six paths sets a busy marker, disables the control that
started it, and hands the rest to a worker thread. `_background_work_active()`
reads those markers, and while it answers True the dialog defers
`reload_from_store()` -- so `Thread.start()` raising `RuntimeError` (the
interpreter cannot create another thread) used to disable a control for good
*and* stop Settings from picking up saved values, for the life of the app,
because the dialog is never recreated. Each test drives one site and asserts
the dialog came back.
"""

from __future__ import annotations

import threading
import time

from PySide6 import QtTest, QtWidgets
from test_settings_dialog_connection import (
    _FakeLogger,
    _FakeSecretStore,
    _FakeSettingsStore,
    _ImmediateThread,
)

from stt_app.model_download_coordinator import model_download_coordinator
from stt_app.settings_dialog import SettingsDialog
from stt_app.settings_store import AppSettings


class _RefusingThread:
    """A `threading.Thread` that fails the way a starved interpreter fails.

    Constructed successfully -- the dialog has already written its busy marker
    by then -- and only `start()` raises, which is exactly the window the
    guards exist for.
    """

    def __init__(self, *args, name: str = "", **kwargs) -> None:
        self.name = name

    def start(self) -> None:
        raise RuntimeError("can't start new thread")

    def is_alive(self) -> bool:  # pragma: no cover - it never ran
        return False

    def join(self, timeout: float | None = None) -> None:  # pragma: no cover
        return None


def _make_dialog(settings: AppSettings | None = None, **kwargs) -> SettingsDialog:
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return SettingsDialog(
        settings_store=_FakeSettingsStore(settings or AppSettings()),
        secret_store=_FakeSecretStore(),
        app_logger=_FakeLogger(),
        **kwargs,
    )


def _refuse_new_threads(monkeypatch) -> None:
    """Make every `threading.Thread.start()` raise, from here on.

    The dialog mixins each import `threading` themselves, and this replaces the
    class on the one module object all of them resolve, so the site under test
    is reached whichever module it lives in. Call it *after* building the
    dialog: construction starts the inventory scan, and refusing that too would
    leave every later assertion reading the scan's failure message instead of
    the one the site under test wrote.
    """
    monkeypatch.setattr(threading, "Thread", _RefusingThread)


def test_a_connection_test_that_cannot_start_gives_the_button_back(monkeypatch):
    dialog = _make_dialog(AppSettings(engine="local"))
    _refuse_new_threads(monkeypatch)
    dialog.test_conn_target_combo.setCurrentIndex(
        dialog.test_conn_target_combo.findData("deepgram")
    )
    dialog.deepgram_key_edit.setText("dg-test-key")

    dialog._test_connection()

    assert dialog._active_connection_test_thread is None
    assert dialog._background_work_active() is False
    assert dialog.test_conn_button.isEnabled() is True
    assert dialog.test_conn_target_combo.isEnabled() is True
    assert "Could not start the connection test" in dialog.test_conn_result.text()


def test_an_update_check_that_cannot_start_gives_the_button_back(monkeypatch):
    dialog = _make_dialog()
    _refuse_new_threads(monkeypatch)

    dialog._check_for_updates()

    assert dialog._active_update_check_thread is None
    assert dialog._background_work_active() is False
    assert dialog.check_updates_button.isEnabled() is True
    assert "Could not start the update check" in dialog._save_status_label.text()


def test_a_model_scan_that_cannot_start_does_not_leave_the_tab_scanning(monkeypatch):
    dialog = _make_dialog()
    _refuse_new_threads(monkeypatch)

    dialog._request_local_model_scan(force=True)

    assert dialog._active_local_model_scan_thread is None
    assert dialog._background_work_active() is False
    # The scan's own status line. It used to write the shared action label,
    # which belongs to the download -- see the download test below.
    assert (
        "Could not start the model scan"
        in dialog.local_models_scan_status_label.text()
    )


def test_a_model_scan_that_cannot_start_leaves_no_state_behind(monkeypatch):
    """The arm repeated half the completion slot and forgot the other half.

    Left behind: the `_local_model_scan_started_at_by_token` entry, which is
    never popped again for that token and so leaks for the process lifetime,
    and the "Checking local model availability in the background." line, which
    described a scan that never started.
    """
    dialog = _make_dialog()
    dialog._cached_local_models_available = False
    _refuse_new_threads(monkeypatch)

    dialog._request_local_model_scan(force=True)

    assert dialog._local_model_scan_started_at_by_token == {}
    assert "in the background" not in dialog.local_models_label.text()


def test_a_failed_download_is_not_reported_as_a_failed_scan(monkeypatch):
    """The download's completion refreshes the inventory, which starts a scan.

    With no thread available both fail, and the scan's message used to land on
    the same label a moment later -- so a user who pressed Download was told
    that a model scan could not be started.
    """
    dialog = _make_dialog()
    _refuse_new_threads(monkeypatch)

    dialog._start_local_model_download(["tiny"])

    shown = dialog.local_models_action_label.text()
    assert "Could not start the download" in shown, shown
    assert "model scan" not in shown, shown


def test_a_download_that_cannot_start_releases_the_queue_and_its_interest(monkeypatch):
    """The queue's claim on the model has to go back with the queue.

    Interest is registered at enqueue so the controller's preload leaves a
    queued model's partial files alone. A start that fails without dropping it
    leaves `has_explicit_interest` true for the rest of the process, and those
    partials are then never cleaned up again.
    """
    dialog = _make_dialog()
    coordinator = model_download_coordinator()
    _refuse_new_threads(monkeypatch)

    dialog._start_local_model_download(["tiny"])

    active, queued, running = dialog._local_model_download_snapshot()
    assert active is None
    assert queued == []
    assert running is False
    assert dialog._active_local_model_download_thread is None
    assert dialog._background_work_active() is False
    assert coordinator.has_explicit_interest("tiny", "") is False
    shown = dialog.local_models_action_label.text()
    assert "Could not start the download" in shown, shown
    assert dialog.local_model_download_progress_bar.isVisible() is False
    assert dialog._local_model_download_progress_timer.isActive() is False


def test_a_benchmark_that_cannot_start_gives_the_run_button_back(monkeypatch, tmp_path):
    audio_path = tmp_path / "sample.wav"
    audio_path.write_bytes(b"RIFF")
    monkeypatch.setattr(
        "stt_app.settings_dialog._scan_cached_models",
        lambda _model_dir="": ["small"],
    )
    # Run the inventory scan inline so the model list is populated before the
    # refusal starts; Run is disabled with an empty list and the site under
    # test would never be reached.
    monkeypatch.setattr(threading, "Thread", _ImmediateThread)

    dialog = _make_dialog()
    dialog.tabs.setCurrentIndex(dialog._benchmark_tab_index)
    # The inventory render is deferred, so poll for it instead of guessing a
    # budget: a fixed wait passes or fails on how much unrelated queued work
    # the suite happens to leave in front of it.
    deadline = time.monotonic() + 10.0
    while dialog.benchmark_models_list.count() == 0 and time.monotonic() < deadline:
        QtTest.QTest.qWait(25)
    dialog._set_benchmark_audio_path(str(audio_path))
    assert dialog.benchmark_models_list.count() == 1
    _refuse_new_threads(monkeypatch)

    dialog._run_local_benchmark()

    assert dialog._active_benchmark_thread is None
    assert dialog._benchmark_cancel_event is None
    assert dialog._background_work_active() is False
    assert dialog.run_benchmark_button.isEnabled() is True
    assert "Could not start the benchmark" in dialog.benchmark_status_label.text()
    # The Details overview was primed with the running summary before the
    # thread was started, and `setPlainText` puts that into its Status row --
    # so it went on reading "running" next to a status line saying the run
    # never began.
    overview = dialog.benchmark_summary_text.overview_table
    status_values = [
        overview.item(row, 1).text()
        for row in range(overview.rowCount())
        if overview.item(row, 0) is not None
        and overview.item(row, 0).text() == "Status"
    ]
    assert status_values, "the overview has no Status row"
    assert all("running" not in value.lower() for value in status_values), (
        f"the overview still claims the benchmark is running: {status_values}"
    )


def test_an_import_that_cannot_start_gives_the_import_controls_back(monkeypatch):
    dialog = _make_dialog(AppSettings(engine="local"))
    dialog._set_selected_import_file("dummy.wav")
    _refuse_new_threads(monkeypatch)

    dialog._start_import_transcription("dummy.wav")

    assert dialog._import_progress_started_at is None
    assert dialog._background_work_active() is False
    assert dialog.import_file_button.isEnabled() is True
    assert dialog.import_start_button.isEnabled() is True
    assert "Could not start the transcription" in dialog.import_result_label.text()
