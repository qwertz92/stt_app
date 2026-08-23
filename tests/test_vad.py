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


def test_the_known_cost_of_the_post_pause_gate_is_a_very_short_word():
    """Make the accepted loss explicit instead of leaving it implicit.

    The cut sits above every desk transient measured (the loudest, a door
    latch, reaches 0.140 s), which necessarily puts it above a word shorter
    than about 180 ms. That word is dropped after a pause. The trade is
    deliberate and asymmetric: a rejected word costs that word, while an
    admitted transient appends an invented sentence that then becomes the
    alignment anchor and can replace the entire transcript.

    If this ever needs to change, it is the transient measurements that have
    to move, not this assertion on its own.
    """
    very_short = _pcm(200, 20) + _pcm(120, 6000) + _pcm(200, 20)
    assert _run(very_short) < STREAMING_NEW_SEGMENT_MIN_SPEECH_S

    just_long_enough = _pcm(200, 20) + _pcm(180, 6000) + _pcm(200, 20)
    assert _run(just_long_enough) >= STREAMING_NEW_SEGMENT_MIN_SPEECH_S

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


def test_real_recorded_speech_clears_the_post_pause_gate():
    """The accept side, measured on the only real audio in the repository.

    Synthetic sine bursts hid this once already: they have no decay and no
    internal structure, so they answer a different question than speech does.
    """
    rate, runs = _real_speech_runs()
    assert runs, "no speech found in the sample; the fixture is broken"
    for index, run in enumerate(runs):
        measured = _in_production_shape(run, rate)
        assert measured >= STREAMING_NEW_SEGMENT_MIN_SPEECH_S, (
            f"real speech run {index} measured {measured:.3f}s and would be "
            "dropped after a pause"
        )


@pytest.mark.parametrize(
    ("label", "peak", "tau_ms", "hz"),
    [
        ("a knuckle knock on the desk", 0.4, 25, 180),
        ("a mechanical key clack", 0.6, 18, 900),
        ("a door latch", 0.3, 35, 120),
        ("a trackpad click", 0.2, 30, 300),
        ("a lip smack", 0.15, 40, 250),
    ],
)
def test_decaying_desk_transients_never_clear_the_post_pause_gate(
    label, peak, tau_ms, hz
):
    """The reject side. A transient that clears this gate does not merely add

    junk: the appended hallucination becomes the alignment anchor for the
    next window, which then cannot align and replaces the whole transcript.

    Rectangular bursts are not enough to test this -- they stop dead, while
    anything that resonates decays over 20-40 ms and measures far longer.
    """
    rate = 16000
    count = int(rate * 0.25)
    decay = b"".join(
        struct.pack(
            "<h",
            int(
                32767
                * peak
                * math.exp(-index / (rate * tau_ms / 1000))
                * math.sin(2 * math.pi * hz * index / rate)
            ),
        )
        for index in range(count)
    )
    measured = _in_production_shape(decay, rate)
    assert measured < STREAMING_NEW_SEGMENT_MIN_SPEECH_S, (
        f"{label} measured {measured:.3f}s and would append a hallucinated "
        "window into the document"
    )


def test_the_post_pause_cut_keeps_a_margin_over_the_loudest_transient():
    """Pin the reasoning, not just the number.

    A threshold that merely clears the loudest transient by one 20 ms bucket is
    one unmeasured desk sound away from admitting hallucinations again -- and an
    admitted transient can replace the whole transcript, not just add to it.
    Requiring a real margin is what stops the value drifting back down towards
    the transient band, which has already happened once.

    Deliberately excluded: a heavy low-frequency thump (peak 0.5, 45 ms decay
    at 150 Hz) measures 0.20 s, which is inside the range of a real 200 ms
    word. No energy threshold separates those two, so pretending one does by
    raising the cut would just delete more speech. That case is handled by
    bounding the damage instead -- see
    `test_an_unalignable_window_cannot_destroy_an_earlier_segment`.
    """
    rate = 16000
    loudest = 0.0
    for peak, tau_ms, hz in (
        (0.4, 25, 180),
        (0.6, 18, 900),
        (0.3, 35, 120),
        (0.2, 30, 300),
        (0.15, 40, 250),
    ):
        count = int(rate * 0.25)
        decay = b"".join(
            struct.pack(
                "<h",
                int(
                    32767
                    * peak
                    * math.exp(-index / (rate * tau_ms / 1000))
                    * math.sin(2 * math.pi * hz * index / rate)
                ),
            )
            for index in range(count)
        )
        loudest = max(loudest, _in_production_shape(decay, rate))

    assert STREAMING_NEW_SEGMENT_MIN_SPEECH_S >= loudest * 1.25, (
        f"the cut is {STREAMING_NEW_SEGMENT_MIN_SPEECH_S}s against a loudest "
        f"measured transient of {loudest:.3f}s -- less than a 25% margin, so an "
        "unmeasured desk sound will get through"
    )
