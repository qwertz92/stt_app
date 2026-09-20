"""The Local tab's download progress area: its numbers and its geometry.

Two separate questions. Which bytes the percentage is measured from -- the
worker's own report where there is one, the destination directory otherwise --
is driven here through the mixin with a light stub. Whether showing it moves
anything needs the real dialog, and is measured rather than eyeballed.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from PySide6 import QtCore, QtWidgets

from stt_app.local_model_download import DownloadBytesSample
from stt_app.model_download_progress import ModelDownloadSpeedTracker
from stt_app.settings_dialog import SettingsDialog
from stt_app.settings_dialog_local import _LocalModelsMixin
from stt_app.settings_store import AppSettings


class _SettingsStore:
    def __init__(self) -> None:
        self._settings = AppSettings()

    def load(self) -> AppSettings:
        return self._settings

    def save(self, settings: AppSettings) -> None:
        self._settings = settings


class _SecretStore:
    def get_api_key(self, _provider: str) -> None:
        return None


class _Logger:
    def diagnostics_text(self) -> str:
        return ""


class _Widget:
    """Records what the refresh paints, without needing Qt."""

    def __init__(self) -> None:
        self.text = ""
        self.tooltip = ""
        self.visible = False
        self.value: int | None = None
        self.fmt = ""
        self.range: tuple[int, int] | None = None

    def setText(self, text):
        self.text = text

    def setToolTip(self, text):
        self.tooltip = text

    def setStyleSheet(self, _sheet):
        pass

    def setVisible(self, value):
        self.visible = bool(value)

    def setValue(self, value):
        self.value = value

    def setFormat(self, fmt):
        self.fmt = fmt

    def setRange(self, low, high):
        self.range = (low, high)


def _mixin_dialog(
    *,
    directory_bytes: int,
    process=None,
    preload_sample=None,
    queued=(),
    monkeypatch=None,
):
    dialog = _LocalModelsMixin.__new__(_LocalModelsMixin)
    dialog._local_model_download_lock = threading.RLock()
    dialog._local_model_download_active = ("granite-speech-5.0-470m-turboctc", "")
    dialog._local_model_download_claimed = None
    dialog._local_model_download_queue = list(queued)
    dialog._local_model_download_worker_running = True
    dialog._local_model_download_process = process
    dialog._local_model_download_speed_tracker = ModelDownloadSpeedTracker()
    dialog._local_model_download_bar_shown = False
    dialog.local_model_download_progress_bar = _Widget()
    dialog.local_model_download_queue_label = _Widget()
    dialog.local_models_action_label = _Widget()
    dialog.model_dir_edit = SimpleNamespace(text=lambda: "")
    dialog._preload_downloading_model = lambda: None
    dialog._controller = SimpleNamespace(
        preload_download_progress=lambda: preload_sample
    )
    monkeypatch.setattr(
        "stt_app.settings_dialog_local._facade",
        lambda: SimpleNamespace(
            estimate_cached_model_bytes=lambda _name, _dir: directory_bytes
        ),
    )
    return dialog


def test_the_percentage_comes_from_the_workers_own_byte_counts(monkeypatch):
    """The directory is a proxy; the worker has the real numbers.

    Here the destination has grown to a sparse 200 MB while the downloader
    has fetched 100 MB of 552 MB -- the shape a chunk-reconstructing
    downloader produces, and the one that showed 36% with 12% present.
    """
    process = SimpleNamespace()
    monkeypatch.setattr(
        "stt_app.settings_dialog_local.model_download_process_progress",
        lambda _process: DownloadBytesSample(100_000_000, 552_442_697, 1.0),
    )
    dialog = _mixin_dialog(
        directory_bytes=200_000_000, process=process, monkeypatch=monkeypatch
    )

    _LocalModelsMixin._refresh_local_model_download_progress(dialog)

    assert dialog.local_model_download_progress_bar.value == 18
    shown = dialog.local_models_action_label.text
    assert "100 of 552 MB (18%)" in shown
    assert "approx." not in shown


def test_a_worker_that_reports_nothing_falls_back_to_directory_growth(monkeypatch):
    monkeypatch.setattr(
        "stt_app.settings_dialog_local.model_download_process_progress",
        lambda _process: None,
    )
    dialog = _mixin_dialog(
        directory_bytes=276_000_000,
        process=SimpleNamespace(),
        monkeypatch=monkeypatch,
    )

    _LocalModelsMixin._refresh_local_model_download_progress(dialog)

    assert dialog.local_model_download_progress_bar.value == 50
    assert "approx. 50%" in dialog.local_models_action_label.text


def test_a_controller_download_uses_the_controllers_own_sample(monkeypatch):
    """A preload download is the same download to the user, so the tab shows
    it -- and must not measure the directory a second time when the
    controller already has the worker's numbers."""
    monkeypatch.setattr(
        "stt_app.settings_dialog_local.model_download_process_progress",
        lambda _process: pytest.fail("asked this tab's own worker"),
    )
    dialog = _mixin_dialog(
        directory_bytes=0,
        preload_sample=DownloadBytesSample(276_000_000, 552_442_697, 1.0),
        monkeypatch=monkeypatch,
    )
    dialog._local_model_download_active = None
    dialog._local_model_download_worker_running = False
    dialog._preload_downloading_model = lambda: "granite-speech-5.0-470m-turboctc"

    _LocalModelsMixin._refresh_local_model_download_progress(dialog)

    assert dialog.local_model_download_progress_bar.value == 50


