from __future__ import annotations

import io
import json
import queue
import threading
import wave
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any

import numpy as np

from ..config import (
    AUDIO_SAMPLE_RATE,
    DEFAULT_LANGUAGE_MODE,
    DOC_MODELS_PATH,
    LOCAL_NEMOTRON_MODEL_SIZES,
    NEMOTRON_LANGUAGE_IDS,
    NEMOTRON_MODEL_SIZE,
    STREAMING_ABORT_JOIN_TIMEOUT_S,
    language_modes_for_selection,
)
from .base import (
    AudioInput,
    ITranscriber,
    ProgressReporter,
    StreamingCallback,
    StreamingErrorCallback,
    TranscriptionError,
)
from .local_webgpu_asr import (
    download_webgpu_model_snapshot,
    resolve_cached_webgpu_model_path,
)

_STREAM_SENTINEL = object()
_DEFAULT_CHUNK_SAMPLES = 8_960


@dataclass
class _InferenceSession:
    processor: Any
    generator: Any
    tokenizer_stream: Any
    text: str = ""


@dataclass
class _StreamResult:
    error: Exception | None = None
    final_text: str = ""


@dataclass(frozen=True)
class _StreamRun:
    """All state that belongs to one immutable stream generation."""

    generation: int
    audio_queue: queue.Queue[bytes | object]
    on_partial: StreamingCallback | None
    on_error: StreamingErrorCallback | None
    abort_requested: threading.Event = dataclass_field(default_factory=threading.Event)
    result: _StreamResult = dataclass_field(default_factory=_StreamResult)


def _default_runtime_module():
    try:
        import onnxruntime_genai as runtime  # type: ignore
    except ImportError as exc:
        raise TranscriptionError(
            "Nemotron requires ONNX Runtime GenAI 0.14.1 or newer. "
            "Install the app dependencies again and restart."
        ) from exc
    return runtime


