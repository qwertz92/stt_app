"""PCM helpers shared by the transcribers.

Resampling for the Nemotron and Granite CTC runtimes, and the splitter that
cuts a long recording into consecutive windows at quiet points: Granite CTC's
passes, and the parts a remote batch provider is sent (`_audio_parts`).
"""

from __future__ import annotations

import io
import wave

import numpy as np

from ..config import AUDIO_SAMPLE_RATE
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


# How far back from the end of a window the split point is looked for. The cut
# is the quietest frame in that stretch, because consecutive windows share no
# audio and a cut inside a word loses it.
_SPLIT_SEARCH_SECONDS = 15.0
# 20 ms at the 16 kHz the app records and the local models are fed; an
# imported WAV at another rate is searched in frames of the same sample count.
_SPLIT_FRAME_SAMPLES = 320


def _quietest_cut(
    samples: np.ndarray,
    start: int,
    limit: int,
    search_samples: int,
) -> int:
    """Index of the quietest frame in the last stretch before `limit`.

    Always strictly between `start` and `limit`, so the caller makes progress
    and no window exceeds its bound. Falls back to `limit` when the searched
    stretch does not hold a whole frame.
    """
    search_start = max(start + _SPLIT_FRAME_SAMPLES, limit - search_samples)
    usable = (limit - search_start) // _SPLIT_FRAME_SAMPLES * _SPLIT_FRAME_SAMPLES
    if usable < _SPLIT_FRAME_SAMPLES:
        return limit
    region = samples[search_start : search_start + usable]
    energies = (region.reshape(-1, _SPLIT_FRAME_SAMPLES) ** 2).mean(axis=1)
    return search_start + int(energies.argmin()) * _SPLIT_FRAME_SAMPLES


def split_into_passes(
    samples: np.ndarray,
    sample_rate: int = AUDIO_SAMPLE_RATE,
    *,
    max_seconds: float | None = None,
    max_samples: int | None = None,
    search_seconds: float = _SPLIT_SEARCH_SECONDS,
) -> list[np.ndarray]:
    """Cut a waveform into consecutive windows of at most `max_seconds`, or of
    at most `max_samples` samples -- exactly one of the two is given.

    The windows concatenate back to the input exactly -- nothing is dropped and
    nothing overlaps -- and a recording at or below the bound is returned
    unsplit. The bound is the caller's own (a memory limit for Granite CTC, a
    request limit for a remote provider). `max_samples` is for a bound that is
    a sample count to begin with, such as a byte cap: sent through seconds,
    `int(n / rate * rate)` comes back as `n - 1` for some counts (11,829 of
    200,000 random counts at seven common rates). `search_seconds` is an
    argument so a test can drive the split on a short signal.
    """
    if max_samples is None:
        if max_seconds is None:
            raise ValueError("split_into_passes needs max_seconds or max_samples")
        max_samples = int(max_seconds * sample_rate)
    elif max_seconds is not None:
        raise ValueError("split_into_passes takes max_seconds or max_samples, not both")
    search_samples = max(_SPLIT_FRAME_SAMPLES, int(search_seconds * sample_rate))
    # At most half the bound. A search as long as the bound reaches back to
    # the window's own start, so a pause at the start of a window was its
    # quietest frame and the window was cut 20 ms in, again and again: a 25 s
    # recording with a 2 s pause at its start became 103 windows at a 10 s
    # bound, 99 of them 20 ms long. Granite's 180 s passes and every remote
    # bound (180 s and up) are far above twice the 15 s search, so this
    # changes nothing for them.
    search_samples = min(search_samples, max_samples // 2)
    if max_samples < _SPLIT_FRAME_SAMPLES or samples.size <= max_samples:
        return [samples]

    windows: list[np.ndarray] = []
    start = 0
    while samples.size - start > max_samples:
        cut = _quietest_cut(samples, start, start + max_samples, search_samples)
        windows.append(samples[start:cut])
        start = cut
    windows.append(samples[start:])
    return windows


def pcm16_wav_bytes(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap mono 16-bit little-endian PCM in a WAV header."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return buffer.getvalue()
