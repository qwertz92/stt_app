"""PCM helpers shared by the local transcribers."""

from __future__ import annotations

import numpy as np


def resample_linear(
    samples: np.ndarray,
    source_rate: int,
    target_rate: int,
) -> np.ndarray:
    """Resample a mono waveform to `target_rate` by linear interpolation.

    Returns float32, and returns the samples unchanged (bar that cast) when the
    rates already match or there is nothing to interpolate between. Linear
    interpolation is not a good resampler -- it has no anti-aliasing filter --
    but every model here is fed 16 kHz audio and a non-16 kHz WAV only reaches
    this through an imported file, so it is the difference between accepting
    that file and refusing it.
    """
    if source_rate == target_rate or samples.size <= 1:
        return np.asarray(samples, dtype=np.float32)
    target_length = max(1, round(samples.size * target_rate / source_rate))
    source_positions = np.arange(samples.size, dtype=np.float64)
    target_positions = np.linspace(
        0,
        samples.size - 1,
        target_length,
        dtype=np.float64,
    )
    return np.asarray(
        np.interp(target_positions, source_positions, samples),
        dtype=np.float32,
    )
