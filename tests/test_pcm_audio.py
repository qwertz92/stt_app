"""`resample_linear` is where a WAV header's sample rate becomes an amount of
work: the Nemotron and Granite CTC runtimes both hand it the rate as read."""

from __future__ import annotations

import tracemalloc

import numpy as np
import pytest

from stt_app.transcriber import _pcm_audio
from stt_app.transcriber._pcm_audio import (
    MIN_SOURCE_SAMPLE_RATE_HZ,
    resample_linear,
    split_into_passes,
)
from stt_app.transcriber.base import TranscriptionError


@pytest.mark.parametrize("rate", [0, 1, 100, MIN_SOURCE_SAMPLE_RATE_HZ - 1])
def test_a_source_rate_below_the_floor_is_refused(rate):
    """The interpolation makes `target / source` times the input's samples, so
    a header declaring 1 Hz turned a 3 KB file into 25.6 million samples,
    1,600 s of "audio" (measured on 2026-09-19 through the Granite CTC
    runtime; Nemotron calls the same function), and 64 KB works out to 512
    million. Two samples keep the unguarded code cheap enough to fail here
    instead of exhausting the machine; a rate of 0, which both readers refuse
    before this, was a `ZeroDivisionError` here."""
    samples = np.zeros(2, dtype=np.float32)

    with pytest.raises(TranscriptionError, match=f"sample rate of {rate} Hz"):
        resample_linear(samples, rate, 16_000)


@pytest.mark.parametrize(
    "rate", [MIN_SOURCE_SAMPLE_RATE_HZ, 11_025, 22_050, 44_100, 48_000]
)
def test_every_rate_a_recorder_writes_comes_out_at_the_target_length(rate):
    # One second of a ramp: linear interpolation of a line is that line, so
    # the values are checkable and not only the length.
    samples = np.linspace(-1.0, 1.0, rate, dtype=np.float32)

    resampled = resample_linear(samples, rate, 16_000)

    assert resampled.dtype == np.float32
    assert resampled.size == 16_000
    expected = np.linspace(-1.0, 1.0, 16_000, dtype=np.float64)
    assert float(np.abs(resampled - expected).max()) < 1e-6


def _whole_array_resample(samples: np.ndarray, source_rate: int, target_rate: int):
    """The resampler as it was before it worked block by block: one position
    array per side, over the whole waveform."""
    target_length = max(1, round(samples.size * target_rate / source_rate))
    return np.asarray(
        np.interp(
            np.linspace(0, samples.size - 1, target_length, dtype=np.float64),
            np.arange(samples.size, dtype=np.float64),
            samples,
        ),
        dtype=np.float32,
    )


@pytest.mark.parametrize(
    ("source_rate", "size"),
    [
        (48_000, 100_003),
        (44_100, 77_777),
        (22_050, 5_000),
        (8_000, 4_099),
        (16_001, 9_001),
    ],
)
def test_blockwise_resampling_is_bit_identical_to_the_whole_array_version(
    monkeypatch, source_rate, size
):
    """Blocks of 1000 target samples, so every block boundary and the last
    partial block are crossed; the interpolation is local, so the bits do not
    depend on where a block starts."""
    monkeypatch.setattr(_pcm_audio, "_RESAMPLE_BLOCK", 1000)
    samples = np.random.default_rng(5).uniform(-1, 1, size).astype(np.float32)

    resampled = resample_linear(samples, source_rate, 16_000)

    expected = _whole_array_resample(samples, source_rate, 16_000)
    assert resampled.dtype == np.float32
    assert resampled.tobytes() == expected.tobytes()


def test_resampling_holds_the_result_and_one_block_not_float64_position_arrays():
    """Measured with tracemalloc (2026-10-03): 20 M samples of 48 kHz audio,
    416 s, peaked at 427 MB through the whole-array version, which is the
    ~3.7 GB per hour of the former known limitation; the block-wise one peaks
    at its 27 MB result plus a block."""
    samples = np.zeros(20_000_000, dtype=np.float32)

    tracemalloc.start()
    try:
        resampled = resample_linear(samples, 48_000, 16_000)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak <= resampled.nbytes + 32 * 1024 * 1024


def test_audio_already_at_the_target_rate_is_returned_as_it_is():
    samples = np.array([0.25, -0.5, 0.75], dtype=np.float32)

    resampled = resample_linear(samples, 16_000, 16_000)

    assert resampled.dtype == np.float32
    assert np.array_equal(resampled, samples)


def test_one_splitter_serves_the_granite_runtime_and_the_remote_parts():
    """Granite CTC's passes and the remote providers' parts are the same
    problem -- consecutive windows cut at a quiet point -- so they share one
    implementation; `test_local_granite_ctc.py` pins its behaviour."""
    from stt_app.transcriber import _audio_parts, _pcm_audio, local_granite_ctc

    assert local_granite_ctc.split_into_passes is _pcm_audio.split_into_passes
    assert _audio_parts.split_into_passes is _pcm_audio.split_into_passes


def _noise_with_silence(seconds: float, *silent: tuple[float, float]) -> np.ndarray:
    rng = np.random.default_rng(3)
    samples = rng.uniform(-0.5, 0.5, int(seconds * 16_000)).astype(np.float32)
    for start_s, stop_s in silent:
        samples[int(start_s * 16_000) : int(stop_s * 16_000)] = 0.0
    return samples


def test_a_bound_within_the_search_window_does_not_cut_twenty_ms_parts():
    """The cut is looked for in the last 15 s before the bound. At a bound of
    15 s or less that stretch reaches back to the window's own start, so a
    pause at the start of a window was its quietest frame: the window was cut
    20 ms in, the next one 20 ms later, and a 25 s recording with a 2 s pause
    at its start came back as 103 windows, 99 of them 20 ms long (measured
    before the fix). The search is at most half the bound, so every window
    but the last carries at least that half."""
    samples = _noise_with_silence(25.0, (0.0, 2.0))

    windows = split_into_passes(samples, 16_000, max_seconds=10.0)

    assert np.array_equal(np.concatenate(windows), samples)
    assert all(window.size <= 10 * 16_000 for window in windows)
    assert all(window.size >= 5 * 16_000 for window in windows[:-1]), [
        window.size for window in windows
    ]


def test_a_short_bound_still_cuts_at_the_pause_in_its_second_half():
    samples = _noise_with_silence(25.0, (7.0, 7.4))

    windows = split_into_passes(samples, 16_000, max_seconds=10.0)

    assert 7.0 * 16_000 <= windows[0].size <= 7.4 * 16_000
