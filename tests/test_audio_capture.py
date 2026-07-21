import threading
import time
import wave
from io import BytesIO

import numpy as np
import pytest

from stt_app.audio_capture import (
    AudioCapture,
    AudioCaptureError,
    WarmMicrophoneStream,
)
from stt_app.audio_devices import InputDeviceNotFoundError
from stt_app.vad import VadDecision


def _wait_until(condition, timeout=2.0):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class FakeInputStream:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False
        self.closed = False
        FakeInputStream.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True


class FakeVad:
    def __init__(self, decision):
        self._decision = decision
        self.reset_called = False

    def reset(self):
        self.reset_called = True

    def process_chunk(self, chunk):
        return self._decision


def test_to_wav_bytes_has_expected_header():
    capture = AudioCapture(sample_rate=16000, channels=1)
    audio = np.ones(160, dtype=np.float32) * 0.1

    wav_bytes = capture._to_wav_bytes(audio)

    with wave.open(BytesIO(wav_bytes), "rb") as wav_file:
        assert wav_file.getframerate() == 16000
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2


def test_stop_returns_empty_bytes_without_chunks():
    capture = AudioCapture()

    wav_bytes = capture.stop()

    assert wav_bytes == b""


def test_auto_stop_callback_runs_once_when_vad_requests_stop():
    event = threading.Event()
    call_count = {"count": 0}

    def callback():
        call_count["count"] += 1
        event.set()

    vad = FakeVad(VadDecision(should_stop=True))
    capture = AudioCapture(vad=vad, auto_stop_callback=callback)

    chunk = np.ones((160, 1), dtype=np.float32) * 0.1
    capture._on_audio(chunk, 160, None, None)
    capture._on_audio(chunk, 160, None, None)

    assert event.wait(timeout=1.0)
    assert call_count["count"] == 1


def test_capture_attaches_to_running_warm_stream(monkeypatch):
    monkeypatch.setattr("stt_app.audio_capture.sd.InputStream", FakeInputStream)
    FakeInputStream.instances = []
    warm = WarmMicrophoneStream(sample_rate=16000, channels=1)
    assert warm.ensure_started() is True
    assert len(FakeInputStream.instances) == 1

    capture = AudioCapture(sample_rate=16000, channels=1, warm_stream=warm)
    capture.start()

    # No second device stream is opened; audio flows via the warm dispatch.
    assert len(FakeInputStream.instances) == 1
    assert capture.is_recording is True
    chunk = np.ones((160, 1), dtype=np.float32) * 0.25
    warm._dispatch(chunk, 160, None, None)
    wav_bytes = capture.stop()
    assert wav_bytes
    # The warm stream keeps running for the next recording.
    assert warm.is_running is True
    # After detaching, further audio is discarded instead of recorded.
    recorded_chunks = len(capture._chunks)
    warm._dispatch(chunk, 160, None, None)
    assert len(capture._chunks) == recorded_chunks
    warm.close()
    assert FakeInputStream.instances[0].closed is True


def test_capture_falls_back_to_cold_stream_when_warm_not_running(monkeypatch):
    monkeypatch.setattr("stt_app.audio_capture.sd.InputStream", FakeInputStream)
    FakeInputStream.instances = []
    warm = WarmMicrophoneStream(sample_rate=16000, channels=1)

    capture = AudioCapture(sample_rate=16000, channels=1, warm_stream=warm)
    capture.start()

    # The warm stream was never started, so the capture opens its own stream.
    assert len(FakeInputStream.instances) == 1
    assert FakeInputStream.instances[0].started is True
    capture.stop()
    assert FakeInputStream.instances[0].closed is True


def test_warm_stream_allows_single_consumer(monkeypatch):
    monkeypatch.setattr("stt_app.audio_capture.sd.InputStream", FakeInputStream)
    FakeInputStream.instances = []
    warm = WarmMicrophoneStream(sample_rate=16000, channels=1)
    assert warm.ensure_started()

    assert warm.attach(lambda *a: None) is True
    assert warm.attach(lambda *a: None) is False
    warm.close()


def test_chunk_callback_receives_pcm16_bytes():
    received = {"payload": b""}

    def on_chunk(payload: bytes) -> None:
        received["payload"] = payload

    capture = AudioCapture(chunk_callback=on_chunk)
    chunk = np.array([[0.5], [-0.5], [0.0]], dtype=np.float32)

    capture._on_audio(chunk, 3, None, None)

    payload = received["payload"]
    assert isinstance(payload, bytes)
    assert len(payload) == 6  # 3 samples * int16


def test_capture_tracks_received_callback_state():
    capture = AudioCapture()
    chunk = np.ones((160, 1), dtype=np.float32) * 0.1

    assert capture.callback_count == 0
    assert capture.has_received_audio is False

    capture._on_audio(chunk, 160, None, None)

    assert capture.callback_count == 1
    assert capture.has_received_audio is True


