"""The Silero VAD v6 speech check (`stt_app.silero_vad`).

Every test that runs the real graph requests `real_silero`: the suite-wide
autouse fixture in conftest makes the detector unavailable, because the rest
of the suite drives the gates with sine tones, which a speech model rightly
rejects.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import threading
import time
import types
import wave
from pathlib import Path

import numpy as np
import pytest
from speech_fixtures import (
    after_a_pause,
    at_peak_level,
    concat,
    excerpt_attribution,
    key_clack,
    knuckle_knock,
    low_thump,
    pcm_bytes,
    room_tone,
    speech_excerpts,
    typing,
)

from stt_app import silero_vad
from stt_app.config import (
    SILENCE_GATE_THRESHOLD_MIN,
    SILERO_BATCH_MIN_PROBABILITY,
    SILERO_STREAM_MIN_PROBABILITY,
)

_SRC = Path(__file__).resolve().parents[1] / "src"


def test_the_asset_is_found_without_importing_faster_whisper(tmp_path):
    """faster-whisper's package init imports ctranslate2, tokenizers and the
    Hub client; finding one data file must not pay for that, and the import
    is what `importlib.util.find_spec` avoids. Checked in a fresh interpreter,
    because other tests of the suite import faster_whisper in-process."""
    env = {
        **os.environ,
        "PYTHONPATH": str(_SRC),
        "APPDATA": str(tmp_path),
        "LOCALAPPDATA": str(tmp_path),
        "HF_HUB_OFFLINE": "1",
    }
    code = (
        "import sys\n"
        "from stt_app.silero_vad import silero_asset_path\n"
        "path = silero_asset_path()\n"
        "print(path)\n"
        "print('faster_whisper' in sys.modules)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=True,
    )
    path_line, imported_line = result.stdout.strip().splitlines()[-2:]
    path = Path(path_line)
    assert path.name == "silero_vad_v6.onnx"
    assert path.parent.name == "assets"
    assert path.is_file() and path.stat().st_size > 0
    assert imported_line == "False", "the lookup imported faster_whisper"


def test_no_faster_whisper_means_no_asset(monkeypatch):
    monkeypatch.setattr(silero_vad.importlib.util, "find_spec", lambda _name: None)
    assert silero_vad.silero_asset_path() is None


def test_a_package_without_the_file_means_no_asset(monkeypatch, tmp_path):
    spec = types.SimpleNamespace(
        submodule_search_locations=[str(tmp_path)], origin=str(tmp_path / "x.py")
    )
    monkeypatch.setattr(silero_vad.importlib.util, "find_spec", lambda _name: spec)
    assert silero_vad.silero_asset_path() is None


def _wav(samples, *, rate=16_000, channels=1, sampwidth=2) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sampwidth)
        wav_file.setframerate(rate)
        wav_file.writeframes(bytes(samples))
    return buffer.getvalue()


_SPEECH = speech_excerpts()["phrase_1030ms"]


@pytest.mark.parametrize(
    "wav",
    [
        b"",
        b"RIFF",
        b"not a wav file at all",
        _wav(b"\x10\x20" * 800, sampwidth=1),
        _wav(pcm_bytes(_SPEECH), rate=44_100),
        _wav(pcm_bytes(concat(_SPEECH, _SPEECH)), channels=2),
        _wav(b""),
    ],
    ids=["empty", "truncated", "garbage", "8-bit", "44.1kHz", "stereo", "no-frames"],
)
def test_audio_the_graph_cannot_take_answers_none(real_silero, wav):
    """None is "could not measure", which every caller must read as "do not
    gate". A 44.1 kHz or stereo file would be measured as something it is
    not, so it is refused rather than guessed at."""
    assert silero_vad.measure_speech_wav(wav) is None


@pytest.mark.parametrize(
    ("pcm", "rate"),
    [(b"", 16_000), (b"\x01", 16_000), (pcm_bytes(_SPEECH), 8_000)],
    ids=["empty", "half-a-sample", "8kHz"],
)
def test_pcm_the_graph_cannot_take_answers_none(real_silero, pcm, rate):
    assert silero_vad.measure_speech_pcm16(pcm, rate) is None


def _fail(*_args, **_kwargs):
    raise RuntimeError("simulated onnxruntime failure")


def test_a_missing_asset_answers_none(real_silero, monkeypatch):
    monkeypatch.setattr(silero_vad, "silero_asset_path", lambda: None)
    assert silero_vad.measure_speech_pcm16(pcm_bytes(_SPEECH), 16_000) is None
    assert silero_vad.speech_probabilities(np.zeros(1600, np.float32)) is None


def test_a_session_that_cannot_be_built_answers_none(real_silero, monkeypatch):
    import onnxruntime

    monkeypatch.setattr(onnxruntime, "InferenceSession", _fail)
    assert silero_vad.measure_speech_pcm16(pcm_bytes(_SPEECH), 16_000) is None


def test_a_graph_with_another_contract_answers_none(real_silero, monkeypatch):
    """A later faster-whisper may ship a graph with other inputs under the
    same name. Fed our tensors it would fail or, worse, answer numbers that
    mean something else; checking the inputs once at load keeps that out."""
    import onnxruntime

    class _OtherGraph:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_inputs(self):
            return [types.SimpleNamespace(name="input", shape=[1, 512])]

        def run(self, *_args, **_kwargs):  # pragma: no cover - must not be reached
            raise AssertionError("a graph with another contract was run")

    monkeypatch.setattr(onnxruntime, "InferenceSession", _OtherGraph)
    assert silero_vad.measure_speech_pcm16(pcm_bytes(_SPEECH), 16_000) is None


def test_a_run_that_fails_answers_none(real_silero, monkeypatch):
    session = silero_vad._get_session()
    assert session is not None

    class _Broken:
        def run(self, *_args, **_kwargs):
            raise RuntimeError("simulated run failure")

    monkeypatch.setattr(silero_vad, "_get_session", lambda: _Broken())
    assert silero_vad.measure_speech_pcm16(pcm_bytes(_SPEECH), 16_000) is None
    assert silero_vad.speech_probabilities(np.zeros(1600, np.float32)) is None


def test_a_failed_load_is_remembered_instead_of_retried(real_silero, monkeypatch):
    """Every post-pause partial asks the detector; a load retried on each of
    them would pay the failure -- and log it -- every 350 ms."""
    attempts = []
    monkeypatch.setattr(
        silero_vad, "silero_asset_path", lambda: attempts.append(1) or None
    )
    for _ in range(3):
        assert silero_vad.measure_speech_pcm16(pcm_bytes(_SPEECH), 16_000) is None
    assert len(attempts) == 1


def test_two_threads_share_one_session(real_silero, monkeypatch):
    """The stream worker and the Qt thread (batch gate) can both be first.
    The load is widened with a sleep so the second thread really arrives
    while the first is still building; exactly one session may result, and
    both threads must measure what one thread measures alone."""
    builds = []
    real_build = silero_vad._build_session

    def _slow_build():
        builds.append(threading.get_ident())
        time.sleep(0.2)
        return real_build()

    monkeypatch.setattr(silero_vad, "_build_session", _slow_build)
    pcm = pcm_bytes(after_a_pause(_SPEECH))
    barrier = threading.Barrier(2)
    results: list[object] = [None, None]
    errors: list[BaseException] = []

    def _worker(index):
        try:
            barrier.wait(timeout=10)
            results[index] = silero_vad.measure_speech_pcm16(pcm, 16_000)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors
    assert len(builds) == 1, f"{len(builds)} sessions were built"
    alone = silero_vad.measure_speech_pcm16(pcm, 16_000)
    assert results[0] == results[1] == alone
    assert alone is not None and alone.max_probability > 0.5


def test_the_probabilities_match_faster_whispers_own_runner(real_silero):
    """The window, context and state contract, checked against the runner
    that ships with the graph. faster-whisper's runner zeroes the last 64
    samples of the caller's own array (it writes through a view to build the
    first window's context), so its final window differs by design and is
    left out; every other window must agree exactly."""
    from faster_whisper.vad import SileroVADModel

    samples = after_a_pause(_SPEECH).astype(np.float32) / 32768.0
    padded = np.pad(samples, (0, -samples.size % 512)).astype(np.float32)
    reference = np.asarray(
        SileroVADModel(str(silero_vad.silero_asset_path()))(padded.copy())
    ).reshape(-1)
    ours = silero_vad.speech_probabilities(samples)
    assert ours is not None
    assert ours.shape == reference.shape
    np.testing.assert_array_equal(ours[:-1], reference[:-1])


def test_scanning_in_blocks_measures_what_one_pass_measures(real_silero):
    """The batch scan runs block by block so it can stop early; the state and
    the 64-sample context are carried across block boundaries, and a slip
    there changes the probabilities of exactly the windows after each
    boundary."""
    excerpts = speech_excerpts()
    samples = concat(
        room_tone(1.3),
        excerpts["phrase_1700ms"],
        room_tone(0.7, seed=3),
        excerpts["phrase_1030ms"],
        room_tone(1.1, seed=4),
    )
    probabilities = silero_vad.speech_probabilities(samples.astype(np.float32) / 32768.0)
    measured = silero_vad.measure_speech_pcm16(pcm_bytes(samples), 16_000)
    assert probabilities is not None and measured is not None
    assert samples.size > 3 * silero_vad.BLOCK_WINDOWS * 512
    assert measured.complete
    assert measured.max_probability == pytest.approx(float(probabilities.max()), abs=0)
    speech = probabilities >= silero_vad.SILERO_SPEECH_PROBABILITY
    assert measured.speech_seconds == pytest.approx(
        int(speech.sum()) * silero_vad.WINDOW_SECONDS
    )


@pytest.mark.parametrize("name", sorted(speech_excerpts()))
def test_real_speech_after_a_pause_is_far_above_both_gates(real_silero, name):
    """The excerpts scored 0.93-0.99 in the calibration; Silero's own speech
    threshold (0.5) is more than twice either gate."""
    measured = silero_vad.measure_speech_pcm16(
        pcm_bytes(after_a_pause(speech_excerpts()[name])), 16_000
    )
    assert measured is not None
    assert measured.max_probability >= 0.5
    assert measured.max_probability >= 2 * max(
        SILERO_STREAM_MIN_PROBABILITY, SILERO_BATCH_MIN_PROBABILITY
    )


@pytest.mark.parametrize(
    ("label", "samples"),
    [
        ("digital silence", np.zeros(8 * 16_000, np.int16)),
        ("room tone -54 dBFS", room_tone(8.0)),
        ("room tone -42 dBFS", room_tone(8.0, rms=0.008)),
        ("knuckle knock", after_a_pause(knuckle_knock())),
        ("low thump", after_a_pause(low_thump())),
        ("typing 120 wpm", concat(room_tone(5.0), typing(120))),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_synthetic_non_speech_stays_below_the_batch_gate(real_silero, label, samples):
    """SYNTHETIC signals (seeded), scanned from the start as the batch check
    scans a recording, as recorded (the first of its two scans): 0.02-0.11 at
    these seeds (measured 2026-09-27). The
    score moves with the noise realisation -- over room-tone seeds 0-49 the
    knock after a pause reaches the cut at 4 and 3 s of room tone at -42 dBFS
    at 1 -- so this pins
    a margin at fixed seeds, not a rate; config.py has the rates. The
    streaming check, which scores the trailing window instead, is tested
    through its production path in test_transcriber.py."""
    measured = silero_vad.measure_speech_pcm16(pcm_bytes(samples), 16_000)
    assert measured is not None
    assert measured.max_probability < SILERO_BATCH_MIN_PROBABILITY, label


def _quiet_word_ended_by_a_keystroke() -> bytes:
    """A word whose loudest 100 ms is SILENCE_GATE_THRESHOLD_MIN, then the
    stop key's clack: 0.06 as recorded, 0.98 amplified (measured)."""
    word = at_peak_level(speech_excerpts()["phrase_1700ms"], SILENCE_GATE_THRESHOLD_MIN)
    gap = np.zeros(1600, np.int16)
    return pcm_bytes(concat(word, gap, key_clack(), gap[:800]))


