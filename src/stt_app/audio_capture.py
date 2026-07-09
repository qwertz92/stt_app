from __future__ import annotations

import io
import logging
import threading
import wave
from pathlib import Path
from typing import Callable

import numpy as np
import sounddevice as sd

from .config import AUDIO_BLOCK_DURATION_MS, AUDIO_CHANNELS, AUDIO_SAMPLE_RATE
from .persistence import atomic_write_bytes
from .vad import EnergyVad


class AudioCaptureError(RuntimeError):
    pass


class WarmMicrophoneStream:
    """Keeps one PortAudio input stream open so recording starts instantly.

    On locked-down machines (EDR/GPO-hooked audio stacks) opening and starting
    an ``InputStream`` can take seconds, and everything spoken before the
    stream runs is lost. With a warm stream the device is opened once; a
    recording merely attaches itself as the consumer of the already-running
    callback, which is effectively instant. The trade-off is that the
    microphone stays open (Windows shows the in-use indicator), which is why
    this is opt-in via the ``keep_microphone_warm`` setting.
    """

    def __init__(
        self,
        sample_rate: int = AUDIO_SAMPLE_RATE,
        channels: int = AUDIO_CHANNELS,
        block_duration_ms: int = AUDIO_BLOCK_DURATION_MS,
        logger: logging.Logger | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.block_size = int(sample_rate * block_duration_ms / 1000)
        self._logger = logger
        self._lock = threading.Lock()
        self._stream = None
        self._consumer: Callable | None = None

    @property
    def is_running(self) -> bool:
        return self._stream is not None

    def ensure_started(self) -> bool:
        """Open and start the shared stream if needed. Safe off the UI thread."""
        with self._lock:
            if self._stream is not None:
                return True
            try:
                stream = sd.InputStream(
                    samplerate=self.sample_rate,
                    channels=self.channels,
                    dtype="float32",
                    blocksize=self.block_size,
                    callback=self._dispatch,
                )
                stream.start()
            except Exception:
                if self._logger is not None:
                    self._logger.exception("Failed to start warm microphone stream")
                return False
            self._stream = stream
            if self._logger is not None:
                self._logger.info(
                    "warm_microphone_stream_started sample_rate=%d block_size=%d",
                    self.sample_rate,
                    self.block_size,
                )
            return True

    def attach(self, consumer: Callable) -> bool:
        """Route the running stream's audio to ``consumer``; False if not running."""
        with self._lock:
            if self._stream is None or self._consumer is not None:
                return False
            self._consumer = consumer
            return True

    def detach(self, consumer: Callable) -> None:
        with self._lock:
            # Bound methods compare equal but are not identical, so use ==.
            if self._consumer == consumer:
                self._consumer = None

    def close(self) -> None:
        with self._lock:
            stream = self._stream
            self._stream = None
            self._consumer = None
        if stream is None:
            return
        try:
            stream.stop()
            stream.close()
        except Exception:
            if self._logger is not None:
                self._logger.exception("Failed to close warm microphone stream")

    def _dispatch(self, indata, frames, time_info, status) -> None:
        consumer = self._consumer
        if consumer is None:
            return
        try:
            consumer(indata, frames, time_info, status)
        except Exception:
            if self._logger is not None:
                self._logger.exception("Warm microphone consumer failed")


class AudioCapture:
    def __init__(
        self,
        sample_rate: int = AUDIO_SAMPLE_RATE,
        channels: int = AUDIO_CHANNELS,
        block_duration_ms: int = AUDIO_BLOCK_DURATION_MS,
        vad: EnergyVad | None = None,
        auto_stop_callback=None,
        chunk_callback: Callable[[bytes], None] | None = None,
        logger: logging.Logger | None = None,
        warm_stream: WarmMicrophoneStream | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.block_size = int(sample_rate * block_duration_ms / 1000)
        self.vad = vad
        self.auto_stop_callback = auto_stop_callback
        self.chunk_callback = chunk_callback
        self._logger = logger
        self._warm_stream = warm_stream

        self._stream = None
        self._warm_attached = False
        self._lock = threading.Lock()
        self._chunks: list[np.ndarray] = []
        self._auto_stop_fired = False

    @property
    def is_recording(self) -> bool:
        return self._stream is not None or self._warm_attached

    def start(self) -> None:
        if self._stream is not None or self._warm_attached:
            return

        self._chunks = []
        self._auto_stop_fired = False
        if self.vad is not None:
            self.vad.reset()

        warm = self._warm_stream
        if (
            warm is not None
            and warm.sample_rate == self.sample_rate
            and warm.block_size == self.block_size
            and warm.attach(self._on_audio)
        ):
            # The shared stream is already running; attaching is instant and
            # audio flows from the very next callback block.
            self._warm_attached = True
            return

        try:
            stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="float32",
                blocksize=self.block_size,
                callback=self._on_audio,
            )
            try:
                stream.start()
            except Exception:
                # ``InputStream`` may have opened the device during
                # construction; close it so PortAudio does not keep the
                # device handle alive when ``start()`` fails.
                try:
                    stream.close()
                except Exception:
                    if self._logger is not None:
                        self._logger.exception("Failed to close audio stream after start failure")
                raise
            self._stream = stream
        except Exception as exc:
            raise AudioCaptureError(f"Failed to start microphone capture: {exc}") from exc

    def stop(self) -> bytes:
        stream = self._stream
        self._stream = None
        if self._warm_attached:
            self._warm_attached = False
            if self._warm_stream is not None:
                # Only detach; the shared warm stream keeps running for the
                # next recording.
                self._warm_stream.detach(self._on_audio)

        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                if self._logger is not None:
                    self._logger.exception("Failed to stop/close audio stream cleanly")

        with self._lock:
            if not self._chunks:
                return b""
            audio = np.concatenate(self._chunks)

        return self._to_wav_bytes(audio)

    def save_wav(self, output_path: Path, wav_bytes: bytes) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(output_path, wav_bytes)

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

        if self.chunk_callback is not None:
            try:
                self.chunk_callback(self._to_pcm16_bytes(mono))
            except Exception:
                if self._logger is not None:
                    self._logger.exception("Streaming chunk callback failed")

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
        pcm = self._to_pcm16_array(audio)

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self.sample_rate)
            wav_file.writeframes(pcm.tobytes())

        return buffer.getvalue()

    def _to_pcm16_array(self, audio: np.ndarray) -> np.ndarray:
        clipped = np.clip(audio, -1.0, 1.0)
        return (clipped * 32767.0).astype(np.int16)

    def _to_pcm16_bytes(self, audio: np.ndarray) -> bytes:
        return self._to_pcm16_array(audio).tobytes()
