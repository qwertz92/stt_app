"""Controller reactions to audio device changes and warm-stream lifecycle."""

from dataclasses import replace

import stt_app.controller as controller_module

from conftest import FakeCapture, make_controller


class _StubWarmStream:
    def __init__(self, opened_device_key=""):
        self._opened_device_key = opened_device_key
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