def test_cold_stream_close_runs_even_when_stop_fails(monkeypatch):
    class StopFailingStream(FakeInputStream):
        def stop(self):
            raise RuntimeError("stop failed")

    monkeypatch.setattr("stt_app.audio_capture.sd.InputStream", StopFailingStream)
    FakeInputStream.instances = []
    capture = AudioCapture()
    capture.start()
    stream = FakeInputStream.instances[0]

    capture.stop()

    assert stream.closed is True


def test_warm_start_failure_closes_partially_opened_stream(monkeypatch):
    class StartFailingStream(FakeInputStream):
        def start(self):
            raise RuntimeError("start failed")

    monkeypatch.setattr("stt_app.audio_capture.sd.InputStream", StartFailingStream)
    FakeInputStream.instances = []
    warm = WarmMicrophoneStream()

    assert warm.ensure_started() is False

    assert FakeInputStream.instances[0].closed is True


def test_attach_does_not_block_while_warm_stream_is_starting(monkeypatch):
    start_entered = threading.Event()
    allow_start = threading.Event()

    class BlockingStartStream(FakeInputStream):
        def start(self):
            start_entered.set()
            assert allow_start.wait(timeout=1.0)
            super().start()

    monkeypatch.setattr("stt_app.audio_capture.sd.InputStream", BlockingStartStream)
    warm = WarmMicrophoneStream()
    starter = threading.Thread(target=warm.ensure_started)
    starter.start()
    assert start_entered.wait(timeout=1.0)

    started = time.perf_counter()
    attached = warm.attach(lambda *_args: None)
    elapsed = time.perf_counter() - started

    assert attached is False
    assert elapsed < 0.1
    allow_start.set()
    starter.join(timeout=1.0)
    warm.close()


def test_warm_attach_requires_matching_device_key(monkeypatch):
    monkeypatch.setattr("stt_app.audio_capture.sd.InputStream", FakeInputStream)
    FakeInputStream.instances = []
    warm = WarmMicrophoneStream(
        sample_rate=16000,
        channels=1,
        device_provider=lambda: ("mic-a", 3),
    )
    assert warm.ensure_started() is True
    assert warm.opened_device_key == "mic-a"
    assert FakeInputStream.instances[0].kwargs["device"] == 3

    capture = AudioCapture(
        sample_rate=16000,
        channels=1,
        warm_stream=warm,
        device_key="mic-b",
        device_resolver=lambda: 7,
    )
    capture.start()

    # The warm stream serves a different device, so the capture opened its
    # own cold stream on the selected one instead of attaching.
    assert capture.uses_warm_stream is False
    assert len(FakeInputStream.instances) == 2
    assert FakeInputStream.instances[1].kwargs["device"] == 7
    capture.stop()
    warm.close()


def test_cold_open_with_missing_selected_device_raises(monkeypatch):
    monkeypatch.setattr("stt_app.audio_capture.sd.InputStream", FakeInputStream)
    FakeInputStream.instances = []

    def _resolver():
        raise InputDeviceNotFoundError("Gone Mic")

    capture = AudioCapture(device_key="Gone Mic", device_resolver=_resolver)

    with pytest.raises(AudioCaptureError) as excinfo:
        capture.start()

    assert "Gone Mic" in str(excinfo.value)
    assert "not connected" in str(excinfo.value)
    assert capture.is_recording is False
    assert FakeInputStream.instances == []


def test_warm_restart_is_deferred_while_a_recording_is_attached(monkeypatch):
    monkeypatch.setattr("stt_app.audio_capture.sd.InputStream", FakeInputStream)
    FakeInputStream.instances = []
    warm = WarmMicrophoneStream(sample_rate=16000, channels=1)
    assert warm.ensure_started() is True
    capture = AudioCapture(sample_rate=16000, channels=1, warm_stream=warm)
    capture.start()
    assert capture.uses_warm_stream is True

    warm.request_restart()

    # Still the original stream: the restart must not cut off the recording.
    assert len(FakeInputStream.instances) == 1
    assert FakeInputStream.instances[0].closed is False
    chunk = np.ones((160, 1), dtype=np.float32) * 0.25
    warm._dispatch(chunk, 160, None, None)
    assert len(capture._chunks) == 1

    capture.stop()
    # Detach executes the deferred restart on a worker thread.
    assert _wait_until(
        lambda: len(FakeInputStream.instances) == 2
        and FakeInputStream.instances[0].closed
        and FakeInputStream.instances[1].started
    )
    assert warm.is_running is True
    warm.close()


def test_warm_request_close_is_deferred_until_detach(monkeypatch):
    monkeypatch.setattr("stt_app.audio_capture.sd.InputStream", FakeInputStream)
    FakeInputStream.instances = []
    warm = WarmMicrophoneStream(sample_rate=16000, channels=1)
    assert warm.ensure_started() is True
    capture = AudioCapture(sample_rate=16000, channels=1, warm_stream=warm)
    capture.start()

    warm.request_close()

    # The recording keeps its audio source until it stops.
    assert FakeInputStream.instances[0].closed is False
    chunk = np.ones((160, 1), dtype=np.float32) * 0.25
    warm._dispatch(chunk, 160, None, None)
    assert len(capture._chunks) == 1
    # A pending close also refuses new consumers.
    assert warm.attach(lambda *a: None) is False

    capture.stop()
    assert _wait_until(lambda: FakeInputStream.instances[0].closed)
    assert warm.is_running is False


