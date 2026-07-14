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


def _close_input_stream(
    stream,
    *,
    logger: logging.Logger | None,
    context: str,
    stop_first: bool = True,
) -> None:
    """Best-effort close that never skips close() when stop() fails."""
    if stop_first:
        try:
            stream.stop()
        except Exception:
            if logger is not None:
                logger.exception("Failed to stop %s", context)
    try:
        stream.close()
    except Exception:
        if logger is not None:
            logger.exception("Failed to close %s", context)


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
        self._starting = False
        self._generation = 0

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._stream is not None

    def ensure_started(self) -> bool:
        """Open and start the shared stream if needed. Safe off the UI thread."""
        with self._lock:
            if self._stream is not None:
                return True
            if self._starting:
                return False
            self._starting = True
            generation = self._generation

        stream = None
        try:
            stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="float32",
                blocksize=self.block_size,
                callback=self._dispatch,
            )
            try:
                stream.start()
            except Exception:
                _close_input_stream(
                    stream,
                    logger=self._logger,
                    context="warm microphone stream after start failure",
                    stop_first=False,
                )
                stream = None
                raise
        except Exception:
            if self._logger is not None:
                self._logger.exception("Failed to start warm microphone stream")
        finally:
            with self._lock:
                self._starting = False
                accepted = stream is not None and generation == self._generation
                if accepted:
                    self._stream = stream

        if not accepted:
            if stream is not None:
                _close_input_stream(
                    stream,
                    logger=self._logger,
                    context="superseded warm microphone stream",
                )
            return False
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
            self._generation += 1
            stream = self._stream
            self._stream = None
            self._consumer = None
        if stream is None:
            return
        _close_input_stream(
            stream,
            logger=self._logger,
            context="warm microphone stream",
        )

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
        self._capture_generation = 0
        self._accepting_audio = False
        self._active_callback: Callable | None = None
        self._callback_count = 0

    @property
    def is_recording(self) -> bool:
        return self._stream is not None or self._warm_attached

    @property
    def callback_count(self) -> int:
        with self._lock:
            return self._callback_count

    @property
    def has_received_audio(self) -> bool:
        return self.callback_count > 0

    @property
    def uses_warm_stream(self) -> bool:
        return self._warm_attached

    def start(self) -> None:
        if self._stream is not None or self._warm_attached:
            return

        with self._lock:
            self._capture_generation += 1
            generation = self._capture_generation
            self._chunks = []
            self._auto_stop_fired = False
            self._accepting_audio = True
            self._callback_count = 0

        def session_callback(indata, frames, time_info, status) -> None:
            self._on_audio_for_generation(
                generation,
                indata,
                frames,
                time_info,
                status,
            )

        self._active_callback = session_callback
        if self.vad is not None:
            self.vad.reset()

        warm = self._warm_stream
        if (
            warm is not None
            and warm.sample_rate == self.sample_rate
            and warm.block_size == self.block_size
            and warm.attach(session_callback)
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
                callback=session_callback,
            )
            try:
                stream.start()
            except Exception:
                # ``InputStream`` may have opened the device during
                # construction; close it so PortAudio does not keep the
                # device handle alive when ``start()`` fails.
                _close_input_stream(
                    stream,
                    logger=self._logger,
                    context="audio stream after start failure",
                    stop_first=False,
                )
                raise
            self._stream = stream
        except Exception as exc:
            with self._lock:
                if generation == self._capture_generation:
                    self._accepting_audio = False
                    self._active_callback = None
            raise AudioCaptureError(f"Failed to start microphone capture: {exc}") from exc

    def stop(self) -> bytes:
        with self._lock:
            self._accepting_audio = False
            self._capture_generation += 1
            active_callback = self._active_callback
            self._active_callback = None
        stream = self._stream
        self._stream = None
        if self._warm_attached:
            self._warm_attached = False
            if self._warm_stream is not None:
                # Only detach; the shared warm stream keeps running for the
                # next recording.
                if active_callback is not None:
                    self._warm_stream.detach(active_callback)

        if stream is not None:
            _close_input_stream(
                stream,
                logger=self._logger,
                context="audio capture stream",
            )

        with self._lock:
            if not self._chunks:
                return b""
            audio = np.concatenate(self._chunks)
            self._chunks = []

        return self._to_wav_bytes(audio)

    def save_wav(self, output_path: Path, wav_bytes: bytes) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(output_path, wav_bytes)

    def _on_audio(self, indata, frames, _time, status) -> None:
        """Process an unscoped callback, retained for direct callers and tests."""
        self._process_audio(indata, frames, status, generation=None)

    def _on_audio_for_generation(
        self,
        generation: int,
        indata,
        frames,
        _time,
        status,
    ) -> None:
        self._process_audio(indata, frames, status, generation=generation)

    def _process_audio(self, indata, frames, status, *, generation: int | None) -> None:
        if status and self._logger is not None:
            self._logger.warning("Audio stream status: %s", status)

        data = np.asarray(indata, dtype=np.float32)
        if data.ndim == 2 and data.shape[1] > 1:
            mono = np.mean(data, axis=1)
        else:
            mono = data.reshape(-1)

        with self._lock:
            if generation is not None and (
                not self._accepting_audio or generation != self._capture_generation
            ):
                return
            self._chunks.append(np.copy(mono))
            self._callback_count += 1
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
