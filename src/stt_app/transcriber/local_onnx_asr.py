"""Local NeMo ASR models served by the pure-Python `onnx-asr` runtime.

This is a third local ONNX path, separate from the Cohere/Granite Node runtime
(`local_webgpu_asr`) and from Nemotron's ONNX Runtime GenAI path
(`local_nemotron`). It needs no Node.js and no additional ONNX Runtime: onnx-asr
resolves the same `onnxruntime` distribution the app already carries.

Both models are batch-only. Their download, cache detection, size estimation and
deletion go through the shared layouts in `local_webgpu_asr`, so only inference
lives here.
"""

from __future__ import annotations

import io
import logging
import threading
import wave
from pathlib import Path

import numpy as np

from ..config import (
    AUDIO_SAMPLE_RATE,
    CANARY_MODEL_SIZE,
    DEFAULT_LANGUAGE_MODE,
    DOC_MODELS_PATH,
    LOCAL_ONNX_ASR_MODEL_SIZES,
    PARAKEET_MODEL_SIZE,
    language_modes_for_selection,
)
from .base import AudioInput, ITranscriber, ProgressReporter, TranscriptionError

logger = logging.getLogger(__name__)

# onnx-asr's registry name for each app model.
_ONNX_ASR_MODEL_NAMES: dict[str, str] = {
    PARAKEET_MODEL_SIZE: "nemo-parakeet-tdt-0.6b-v3",
    CANARY_MODEL_SIZE: "nemo-canary-1b-v2",
}


def _pcm_bytes_to_float32(data: bytes) -> np.ndarray:
    """Convert 16-bit little-endian PCM to the float32 waveform onnx-asr wants."""
    if len(data) % 2:
        data = data[:-1]
    samples = np.frombuffer(data, dtype="<i2").astype(np.float32)
    return samples / 32768.0


