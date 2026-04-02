from __future__ import annotations

import io
import os
import queue
import shutil
import tempfile
import threading
import time
import wave
from pathlib import Path

from ..config import (
    AUDIO_SAMPLE_RATE,
    DEFAULT_LANGUAGE_MODE,
    DEFAULT_MODEL_SIZE,
    DOC_MODELS_PATH,
    DOC_SSL_PROXY_PATH,
    MODEL_REPO_MAP,
    STREAMING_ABORT_JOIN_TIMEOUT_S,
    STREAMING_PARTIAL_INTERVAL_S,
    STREAMING_PARTIAL_MIN_AUDIO_S,
    STREAMING_PARTIAL_WINDOW_S,
    VALID_MODEL_SIZES,
)
from ..ssl_utils import is_ssl_error as _is_ssl_error
from .base import AudioInput, ITranscriber, StreamingCallback, TranscriptionError

_STREAM_SENTINEL = object()
_DOWNLOAD_ALLOW_PATTERNS: list[str] = [
    "config.json",
    "preprocessor_config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.*",
]

# --- HuggingFace repo mapping (imported from config) ---
_MODEL_REPO_MAP = MODEL_REPO_MAP

# Reverse map: folder-safe repo name → short model name.
# e.g. "models--Systran--faster-whisper-small" → "small"
_REPO_FOLDER_TO_SHORT: dict[str, str] = {}
for _short, _repo in _MODEL_REPO_MAP.items():
    _folder_name = f"models--{_repo.replace('/', '--')}"
    _REPO_FOLDER_TO_SHORT[_folder_name] = _short


def _default_hf_cache_dir() -> str:
    """Return the default HuggingFace Hub cache directory."""
    hf_home = os.environ.get("HF_HOME", "")
    if hf_home:
        return os.path.join(hf_home, "hub")
    hf_cache = os.environ.get("HF_HUB_CACHE", "")
    if hf_cache:
        return hf_cache
    return os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub")


def _model_cache_dirs(model_name: str, model_dir: str = "") -> list[Path]:
    """Return possible cache directories for a model.

    Includes both HuggingFace-style cache folders and flat model folders.
    """
    repo_id = _MODEL_REPO_MAP.get(model_name)
    if repo_id is None:
        return []

    search_dirs: list[str] = []
    if model_dir and model_dir.strip():
        search_dirs.append(model_dir.strip())
    search_dirs.append(_default_hf_cache_dir())

    folder_name = f"models--{repo_id.replace('/', '--')}"
    repo_basename = repo_id.rsplit("/", 1)[-1]

    seen: set[Path] = set()
    dirs: list[Path] = []
    for base_dir in search_dirs:
        base = Path(base_dir)
        hf_style = base / folder_name
        flat = base / repo_basename
        for path in (hf_style, flat):
            if path in seen:
                continue
            seen.add(path)
            dirs.append(path)
    return dirs


def estimate_cached_model_bytes(model_name: str, model_dir: str = "") -> int:
    """Estimate the current on-disk bytes for a model cache directory.

    When multiple cache roots are present, returns the largest observed size
    to avoid double-counting duplicate copies of the same model.
    """
    max_bytes = 0
    for root in _model_cache_dirs(model_name, model_dir):
        if not root.is_dir():
            continue
        total = 0
        try:
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        except OSError:
            continue
        if total > max_bytes:
            max_bytes = total
    return max_bytes


def cached_model_paths(model_name: str, model_dir: str = "") -> list[Path]:
    """Return existing local directories that contain the model cache."""
    existing: list[Path] = []
    for candidate in _model_cache_dirs(model_name, model_dir):
        if candidate.exists():
            existing.append(candidate)
    return existing


def delete_cached_model(model_name: str, model_dir: str = "") -> int:
    """Delete local cache directories for a model.

    Returns the number of removed directories.
    """
    removed = 0
    for root in cached_model_paths(model_name, model_dir):
        try:
            shutil.rmtree(root)
            removed += 1
        except FileNotFoundError:
            continue
    return removed


def format_model_download_error(model_name: str, exc: Exception) -> str:
    if _is_ssl_error(exc):
        return (
            "SSL certificate verification failed while downloading the model. "
            "This is commonly caused by a corporate proxy. "
            "Set REQUESTS_CA_BUNDLE to your corporate CA bundle or download the model "
            f"on another machine. See {DOC_SSL_PROXY_PATH} and {DOC_MODELS_PATH}."
        )
    return f"Model download failed for '{model_name}': {exc}"


