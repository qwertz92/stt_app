"""`resample_linear` is where a WAV header's sample rate becomes an amount of
work: the Nemotron and Granite CTC runtimes both hand it the rate as read."""

from __future__ import annotations

import numpy as np
import pytest

from stt_app.transcriber._pcm_audio import (
    MIN_SOURCE_SAMPLE_RATE_HZ,
    resample_linear,
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


def test_audio_already_at_the_target_rate_is_returned_as_it_is():
    samples = np.array([0.25, -0.5, 0.75], dtype=np.float32)

    resampled = resample_linear(samples, 16_000, 16_000)

    assert resampled.dtype == np.float32
    assert np.array_equal(resampled, samples)
