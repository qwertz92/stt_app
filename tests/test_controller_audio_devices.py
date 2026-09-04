"""Controller reactions to audio device changes and warm-stream lifecycle."""

from dataclasses import replace

import pytest
from conftest import FakeCapture, make_controller

import stt_app.controller as controller_module


class _StubWarmStream:
    def __init__(
        self, opened_device_key="", *, is_opening=False, opening_device_key=None
    ):
        self._opened_device_key = opened_device_key
        self.is_opening = is_opening
        self.opening_device_key = opening_device_key
        self.request_close_calls = 0
        self.request_restart_calls = 0
        self.ensure_started_calls = 0
        self.close_if_idle_result = True
        self.close_if_idle_calls = 0
        self.close_calls = 0

    @property
    def opened_device_key(self):
        return self._opened_device_key

    def request_close(self):
        self.request_close_calls += 1

    def request_restart(self):
        self.request_restart_calls += 1

    def ensure_started(self):
        self.ensure_started_calls += 1
        return True

    def close_if_idle(self):
        self.close_if_idle_calls += 1
        return self.close_if_idle_result

    def close(self):
        self.close_calls += 1


def test_disabling_warm_stream_defers_close_instead_of_hard_close():
    controller, app = make_controller()
    stub = _StubWarmStream()
    controller._warm_mic_stream = stub
    controller._settings = replace(
        controller._settings, keep_microphone_warm=False
    )

    controller._sync_warm_microphone_stream()

    assert controller._warm_mic_stream is None
    assert stub.request_close_calls == 1
    assert stub.close_calls == 0
    controller.shutdown()
    _ = app


def test_changing_selected_microphone_restarts_warm_stream():
    controller, app = make_controller()
    stub = _StubWarmStream(opened_device_key="Old Mic")
    controller._warm_mic_stream = stub
    controller._settings = replace(
        controller._settings,
        keep_microphone_warm=True,
        input_device_name="New Mic",
    )

    controller._sync_warm_microphone_stream()

    assert stub.request_restart_calls == 1
    controller.shutdown()
    _ = app


def test_unchanged_selected_microphone_does_not_restart_warm_stream():
    controller, app = make_controller()
    stub = _StubWarmStream(opened_device_key="Same Mic")
    controller._warm_mic_stream = stub
    controller._settings = replace(
        controller._settings,
        keep_microphone_warm=True,
        input_device_name="Same Mic",
    )

    controller._sync_warm_microphone_stream()

    assert stub.request_restart_calls == 0
    assert stub.request_close_calls == 0
    controller.shutdown()
    _ = app


def test_resume_restart_delegates_to_request_restart_even_mid_recording():
    controller, app = make_controller()
    stub = _StubWarmStream()
    controller._warm_mic_stream = stub
    # The stream defers internally while attached, so the controller no
    # longer skips the restart when a capture exists (the old pre-check
    # raced recording start).
    controller._audio_capture = FakeCapture()

    controller._restart_warm_microphone_stream_after_resume()

    assert stub.request_restart_calls == 1
    controller._audio_capture = None
    controller.shutdown()
    _ = app


def test_device_change_defers_refresh_while_recording_active():
    controller, app = make_controller()
    controller._audio_capture = FakeCapture()

    controller._on_audio_device_change_settled()

    assert controller._pending_audio_device_refresh is True

    controller._audio_capture = None
    controller._maybe_resume_pending_audio_device_refresh()
    assert controller._pending_audio_device_refresh is False
    assert controller._audio_device_change_timer.isActive()
    controller.shutdown()
    _ = app


def test_refresh_worker_defers_when_warm_stream_is_attached(monkeypatch):
    controller, app = make_controller()
    stub = _StubWarmStream()
    stub.close_if_idle_result = False
    controller._warm_mic_stream = stub
    refresh_calls = []
    monkeypatch.setattr(
        controller_module.audio_devices,
        "try_refresh_input_devices",
        lambda logger=None: refresh_calls.append(True) or True,
    )

    controller._refresh_audio_devices_worker()

    assert controller._pending_audio_device_refresh is True
    assert refresh_calls == []
    assert stub.ensure_started_calls == 0
    controller.shutdown()
    _ = app