def test_the_amplified_scan_keeps_quiet_speech_the_natural_scan_misses(real_silero):
    checked = silero_vad.check_speech_pcm16(_quiet_word_ended_by_a_keystroke(), 16_000)
    assert checked is not None
    assert checked.natural.complete
    assert checked.natural.max_probability < SILERO_BATCH_MIN_PROBABILITY
    assert checked.amplified is not None
    assert checked.amplified.max_probability >= 0.5
    assert not checked.no_speech


@pytest.mark.parametrize(
    ("label", "samples"),
    [
        ("digital silence", np.zeros(3 * 16_000, np.int16)),
        ("room tone -54 dBFS", room_tone(3.0)),
        ("low thump", after_a_pause(low_thump())),
        ("typing 120 wpm", typing(120, 3.0)),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_both_scans_find_no_speech_in_synthetic_non_speech(real_silero, label, samples):
    """SYNTHETIC and seeded; the amplified scan lifts them to 0.02-0.13
    (measured). Not every noise stays below it -- config.py has the rates."""
    checked = silero_vad.check_speech_pcm16(pcm_bytes(samples), 16_000)
    assert checked is not None
    assert checked.amplified is not None
    assert checked.no_speech, (
        label,
        checked.natural.max_probability,
        checked.amplified.max_probability,
    )


def test_speech_at_its_own_level_needs_no_second_scan(real_silero):
    checked = silero_vad.check_speech_pcm16(
        pcm_bytes(after_a_pause(speech_excerpts()["word_220ms"])), 16_000
    )
    assert checked is not None
    assert checked.natural.max_probability >= 0.5
    assert checked.amplified is None
    assert not checked.no_speech


def test_an_amplified_scan_that_fails_never_reports_no_speech(real_silero, monkeypatch):
    real_measure = silero_vad.measure_speech_pcm16

    def _amplified_fails(*args, gain=1.0, **kwargs):
        if gain != 1.0:
            return None
        return real_measure(*args, gain=gain, **kwargs)

    monkeypatch.setattr(silero_vad, "measure_speech_pcm16", _amplified_fails)
    checked = silero_vad.check_speech_pcm16(pcm_bytes(room_tone(3.0)), 16_000)
    assert checked is not None
    assert checked.natural.max_probability < SILERO_BATCH_MIN_PROBABILITY
    assert checked.amplified is None
    assert not checked.no_speech


def test_an_incomplete_scan_is_not_amplified_and_never_reports_no_speech(real_silero):
    checked = silero_vad.check_speech_pcm16(
        pcm_bytes(room_tone(5.0)), 16_000, max_scan_s=2.0
    )
    assert checked is not None
    assert not checked.natural.complete
    assert checked.amplified is None
    assert not checked.no_speech


def test_the_gain_is_clipped_to_full_scale(real_silero):
    """An x8 copy of speech at a normal level clips; it must still be scored
    as audio in [-1, 1], which the graph was trained on, not as values of 8."""
    loud = pcm_bytes(after_a_pause(speech_excerpts()["phrase_1700ms"]))
    clipped = silero_vad.measure_speech_pcm16(loud, 16_000, gain=8.0)
    assert clipped is not None
    assert clipped.max_probability >= 0.5


def test_the_scan_stops_once_the_speech_is_certain(real_silero):
    phrase = speech_excerpts()["phrase_1700ms"]
    long_speech = concat(*([phrase] * 12))
    measured = silero_vad.measure_speech_pcm16(
        pcm_bytes(long_speech), 16_000, stop_after_speech_s=1.0
    )
    assert measured is not None
    assert measured.speech_seconds >= 1.0
    assert not measured.complete
    assert measured.scanned_seconds < measured.total_seconds / 2


def test_a_scan_budget_leaves_the_rest_unmeasured(real_silero):
    measured = silero_vad.measure_speech_pcm16(
        pcm_bytes(room_tone(5.0)), 16_000, max_scan_s=2.0
    )
    assert measured is not None
    assert not measured.complete
    assert 2.0 <= measured.scanned_seconds < 2.0 + silero_vad.WINDOW_SECONDS
    assert measured.total_seconds == pytest.approx(5.0)


def test_the_committed_speech_carries_its_attribution():
    """CC BY 4.0 requires the attribution to travel with the excerpts."""
    text = excerpt_attribution()
    assert "LibriSpeech" in text and "CC BY 4.0" in text
    assert "openslr.org/12" in text
