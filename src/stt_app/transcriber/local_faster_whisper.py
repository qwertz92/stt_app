from __future__ import annotations

import io
import logging
import os
import queue
import shutil
import tempfile
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..config import (
    AUDIO_SAMPLE_RATE,
    DEFAULT_CUSTOM_VOCABULARY,
    DEFAULT_LANGUAGE_MODE,
    DEFAULT_MODEL_SIZE,
    DOC_MODELS_PATH,
    DOC_SSL_PROXY_PATH,
    FASTER_WHISPER_MODEL_SIZES,
    LOCAL_ONNX_MODEL_SIZES,
    MODEL_REPO_MAP,
    STREAMING_ABORT_JOIN_TIMEOUT_S,
    STREAMING_PARTIAL_INTERVAL_S,
    STREAMING_PARTIAL_MIN_AUDIO_S,
    STREAMING_PARTIAL_WINDOW_S,
    VALID_MODEL_SIZES,
    language_modes_for_selection,
    parse_custom_vocabulary,
)
from ..ssl_utils import is_ssl_error as _is_ssl_error
from ..streaming_text import append_only_stream_partial_candidate
from .base import (
    AudioInput,
    ITranscriber,
    StreamingCallback,
    StreamingErrorCallback,
    TranscriptionCanceled,
    TranscriptionError,
)

logger = logging.getLogger(__name__)

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


@dataclass
class _StreamResult:
    error: Exception | None = None
    final_text: str = ""
    merged_text: str = ""
    last_partial_at: float = 0.0
    last_partial_size: int = 0
    error_reported: bool = False


@dataclass(frozen=True)
class _StreamingSession:
    """State owned by exactly one streaming worker.

    A timed-out abort may leave its daemon worker alive briefly. Keeping every
    mutable input and output on this generation-scoped object prevents that
    retired worker from reading audio or publishing results into a later
    session.
    """

    generation: int
    audio_queue: queue.Queue[bytes | object]
    on_partial: StreamingCallback | None
    on_error: StreamingErrorCallback | None
    pcm_buffer: bytearray = field(default_factory=bytearray)
    abort_requested: threading.Event = field(default_factory=threading.Event)
    result: _StreamResult = field(default_factory=_StreamResult)


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


def cleanup_incomplete_model_download(
    model_name: str,
    model_dir: str = "",
) -> tuple[int, int]:
    """Remove unusable partial files left by an interrupted model download."""
    removed_files = 0
    removed_bytes = 0
    for root in _model_cache_dirs(model_name, model_dir):
        if not root.is_dir():
            continue
        try:
            incomplete_paths = list(root.rglob("*.incomplete"))
        except OSError:
            continue
        for path in incomplete_paths:
            if not path.is_file():
                continue
            try:
                removed_bytes += path.stat().st_size
                path.unlink()
                removed_files += 1
            except FileNotFoundError:
                continue
            except OSError:
                continue

        try:
            directories = sorted(
                (path for path in root.rglob("*") if path.is_dir()),
                key=lambda path: len(path.parts),
                reverse=True,
            )
        except OSError:
            directories = []
        for directory in [*directories, root]:
            try:
                directory.rmdir()
            except OSError:
                continue
    return removed_files, removed_bytes


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
    if model_name in LOCAL_ONNX_MODEL_SIZES:
        from .local_webgpu_asr import download_webgpu_model_snapshot

        return download_webgpu_model_snapshot(model_name, model_dir)

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
        return _download_faster_whisper_via_modelscope(
            repo_id, model_dir, model_name, exc
        )


def _download_faster_whisper_via_modelscope(
    repo_id: str,
    model_dir: str,
    model_name: str,
    hf_error: Exception,
) -> str:
    """Fall back to the ModelScope mirror when Hugging Face is unreachable.

    Corporate proxies (e.g. Zscaler) may block Hugging Face wholesale. ModelScope
    hosts the same repo IDs and serves the weights from its own CDN. The files
    are written into the standard Hugging Face hub cache layout so faster-whisper
    resolves them exactly like a real download.
    """
    from . import modelscope_mirror as ms

    if not ms.modelscope_fallback_enabled() or not ms.repo_available(repo_id):
        raise RuntimeError(
            format_model_download_error(model_name, hf_error)
        ) from hf_error

    cache_dir = (
        model_dir.strip()
        if model_dir and model_dir.strip()
        else _default_hf_cache_dir()
    )
    logger.warning(
        "Hugging Face download failed for %s (%s); trying ModelScope mirror.",
        repo_id,
        hf_error,
    )
    try:
        path = ms.download_faster_whisper_to_cache(
            repo_id, cache_dir, allow_patterns=_DOWNLOAD_ALLOW_PATTERNS
        )
    except Exception as ms_error:
        raise RuntimeError(
            f"Model download for '{model_name}' failed on Hugging Face "
            f"({hf_error}) and on the ModelScope mirror ({ms_error})."
        ) from ms_error
    logger.info("Downloaded %s from ModelScope mirror.", repo_id)
    return path


