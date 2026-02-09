from __future__ import annotations

import io
import logging
import threading
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

from .config import AUDIO_BLOCK_DURATION_MS, AUDIO_CHANNELS, AUDIO_SAMPLE_RATE
from .vad import EnergyVad


class AudioCaptureError(RuntimeError):
    pass


class AudioCapture:
    def __init__(
        self,
        sample_rate: int = AUDIO_SAMPLE_RATE,
        channels: int = AUDIO_CHANNELS,
        block_duration_ms: int = AUDIO_BLOCK_DURATION_MS,
        vad: EnergyVad | None = None,
        auto_stop_callback=None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.block_size = int(sample_rate * block_duration_ms / 1000)
        self.vad = vad
        self.auto_stop_callback = auto_stop_callback
        self._logger = logger

        self._stream = None
        self._lock = threading.Lock()
        self._chunks: list[np.ndarray] = []
        self._auto_stop_fired = False

    @property
    def is_recording(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        if self._stream is not None:
            return

        self._chunks = []
        self._auto_stop_fired = False
        if self.vad is not None:
            self.vad.reset()

        try:
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="float32",
                blocksize=self.block_size,
                callback=self._on_audio,
            )
            self._stream.start()
        except Exception as exc:
            self._stream = None
            raise AudioCaptureError(f"Failed to start microphone capture: {exc}") from exc

    def stop(self) -> bytes:
        stream = self._stream
        self._stream = None

        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

        with self._lock:
            if not self._chunks:
                return b""
            audio = np.concatenate(self._chunks)

        return self._to_wav_bytes(audio)

    def save_wav(self, output_path: Path, wav_bytes: bytes) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(wav_bytes)

    def _on_audio(self, indata, frames, _time, status) -> None:
        if status and self._logger is not None:
            self._logger.warning("Audio stream status: %s", status)

        data = np.asarray(indata, dtype=np.float32)
        if data.ndim == 2 and data.shape[1] > 1:
            mono = np.mean(data, axis=1)
        else:
            mono = data.reshape(-1)

        with self._lock:
            self._chunks.append(np.copy(mono))

        if self.vad is None:
            return

        decision = self.vad.process_chunk(mono)
        if (
            decision.should_stop
            and self.auto_stop_callback is not None
            and not self._auto_stop_fired
        ):
            self._auto_stop_fired = True
            threading.Thread(target=self.auto_stop_callback, daemon=True).start()

    def _to_wav_bytes(self, audio: np.ndarray) -> bytes:
        clipped = np.clip(audio, -1.0, 1.0)
        pcm = (clipped * 32767.0).astype(np.int16)

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self.sample_rate)
            wav_file.writeframes(pcm.tobytes())

        return buffer.getvalue()
