import math
import struct
import wave
from pathlib import Path

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



def _run(pcm):
    return measure_longest_speech_run_s(
        pcm,
        16000,
        DEFAULT_SILENCE_GATE_THRESHOLD,
        window_ms=STREAMING_SPEECH_RUN_WINDOW_MS,
    )


@pytest.mark.parametrize("duration_ms", [200, 250, 300, 600])
def test_a_real_spoken_word_still_counts_as_speech(duration_ms):
    """The accept side: rejecting speech is transcript loss too.

    A short answer after a pause -- "Ja.", "Stop." -- must still be able to
    extend the transcript. An earlier threshold of 0.35 dropped these
    outright.
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

_SAMPLE_WAV = Path(__file__).resolve().parents[1] / "samples" / "benchmark_sample.wav"


def _in_production_shape(pcm, sample_rate=16000):
    """Embed audio in 7 s of room tone and measure it the way production does.

    `_stream_window_has_speech` takes the longest run in the whole trailing
    window. Measuring short excerpts instead truncates every run at the
    excerpt edge and produces values the code never computes -- that mistake
    once halved this threshold below every desk transient.
    """
    quiet = _pcm(7000, 20, sample_rate)
    return measure_longest_speech_run_s(
        quiet + pcm,
        sample_rate,
        DEFAULT_SILENCE_GATE_THRESHOLD,
        window_ms=STREAMING_SPEECH_RUN_WINDOW_MS,
    )


def _real_speech_runs():
    """Contiguous above-threshold stretches of the repository sample."""
    with wave.open(str(_SAMPLE_WAV), "rb") as handle:
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    bucket = rate * STREAMING_SPEECH_RUN_WINDOW_MS // 1000
    loud = [
        float(np.sqrt(np.mean(samples[i:i + bucket] ** 2)))
        >= DEFAULT_SILENCE_GATE_THRESHOLD
        for i in range(0, len(samples) - bucket, bucket)
    ]
    runs, start = [], None
    for index, is_loud in enumerate(loud):
        if is_loud and start is None:
            start = index
        elif not is_loud and start is not None:
            runs.append(raw[start * bucket * 2:index * bucket * 2])
            start = None
    if start is not None:
        runs.append(raw[start * bucket * 2:])
    return rate, runs


def _word_with_closure(voiced_ms, closure_ms, rate=16000):
    """A word shaped like real speech: voiced, silent closure, voiced.

    A continuous tone is not a word. Every voiceless stop consonant leaves
    40-100 ms of genuine silence in the middle, which breaks the contiguous
    run this meter measures -- and that is precisely what three earlier
    thresholds failed to account for.
    """
    half = voiced_ms // 2
    return _pcm(half, 6000, rate) + _pcm(closure_ms, 20, rate) + _pcm(half, 6000, rate)


@pytest.mark.parametrize(
    ("label", "voiced_ms", "closure_ms"),
    [
        ("'Stopp.' with its stop closure", 180, 50),
        ("'Bitte.' with its tt closure", 160, 85),
        ("a 250 ms word with a 40 ms closure", 250, 40),
        ("a 300 ms word with a 60 ms closure", 300, 60),
    ],
)
def test_a_word_with_an_internal_closure_still_counts_as_speech(
    label, voiced_ms, closure_ms
):
    """The accept side, with realistically shaped words.

    Thresholds of 0.35, 0.15 and 0.18 were all set against continuous tones
    and each deleted these words after a pause -- silently, with only a
    debug log. The word is gone from the document AND from history.
    """
    measured = _in_production_shape(_word_with_closure(voiced_ms, closure_ms))
    assert measured >= STREAMING_NEW_SEGMENT_MIN_SPEECH_S, (
        f"{label} measured {measured:.3f}s and would be dropped after a pause"
    )


@pytest.mark.parametrize(
    ("label", "amplitude"),
    [
        ("digital silence", 0),
        ("room tone at -68 dBFS", 20),
        ("room tone at -64 dBFS", 30),
        # The real boundary is the gate itself at -48 dBFS, so test
        # right up against it rather than 16 dB below.
        ("room tone at -50 dBFS", 146),
    ],
)
def test_silence_never_clears_the_post_pause_gate(label, amplitude):
    """The one guarantee this gate actually provides.

    An earlier version of this test asserted that typing is rejected, using
    undecayed 5 ms clicks that measure 0.020 s -- four times below the cut,
    so it never came near the branch. Measured with a realistic decay tail, a
    single key clack measures exactly the cut and typing above ~130 wpm
    reports seconds of "speech". The gate does NOT filter keyboard noise.

    What it does block is silence, which is the case that once grew a
    transcript to 896 invented words with an open microphone.
    """
    measured = _in_production_shape(_pcm(500, amplitude))
    assert measured < STREAMING_NEW_SEGMENT_MIN_SPEECH_S, (
        f"{label} measured {measured:.3f}s and would let the model invent a "
        "sentence from silence"
    )


def test_the_gate_does_not_pretend_to_filter_keyboard_noise():
    """Pin the limitation so nobody claims the guarantee is wider than it is.

    Three rounds of review were spent on thresholds chosen as if keystrokes
    and short words were separable. They are not: a mechanical key clack and
    "Bitte." measure within one bucket of each other. Anyone tightening this
    has to change these numbers first, deliberately.
    """
    rate = 16000
    key_clack = b"".join(
        struct.pack(
            "<h",
            int(
                32767
                * 0.6
                * math.exp(-index / (rate * 18 / 1000))
                * math.sin(2 * math.pi * 900 * index / rate)
            ),
        )
        for index in range(int(rate * 0.25))
    )
    clack = _in_production_shape(key_clack, rate)
    spoken = _in_production_shape(_word_with_closure(160, 85))

    assert abs(clack - spoken) <= 0.02, (
        f"a key clack measures {clack:.3f}s and a short word {spoken:.3f}s -- "
        "if these have genuinely separated, the gate could be tightened"
    )

def test_the_overlap_between_short_speech_and_transients_is_acknowledged():
    """Pin the fact that the classes overlap, so nobody "fixes" it by raising

    the threshold again. Three thresholds have already been set as if a clean
    cut existed, and each one deleted real words.
    """
    shortest_word = _in_production_shape(_word_with_closure(180, 50))
    knuckle_knock = _in_production_shape(
        b"".join(
            struct.pack(
                "<h",
                int(
                    32767
                    * 0.4
                    * math.exp(-index / (16000 * 25 / 1000))
                    * math.sin(2 * math.pi * 180 * index / 16000)
                ),
            )
            for index in range(int(16000 * 0.25))
        )
    )
    assert abs(shortest_word - knuckle_knock) <= 0.02, (
        f"a short word measures {shortest_word:.3f}s and a knock "
        f"{knuckle_knock:.3f}s -- if these have genuinely separated, the "
        "threshold reasoning in config.py needs revisiting"
    )
    assert shortest_word >= STREAMING_NEW_SEGMENT_MIN_SPEECH_S, (
        "the cut is above the shortest realistic word, so that word is "
        "silently deleted after a pause"
    )