class LocalNemotronTranscriber(ProgressReporter, ITranscriber):
    """True cache-aware Nemotron streaming through ONNX Runtime GenAI."""

    def __init__(
        self,
        model_size: str = NEMOTRON_MODEL_SIZE,
        language_mode: str = DEFAULT_LANGUAGE_MODE,
        offline_mode: bool = False,
        model_dir: str = "",
        runtime_module=None,
        provider_order: tuple[str, ...] = ("dml", "cpu"),
        use_runtime_vad: bool = False,
    ) -> None:
        if model_size not in LOCAL_NEMOTRON_MODEL_SIZES:
            raise ValueError(f"Unsupported Nemotron model '{model_size}'.")
        ProgressReporter.__init__(self)
        self.model_size = model_size
        self.language_mode = str(language_mode or DEFAULT_LANGUAGE_MODE).lower()
        self.offline_mode = bool(offline_mode)
        self.model_dir = str(model_dir or "").strip()
        self.provider_order = tuple(provider_order) or ("cpu",)
        self.use_runtime_vad = bool(use_runtime_vad)
        self._runtime_module = runtime_module

        self._model_lock = threading.Lock()
        self._inference_lock = threading.Lock()
        self._model = None
        self._runtime = None
        self._runtime_device = ""
        self._runtime_fallback_details: list[str] = []
        self._sample_rate = AUDIO_SAMPLE_RATE
        self._chunk_samples = _DEFAULT_CHUNK_SAMPLES

        self._stream_lock = threading.Lock()
        self._stream_active = False
        self._stream_generation = 0
        self._stream_run: _StreamRun | None = None
        self._stream_thread: threading.Thread | None = None
        self._stream_workers: dict[int, threading.Thread] = {}
        self._close_requested = False

    @property
    def runtime_device(self) -> str:
        return self._runtime_device

    @property
    def is_model_loaded(self) -> bool:
        return self._model is not None

    @property
    def runtime_details_text(self) -> str:
        if not self._runtime_fallback_details:
            return ""
        return "Fallback attempts: " + "; ".join(self._runtime_fallback_details)

    def runtime_status_text(self) -> str:
        if self._runtime_device == "dml":
            return "Nemotron ORT GenAI active on DirectML GPU."
        if self._runtime_device == "cpu":
            return "Nemotron ORT GenAI active on CPU."
        return "Nemotron ORT GenAI is not loaded yet."

    def _ensure_snapshot(self) -> Path:
        snapshot = resolve_cached_webgpu_model_path(self.model_size, self.model_dir)
        if snapshot is not None:
            return snapshot
        if self.offline_mode:
            raise TranscriptionError(
                f"Nemotron model '{self.model_size}' is not cached locally. "
                f"Disable Offline mode or download it first. See {DOC_MODELS_PATH}."
            )
        try:
            download_webgpu_model_snapshot(self.model_size, self.model_dir)
        except Exception as exc:
            raise TranscriptionError(
                f"Failed to download Nemotron model '{self.model_size}': {exc}"
            ) from exc
        snapshot = resolve_cached_webgpu_model_path(self.model_size, self.model_dir)
        if snapshot is None:
            raise TranscriptionError(
                f"Downloaded '{self.model_size}', but no complete INT4 snapshot "
                "was found."
            )
        return snapshot

    def _load_audio_config(self, snapshot: Path) -> None:
        try:
            raw = json.loads(
                (snapshot / "genai_config.json").read_text(encoding="utf-8")
            )
            model = raw["model"]
            self._sample_rate = int(model["sample_rate"])
            self._chunk_samples = int(model["chunk_samples"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TranscriptionError(
                "Nemotron genai_config.json is missing or invalid."
            ) from exc

    def _create_config(self, runtime, snapshot: Path, provider: str):
        config = runtime.Config(str(snapshot))
        config.clear_providers()
        if provider != "cpu":
            config.append_provider(provider)
        return config

    def _ensure_model(self):
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is not None:
                return self._model

            snapshot = self._ensure_snapshot()
            self._load_audio_config(snapshot)
            runtime = self._runtime_module or _default_runtime_module()
            failures: list[str] = []
            for provider in self.provider_order:
                try:
                    self._emit_progress(
                        f"Loading Nemotron INT4 with {provider.upper()}."
                    )
                    config = self._create_config(runtime, snapshot, provider)
                    model = runtime.Model(config)
                except Exception as exc:
                    failures.append(f"{provider}: {exc}")
                    continue
                self._runtime = runtime
                self._model = model
                self._runtime_device = provider
                self._runtime_fallback_details = failures
                self._emit_progress(self.runtime_status_text())
                return model

            detail = "; ".join(failures) or "no execution provider was attempted"
            raise TranscriptionError(f"Nemotron ORT GenAI failed to load: {detail}")

    def preload_model(self) -> None:
        self._ensure_model()

    def _language_id(self) -> int:
        supported = language_modes_for_selection("local", self.model_size)
        mode = self.language_mode if self.language_mode in supported else "auto"
        return NEMOTRON_LANGUAGE_IDS.get(mode, NEMOTRON_LANGUAGE_IDS["auto"])

    def _create_session(self) -> _InferenceSession:
        model = self._ensure_model()
        runtime = self._runtime
        if runtime is None:
            raise TranscriptionError("Nemotron ORT GenAI runtime is unavailable.")
        processor = runtime.StreamingProcessor(model)
        if self.use_runtime_vad:
            try:
                processor.set_option("use_vad", "true")
            except Exception:
                processor.set_option("use_vad", "false")
        else:
            processor.set_option("use_vad", "false")
        tokenizer = runtime.Tokenizer(model)
        params = runtime.GeneratorParams(model)
        generator = runtime.Generator(model, params)
        generator.set_runtime_option("lang_id", str(self._language_id()))
        return _InferenceSession(
            processor=processor,
            generator=generator,
            tokenizer_stream=tokenizer.create_stream(),
        )

    @staticmethod
    def _decode_available(session: _InferenceSession) -> str:
        text = ""
        while not session.generator.is_done():
            session.generator.generate_next_token()
            tokens = session.generator.get_next_tokens()
            for token in tokens:
                piece = session.tokenizer_stream.decode(token)
                if piece:
                    text += str(piece)
        session.text += text
        return text

    def _process_samples(self, session: _InferenceSession, samples: np.ndarray) -> str:
        inputs = session.processor.process(samples.astype(np.float32, copy=False))
        if inputs is None:
            return ""
        session.generator.set_inputs(inputs)
        return self._decode_available(session)

    def _flush_session(self, session: _InferenceSession) -> str:
        inputs = session.processor.flush()
        if inputs is None:
            return ""
        session.generator.set_inputs(inputs)
        return self._decode_available(session)

    def _transcribe_samples(self, samples: np.ndarray) -> str:
        with self._inference_lock:
            session = self._create_session()
            for offset in range(0, len(samples), self._chunk_samples):
                self._process_samples(
                    session,
                    samples[offset : offset + self._chunk_samples],
                )
            self._flush_session(session)
            return session.text.strip()

    def transcribe_batch(self, audio_source: AudioInput) -> str:
        try:
            samples = self._load_wav_samples(audio_source)
            return self._transcribe_samples(samples)
        except TranscriptionError:
            raise
        except Exception as exc:
            raise TranscriptionError(f"Nemotron transcription failed: {exc}") from exc

    def start_stream(
        self,
        on_partial: StreamingCallback | None = None,
        on_error: StreamingErrorCallback | None = None,
    ) -> None:
        self._ensure_model()
        with self._stream_lock:
            if self._stream_active:
                raise TranscriptionError("Streaming session already active.")
            self._stream_generation += 1
            run = _StreamRun(
                generation=self._stream_generation,
                audio_queue=queue.Queue(),
                on_partial=on_partial,
                on_error=on_error,
            )
            self._stream_active = True
            self._stream_run = run
            self._close_requested = False
            thread = threading.Thread(
                target=self._stream_worker,
                args=(run,),
                name="stt_app_nemotron_stream",
                daemon=True,
            )
            self._stream_thread = thread
            self._stream_workers[run.generation] = thread
        thread.start()

    def push_audio_chunk(self, chunk: bytes) -> None:
        payload = bytes(chunk or b"")
        if not payload:
            return
        with self._stream_lock:
            run = self._stream_run if self._stream_active else None
        if run is None:
            raise TranscriptionError("Streaming session is not active.")
        run.audio_queue.put(payload)

    def stop_stream(self) -> str:
        with self._stream_lock:
            run = self._stream_run if self._stream_active else None
            if run is None:
                raise TranscriptionError("Streaming session is not active.")
            thread = self._stream_thread
        if thread is None:
            raise TranscriptionError("Streaming session was not initialized correctly.")
        run.audio_queue.put(_STREAM_SENTINEL)
        thread.join()
        with self._stream_lock:
            error = run.result.error
            text = run.result.final_text
            self._reset_stream_fields(run)
        if error is not None:
            raise TranscriptionError(f"Nemotron streaming failed: {error}") from error
        return text.strip()

    def abort_stream(self) -> None:
        with self._stream_lock:
            run = self._stream_run if self._stream_active else None
            if run is None:
                return
            thread = self._stream_thread
            run.abort_requested.set()
        run.audio_queue.put(_STREAM_SENTINEL)
        if thread is not None:
            thread.join(timeout=STREAMING_ABORT_JOIN_TIMEOUT_S)
        with self._stream_lock:
            self._reset_stream_fields(run)

    def close(self) -> None:
        self.abort_stream()
        with self._stream_lock:
            if any(thread.is_alive() for thread in self._stream_workers.values()):
                # A timed-out abort can leave inference inside native code. Its
                # model/runtime must remain alive until that retired worker
                # exits; the worker performs the deferred clear in ``finally``.
                self._close_requested = True
                return
            self._clear_runtime()

    def _clear_runtime(self) -> None:
        with self._model_lock:
            self._model = None
            self._runtime = None
            self._runtime_device = ""

    def _stream_worker(self, run: _StreamRun) -> None:
        pcm_buffer = bytearray()
        try:
            with self._inference_lock:
                session = self._create_session()
                chunk_bytes = self._chunk_samples * 2
                while True:
                    if run.abort_requested.is_set():
                        return
                    item = run.audio_queue.get()
                    if item is _STREAM_SENTINEL:
                        break
                    pcm_buffer.extend(item)
                    while len(pcm_buffer) >= chunk_bytes:
                        payload = bytes(pcm_buffer[:chunk_bytes])
                        del pcm_buffer[:chunk_bytes]
                        self._process_stream_pcm(session, payload, run)

                if run.abort_requested.is_set():
                    return
                if pcm_buffer:
                    self._process_stream_pcm(session, bytes(pcm_buffer), run)
                self._flush_session(session)
                run.result.final_text = session.text
        except Exception as exc:
            run.result.error = exc
            callback = run.on_error
            if callback is not None:
                try:
                    callback(f"Nemotron streaming failed: {exc}")
                except Exception:
                    pass
        finally:
            with self._stream_lock:
                self._stream_workers.pop(run.generation, None)
                if self._close_requested and not self._stream_workers:
                    self._close_requested = False
                    # Keep the stream lock through the clear so a new stream
                    # cannot adopt the soon-to-be-retired model in between the
                    # decision and the actual teardown.
                    self._clear_runtime()

    def _process_stream_pcm(
        self,
        session: _InferenceSession,
        pcm_bytes: bytes,
        run: _StreamRun,
    ) -> None:
        usable = len(pcm_bytes) - (len(pcm_bytes) % 2)
        if usable <= 0:
            return
        samples = (
            np.frombuffer(pcm_bytes[:usable], dtype="<i2").astype(np.float32) / 32768.0
        )
        piece = self._process_samples(session, samples)
        if not piece or run.abort_requested.is_set():
            return
        callback = run.on_partial
        text = session.text.strip()
        if callback is not None and text:
            try:
                callback(text)
            except Exception:
                pass

    def _reset_stream_fields(self, run: _StreamRun) -> None:
        if self._stream_run is not run:
            return
        self._stream_active = False
        self._stream_run = None
        self._stream_thread = None

    def _load_wav_samples(self, audio_source: AudioInput) -> np.ndarray:
        source = (
            io.BytesIO(audio_source)
            if isinstance(audio_source, bytes)
            else str(audio_source)
        )
        try:
            with wave.open(source, "rb") as wav_file:
                channels = wav_file.getnchannels()
                sample_width = wav_file.getsampwidth()
                sample_rate = wav_file.getframerate()
                frame_count = wav_file.getnframes()
                raw = wav_file.readframes(frame_count)
        except (OSError, EOFError, wave.Error) as exc:
            raise TranscriptionError(
                "Nemotron currently accepts uncompressed PCM WAV audio only."
            ) from exc

        if sample_rate <= 0:
            raise TranscriptionError("Nemotron WAV input has an invalid sample rate.")
        samples = self._decode_pcm(raw, sample_width)
        if channels > 1:
            samples = samples[: len(samples) - (len(samples) % channels)]
            samples = samples.reshape(-1, channels).mean(axis=1)
        if sample_rate != self._sample_rate and len(samples) > 1:
            target_length = max(
                1,
                round(len(samples) * self._sample_rate / sample_rate),
            )
            source_positions = np.arange(len(samples), dtype=np.float64)
            target_positions = np.linspace(
                0,
                len(samples) - 1,
                target_length,
                dtype=np.float64,
            )
            samples = np.interp(target_positions, source_positions, samples)
        return np.asarray(samples, dtype=np.float32)

    @staticmethod
    def _decode_pcm(raw: bytes, sample_width: int) -> np.ndarray:
        if sample_width == 1:
            return (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128) / 128
        if sample_width == 2:
            return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
        if sample_width == 4:
            return np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648
        raise TranscriptionError(
            f"Nemotron does not support {sample_width * 8}-bit PCM WAV input."
        )