def test_warm_request_close_without_consumer_closes(monkeypatch):
    monkeypatch.setattr("stt_app.audio_capture.sd.InputStream", FakeInputStream)
    FakeInputStream.instances = []
    warm = WarmMicrophoneStream(sample_rate=16000, channels=1)
    assert warm.ensure_started() is True

    warm.request_close()

    assert _wait_until(lambda: FakeInputStream.instances[0].closed)
    assert warm.is_running is False


def test_warm_restart_reresolves_the_device(monkeypatch):
    monkeypatch.setattr("stt_app.audio_capture.sd.InputStream", FakeInputStream)
    FakeInputStream.instances = []
    device = {"key": "mic-a", "index": 1}
    warm = WarmMicrophoneStream(
        sample_rate=16000,
        channels=1,
        device_provider=lambda: (device["key"], device["index"]),
    )
    assert warm.ensure_started() is True
    assert FakeInputStream.instances[0].kwargs["device"] == 1

    device["key"] = "mic-b"
    device["index"] = 5
    warm.request_restart()

    assert _wait_until(
        lambda: len(FakeInputStream.instances) == 2
        and FakeInputStream.instances[0].closed
        and warm.opened_device_key == "mic-b"
    )
    assert FakeInputStream.instances[1].kwargs["device"] == 5
    warm.close()


def test_warm_close_if_idle_refuses_while_attached(monkeypatch):
    monkeypatch.setattr("stt_app.audio_capture.sd.InputStream", FakeInputStream)
    FakeInputStream.instances = []
    warm = WarmMicrophoneStream(sample_rate=16000, channels=1)
    assert warm.ensure_started() is True
    capture = AudioCapture(sample_rate=16000, channels=1, warm_stream=warm)
    capture.start()

    assert warm.close_if_idle() is False
    assert FakeInputStream.instances[0].closed is False

    capture.stop()
    assert warm.close_if_idle() is True
    assert FakeInputStream.instances[0].closed is True
    assert warm.is_running is False


def test_late_warm_callback_cannot_write_into_next_recording(monkeypatch):
    monkeypatch.setattr("stt_app.audio_capture.sd.InputStream", FakeInputStream)
    warm = WarmMicrophoneStream(sample_rate=16000, channels=1)
    assert warm.ensure_started()
    capture = AudioCapture(sample_rate=16000, channels=1, warm_stream=warm)
    chunk = np.ones((160, 1), dtype=np.float32) * 0.25

    capture.start()
    old_callback = warm._consumer
    assert old_callback is not None
    capture.stop()
    capture.start()
    new_callback = warm._consumer
    assert new_callback is not None

    old_callback(chunk, 160, None, None)
    assert capture._chunks == []
    new_callback(chunk, 160, None, None)
    assert len(capture._chunks) == 1
    capture.stop()
    warm.close()


def test_cold_open_passes_extra_settings_for_explicit_device(monkeypatch):
    """Explicitly selected (WASAPI) devices need host-API stream settings.

    Without them, WASAPI shared mode rejects the app's 16 kHz capture rate
    with paInvalidSampleRate (-9997) when the endpoint mix format differs.
    """
    monkeypatch.setattr("stt_app.audio_capture.sd.InputStream", FakeInputStream)
    FakeInputStream.instances = []
    sentinel = object()
    seen: list[int | None] = []

    def fake_extra_settings(device_index):
        seen.append(device_index)
        return sentinel

    monkeypatch.setattr(
        "stt_app.audio_capture.input_stream_extra_settings",
        fake_extra_settings,
    )
    capture = AudioCapture(
        sample_rate=16000,
        channels=1,
        device_resolver=lambda: 7,
    )

    capture.start()

    assert seen == [7]
    assert FakeInputStream.instances[0].kwargs["device"] == 7
    assert FakeInputStream.instances[0].kwargs["extra_settings"] is sentinel
    capture.stop()


def test_warm_open_passes_extra_settings_for_explicit_device(monkeypatch):
    monkeypatch.setattr("stt_app.audio_capture.sd.InputStream", FakeInputStream)
    FakeInputStream.instances = []
    sentinel = object()

    monkeypatch.setattr(
        "stt_app.audio_capture.input_stream_extra_settings",
        lambda device_index: sentinel if device_index == 7 else None,
    )
    warm = WarmMicrophoneStream(
        sample_rate=16000,
        channels=1,
        device_provider=lambda: ("USB Microphone", 7),
    )

    assert warm.ensure_started()

    assert FakeInputStream.instances[0].kwargs["device"] == 7
    assert FakeInputStream.instances[0].kwargs["extra_settings"] is sentinel
    warm.close()
