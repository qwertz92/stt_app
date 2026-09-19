"""PCM helpers shared by the local transcribers."""

from __future__ import annotations

import numpy as np

from .base import TranscriptionError

# The lowest source rate `resample_linear` accepts. 8 kHz is telephony's rate:
# the lowest speech is recorded at, and the lowest the onnx-asr runtime accepts
# as well, so this refuses no file a recorder writes. A header below it is
# damage, and the interpolation makes `target_rate / source_rate` times the
# file's samples out of it -- measured on 2026-09-19 through the Granite CTC
# runtime, a 3 KB WAV declaring 1 Hz became 25.6 million samples (1,600 s of
# "audio"), and 64 KB works out to 512 million, 2 GB as float32 before the
# float64 intermediates below. Nemotron calls the same function. With the
# floor the factor is at most two.
MIN_SOURCE_SAMPLE_RATE_HZ = 8_000


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

    `source_rate` is whatever the file's header declares, so it is checked
    here, where it turns into an amount of work, whatever the reader in front
    of it checked: a rate below `MIN_SOURCE_SAMPLE_RATE_HZ` raises
    `TranscriptionError`.
    """
    if source_rate < MIN_SOURCE_SAMPLE_RATE_HZ:
        raise TranscriptionError(
            f"Could not read the audio: the file declares a sample rate of "
            f"{source_rate} Hz, and the local models need "
            f"{MIN_SOURCE_SAMPLE_RATE_HZ} Hz or more."
        )
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
