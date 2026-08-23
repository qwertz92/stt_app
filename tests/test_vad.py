import math
import struct

import numpy as np

from stt_app.config import (
    DEFAULT_SILENCE_GATE_THRESHOLD,
    STREAMING_NEW_SEGMENT_MIN_SPEECH_S,
)
from stt_app.vad import EnergyVad, measure_longest_speech_run_s


def test_vad_detects_speech_then_silence_stop():
    vad = EnergyVad(
        sample_rate=1000,
        energy_threshold=0.05,
        min_speech_ms=100,
        max_silence_ms=200,
    )

    quiet = np.zeros(50, dtype=np.float32)  # 50ms
    loud = np.ones(50, dtype=np.float32) * 0.5  # 50ms

    for _ in range(3):
        decision = vad.process_chunk(quiet)
        assert decision.speech_started is False
        assert decision.should_stop is False

    decision = vad.process_chunk(loud)
    assert decision.speech_started is False

    decision = vad.process_chunk(loud)
    assert decision.speech_started is True
    assert decision.should_stop is False

    for _ in range(3):
        decision = vad.process_chunk(quiet)
        assert decision.should_stop is False

    decision = vad.process_chunk(quiet)
    assert decision.should_stop is True


def test_vad_reset_clears_state():
    vad = EnergyVad(sample_rate=1000, energy_threshold=0.05)
    loud = np.ones(200, dtype=np.float32) * 0.4

    vad.process_chunk(loud)
    assert vad.has_detected_speech is True

    vad.reset()
    assert vad.has_detected_speech is False


def _wav_bytes_from_float(audio: np.ndarray, sample_rate: int = 16000) -> bytes:
    import io
    import wave

    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm.tobytes())
    return buffer.getvalue()


def test_peak_windowed_rms_detects_short_quiet_speech():
    from stt_app.vad import peak_windowed_rms_from_wav

    # 1 s of silence with a single 100 ms whisper-level burst: windowing must
    # report the burst level, which full-recording averaging would dilute.
    audio = np.zeros(16000, dtype=np.float32)
    audio[8000:9600] = 0.05

    peak = peak_windowed_rms_from_wav(_wav_bytes_from_float(audio))

    assert peak > 0.03
    full_rms = float(np.sqrt(np.mean(audio * audio)))
    assert full_rms < 0.02


def test_peak_windowed_rms_reports_silence_and_bad_input():
    from stt_app.vad import peak_windowed_rms_from_wav

    silence = np.zeros(16000, dtype=np.float32)
    assert peak_windowed_rms_from_wav(_wav_bytes_from_float(silence)) < 0.0005
    assert peak_windowed_rms_from_wav(b"") == 0.0
    assert peak_windowed_rms_from_wav(b"RIFF") == 0.0


def test_unmeasurable_audio_is_not_reported_as_silence():
    """The silence gate must not treat undecodable audio as "no speech"."""
    from stt_app.vad import measure_peak_windowed_rms

    assert measure_peak_windowed_rms(b"") is None
    assert measure_peak_windowed_rms(b"RIFF") is None
    silence = _wav_bytes_from_float(np.zeros(16000, dtype=np.float32))
    assert measure_peak_windowed_rms(silence) == 0.0

def _pcm(milliseconds, amplitude, sample_rate=16000):
    count = int(sample_rate * milliseconds / 1000)
    return b"".join(
        struct.pack("<h", int(amplitude * math.sin(index / 8.0)))
        for index in range(count)
    )


def test_scattered_transients_do_not_add_up_to_speech():
    """Typing must not look like a spoken word.

    This meter decides whether a rolling window that follows a long pause may
    be appended to the transcript on trust, so anything it mistakes for
    speech becomes an invented sentence pasted into the document. A peak
    reading is cleared by one 5 ms click. Summing every loud window is
    cleared by two clicks 300 ms apart -- measured, they total exactly as
    much as one 150 ms word. Only the longest unbroken run separates them:
    speech is continuous, keystrokes are isolated spikes.
    """
    threshold = DEFAULT_SILENCE_GATE_THRESHOLD
    quiet = _pcm(300, 20)
    click = _pcm(5, 9000)

    one_click = quiet + click + quiet
    two_clicks = quiet + click + _pcm(300, 20) + click + quiet
    three_clicks = quiet + (click + _pcm(300, 20)) * 3
    spoken_word = quiet + _pcm(150, 6000) + quiet

    measure = measure_longest_speech_run_s
    assert measure(one_click, 16000, threshold) < STREAMING_NEW_SEGMENT_MIN_SPEECH_S
    assert measure(two_clicks, 16000, threshold) < STREAMING_NEW_SEGMENT_MIN_SPEECH_S, (
        "two clicks summed past the gate; the meter is adding runs instead "
        "of taking the longest"
    )
    assert (
        measure(three_clicks, 16000, threshold)
        < STREAMING_NEW_SEGMENT_MIN_SPEECH_S
    )
    assert (
        measure(spoken_word, 16000, threshold)
        >= STREAMING_NEW_SEGMENT_MIN_SPEECH_S
    ), "a real short word must still be able to extend the transcript"


def test_unmeasurable_audio_is_never_reported_as_silence():
    """Callers must not skip audio they could not measure."""
    assert measure_longest_speech_run_s(b"", 16000, 0.004) is None
    assert measure_longest_speech_run_s(b"\x00", 16000, 0.004) is None
    assert measure_longest_speech_run_s(_pcm(100, 6000), 0, 0.004) is None
