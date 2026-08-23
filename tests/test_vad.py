import math
import struct

import numpy as np
import pytest

from stt_app.config import (
    DEFAULT_SILENCE_GATE_THRESHOLD,
    STREAMING_NEW_SEGMENT_MIN_SPEECH_S,
    STREAMING_SPEECH_RUN_WINDOW_MS,
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


def _clicks(count, gap_ms, click_ms=5, amplitude=9000):
    audio = _pcm(200, 20)
    for _ in range(count):
        audio += _pcm(click_ms, amplitude) + _pcm(gap_ms, 20)
    return audio + _pcm(200, 20)


def _run(pcm):
    return measure_longest_speech_run_s(
        pcm,
        16000,
        DEFAULT_SILENCE_GATE_THRESHOLD,
        window_ms=STREAMING_SPEECH_RUN_WINDOW_MS,
    )


@pytest.mark.parametrize(
    ("label", "count", "gap_ms", "click_ms"),
    [
        ("one click", 1, 300, 5),
        ("two clicks 300 ms apart", 2, 300, 5),
        ("two clicks 150 ms apart", 2, 150, 5),
        ("two clicks 100 ms apart", 2, 100, 5),
        ("a mouse double-click", 2, 80, 30),
        ("typing at 80 wpm", 12, 150, 5),
        ("typing at 120 wpm", 15, 100, 5),
        ("one long 50 ms click", 1, 300, 50),
    ],
)
def test_typing_is_never_mistaken_for_speech(label, count, gap_ms, click_ms):
    """Keystrokes must not authorise appending a hallucinated window.

    This meter decides whether a rolling window after a long pause may be
    appended to the transcript on trust, so whatever it takes for speech
    becomes an invented sentence pasted into the document.

    The bucket size is what matters here, not the threshold. At 100 ms
    buckets two keystrokes 100-150 ms apart land in ADJACENT buckets, the
    run never breaks, and typing at 120 wpm measured a 1.5 s "speech" run --
    longer than most words. At 20 ms the gap between keystrokes gets its own
    bucket and breaks the run.
    """
    measured = _run(_clicks(count, gap_ms, click_ms=click_ms))
    assert measured < STREAMING_NEW_SEGMENT_MIN_SPEECH_S, (
        f"{label} measured {measured:.3f}s and would be appended as speech"
    )


@pytest.mark.parametrize("duration_ms", [150, 200, 300, 600])
def test_a_real_spoken_word_still_counts_as_speech(duration_ms):
    """The other side of the cut: rejecting speech is transcript loss.

    A short answer after a pause -- "Ja.", "Stop." -- must still be able to
    extend the transcript. An earlier threshold dropped exactly these.
    """
    audio = _pcm(200, 20) + _pcm(duration_ms, 6000) + _pcm(200, 20)
    measured = _run(audio)
    assert measured >= STREAMING_NEW_SEGMENT_MIN_SPEECH_S, (
        f"a {duration_ms} ms word measured {measured:.3f}s and would be dropped"
    )

def test_unmeasurable_audio_is_never_reported_as_silence():
    """Callers must not skip audio they could not measure."""
    assert measure_longest_speech_run_s(b"", 16000, 0.004) is None
    assert measure_longest_speech_run_s(b"\x00", 16000, 0.004) is None
    assert measure_longest_speech_run_s(_pcm(100, 6000), 0, 0.004) is None
