from __future__ import annotations

import io
import wave
from dataclasses import dataclass

import numpy as np

from .config import (
    AUDIO_SAMPLE_RATE,
    SILENCE_GATE_WINDOW_MS,
    VAD_ENERGY_THRESHOLD,
    VAD_MAX_SILENCE_MS,
    VAD_MIN_SPEECH_MS,
)


def peak_windowed_rms_from_wav(
    wav_bytes: bytes,
    window_ms: int = SILENCE_GATE_WINDOW_MS,
) -> float:
    """Loudest windowed RMS level (0..1) of a 16-bit PCM WAV recording.

    Used by the silence gate: a recording whose loudest window stays below a
    (user-tunable) threshold contains no speech, so it can skip transcription
    instead of letting the model hallucinate words from silence. Windowing
    keeps a short whisper detectable that full-recording averaging would
    dilute. Returns 0.0 for empty or unreadable audio.
    """
    if not wav_bytes:
        return 0.0
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
            sample_rate = wav_file.getframerate()
            sample_width = wav_file.getsampwidth()
            frames = wav_file.readframes(wav_file.getnframes())
    except Exception:
        return 0.0
    if sample_width != 2 or not frames:
        return 0.0

    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    window = max(1, int(sample_rate * (max(1, window_ms) / 1000.0)))
    peak = 0.0
    for start in range(0, samples.size, window):
        chunk = samples[start:start + window]
        rms = float(np.sqrt(np.mean(chunk * chunk)))
        if rms > peak:
            peak = rms
    return peak


@dataclass(slots=True)
class VadDecision:
    speech_started: bool = False
    should_stop: bool = False


class EnergyVad:
    def __init__(
        self,
        sample_rate: int = AUDIO_SAMPLE_RATE,
        energy_threshold: float = VAD_ENERGY_THRESHOLD,
        min_speech_ms: int = VAD_MIN_SPEECH_MS,
        max_silence_ms: int = VAD_MAX_SILENCE_MS,
    ) -> None:
        self.sample_rate = sample_rate
        self.energy_threshold = float(energy_threshold)
        self.min_speech_samples = int(sample_rate * (min_speech_ms / 1000.0))
        self.max_silence_samples = int(sample_rate * (max_silence_ms / 1000.0))

        self._speech_run = 0
        self._silence_run = 0
        self.has_detected_speech = False

    def reset(self) -> None:
        self._speech_run = 0
        self._silence_run = 0
        self.has_detected_speech = False

    def process_chunk(self, chunk: np.ndarray) -> VadDecision:
        if chunk.size == 0:
            return VadDecision()

        mono = np.asarray(chunk, dtype=np.float32).reshape(-1)
        energy = float(np.sqrt(np.mean(mono * mono)))

        speech_started = False
        should_stop = False

        if energy >= self.energy_threshold:
            self._speech_run += mono.size
            self._silence_run = 0

            if (
                not self.has_detected_speech
                and self._speech_run >= self.min_speech_samples
            ):
                self.has_detected_speech = True
                speech_started = True
        else:
            if not self.has_detected_speech:
                self._speech_run = 0
            else:
                self._silence_run += mono.size
                if self._silence_run >= self.max_silence_samples:
                    should_stop = True

        return VadDecision(speech_started=speech_started, should_stop=should_stop)
