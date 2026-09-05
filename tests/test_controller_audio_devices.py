"""Controller reactions to audio device changes and warm-stream lifecycle."""

import threading
import time
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

    @property
    def is_running(self):
        return self._opened_device_key is not None

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


def _warm_controller_inside_a_blocking_close(monkeypatch, refresh):
    """A controller whose refresh worker is inside the warm stream's close.

    Returns the controller, the app, the warm stream and the worker thread;
    the close is released by setting `_FakeStream.block_close`.
    """
    from stt_app import audio_devices

    monkeypatch.setattr(audio_devices, "resolve_input_device", lambda name: None)
    monkeypatch.setattr("stt_app.audio_capture.sd.InputStream", _FakeStream)
    _FakeStream.block_close = threading.Event()
    _FakeStream.closing = threading.Event()
    monkeypatch.setattr(audio_devices, "try_refresh_input_devices", refresh)
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
    return controller, app, stream, worker


def _release_the_close(worker):
    _FakeStream.block_close.set()
    worker.join(timeout=5)
    _FakeStream.block_close = None
    _FakeStream.closing = None


def test_the_device_refresh_closes_the_warm_stream_outside_the_portaudio_guard(
    monkeypatch,
):
    """A cold recording start takes the PortAudio guard on the Qt thread,
    and a close on a locked-down audio stack is bounded by nothing. Held
    across the warm stream's close, the guard froze the Qt thread for the
    whole of it, after the start beep had already played (measured: 3.0 s
    for a 3 s close, 30.0 s for one that outlasted `_CLOSE_WAIT_S`). Only
    the re-enumeration needs PortAudio to itself."""
    from stt_app import audio_devices

    controller, app, stream, worker = _warm_controller_inside_a_blocking_close(
        monkeypatch, lambda logger=None: True
    )
    try:
        # What a cold `AudioCapture.start` on the Qt thread does first.
        guard = audio_devices.portaudio_guard()
        taken = guard.acquire(timeout=0.5)
        assert taken, "the refresh worker held the PortAudio guard across the close"
        guard.release()
    finally:
        _release_the_close(worker)

    assert controller._pending_audio_device_refresh is False
    assert _wait_until(lambda: stream.is_running)
    controller.shutdown()
    _ = app


def test_a_save_during_the_device_refresh_still_lets_the_reenumeration_run(
    monkeypatch,
):
    """The refresh worker is inside the warm stream's close when the user
    presses Save. The guard is free during a close, so the save's retry
    opens a second stream beside the closing one instead of queueing behind
    it -- and `close_if_idle`'s loop, which re-reads `_stream` after every
    close of its own, retires that stream too before answering True, so
    the re-enumeration still runs against an empty registry."""
    from stt_app import audio_devices

    seen_live: list[int] = []
    live_before = audio_devices.live_stream_count()

    def _refresh(logger=None):
        # The real one refuses while any stream is registered.
        live = audio_devices.live_stream_count()
        seen_live.append(live)
        return live == live_before

    controller, app, stream, worker = _warm_controller_inside_a_blocking_close(
        monkeypatch, _refresh
    )
    # The save, while the close blocks: its retry is not queued behind it.
    controller._sync_warm_microphone_stream()
    try:
        assert _wait_until(
            lambda: audio_devices.live_stream_count() == live_before + 2
        ), "the save's open waited for the close"
        assert _wait_until(lambda: stream.is_running)
        assert seen_live == [], "the re-enumeration ran before the close finished"
    finally:
        _release_the_close(worker)

    assert seen_live == [live_before]
    assert controller._pending_audio_device_refresh is False
    assert _wait_until(lambda: stream.is_running)
    controller.shutdown()
    _ = app


def test_a_stream_registered_before_the_reenumeration_is_closed_by_one_more_round(
    monkeypatch,
):
    """What `close_if_idle` cannot cover is the gap between its answer and
    the re-enumeration taking the guard: a warm open landing there opens on
    the stale device list and the re-enumeration is refused. The guard hold
    used to close that gap at the price of the Qt thread; the worker now
    closes the stream that slipped in and re-enumerates once more. The
    refresh stub registers the stream itself before refusing, which is the
    order the worker observes."""
    from stt_app import audio_devices

    monkeypatch.setattr(audio_devices, "resolve_input_device", lambda name: None)
    monkeypatch.setattr("stt_app.audio_capture.sd.InputStream", _FakeStream)
    seen_live: list[int] = []
    live_before = audio_devices.live_stream_count()
    controller, app = make_controller()
    controller._settings = replace(controller._settings, keep_microphone_warm=True)
    controller._sync_warm_microphone_stream()
    stream = controller._warm_mic_stream
    assert _wait_until(lambda: stream.is_running)

    def _refresh(logger=None):
        if not seen_live:
            # A settings save's retry that passed its gate in the gap.
            assert stream.ensure_started() is True
        live = audio_devices.live_stream_count()
        seen_live.append(live)
        return live == live_before

    monkeypatch.setattr(audio_devices, "try_refresh_input_devices", _refresh)

    controller._refresh_audio_devices_worker()

    assert seen_live == [live_before + 1, live_before]
    assert controller._pending_audio_device_refresh is False
    assert _wait_until(lambda: stream.is_running)
    controller.shutdown()
    _ = app


