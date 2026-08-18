"""The Local tab must not call a model missing while it is being downloaded.

The app has two independent download paths: the settings dialog's own queue,
and the controller's background preload (started after a save, or lazily on
first use). Only the first one was ever reflected in the Local tab, so
selecting an absent model and saving produced a silent download: the list kept
reporting "Not downloaded", and a second download started from that tab then
competed with the first for the same link.
"""

from __future__ import annotations

import threading

from stt_app.settings_dialog_local import _LocalModelsMixin


class _Controller:
    def __init__(self, downloading: str | None = None, explode: bool = False):
        self._downloading = downloading
        self._explode = explode

    def preload_downloading_model(self) -> str | None:
        if self._explode:
            raise RuntimeError("controller is shutting down")
        return self._downloading


class _Dialog(_LocalModelsMixin):
    """Just enough state for the download-state helpers."""

    def __init__(self, controller=None, active=None, queued=None):
        self._controller = controller
        self._local_model_download_lock = threading.Lock()
        self._local_model_download_active = active
        self._local_model_download_queue = list(queued or [])
        self._local_model_download_worker_running = active is not None
        self._local_model_download_completed_names = set()


def test_controller_download_is_reported_as_active():
    dialog = _Dialog(controller=_Controller("cohere-transcribe-03-2026"))
    assert dialog._local_model_download_state("cohere-transcribe-03-2026") == "active"


def test_controller_download_counts_as_pending():
    """Otherwise the tab offers to start a second copy of the same download."""
    dialog = _Dialog(controller=_Controller("cohere-transcribe-03-2026"))
    assert "cohere-transcribe-03-2026" in dialog._local_model_download_pending_names()


def test_other_models_are_unaffected_by_a_controller_download():
    dialog = _Dialog(controller=_Controller("cohere-transcribe-03-2026"))
    assert dialog._local_model_download_state("large-v3-turbo") == ""
    assert "large-v3-turbo" not in dialog._local_model_download_pending_names()


def test_dialog_queue_state_still_wins_and_still_works():
    dialog = _Dialog(
        controller=_Controller(None),
        active=("large-v3-turbo", ""),
        queued=[("small", "")],
    )
    assert dialog._local_model_download_state("large-v3-turbo") == "active"
    assert dialog._local_model_download_state("small") == "queued"
    assert dialog._local_model_download_state("medium") == ""


def test_missing_controller_is_tolerated():
    """The dialog is constructed in tests and tools without a real controller."""
    dialog = _Dialog(controller=object())
    assert dialog._preload_downloading_model() is None
    assert dialog._local_model_download_state("small") == ""


def test_controller_error_does_not_break_the_list():
    """A controller tearing down must not take the settings list with it."""
    dialog = _Dialog(controller=_Controller(explode=True))
    assert dialog._preload_downloading_model() is None
    assert dialog._local_model_download_state("small") == ""


def test_empty_model_name_is_not_treated_as_a_download():
    dialog = _Dialog(controller=_Controller(""))
    assert dialog._preload_downloading_model() is None


def test_non_string_controller_answer_is_ignored():
    """Test doubles return stand-ins; only a real model name may match."""
    class _MockLike:
        def preload_downloading_model(self):
            return object()

    dialog = _Dialog(controller=_MockLike())
    assert dialog._preload_downloading_model() is None
    assert dialog._local_model_download_state("small") == ""


def test_watch_timer_only_runs_while_the_local_tab_is_in_front():
    """A timer left running for the dialog's lifetime fires on every dialog."""
    class _Timer:
        def __init__(self):
            self.running = False

        def start(self):
            self.running = True

        def stop(self):
            self.running = False

    dialog = _Dialog(controller=_Controller(None))
    dialog._preload_download_watch_timer = _Timer()
    dialog._local_tab_index = 3

    dialog._sync_preload_download_watch(3)
    assert dialog._preload_download_watch_timer.running is True

    dialog._sync_preload_download_watch(0)
    assert dialog._preload_download_watch_timer.running is False


def test_watch_sync_before_the_timer_exists_is_harmless():
    dialog = _Dialog(controller=_Controller(None))
    dialog._local_tab_index = 3
    dialog._sync_preload_download_watch(3)  # must not raise