def _read_wav_float32(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as handle:
        if handle.getsampwidth() != 2:
            raise TranscriptionError(
                "Only 16-bit PCM WAV audio is supported by this local model."
            )
        channels = handle.getnchannels()
        sample_rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    waveform = _pcm_bytes_to_float32(frames)
    if channels > 1:
        # Average to mono; the models are single-channel.
        usable = (waveform.size // channels) * channels
        waveform = waveform[:usable].reshape(-1, channels).mean(axis=1)
    return waveform, sample_rate


class LocalOnnxAsrTranscriber(ITranscriber, ProgressReporter):
    """Batch transcription for Parakeet TDT and Canary via `onnx-asr`."""

    def __init__(
        self,
        model_size: str,
        language_mode: str = DEFAULT_LANGUAGE_MODE,
        *,
        offline_mode: bool = False,
        model_dir: str = "",
    ) -> None:
        ProgressReporter.__init__(self)
        if model_size not in LOCAL_ONNX_ASR_MODEL_SIZES:
            raise TranscriptionError(f"Unsupported onnx-asr model '{model_size}'.")
        self.model_size = model_size
        self.offline_mode = bool(offline_mode)
        self.model_dir = model_dir or ""
        self._language_mode = self._normalize_language_mode(language_mode)
        self._model: object | None = None
        self._model_lock = threading.Lock()
        # Reported to the benchmark and the runtime status line. This runtime is
        # CPU-only by construction: onnx-asr offers no DirectML path that can
        # coexist with the ONNX Runtime the app already ships (installing
        # `onnxruntime-directml` overwrites it and breaks onnxruntime-genai).
        self.runtime_device = "cpu"
        self.gpu_available = False
        self.runtime_details_text = ""
        self.runtime_warning = (
            "This model runs on CPU. onnx-asr has no GPU path here that can "
            "coexist with the app's ONNX Runtime."
        )

    def runtime_status_text(self) -> str:
        return "onnx-asr runtime active on CPU"

    # -- language ---------------------------------------------------------

    def _normalize_language_mode(self, mode: str) -> str:
        """Restrict to the modes this model was actually trained for.

        Canary has no auto-detect: `onnx-asr` hardcodes the `<|en|>` source and
        target token, so an unset language makes it *translate* into English
        instead of transcribing. It therefore never accepts ``auto``, and an
        untrained code would raise ``KeyError`` deep inside the runtime, so it
        is rejected here instead.
        """
        requested = str(mode or DEFAULT_LANGUAGE_MODE).strip().lower()
        supported = language_modes_for_selection("local", self.model_size)
        if requested in supported:
            return requested
        return supported[0]

    def _recognize_kwargs(self) -> dict[str, str]:
        # Parakeet TDT v3 is implicitly multilingual and ignores the argument,
        # so passing it would only invite the illusion of control.
        if self.model_size == PARAKEET_MODEL_SIZE:
            return {}
        return {"language": self._language_mode}

    # -- model lifecycle --------------------------------------------------

    def _resolve_model_path(self) -> Path:
        from .local_webgpu_asr import resolve_cached_webgpu_model_path

        cached = resolve_cached_webgpu_model_path(self.model_size, self.model_dir)
        if cached is not None:
            return cached
        if self.offline_mode:
            raise TranscriptionError(
                f"Local model '{self.model_size}' is not cached locally. "
                f"Disable Offline mode or download it first. See {DOC_MODELS_PATH}."
            )
        from .local_faster_whisper import download_model_snapshot

        download_model_snapshot(self.model_size, self.model_dir)
        cached = resolve_cached_webgpu_model_path(self.model_size, self.model_dir)
        if cached is None:
            raise TranscriptionError(
                f"Downloaded '{self.model_size}' but no complete int8 snapshot "
                f"was found. See {DOC_MODELS_PATH}."
            )
        return cached

    def _load_model(self) -> object:
        try:
            import onnx_asr
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise TranscriptionError(
                "The onnx-asr runtime is not installed. Reinstall the "
                "application dependencies and try again."
            ) from exc

        model_path = self._resolve_model_path()
        self._emit_progress(
            f"Loading {self.model_size} (onnx-asr, CPU)..."
        )
        try:
            return onnx_asr.load_model(
                _ONNX_ASR_MODEL_NAMES[self.model_size],
                str(model_path),
                quantization="int8",
            )
        except Exception as exc:
            raise TranscriptionError(
                f"Failed to load local model '{self.model_size}': {exc}"
            ) from exc

    def preload_model(self) -> None:
        with self._model_lock:
            if self._model is None:
                self._model = self._load_model()

    def close(self) -> None:
        with self._model_lock:
            self._model = None

    # -- transcription ----------------------------------------------------

    def transcribe_batch(self, audio_source: AudioInput) -> str:
        if isinstance(audio_source, (str, Path)):
            waveform, sample_rate = _read_wav_float32(Path(audio_source))
        elif isinstance(audio_source, (bytes, bytearray)):
            payload = bytes(audio_source)
            if payload[:4] == b"RIFF":
                with io.BytesIO(payload) as buffer, wave.open(buffer, "rb") as handle:
                    channels = handle.getnchannels()
                    sample_rate = handle.getframerate()
                    frames = handle.readframes(handle.getnframes())
                waveform = _pcm_bytes_to_float32(frames)
                if channels > 1:
                    usable = (waveform.size // channels) * channels
                    waveform = waveform[:usable].reshape(-1, channels).mean(axis=1)
            else:
                waveform = _pcm_bytes_to_float32(payload)
                sample_rate = AUDIO_SAMPLE_RATE
        else:
            raise TranscriptionError("Unsupported audio input for a local model.")

        if waveform.size == 0:
            return ""

        with self._model_lock:
            if self._model is None:
                self._model = self._load_model()
            model = self._model

        self._emit_progress(f"Transcribing with {self.model_size} (onnx-asr, CPU)...")
        try:
            text = model.recognize(  # type: ignore[attr-defined]
                waveform,
                sample_rate=sample_rate,
                **self._recognize_kwargs(),
            )
        except Exception as exc:
            raise TranscriptionError(
                f"Local transcription failed for '{self.model_size}': {exc}"
            ) from exc
        return str(text or "").strip()