def test_the_queue_line_names_what_starts_next(monkeypatch):
    monkeypatch.setattr(
        "stt_app.settings_dialog_local.model_download_process_progress",
        lambda _process: DownloadBytesSample(1_000, 552_442_697, 1.0),
    )
    dialog = _mixin_dialog(
        directory_bytes=0,
        process=SimpleNamespace(),
        queued=(("parakeet-tdt-0.6b-v3", ""), ("canary-1b-v2", "")),
        monkeypatch=monkeypatch,
    )

    _LocalModelsMixin._refresh_local_model_download_progress(dialog)

    assert (
        dialog.local_model_download_queue_label.text
        == "Next: NVIDIA Parakeet TDT 0.6B v3 (+1 more)"
    )
    assert dialog.local_model_download_queue_label.visible is True
    # The whole line stays readable even when the label has to elide it.
    assert dialog.local_model_download_queue_label.tooltip == (
        "Next: NVIDIA Parakeet TDT 0.6B v3 (+1 more)"
    )


def test_ending_a_download_clears_the_queue_line(monkeypatch):
    monkeypatch.setattr(
        "stt_app.settings_dialog_local.model_download_process_progress",
        lambda _process: DownloadBytesSample(1_000, 552_442_697, 1.0),
    )
    dialog = _mixin_dialog(
        directory_bytes=0,
        process=SimpleNamespace(),
        queued=(("parakeet-tdt-0.6b-v3", ""),),
        monkeypatch=monkeypatch,
    )
    _LocalModelsMixin._refresh_local_model_download_progress(dialog)

    _LocalModelsMixin._hide_local_model_download_progress(dialog)

    assert dialog.local_model_download_queue_label.text == ""
    assert dialog.local_model_download_queue_label.visible is False
    assert dialog.local_model_download_progress_bar.visible is False


# --- geometry -----------------------------------------------------------


@pytest.fixture
def dialog(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    settings_dialog = SettingsDialog(
        settings_store=_SettingsStore(),
        secret_store=_SecretStore(),
        app_logger=_Logger(),
    )
    yield settings_dialog
    settings_dialog.close()
    app.processEvents()


def _open_local_tab(dialog: SettingsDialog) -> None:
    dialog.show()
    dialog.tabs.setCurrentIndex(dialog._local_tab_index)
    QtWidgets.QApplication.processEvents()


def test_the_whole_download_area_moves_nothing_from_start_to_finish(
    dialog: SettingsDialog,
) -> None:
    """The hard rule of this project, measured rather than eyeballed.

    The bar and the queue line both appear the instant a download starts.
    Without their space retained, pressing Download pulls
    Download/Cancel/Delete up under the cursor -- with Cancel sliding into
    the place the pointer is already on -- and pushes them back down when the
    download ends, shrinking the model list twice per download.
    """
    _open_local_tab(dialog)

    watched = {
        "list": dialog.local_models_list,
        "action label": dialog.local_models_action_label,
        "download": dialog.download_selected_models_button,
        "cancel": dialog.cancel_model_downloads_button,
        "delete": dialog.delete_selected_model_button,
        "bar": dialog.local_model_download_progress_bar,
        "queue line": dialog.local_model_download_queue_label,
    }

    def geometry() -> dict[str, tuple[int, int]]:
        QtWidgets.QApplication.processEvents()
        return {
            name: (widget.mapTo(dialog, QtCore.QPoint(0, 0)).y(), widget.height())
            for name, widget in watched.items()
        }

    idle = geometry()
    assert geometry() == idle, "the tab had not settled before the measurement"

    dialog._set_download_queue_line(["NVIDIA Parakeet TDT 0.6B v3 (+2 more)"])
    dialog.local_model_download_progress_bar.setValue(1)
    dialog._show_download_widgets(True)
    assert geometry() == idle, "starting a download moved something"

    # Every digit count the percentage passes through, and a queue line long
    # enough to elide: neither may resize its own row.
    for percent in (9, 38, 100):
        dialog.local_model_download_progress_bar.setValue(percent)
        dialog._set_download_queue_line(
            ["A model with a deliberately very long display name"] * percent
        )
        assert geometry() == idle, f"the area moved at {percent}%"

    dialog._show_download_widgets(False)
    dialog._set_download_queue_line([])
    assert geometry() == idle, "finishing a download moved something"


def test_the_progress_bar_fits_the_percentage_it_has_to_show(
    dialog: SettingsDialog,
) -> None:
    """Its height is pinned, so it has to be pinned from the font.

    Windows' "Text size" raises the application font without the DPI, so a
    pixel constant clips exactly the text the bar exists to show.
    """
    _open_local_tab(dialog)
    bar = dialog.local_model_download_progress_bar

    assert bar.height() >= bar.fontMetrics().height()
    assert bar.minimumHeight() == bar.maximumHeight()
