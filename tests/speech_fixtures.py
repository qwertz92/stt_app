"""Real speech and synthetic non-speech for the Silero speech-check tests.

`data/librispeech_excerpts.npz` holds six short excerpts of LibriSpeech
dev-clean utterances as 16 kHz mono int16 arrays: four single words (their
voiced part 140, 220, 250 and 340 ms, each padded by 20 ms) and two phrases
(1.03 and 1.70 s). They are the only recorded speech in the repository -- the
sample in `samples/` is sine tones -- and the Silero graph is a speech model,
so tones cannot stand in for it the way they do for the energy meters.

Attribution (CC BY 4.0, also stored in the file under `attribution`):
LibriSpeech ASR corpus, V. Panayotov, G. Chen, D. Povey, S. Khudanpur,
"Librispeech: an ASR corpus based on public domain audio books", ICASSP 2015,
https://www.openslr.org/12, licensed CC BY 4.0
(https://creativecommons.org/licenses/by/4.0/). Utterances 1272-128104-0013,
-0005, -0003, -0008, -0000 and -0001; changes: cut to the spans named in the
file's `attribution` string.

Everything else here is SYNTHETIC and seeded, built to the shapes the
calibration measured (room tone at -54 dBFS, a knuckle knock and a low thump
that the energy gate admits after a pause). Not recordings.
"""

from __future__ import annotations

import io
import wave
from pathlib import Path

import numpy as np

RATE = 16_000
_EXCERPTS = Path(__file__).resolve().parent / "data" / "librispeech_excerpts.npz"


def speech_excerpts() -> dict[str, np.ndarray]:
    """The six LibriSpeech excerpts, name -> int16 samples at 16 kHz."""
    with np.load(_EXCERPTS) as data:
        return {name: data[name].copy() for name in data.files if name != "attribution"}


def excerpt_attribution() -> str:
    with np.load(_EXCERPTS) as data:
        return str(data["attribution"])


def room_tone(seconds: float, *, rms: float = 0.002, seed: int = 0) -> np.ndarray:
    """White noise at -54 dBFS by default: a quiet room, below the silence gate."""
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(int(seconds * RATE)) * rms
    return _to_int16(noise)


def knuckle_knock(seed: int = 1) -> np.ndarray:
    """150 ms: a 200 Hz ring decaying over 25 ms plus a 5 ms noise burst.

    Its longest run above the default silence gate at 20 ms buckets is 0.10 s,
    above STREAMING_NEW_SEGMENT_MIN_SPEECH_S, so the energy gate admits it
    after a pause. Silero's score moves with the seed: as the streaming check
    sees it after a pause 0.064-0.227 over 50 seeds (median 0.108; 3 of 50
    below the 0.08 cut), as the batch check sees it as recorded 45 of 50 below
    0.15, and with the amplified second scan only 1 of 50.
    """
    rng = np.random.default_rng(seed)
    n = int(0.150 * RATE)
    t = np.arange(n) / RATE
    ring = np.sin(2 * np.pi * 200 * t) * np.exp(-t / 0.025) * 0.4
    burst = rng.standard_normal(n) * np.exp(-t / 0.005)
    burst = burst / np.abs(burst).max() * 0.1
    return _to_int16(ring + burst)


def low_thump() -> np.ndarray:
    """300 ms of a 60 Hz sine decaying over 50 ms, peak 0.5: a heavy desk thump.

    Longest energy run 0.22 s -- the energy gate admits it comfortably."""
    n = int(0.300 * RATE)
    t = np.arange(n) / RATE
    return _to_int16(np.sin(2 * np.pi * 60 * t) * np.exp(-t / 0.050) * 0.5)


def typing(wpm: int, seconds: float = 3.0) -> np.ndarray:
    """Key clacks over room tone: 40 ms noise bursts decaying over 6 ms, peak
    0.3, five keystrokes per word at `wpm`, each jittered by up to a fifth of
    the gap. Seeded by `wpm`, so every rate is one fixed signal."""
    rng = np.random.default_rng(wpm)
    audio = room_tone(seconds, seed=wpm).astype(np.float32)
    gap = 60.0 / (wpm * 5)
    n = int(0.040 * RATE)
    decay = np.exp(-np.arange(n) / RATE / 0.006)
    t = 0.05
    while t < seconds - 0.05:
        clack = rng.standard_normal(n) * decay
        clack = clack / np.abs(clack).max() * 0.3 * 32768
        start = int((t + rng.uniform(-0.2, 0.2) * gap) * RATE)
        audio[start:start + n] += clack[: max(0, audio.size - start)]
        t += gap
    return np.clip(audio, -32768, 32767).astype(np.int16)


def key_clack(seed: int = 7) -> np.ndarray:
    """One 40 ms key clack, peak 0.3 -- the stop hotkey's own keystroke, the
    shape `typing` repeats. Its loudest 100 ms is 0.024, so it lifts any
    recording it ends over the default silence gate on its own."""
    rng = np.random.default_rng(seed)
    n = int(0.040 * RATE)
    clack = rng.standard_normal(n) * np.exp(-np.arange(n) / RATE / 0.006)
    return _to_int16(clack / np.abs(clack).max() * 0.3)


def at_peak_level(samples: np.ndarray, level: float) -> np.ndarray:
    """``samples`` scaled so their loudest 100 ms window measures ``level``."""
    from stt_app.vad import measure_peak_windowed_rms

    peak = measure_peak_windowed_rms(wav_bytes(samples))
    return _to_int16(np.asarray(samples, dtype=np.float32) / 32768.0 * (level / peak))


def concat(*parts: np.ndarray) -> np.ndarray:
    return np.concatenate([np.asarray(part, dtype=np.int16) for part in parts])


def after_a_pause(event: np.ndarray, *, seed: int = 0) -> np.ndarray:
    """What the post-pause gate measures: 7.5 s of quiet, the event, 0.3 s."""
    return concat(room_tone(7.5, seed=seed), event, room_tone(0.3, seed=seed + 100))


def pcm_bytes(samples: np.ndarray) -> bytes:
    return np.asarray(samples, dtype=np.int16).tobytes()


def wav_bytes(samples: np.ndarray, *, rate: int = RATE, channels: int = 1) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(rate)
        wav_file.writeframes(pcm_bytes(samples))
    return buffer.getvalue()


def _to_int16(samples: np.ndarray) -> np.ndarray:
    return np.clip(np.round(samples * 32768.0), -32768, 32767).astype(np.int16)