class _BlockingCloseIfIdleStub(_StubWarmStream):
    """`close_if_idle` blocks until the test releases it."""

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def close_if_idle(self):
        self.close_if_idle_calls += 1
        self.entered.set()
        assert self.release.wait(timeout=5)
        return True


def test_two_device_refreshes_run_one_after_the_other(monkeypatch):
    """Several device notifications more than the settle interval apart
    start a worker each. The PortAudio guard used to serialize them and is
    no longer held across the close; two workers inside one close would
    otherwise close what the other has just reopened, so they queue on a
    lock of their own."""
    controller, app = make_controller()
    stub = _BlockingCloseIfIdleStub()
    controller._warm_mic_stream = stub
    refresh_calls = []
    monkeypatch.setattr(
        controller_module.audio_devices,
        "try_refresh_input_devices",
        lambda logger=None: refresh_calls.append(True) or True,
    )
    first = threading.Thread(
        target=controller._refresh_audio_devices_worker, daemon=True
    )
    second = threading.Thread(
        target=controller._refresh_audio_devices_worker, daemon=True
    )
    first.start()
    assert stub.entered.wait(timeout=5)
    second.start()
    time.sleep(0.2)
    try:
        assert stub.close_if_idle_calls == 1, "the second worker ran beside the first"
    finally:
        stub.release.set()
        first.join(timeout=5)
        second.join(timeout=5)

    assert stub.close_if_idle_calls == 2
    assert refresh_calls == [True, True]
    assert stub.ensure_started_calls == 2
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


class _SlowCloseStream(_FakeStream):
    """A `_FakeStream` whose close sleeps `close_delay_s` first."""

    close_delay_s = 0.0

    def close(self):
        time.sleep(_SlowCloseStream.close_delay_s)
        super().close()


def test_a_stream_a_helper_is_closing_in_the_gap_is_waited_for_by_the_second_round(
    monkeypatch,
):
    """The second round asked `is_running` before `close_if_idle`, and a
    stream is registered while `_stream` is already None in exactly the
    states `close_if_idle` was written to wait for: a helper closing it
    outside the lock, an open in flight, a superseded open's retirement. A
    save's reopen landing in the gap and then handed to a restart helper
    made the first re-enumeration refuse, the gate skipped the second
    round, and the refresh was deferred to the next recording stop -- the
    symptom the docstring records as fixed."""
    from stt_app import audio_devices

    monkeypatch.setattr(audio_devices, "resolve_input_device", lambda name: None)
    monkeypatch.setattr("stt_app.audio_capture.sd.InputStream", _SlowCloseStream)
    _SlowCloseStream.close_delay_s = 0.0
    seen: list[tuple[int, bool]] = []
    live_before = audio_devices.live_stream_count()
    controller, app = make_controller()
    controller._settings = replace(controller._settings, keep_microphone_warm=True)
    controller._sync_warm_microphone_stream()
    stream = controller._warm_mic_stream
    assert _wait_until(lambda: stream.is_running)

    def _refresh(logger=None):
        if not seen:
            # A save's reopen that passed its gate in the gap, then a
            # microphone change hands it to a restart helper: registered,
            # `_stream` None, the helper inside a slow close.
            assert stream.ensure_started() is True
            _SlowCloseStream.close_delay_s = 0.3
            stream.request_restart()
            assert stream.is_running is False
        live = audio_devices.live_stream_count()
        seen.append((live, stream.is_running))
        return live == live_before

    monkeypatch.setattr(audio_devices, "try_refresh_input_devices", _refresh)
    try:
        controller._refresh_audio_devices_worker()
    finally:
        _SlowCloseStream.close_delay_s = 0.0

    assert seen == [(live_before + 1, False), (live_before, False)]
    assert controller._pending_audio_device_refresh is False
    assert _wait_until(lambda: stream.is_running)
    controller.shutdown()
    _ = app


def test_a_successful_device_refresh_discharges_an_earlier_refusal(monkeypatch):
    """A refused worker arms `_pending_audio_device_refresh`; a later worker
    that re-enumerated successfully left it armed, so the next recording
    stop closed, re-enumerated and reopened the warm stream for a refresh
    that had already happened -- on a locked-down stack, the cold-open
    latency the feature exists to hide, right after a dictation."""
    controller, app = make_controller()
    stub = _StubWarmStream()
    controller._warm_mic_stream = stub
    monkeypatch.setattr(
        controller_module.audio_devices,
        "try_refresh_input_devices",
        lambda logger=None: True,
    )
    controller._pending_audio_device_refresh = True

    controller._refresh_audio_devices_worker()

    assert controller._pending_audio_device_refresh is False
    assert stub.ensure_started_calls == 1
    controller.shutdown()
    _ = app


def test_a_refresh_deferred_during_the_worker_stays_owed(monkeypatch):
    """The discharge happens as the worker starts, not as it ends: a device
    event deferred on the Qt thread while this worker runs (a recording
    started meanwhile) may postdate the worker's enumeration."""
    controller, app = make_controller()
    stub = _StubWarmStream()
    controller._warm_mic_stream = stub

    def _refresh(logger=None):
        controller._pending_audio_device_refresh = True
        return True

    monkeypatch.setattr(
        controller_module.audio_devices, "try_refresh_input_devices", _refresh
    )

    controller._refresh_audio_devices_worker()

    assert controller._pending_audio_device_refresh is True
    controller.shutdown()
    _ = app