def download_model_snapshot(model_name: str, model_dir: str = "") -> str:
    try:
        from huggingface_hub import snapshot_download  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "huggingface_hub is not installed. Install dependencies and try again."
        ) from exc

    repo_id = _MODEL_REPO_MAP.get(model_name)
    if repo_id is None:
        raise ValueError(f"Unknown model '{model_name}'.")

    kwargs: dict[str, object] = {
        "allow_patterns": _DOWNLOAD_ALLOW_PATTERNS,
    }
    if model_dir and model_dir.strip():
        kwargs["cache_dir"] = model_dir.strip()

    try:
        return str(snapshot_download(repo_id, **kwargs))
    except Exception as exc:
        raise RuntimeError(format_model_download_error(model_name, exc)) from exc


def find_cached_models(model_dir: str = "") -> list[str]:
    """Scan HF cache (and optional custom model_dir) for locally available models.

    Returns a list of short model names (e.g. ``["tiny", "small"]``) that
    have a valid snapshot directory with at least ``config.json`` and
    ``model.bin``.
    """
    found: set[str] = set()

    search_dirs: list[str] = []
    if model_dir and model_dir.strip():
        search_dirs.append(model_dir.strip())
    search_dirs.append(_default_hf_cache_dir())

    required_files = {"config.json", "model.bin"}

    for base_dir in search_dirs:
        base = Path(base_dir)
        if not base.is_dir():
            continue

        # Check HF-style cache: models--<org>--<name>/snapshots/<hash>/
        for entry in base.iterdir():
            if not entry.is_dir():
                continue
            short_name = _REPO_FOLDER_TO_SHORT.get(entry.name)
            if short_name is None:
                continue
            snapshots_dir = entry / "snapshots"
            if not snapshots_dir.is_dir():
                continue
            for snapshot in snapshots_dir.iterdir():
                if not snapshot.is_dir():
                    continue
                files = {f.name for f in snapshot.iterdir() if f.is_file()}
                if required_files.issubset(files):
                    found.add(short_name)
                    break

        # Check flat model directories (direct path usage).
        for short_name, repo_id in _MODEL_REPO_MAP.items():
            # e.g. <base_dir>/faster-whisper-small/
            repo_basename = repo_id.rsplit("/", 1)[-1]
            flat_dir = base / repo_basename
            if flat_dir.is_dir():
                files = {f.name for f in flat_dir.iterdir() if f.is_file()}
                if required_files.issubset(files):
                    found.add(short_name)

    # Return in the canonical order from VALID_MODEL_SIZES.
    return [m for m in VALID_MODEL_SIZES if m in found]


def _default_model_factory(*args, **kwargs):
    from faster_whisper import WhisperModel  # type: ignore

    return WhisperModel(*args, **kwargs)


