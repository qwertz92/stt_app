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

    def device_state(self):
        return (self.opened_device_key, self.opening_device_key, self.is_opening)

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
        # Not published yet -- the two statements between the open's gate
        # and its settings read: the open then reads the saved selection.
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


class _FakeStream:
    """A `sd.InputStream` that opens at once and, on request, closes slowly."""

    block_close = None  # a threading.Event the test sets, or None
    closing = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        if _FakeStream.closing is not None:
            _FakeStream.closing.set()
        if _FakeStream.block_close is not None:
            assert _FakeStream.block_close.wait(timeout=5)


def _wait_until(condition, timeout=2.0):
    import time

    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def test_a_microphone_change_saved_during_the_device_query_restarts_the_open(
    monkeypatch,
):
    """The real warm stream, opening on the real controller thread: the
    device query blocks (a locked-down audio stack), the user saves another
    microphone meanwhile. The open must be restarted onto the new one."""
    import threading

    from stt_app import audio_devices

    resolving = threading.Event()
    release = threading.Event()
    resolved: list[str] = []

    def _resolve(name):
        resolved.append(name)
        if len(resolved) == 1:
            resolving.set()
            assert release.wait(timeout=5)
        return 3

    monkeypatch.setattr(audio_devices, "resolve_input_device", _resolve)
    monkeypatch.setattr("stt_app.audio_capture.sd.InputStream", _FakeStream)
    _FakeStream.block_close = None
    _FakeStream.closing = None
    controller, app = make_controller()
    controller._settings = replace(
        controller._settings, keep_microphone_warm=True, input_device_name="Mic A"
    )
    controller._sync_warm_microphone_stream()
    stream = controller._warm_mic_stream
    assert resolving.wait(timeout=5)
    assert stream.opening_device_key == "Mic A"

    controller._settings = replace(controller._settings, input_device_name="Mic B")
    controller._sync_warm_microphone_stream()
    release.set()

    assert _wait_until(lambda: stream.opened_device_key == "Mic B"), (
        stream.device_state(),
        resolved,
    )
    assert resolved == ["Mic A", "Mic B"]
    controller.shutdown()
    _ = app


def test_a_save_during_the_device_refresh_waits_for_the_reenumeration(monkeypatch):
    """The refresh worker is inside `close_if_idle`'s close when the user
    presses Save. The save's retry used to open a second stream beside the
    closing one, and the re-enumeration -- which refuses while any stream is
    registered -- was refused and deferred to the next recording stop."""
    import threading

    from stt_app import audio_devices

    monkeypatch.setattr(audio_devices, "resolve_input_device", lambda name: None)
    monkeypatch.setattr("stt_app.audio_capture.sd.InputStream", _FakeStream)
    _FakeStream.block_close = threading.Event()
    _FakeStream.closing = threading.Event()
    seen_live: list[int] = []
    live_before = audio_devices.live_stream_count()

    def _refresh(logger=None):
        seen_live.append(audio_devices.live_stream_count())
        return True

    monkeypatch.setattr(audio_devices, "try_refresh_input_devices", _refresh)
    controller, app = make_controller()
    controller._settings = replace(controller._settings, keep_microphone_warm=True)
    controller._sync_warm_microphone_stream()
    stream = controller._warm_mic_stream
    assert _wait_until(lambda: stream.is_running)

    worker = threading.Thread(
        target=controller._refresh_audio_devices_worker, daemon=True
    )
    worker.start()
    assert _FakeStream.closing.wait(timeout=5)
    # The save, while the close blocks.
    controller._sync_warm_microphone_stream()
    import time

    time.sleep(0.2)
    try:
        assert seen_live == [], "the re-enumeration ran before the close finished"
        assert audio_devices.live_stream_count() == live_before + 1
    finally:
        _FakeStream.block_close.set()
        worker.join(timeout=5)

    assert seen_live == [live_before], "the re-enumeration saw a live stream"
    assert controller._pending_audio_device_refresh is False
    assert _wait_until(lambda: stream.is_running)
    _FakeStream.block_close = None
    _FakeStream.closing = None
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
