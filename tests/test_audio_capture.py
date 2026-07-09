import threading
import wave
from io import BytesIO

import numpy as np

from stt_app.audio_capture import AudioCapture, WarmMicrophoneStream
from stt_app.vad import VadDecision


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