class LocalFasterWhisperTranscriber(ITranscriber):
    def __init__(
        self,
        model_size: str = DEFAULT_MODEL_SIZE,
        language_mode: str = DEFAULT_LANGUAGE_MODE,
        device: str = "auto",
        compute_type: str = "int8",
        vad_filter: bool = True,
        stream_sample_rate: int = AUDIO_SAMPLE_RATE,
        stream_partial_interval_s: float = STREAMING_PARTIAL_INTERVAL_S,
        stream_partial_min_audio_s: float = STREAMING_PARTIAL_MIN_AUDIO_S,
        stream_partial_window_s: float = STREAMING_PARTIAL_WINDOW_S,
        model_factory=None,
        offline_mode: bool = False,
        model_dir: str = "",
    ) -> None:
        self.model_size = model_size
        self.language_mode = language_mode
        self.device = device
        self.compute_type = compute_type
        self.vad_filter = vad_filter
        self.stream_sample_rate = max(1, int(stream_sample_rate))
        self.stream_partial_interval_s = max(0.0, float(stream_partial_interval_s))
        self.stream_partial_min_audio_s = max(0.0, float(stream_partial_min_audio_s))
        self.stream_partial_window_s = max(0.0, float(stream_partial_window_s))
        self._model_factory = model_factory or _default_model_factory
        self._model = None
        self._model_lock = threading.Lock()
        self._offline_mode = offline_mode
        self._model_dir = (model_dir or "").strip()

        self._stream_lock = threading.Lock()
        self._stream_active = False
        self._stream_on_partial: StreamingCallback | None = None
        self._stream_queue: queue.Queue[bytes | object] | None = None
        self._stream_thread: threading.Thread | None = None
        self._stream_pcm_buffer = bytearray()
        self._stream_error: Exception | None = None
        self._stream_final_text: str = ""
        self._stream_latest_text: str = ""
        self._stream_last_partial_at = 0.0
        self._stream_last_partial_size = 0
        self._stream_abort_requested = False

    def _ensure_model(self):
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is None:
                kwargs: dict = {
                    "device": self.device,
                    "compute_type": self.compute_type,
                }
                # Use WhisperModel's native local_files_only instead of env var.
                if self._offline_mode:
                    kwargs["local_files_only"] = True
                # download_root controls where HF caches model snapshots.
                if self._model_dir:
                    kwargs["download_root"] = self._model_dir
                self._model = self._model_factory(self.model_size, **kwargs)
            return self._model

    def preload_model(self) -> None:
        """Eagerly load/download the model.  Raises on failure."""
        self._ensure_model()

    @property
    def is_model_loaded(self) -> bool:
        return self._model is not None

    def _language_arg(self) -> str | None:
        mode = (self.language_mode or DEFAULT_LANGUAGE_MODE).strip().lower()
        if mode == DEFAULT_LANGUAGE_MODE:
            return None
        return mode

    def _format_transcription_error(self, exc: Exception) -> str:
        if isinstance(exc, ModuleNotFoundError):
            missing = exc.name or "unknown"
            return (
                f"Missing dependency '{missing}'. "
                "Run `uv sync --group dev` and restart the app."
            )
        msg = str(exc)
        msg_lower = msg.lower()

        # Detect SSL / certificate errors (corporate proxy / Zscaler).
        if _is_ssl_error(exc):
            return (
                "SSL certificate verification failed (likely a corporate "
                "proxy such as Zscaler). The model cannot be downloaded.\n"
                "Fix: set REQUESTS_CA_BUNDLE to your corporate CA .pem, "
                "or download the model on another machine and transfer it.\n"
                f"See {DOC_SSL_PROXY_PATH} for details."
            )

        # Detect HuggingFace Hub connectivity / offline-cache errors
        # (common on corporate machines with restricted internet).
        if "hub" in msg_lower and (
            "snapshot" in msg_lower
            or "internet" in msg_lower
            or "localentrynotfounderror" in msg_lower
        ):
            return (
                "Whisper model is not cached locally and the HuggingFace Hub "
                "is unreachable (common on corporate/restricted networks). "
                "Fix: download the model on a machine with internet access "
                "(run the app once), then copy the folder "
                "%USERPROFILE%\\.cache\\huggingface to this machine. "
                "Alternatively, enable 'Offline mode' in Settings "
                "if the model is already cached."
                f" See {DOC_MODELS_PATH}."
            )
        return msg

    def transcribe_batch(self, audio_source: AudioInput) -> str:
        temp_path: Path | None = None

        try:
            if isinstance(audio_source, bytes):
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
                    handle.write(audio_source)
                    temp_path = Path(handle.name)
                input_for_model = str(temp_path)
            else:
                input_for_model = str(audio_source)

            model = self._ensure_model()
            segments, _info = model.transcribe(
                input_for_model,
                language=self._language_arg(),
                vad_filter=self.vad_filter,
            )

            parts = []
            for segment in segments:
                text = getattr(segment, "text", "")
                if text:
                    stripped = str(text).strip()
                    if stripped:
                        parts.append(stripped)

            return " ".join(parts).strip()

        except Exception as exc:
            detail = self._format_transcription_error(exc)
            raise TranscriptionError(f"Local transcription failed: {detail}") from exc
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def start_stream(self, on_partial: StreamingCallback | None = None) -> None:
        with self._stream_lock:
            if self._stream_active:
                raise TranscriptionError("Streaming session already active.")
            self._stream_active = True
            self._stream_on_partial = on_partial
            self._stream_queue = queue.Queue()
            self._stream_pcm_buffer = bytearray()
            self._stream_error = None
            self._stream_final_text = ""
            self._stream_latest_text = ""
            self._stream_last_partial_at = time.monotonic()
            self._stream_last_partial_size = 0
            self._stream_abort_requested = False
            thread = threading.Thread(
                target=self._stream_worker,
                name="stt_app_stream_worker",
                daemon=True,
            )
            self._stream_thread = thread
        thread.start()

    def push_audio_chunk(self, chunk: bytes) -> None:
        payload = bytes(chunk or b"")
        if not payload:
            return
        with self._stream_lock:
            stream_active = self._stream_active
            stream_queue = self._stream_queue
        if not stream_active or stream_queue is None:
            raise TranscriptionError("Streaming session is not active.")
        stream_queue.put(payload)

    def stop_stream(self) -> str:
        with self._stream_lock:
            if not self._stream_active:
                raise TranscriptionError("Streaming session is not active.")
            stream_queue = self._stream_queue
            stream_thread = self._stream_thread

        if stream_queue is None or stream_thread is None:
            raise TranscriptionError("Streaming session was not initialized correctly.")

        stream_queue.put(_STREAM_SENTINEL)
        stream_thread.join()

        with self._stream_lock:
            stream_error = self._stream_error
            text = self._stream_final_text
            self._reset_stream_fields()

        if stream_error is not None:
            detail = self._format_transcription_error(
                stream_error
                if isinstance(stream_error, Exception)
                else Exception(str(stream_error))
            )
            raise TranscriptionError(
                f"Local streaming failed: {detail}"
            ) from stream_error
        return text.strip()

    def abort_stream(self) -> None:
        with self._stream_lock:
            if not self._stream_active:
                return
            self._stream_abort_requested = True
            stream_queue = self._stream_queue
            stream_thread = self._stream_thread

        if stream_queue is not None:
            stream_queue.put(_STREAM_SENTINEL)
        if stream_thread is not None:
            stream_thread.join(timeout=STREAMING_ABORT_JOIN_TIMEOUT_S)

        with self._stream_lock:
            self._reset_stream_fields()

    def _reset_stream_fields(self) -> None:
        """Reset all streaming state. Must be called with ``_stream_lock`` held."""
        self._stream_active = False
        self._stream_on_partial = None
        self._stream_queue = None
        self._stream_thread = None
        self._stream_pcm_buffer = bytearray()
        self._stream_error = None
        self._stream_final_text = ""
        self._stream_latest_text = ""
        self._stream_last_partial_size = 0
        self._stream_last_partial_at = 0.0
        self._stream_abort_requested = False

    def _stream_worker(self) -> None:
        while True:
            with self._stream_lock:
                stream_queue = self._stream_queue
                if self._stream_abort_requested:
                    return
            if stream_queue is None:
                return

            item = stream_queue.get()
            if item is _STREAM_SENTINEL:
                break

            if isinstance(item, (bytes, bytearray)) and item:
                with self._stream_lock:
                    self._stream_pcm_buffer.extend(item)

            self._maybe_emit_partial()

        # Capture abort flag under lock before it can be reset by
        # abort_stream / _reset_stream_fields on another thread.
        with self._stream_lock:
            aborted = self._stream_abort_requested

        if aborted:
            with self._stream_lock:
                # Fields may already be reset by abort_stream(); write
                # only if the session is still ours to finalize.
                if self._stream_final_text == "":
                    self._stream_final_text = self._stream_latest_text
            return

        try:
            final_text = self._transcribe_current_stream_buffer()
        except Exception as exc:
            with self._stream_lock:
                self._stream_error = exc
            return

        with self._stream_lock:
            self._stream_final_text = final_text

    def _maybe_emit_partial(self) -> None:
        with self._stream_lock:
            callback = self._stream_on_partial
            if self._stream_abort_requested:
                return
            now = time.monotonic()
            elapsed = now - self._stream_last_partial_at
            min_audio_bytes = int(
                self.stream_partial_min_audio_s * self.stream_sample_rate * 2
            )
            current_size = len(self._stream_pcm_buffer)
            has_new_audio = current_size > self._stream_last_partial_size
            should_emit = (
                callback is not None
                and has_new_audio
                and current_size >= min_audio_bytes
                and elapsed >= self.stream_partial_interval_s
            )

        if not should_emit:
            return

        try:
            text = self._transcribe_current_stream_buffer(
                max_window_seconds=self.stream_partial_window_s
            )
        except Exception as exc:
            with self._stream_lock:
                self._stream_error = exc
            return

        with self._stream_lock:
            self._stream_latest_text = text

        if callback is not None and text.strip():
            try:
                callback(text)
            except Exception:
                pass

        with self._stream_lock:
            self._stream_last_partial_at = time.monotonic()
            self._stream_last_partial_size = len(self._stream_pcm_buffer)

    def _transcribe_current_stream_buffer(
        self,
        max_window_seconds: float | None = None,
    ) -> str:
        with self._stream_lock:
            snapshot = bytes(self._stream_pcm_buffer)
        if not snapshot:
            return ""
        if max_window_seconds is not None and max_window_seconds > 0:
            max_bytes = int(max_window_seconds * self.stream_sample_rate * 2)
            if max_bytes > 0 and len(snapshot) > max_bytes:
                snapshot = snapshot[-max_bytes:]
        wav_bytes = self._pcm16_to_wav_bytes(snapshot)
        return self.transcribe_batch(wav_bytes)

    def _pcm16_to_wav_bytes(self, pcm_bytes: bytes) -> bytes:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self.stream_sample_rate)
            wav_file.writeframes(pcm_bytes)
        return buffer.getvalue()
