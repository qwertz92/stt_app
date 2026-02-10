from __future__ import annotations

import argparse
import math
import wave
from pathlib import Path

import numpy as np


def _tone(
    *,
    sample_rate: int,
    frequency_hz: float,
    duration_s: float,
    amplitude: float,
) -> np.ndarray:
    samples = int(sample_rate * duration_s)
    t = np.arange(samples, dtype=np.float32) / float(sample_rate)
    return (np.sin(2.0 * math.pi * frequency_hz * t) * amplitude).astype(np.float32)


def _silence(*, sample_rate: int, duration_s: float) -> np.ndarray:
    samples = int(sample_rate * duration_s)
    return np.zeros(samples, dtype=np.float32)


def create_sample_audio(sample_rate: int = 16_000) -> np.ndarray:
    parts = [
        _silence(sample_rate=sample_rate, duration_s=0.2),
        _tone(sample_rate=sample_rate, frequency_hz=220.0, duration_s=0.35, amplitude=0.25),
        _silence(sample_rate=sample_rate, duration_s=0.08),
        _tone(sample_rate=sample_rate, frequency_hz=260.0, duration_s=0.25, amplitude=0.22),
        _silence(sample_rate=sample_rate, duration_s=0.08),
        _tone(sample_rate=sample_rate, frequency_hz=310.0, duration_s=0.32, amplitude=0.23),
        _silence(sample_rate=sample_rate, duration_s=0.12),
        _tone(sample_rate=sample_rate, frequency_hz=270.0, duration_s=0.24, amplitude=0.21),
        _silence(sample_rate=sample_rate, duration_s=0.45),
    ]
    return np.concatenate(parts)


def write_wav(path: Path, audio: np.ndarray, sample_rate: int = 16_000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(audio, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a tiny synthetic benchmark WAV sample."
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("samples/benchmark_sample.wav"),
        help="Output WAV path.",
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=16_000,
        help="Output WAV sample rate.",
    )
    args = parser.parse_args()

    audio = create_sample_audio(sample_rate=args.sample_rate)
    write_wav(args.out, audio, sample_rate=args.sample_rate)
    print(f"Wrote sample audio to: {args.out.resolve()}")
    print(
        "Note: this file is synthetic tones for pipeline benchmarking, "
        "not realistic speech-WER evaluation."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