def test_refresh_worker_reenumerates_and_reopens_warm_stream(monkeypatch):
    controller, app = make_controller()
    stub = _StubWarmStream()
    controller._warm_mic_stream = stub
    refresh_calls = []
    monkeypatch.setattr(
        controller_module.audio_devices,
        "try_refresh_input_devices",
        lambda logger=None: refresh_calls.append(True) or True,
    )

    controller._refresh_audio_devices_worker()

    assert stub.close_if_idle_calls == 1
    assert refresh_calls == [True]
    assert stub.ensure_started_calls == 1
    assert controller._pending_audio_device_refresh is False
    controller.shutdown()
    _ = app


def test_device_change_signal_starts_coalescing_timer():
    controller, app = make_controller()

    controller._on_audio_devices_changed("default")

    assert controller._audio_device_change_timer.isActive()
    controller.shutdown()
    _ = app


def test_a_microphone_change_saved_while_the_warm_stream_opens_restarts_it(
    monkeypatch,
):
    """`opened_device_key` is None during an open, exactly as when it failed.

    The save took the retry branch, whose `ensure_started` no-ops on the
    `_starting` guard, so the in-flight open finished on the previous
    microphone and nothing restarted it: the warm stream was pinned to the
    old device for the session, `attach` refused it, and every recording
    cold-opened. The losing case is the slow open -- the one the feature
    exists for.
    """
    controller, app = make_controller()
    stub = _StubWarmStream(
        opened_device_key=None, is_opening=True, opening_device_key="Mic A"
    )
    controller._warm_mic_stream = stub
    controller._settings = replace(
        controller._settings, keep_microphone_warm=True, input_device_name="Mic B"
    )
    monkeypatch.setattr(
        controller,
        "_start_warm_microphone_stream_async",
        lambda: stub.ensure_started(),
    )

    controller._sync_warm_microphone_stream()

    assert stub.request_restart_calls == 1
    assert stub.ensure_started_calls == 0
    controller.shutdown()
    _ = app


@pytest.mark.parametrize(
    ("resolving", "selected"),
    [
        ("Mic B", "Mic B"),
        ("", ""),
        # Not resolved yet: the open reads the live settings itself.
        (None, "Mic B"),
    ],
)
def test_a_save_that_keeps_the_microphone_does_not_restart_an_open_in_flight(
    monkeypatch, resolving, selected
):
    """`opened_device_key` is None during every open, so comparing it with the
    selected device restarted the open on *every* save -- an opacity or hotkey
    change discarded the in-flight open and paid the cold-open latency the
    warm stream exists to hide, twice."""
    controller, app = make_controller()
    stub = _StubWarmStream(
        opened_device_key=None, is_opening=True, opening_device_key=resolving
    )
    controller._warm_mic_stream = stub
    controller._settings = replace(
        controller._settings,
        keep_microphone_warm=True,
        input_device_name=selected,
    )
    monkeypatch.setattr(
        controller,
        "_start_warm_microphone_stream_async",
        lambda: stub.ensure_started(),
    )

    controller._sync_warm_microphone_stream()

    assert stub.request_restart_calls == 0
    assert stub.ensure_started_calls == 0
    controller.shutdown()
    _ = app


def test_a_failed_earlier_open_is_retried_on_save_without_a_restart(monkeypatch):
    controller, app = make_controller()
    stub = _StubWarmStream(opened_device_key=None, is_opening=False)
    controller._warm_mic_stream = stub
    controller._settings = replace(
        controller._settings, keep_microphone_warm=True, input_device_name="Mic B"
    )
    monkeypatch.setattr(
        controller,
        "_start_warm_microphone_stream_async",
        lambda: stub.ensure_started(),
    )

    controller._sync_warm_microphone_stream()

    assert stub.ensure_started_calls == 1
    assert stub.request_restart_calls == 0
    controller.shutdown()
    _ = app
