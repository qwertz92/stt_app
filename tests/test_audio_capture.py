import threading
import wave
from io import BytesIO

import numpy as np

from stt_app.audio_capture import AudioCapture
from stt_app.vad import VadDecision


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