def _directory_has_required_files(directory: Path, required_files: set[str]) -> bool:
    if not directory.is_dir():
        return False
    try:
        files = {entry.name for entry in directory.iterdir() if entry.is_file()}
    except OSError:
        return False
    return required_files.issubset(files)


def _has_valid_model_snapshot(cache_dir: Path, required_files: set[str]) -> bool:
    snapshots_dir = cache_dir / "snapshots"
    if not snapshots_dir.is_dir():
        return False
    try:
        for snapshot in snapshots_dir.iterdir():
            if not snapshot.is_dir():
                continue
            if _directory_has_required_files(snapshot, required_files):
                return True
    except OSError:
        return False
    return False


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

    existing_search_dirs: list[Path] = []
    seen_search_dirs: set[Path] = set()
    for base_dir in search_dirs:
        base = Path(base_dir)
        if not base.is_dir() or base in seen_search_dirs:
            continue
        seen_search_dirs.add(base)
        existing_search_dirs.append(base)

    required_files = {"config.json", "model.bin"}

    for short_name in FASTER_WHISPER_MODEL_SIZES:
        repo_id = _MODEL_REPO_MAP.get(short_name)
        if repo_id is None:
            continue

        folder_name = f"models--{repo_id.replace('/', '--')}"
        repo_basename = repo_id.rsplit("/", 1)[-1]

        for base in existing_search_dirs:
            if _has_valid_model_snapshot(base / folder_name, required_files):
                found.add(short_name)
                break

            flat_dir = base / repo_basename
            if _directory_has_required_files(flat_dir, required_files):
                found.add(short_name)
                break

    try:
        from .local_webgpu_asr import find_cached_webgpu_models

        found.update(find_cached_webgpu_models(model_dir or _default_hf_cache_dir()))
    except Exception:
        pass

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
        stream_final_full_pass: bool = True,
        model_factory=None,
        offline_mode: bool = False,
        model_dir: str = "",
        custom_vocabulary: str = DEFAULT_CUSTOM_VOCABULARY,
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
        self.stream_final_full_pass = bool(stream_final_full_pass)
        self._model_factory = model_factory or _default_model_factory
        self._model = None
        self._model_lock = threading.Lock()
        self._offline_mode = offline_mode
        self._model_dir = (model_dir or "").strip()
        self._initial_prompt = self._build_initial_prompt(custom_vocabulary)

        self._stream_lock = threading.Lock()
        self._stream_active = False
        self._stream_generation = 0
        self._stream_session: _StreamingSession | None = None
        self._stream_thread: threading.Thread | None = None
        # Kept as a test/debug convenience for inspecting a non-running buffer.
        # Live workers never read this alias; they receive their session object.
        self._stream_pcm_buffer = bytearray()
        self._cancel_check: Callable[[], bool] | None = None

    def set_cancel_check(self, cancel_check: Callable[[], bool] | None) -> None:
        """Install a callable polled during batch decoding to stop early.

        faster-whisper decodes lazily per segment, so checking between segments
        lets a long batch transcription be aborted promptly without finishing
        the whole recording.
        """
        self._cancel_check = cancel_check

    def _is_cancel_requested(self) -> bool:
        check = self._cancel_check
        if check is None:
            return False
        try:
            return bool(check())
        except Exception:
            return False

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
        supported_modes = language_modes_for_selection("local", self.model_size)
        if mode == DEFAULT_LANGUAGE_MODE or mode not in supported_modes:
            return None
        return mode

    @staticmethod
    def _build_initial_prompt(custom_vocabulary: str) -> str | None:
        """Build the Whisper ``initial_prompt`` from the custom vocabulary setting.

        Whisper's biasing convention treats ``initial_prompt`` as prior context
        text; a plain comma-separated term list nudges recognition toward those
        terms. Returns ``None`` when no terms are configured so the parameter
        is omitted entirely.
        """
        terms = parse_custom_vocabulary(custom_vocabulary)
        if not terms:
            return None
        return ", ".join(terms)

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

            if self._is_cancel_requested():
                raise TranscriptionCanceled()

            model = self._ensure_model()
            transcribe_kwargs: dict = {
                "language": self._language_arg(),
                "vad_filter": self.vad_filter,
            }
            if self._initial_prompt:
                transcribe_kwargs["initial_prompt"] = self._initial_prompt
            segments, _info = model.transcribe(input_for_model, **transcribe_kwargs)

            parts = []
            for segment in segments:
                # Decoding happens lazily as we iterate, so checking here lets a
                # long transcription stop between segments instead of finishing.
                if self._is_cancel_requested():
                    raise TranscriptionCanceled()
                text = getattr(segment, "text", "")
                if text:
                    stripped = str(text).strip()
                    if stripped:
                        parts.append(stripped)

            return " ".join(parts).strip()

        except TranscriptionCanceled:
            raise
        except Exception as exc:
            detail = self._format_transcription_error(exc)
            raise TranscriptionError(f"Local transcription failed: {detail}") from exc
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def start_stream(
        self,
        on_partial: StreamingCallback | None = None,
        on_error: StreamingErrorCallback | None = None,
    ) -> None:
        with self._stream_lock:
            if self._stream_active:
                raise TranscriptionError("Streaming session already active.")
            self._stream_generation += 1
            session = _StreamingSession(
                generation=self._stream_generation,
                audio_queue=queue.Queue(),
                on_partial=on_partial,
                on_error=on_error,
            )
            session.result.last_partial_at = time.monotonic()
            self._stream_active = True
            self._stream_session = session
            self._stream_pcm_buffer = session.pcm_buffer
            thread = threading.Thread(
                target=self._stream_worker,
                args=(session,),
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
            session = self._stream_session if self._stream_active else None
        if session is None:
            raise TranscriptionError("Streaming session is not active.")
        session.audio_queue.put(payload)

    def stop_stream(self) -> str:
        with self._stream_lock:
            session = self._stream_session if self._stream_active else None
            if session is None:
                raise TranscriptionError("Streaming session is not active.")
            stream_thread = self._stream_thread

        if stream_thread is None:
            raise TranscriptionError("Streaming session was not initialized correctly.")

        session.audio_queue.put(_STREAM_SENTINEL)
        stream_thread.join()

        with self._stream_lock:
            stream_error = session.result.error
            text = session.result.final_text
            self._reset_stream_fields(session)

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
            session = self._stream_session if self._stream_active else None
            if session is None:
                return
            stream_thread = self._stream_thread
            session.abort_requested.set()

        session.audio_queue.put(_STREAM_SENTINEL)
        if stream_thread is not None:
            stream_thread.join(timeout=STREAMING_ABORT_JOIN_TIMEOUT_S)

        with self._stream_lock:
            self._reset_stream_fields(session)

    def _reset_stream_fields(self, session: _StreamingSession) -> None:
        """Reset all streaming state. Must be called with ``_stream_lock`` held."""
        if self._stream_session is not session:
            return
        self._stream_active = False
        self._stream_session = None
        self._stream_thread = None
        self._stream_pcm_buffer = bytearray()

    def _notify_stream_error(self, session: _StreamingSession, exc: Exception) -> None:
        with self._stream_lock:
            callback = session.on_error
            if callback is None or session.result.error_reported:
                return
            session.result.error_reported = True

        detail = self._format_transcription_error(exc)
        try:
            callback(f"Local streaming failed: {detail}")
        except Exception:
            pass

    def _stream_worker(self, session: _StreamingSession) -> None:
        while True:
            if session.abort_requested.is_set():
                return

            item = session.audio_queue.get()
            if item is _STREAM_SENTINEL:
                break

            if isinstance(item, (bytes, bytearray)) and item:
                session.pcm_buffer.extend(item)

            self._maybe_emit_partial(session)

        # Capture abort flag under lock before it can be reset by
        # abort_stream / _reset_stream_fields on another thread.
        if session.abort_requested.is_set():
            if session.result.final_text == "":
                session.result.final_text = session.result.merged_text
            return

        try:
            if self.stream_final_full_pass:
                final_text = self._transcribe_current_stream_buffer(session=session)
            else:
                # Fast finalization: transcribe only the trailing window to
                # cover audio after the last partial, then merge it into the
                # accumulated live text instead of re-transcribing everything.
                tail_text = self._transcribe_current_stream_buffer(
                    max_window_seconds=self.stream_partial_window_s,
                    session=session,
                )
                final_text = append_only_stream_partial_candidate(
                    session.result.merged_text,
                    tail_text,
                )
        except Exception as exc:
            session.result.error = exc
            return

        session.result.final_text = final_text

    def _maybe_emit_partial(self, session: _StreamingSession | None = None) -> None:
        if session is None:
            with self._stream_lock:
                session = self._stream_session
        if session is None or session.abort_requested.is_set():
            return
        callback = session.on_partial
        now = time.monotonic()
        elapsed = now - session.result.last_partial_at
        min_audio_bytes = int(
            self.stream_partial_min_audio_s * self.stream_sample_rate * 2
        )
        current_size = len(session.pcm_buffer)
        has_new_audio = current_size > session.result.last_partial_size
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
                max_window_seconds=self.stream_partial_window_s,
                session=session,
            )
        except Exception as exc:
            was_aborted = session.abort_requested.is_set()
            session.result.error = exc
            session.abort_requested.set()
            if not was_aborted:
                self._notify_stream_error(session, exc)
            return

        if session.abort_requested.is_set():
            return
        session.result.merged_text = append_only_stream_partial_candidate(
            session.result.merged_text,
            text,
        )

        if callback is not None and text.strip():
            try:
                callback(text)
            except Exception:
                pass

        session.result.last_partial_at = time.monotonic()
        session.result.last_partial_size = len(session.pcm_buffer)

    def _transcribe_current_stream_buffer(
        self,
        max_window_seconds: float | None = None,
        *,
        session: _StreamingSession | None = None,
    ) -> str:
        if session is None:
            with self._stream_lock:
                current = self._stream_session
                snapshot = bytes(
                    current.pcm_buffer
                    if current is not None
                    else self._stream_pcm_buffer
                )
        else:
            snapshot = bytes(session.pcm_buffer)
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
