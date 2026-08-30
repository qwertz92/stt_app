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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..config import (
    AUDIO_SAMPLE_RATE,
    DEFAULT_CUSTOM_VOCABULARY,
    DEFAULT_FASTER_WHISPER_MODEL_SIZE,
    DEFAULT_LANGUAGE_MODE,
    DEFAULT_SILENCE_GATE_ENABLED,
    DEFAULT_SILENCE_GATE_THRESHOLD,
    DOC_MODELS_PATH,
    DOC_SSL_PROXY_PATH,
    FASTER_WHISPER_MODEL_SIZES,
    LOCAL_ONNX_MODEL_SIZES,
    MODEL_REPO_MAP,
    MODELS_WITHOUT_MODELSCOPE_MIRROR,
    STREAMING_ABORT_JOIN_TIMEOUT_S,
    STREAMING_NEW_SEGMENT_MIN_SPEECH_S,
    STREAMING_PARTIAL_INTERVAL_S,
    STREAMING_PARTIAL_MIN_AUDIO_S,
    STREAMING_PARTIAL_WINDOW_S,
    STREAMING_SPEECH_RUN_WINDOW_MS,
    VALID_MODEL_SIZES,
    language_modes_for_selection,
    parse_custom_vocabulary,
)
from ..ssl_utils import is_ssl_error as _is_ssl_error
from ..streaming_text import merge_rolling_window, merge_rolling_window_transcript
from ..vad import measure_longest_speech_run_s, measure_peak_windowed_rms_pcm
from .base import (
    AudioInput,
    ITranscriber,
    StreamingCallback,
    StreamingErrorCallback,
    TranscriptionCanceled,
    TranscriptionError,
    canceled_download_is_a_cancel,
)

logger = logging.getLogger(__name__)

# How long a stream may run without a single quiet slice before the log
# says the noise floor has disabled pause detection.
_NOISE_FLOOR_WARN_AFTER_S = 20.0

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
    # Audio skipped as silence since the last decoded window. Once it exceeds
    # the window length the next window can no longer overlap what was already
    # transcribed, so it is a new segment and must be appended, not merged.
    silent_seconds: float = 0.0
    # Text closed off by an earlier measured pause. A later unalignable
    # window replaces only what follows it, so one bad window costs one
    # segment instead of the whole dictation.
    segment_floor: str = ""
    loud_since: float | None = None
    noise_floor_warned: bool = False
    # Once per session, like `noise_floor_warned`: the partial callback runs
    # every ~350 ms, so an unbounded log would flood.
    partial_callback_failed: bool = False
    # The byte range of the audio the last decode covered. When the next
    # window starts at or after `last_window_end` the two share no audio at
    # all -- see `_window_shares_no_audio_with_the_last`.
    last_window_start: int = 0
    last_window_end: int = 0
    slow_decode_warned: bool = False


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


def download_destination_dir(model_name: str, model_dir: str = "") -> Path | None:
    """Return the one directory a download for this model writes into.

    Download progress is derived from cache growth, so it must watch exactly the
    directory the downloader targets — never merely a *candidate* location for an
    existing copy. Local ONNX models download into a flat `local_dir`;
    faster-whisper models download into the HuggingFace blob/snapshot cache under
    the configured `cache_dir`. Measuring any other layout or cache root reports a
    foreign directory's size as this download's progress, which is how a stale
    full-repo copy could show 100% while the real download had barely started.
    """
    if model_name in LOCAL_ONNX_MODEL_SIZES:
        from .local_webgpu_asr import webgpu_download_destination

        return webgpu_download_destination(model_name, model_dir)

    repo_id = _MODEL_REPO_MAP.get(model_name)
    if repo_id is None:
        return None

    base_dir = (
        model_dir.strip()
        if model_dir and model_dir.strip()
        else _default_hf_cache_dir()
    )
    return Path(base_dir) / f"models--{repo_id.replace('/', '--')}"


def estimate_cached_model_bytes(model_name: str, model_dir: str = "") -> int:
    """Estimate the current on-disk bytes of a model's download destination.

    Until that destination exists, fall back to a cache root holding a
    *complete, loadable* snapshot. A local ONNX model cached in the legacy
    `models--<repo>` layout is still resolved and loaded from there, so
    measuring only the flat destination reported 0 bytes and a 0% "Downloading"
    bar for a model that is fully present.

    The fallback deliberately requires a valid snapshot rather than taking the
    largest candidate directory. Sizing any candidate is what let the NAR
    repo's 9.4 GB fp32 conversion copy pose as this model's download progress;
    it carries none of the required `int8/*` files, so it can never qualify.
    An in-flight download has no valid snapshot anywhere either, so it starts
    at 0% and is measured only at its destination.
    """
    root = download_destination_dir(model_name, model_dir)
    if root is not None and root.is_dir():
        return _directory_size_bytes(root)
    cached_root = _complete_cached_model_root(model_name, model_dir)
    return 0 if cached_root is None else _directory_size_bytes(cached_root)


def _complete_cached_model_root(model_name: str, model_dir: str = "") -> Path | None:
    if model_name in LOCAL_ONNX_MODEL_SIZES:
        from .local_webgpu_asr import resolve_cached_webgpu_model_root

        return resolve_cached_webgpu_model_root(model_name, model_dir)

    for root in _model_cache_dirs(model_name, model_dir):
        if _has_valid_model_snapshot(root, {"config.json", "model.bin"}):
            return root
    return None


def _directory_size_bytes(root: Path) -> int:
    if not root.is_dir():
        return 0
    total = 0
    try:
        for path in root.rglob("*"):
            # A HuggingFace snapshot entry can be a symlink to a blob that is
            # already counted; stat() follows it, so counting both doubles the
            # measured size and reports 100% at half a download.
            if path.is_symlink() or not path.is_file():
                continue
            try:
                total += path.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def cached_model_paths(model_name: str, model_dir: str = "") -> list[Path]:
    """Return existing local directories that contain the model cache."""
    return [
        candidate
        for candidate in _model_cache_dirs(model_name, model_dir)
        if candidate.exists()
    ]


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
            incomplete_paths = [
                path
                for pattern in ("*.incomplete", "*.ms-part")
                for path in root.rglob(pattern)
            ]
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
    ssl_hint = (
        " The failure looked like a certificate error, which a corporate proxy "
        "also produces; if the network does allow Hugging Face, point "
        f"REQUESTS_CA_BUNDLE at your CA bundle and retry. See {DOC_SSL_PROXY_PATH}."
        if _is_ssl_error(exc)
        else ""
    )
    if model_name in MODELS_WITHOUT_MODELSCOPE_MIRROR:
        # The generic wording ends in "check your internet connection", which
        # sends people chasing the one thing that is fine. This model simply
        # has no second source. A TLS-intercepting proxy reports the block as a
        # certificate failure, so both causes are named rather than guessed at.
        return (
            f"'{model_name}' could not be downloaded: Hugging Face is not "
            "reachable from this machine and this model has no ModelScope "
            "mirror, so there is no second source to fall back to. Download it "
            "on an unrestricted machine and point 'Model Dir' at it, pick a "
            "mirrored model, or ask IT to allow huggingface.co. See "
            f"{DOC_MODELS_PATH}.{ssl_hint}"
        )
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
    """Scan HF cache (and optional custom model_dir) for cached models.

    Returns short model names (e.g. ``["small", "parakeet-tdt-0.6b-v3"]``) in
    the canonical order of ``VALID_MODEL_SIZES``.

    Two different checks, because the two families are stored differently: a
    faster-whisper model needs a snapshot directory carrying at least
    ``config.json`` and ``model.bin``, while the ONNX models are delegated to
    ``find_cached_webgpu_models``, which validates each model's own required
    file list. Both search the configured Model Dir and the default cache.
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

        # `model_dir` unchanged rather than defaulted. Behaviour-neutral --
        # the ONNX scan adds the default cache itself now, exactly like the
        # Whisper loop above -- but defaulting a root before handing it to a
        # function that defaults the same root reads as if it mattered.
        found.update(find_cached_webgpu_models(model_dir))
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
        model_size: str = DEFAULT_FASTER_WHISPER_MODEL_SIZE,
        language_mode: str = DEFAULT_LANGUAGE_MODE,
        device: str = "auto",
        compute_type: str = "int8",
        vad_filter: bool = True,
        stream_sample_rate: int = AUDIO_SAMPLE_RATE,
        stream_partial_interval_s: float = STREAMING_PARTIAL_INTERVAL_S,
        stream_partial_min_audio_s: float = STREAMING_PARTIAL_MIN_AUDIO_S,
        stream_partial_window_s: float = STREAMING_PARTIAL_WINDOW_S,
        stream_final_full_pass: bool = True,
        silence_gate_enabled: bool = DEFAULT_SILENCE_GATE_ENABLED,
        silence_gate_threshold: float = DEFAULT_SILENCE_GATE_THRESHOLD,
        model_factory=None,
        offline_mode: bool = False,
        model_dir: str = "",
        custom_vocabulary: str = DEFAULT_CUSTOM_VOCABULARY,
    ) -> None:
        self.model_size = model_size
        # Needs self.model_size, so this must run after it is assigned above.
        self.set_language_mode(language_mode)
        self.device = device
        self.compute_type = compute_type
        self.vad_filter = vad_filter
        self.stream_sample_rate = max(1, int(stream_sample_rate))
        self.stream_partial_interval_s = max(0.0, float(stream_partial_interval_s))
        self.stream_partial_min_audio_s = max(0.0, float(stream_partial_min_audio_s))
        self.stream_partial_window_s = max(0.0, float(stream_partial_window_s))
        self.stream_final_full_pass = bool(stream_final_full_pass)
        # Streaming windows use the same gate as batch: a window with no speech
        # in it must not be decoded, because the model invents words from it.
        self.silence_gate_enabled = bool(silence_gate_enabled)
        self.silence_gate_threshold = float(silence_gate_threshold)
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

    def set_cancel_check(self, cancel_check: Callable[[], bool] | None) -> None:
        """Install a callable polled during batch decoding to stop early.

        faster-whisper decodes lazily per segment, so checking between segments
        lets a long batch transcription be aborted promptly without finishing
        the whole recording.
        """
        # Through the base class: it also re-arms the once-per-check log latch,
        # and this runtime is cached for the whole app lifetime, so skipping
        # that reduced "once per installed check" to once per process.
        super().set_cancel_check(cancel_check)

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
                # WhisperModel downloads inside its own constructor via
                # huggingface_hub, which no grep of this repo reveals. Fetch
                # first, through the one download slot, so the default engine
                # cannot race the Local tab or another transcriber into the
                # same cache directory.
                self._coordinated_download_if_missing()
                self._model = self._model_factory(self.model_size, **kwargs)
            return self._model

    def _coordinated_download_if_missing(self) -> None:
        if self._offline_mode:
            return
        # Gate on the directory WhisperModel will actually resolve, not on
        # find_cached_models: that also accepts the default cache and a flat
        # layout, so with a custom Model Dir it reported "cached" for a model
        # the constructor would still download — uncoordinated.
        destination = download_destination_dir(self.model_size, self._model_dir)
        if destination is not None and _has_valid_model_snapshot(
            destination, {"config.json", "model.bin"}
        ):
            return
        from ..model_download_coordinator import run_coordinated_download

        with canceled_download_is_a_cancel():
            run_coordinated_download(
                self.model_size,
                self._model_dir,
                lambda: download_model_snapshot(self.model_size, self._model_dir),
                # `_is_cancel_requested`, not the raw attribute: a check that
                # raises must never fail the work, and the coordinator re-raises
                # whatever escapes it.
                cancel_check=self._is_cancel_requested,
            )

    def preload_model(self) -> None:
        """Eagerly load/download the model.  Raises on failure."""
        self._ensure_model()

    @property
    def is_model_loaded(self) -> bool:
        return self._model is not None

    def _normalize_language_mode(self, mode: str) -> str:
        normalized = (mode or DEFAULT_LANGUAGE_MODE).strip().lower()
        supported_modes = language_modes_for_selection("local", self.model_size)
        if normalized not in supported_modes:
            normalized = DEFAULT_LANGUAGE_MODE
        return normalized

    def _language_arg(self) -> str | None:
        # Auto-detect is expressed to faster-whisper as no language hint.
        if self._language_mode == DEFAULT_LANGUAGE_MODE:
            return None
        return self._language_mode

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
        """Run the streaming loop, and never end without saying why.

        Only the decode inside `_maybe_emit_partial` and the finalization
        below were guarded. The energy meters, the merge and the buffer append
        were not, so an exception in any of them simply ended the thread:
        `stop_stream` joined a dead worker, found no error and an empty
        `final_text`, and the whole dictation reached the user as "No speech
        detected". A windowed build has no stderr either, so
        `threading.excepthook` printed the traceback nowhere at all.

        Recording the error routes it through the controller's failure path,
        which keeps the live transcript. It is deliberately not re-raised:
        this is the top of a worker thread, so re-raising only writes to that
        same missing stderr.

        Recording it is not enough on its own, and that was the first version
        of this guard. `session.result.error` is read at *stop* time, so until
        the user presses the hotkey nothing on screen changes: the live text
        simply stops advancing, which is indistinguishable from having stopped
        talking. Everything said between the failure and that keypress is
        never decoded and cannot be recovered, and the capture keeps pushing
        ~32 kB/s into a queue nobody drains. `_notify_stream_error` is what
        ends the session there and then; it latches on `error_reported`, so
        an error the decode handler already reported is not reported twice.
        """
        try:
            self._run_stream_worker(session)
        except BaseException as exc:
            logger.exception("streaming_worker_failed")
            failure = session.result.error
            if failure is None:
                failure = (
                    exc
                    if isinstance(exc, Exception)
                    else RuntimeError(str(exc) or type(exc).__name__)
                )
                session.result.error = failure
            self._notify_stream_error(session, failure)

    def _run_stream_worker(self, session: _StreamingSession) -> None:
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
                # The same silence gate as the partials. Without it a
                # dictation that ends with a few seconds of quiet decoded one
                # last hallucinated window here, and because a hallucination
                # cannot be aligned the merge replaced the entire transcript
                # with it -- the whole dictation lost at the last step.
                # Two ways the trailing window must not be decoded: it is
                # silent, or the pause before it was longer than the window and
                # what little sound it holds is not enough to be speech. The
                # second case is a transient -- a click, a chair creak -- and
                # decoding it produces an invented sentence that, being
                # unalignable, replaces the entire dictation at the last step.
                tail_after_long_pause = (
                    self.stream_partial_window_s > 0
                    and session.result.silent_seconds >= self.stream_partial_window_s
                )
                if self._stream_tail_window_is_silent(session) or (
                    tail_after_long_pause
                    and self.silence_gate_enabled
                    and not self._stream_window_has_speech(session)
                ):
                    final_text = session.result.merged_text
                else:
                    previous_window_end = session.result.last_window_end
                    tail_text = self._transcribe_current_stream_buffer(
                        max_window_seconds=self.stream_partial_window_s,
                        session=session,
                    )
                    # The trailing window can be disjoint from the last decoded
                    # partial for the same reason one partial can be disjoint
                    # from the one before it, and here it costs the whole
                    # dictation in a single step rather than gradually.
                    disjoint = self._window_shares_no_audio_with_the_last(
                        session,
                        previous_window_end,
                        pause_explains_the_gap=tail_after_long_pause,
                    )
                    final_text = merge_rolling_window_transcript(
                        session.result.merged_text,
                        tail_text,
                        protected_prefix=session.result.segment_floor,
                        # Same rule as the partial path: append only when the
                        # decoded window really holds speech. Reading
                        # `silent_seconds` alone let a transient at the end of a
                        # long pause append an invented sentence.
                        new_segment=(
                            disjoint and self._decoded_window_has_speech(session)
                        )
                        or (
                            tail_after_long_pause
                            and self._stream_window_has_speech(session)
                        ),
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

        # Do not decode silence. faster-whisper invents words from it — that is
        # why the batch silence gate exists — and here every invented window is
        # unalignable, so it used to replace the whole accumulated transcript.
        new_audio = bytes(session.pcm_buffer[session.result.last_partial_size:])
        quiet_slice = self._stream_slice_is_quiet(new_audio)
        self._warn_once_if_the_room_is_never_quiet(session, quiet_slice)
        if quiet_slice:
            # Tracked even when the gate is switched off: `silent_seconds` also
            # drives the pause detection below, and wiring two behaviours to one
            # checkbox meant disabling the gate silently disabled that too.
            session.result.silent_seconds += len(new_audio) / (
                self.stream_sample_rate * 2
            )
            if self.silence_gate_enabled:
                session.result.last_partial_at = time.monotonic()
                session.result.last_partial_size = len(session.pcm_buffer)
                return

        # A pause longer than the window means the coming window shares no
        # audio with what is already transcribed, so it cannot be aligned. That
        # is the most dangerous input there is: the window is mostly silence,
        # which is exactly what makes the model invent words. So measure the
        # window that will ACTUALLY be decoded -- not the slice that just
        # arrived -- and require real speech in it, which a keyboard click or a
        # chair creak cannot fake.
        pause_exceeded_window = (
            self.stream_partial_window_s > 0
            and session.result.silent_seconds >= self.stream_partial_window_s
        )
        new_segment = False
        if pause_exceeded_window:
            new_segment = self._stream_window_has_speech(session)
            if not new_segment and self.silence_gate_enabled:
                # Too little speech to append on trust -- and too little to
                # trust a replace either. A transient ending a long pause would
                # otherwise decode to an invented sentence that, being
                # unalignable, wipes the real transcript. Skip it and keep
                # counting the pause.
                logger.debug(
                    "stream_window_after_pause_skipped: too little speech to "
                    "decode a window that cannot be aligned."
                )
                session.result.last_partial_at = time.monotonic()
                session.result.last_partial_size = len(session.pcm_buffer)
                return
        if not quiet_slice:
            # Reset only on audio that actually carried sound. Resetting
            # unconditionally meant that with the gate switched off the
            # counter was incremented and zeroed on the same call, so it
            # never accumulated, `pause_exceeded_window` was never true, and
            # the pause handling was silently dead -- the exact coupling the
            # decoupling above was meant to remove.
            session.result.silent_seconds = 0.0

        previous_window_end = session.result.last_window_end
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
        previous_text = session.result.merged_text
        if self._window_shares_no_audio_with_the_last(
            session,
            previous_window_end,
            # Captured before the reset a few lines up: a pause that has just
            # ended has already zeroed `silent_seconds`, and that is exactly
            # the case whose gap is silence rather than lost speech.
            pause_explains_the_gap=pause_exceeded_window,
        ):
            # The pause case reached by the other road, and proven rather than
            # measured: nothing already transcribed can be revised by a window
            # that shares none of its audio, and there is no seam to search
            # for. Pinning the floor below is what turns the replace into an
            # append; `new_segment` only skips a search that cannot succeed --
            # and that search does sometimes succeed by coincidence, on words
            # the two windows happen to share, which swallows the window
            # instead of appending it.
            #
            # Appending on trust still requires the same proof the pause route
            # demands: that the decoded window really holds speech. Without
            # it this route was the one unguarded append in the design. A
            # decode near RTF 1 makes every increment ~8 s long, so a single
            # keystroke in it defeats `_stream_slice_is_quiet` (a peak
            # measure), `silent_seconds` resets, the pause route never runs,
            # and a mostly-silent window is decoded, invented, appended AND
            # pinned as the floor -- once per decode, growing linearly.
            # Measured over five whisper-typical silence hallucinations: 23
            # words against the 5 a bounded replace keeps. A window that
            # cannot be shown to hold speech falls through to that bounded
            # replace instead.
            new_segment = new_segment or self._decoded_window_has_speech(session)
        if new_segment:
            # Everything up to here is closed off: the pause proved no later
            # window shares audio with it.
            #
            # Known limitation: if an admitted transient produced a
            # hallucination in the previous segment, the pause pins it and no
            # later window can remove it. Bounded junk that stays beats real
            # text that disappears.
            session.result.segment_floor = previous_text
        merge = merge_rolling_window(
            previous_text,
            text,
            new_segment=new_segment,
            protected_prefix=session.result.segment_floor,
        )
        session.result.merged_text = merge.text
        # Advance the floor when a window ALIGNED with what came before and
        # actually added something. Both halves are load-bearing:
        #
        # - Aligning is the corroboration: two overlapping windows agreed on
        #   the seam. Inferring that from `startswith` was wrong in both
        #   directions -- once a floor exists the replace branch also returns
        #   text starting with `previous`, which is what let junk be pinned.
        # - Requiring growth is what stops the ratchet. Whisper repeats the
        #   same invented phrase across windows that share 96% of their audio,
        #   and two identical windows "align" trivially. Pinning that made the
        #   phrase permanent, and the next drift appended a fresh one after
        #   it: measured at 53 words from 4 of real speech, growing
        #   linearly with the pause. A repeat leaves the text unchanged, so
        #   requiring growth skips exactly that case while real speech, which
        #   adds words, advances normally.
        if (
            previous_text
            and merge.aligned
            and merge.text != previous_text
        ):
            session.result.segment_floor = previous_text

        # Emit the *merged* transcript, not the raw window. The controller kept
        # its own copy of this merge over raw windows, so the same stitching ran
        # twice and could disagree; and a raw window never contains the text
        # already inserted, which froze live insertion for the rest of the
        # session once the locked prefix no longer matched.
        if callback is not None and session.result.merged_text.strip():
            try:
                callback(session.result.merged_text)
            except Exception:
                # Swallowed on purpose -- one failed delivery must not end the
                # dictation, and the next partial carries the merged text
                # again. But not silently: this callback is what puts live
                # text on screen and into the document, so failing it
                # without a log makes a dead live-insertion path
                # indistinguishable from a user who simply stopped talking.
                if not session.result.partial_callback_failed:
                    session.result.partial_callback_failed = True
                    logger.exception(
                        "Streaming partial callback failed; live text is not "
                        "being delivered. Logged once per session."
                    )

        session.result.last_partial_at = time.monotonic()
        session.result.last_partial_size = len(session.pcm_buffer)

    def _trailing_window(
        self,
        session: _StreamingSession,
        max_window_seconds: float | None,
    ) -> tuple[bytes, int, int]:
        """The trailing window of the buffer, and the byte range it covers.

        Copies only the window. `bytes(pcm_buffer)` copies the whole recording,
        which grows without bound: measured at 16 kHz mono, 0.21 ms for one
        minute of audio, 0.93 ms for five and 3.14 ms for fifteen, against a
        flat 0.10 ms here -- and a partial runs every ~350 ms, taking up to
        three of these copies when the pause branch also measures the window.

        The end is read once and both the slice and the reported range use it,
        so the offsets describe exactly the bytes returned even though the
        capture thread keeps appending to the buffer.
        """
        buffer = session.pcm_buffer
        end = len(buffer)
        start = 0
        if max_window_seconds is not None and max_window_seconds > 0:
            max_bytes = int(max_window_seconds * self.stream_sample_rate * 2)
            if max_bytes > 0 and end > max_bytes:
                start = end - max_bytes
        return bytes(buffer[start:end]), start, end

    def _window_shares_no_audio_with_the_last(
        self,
        session: _StreamingSession,
        previous_window_end: int,
        *,
        pause_explains_the_gap: bool,
    ) -> bool:
        """Did the previous window's audio scroll fully out of this one?

        A decode that takes about as long as the window itself -- a large model
        on a slow machine, RTF near 1 -- advances the trailing window by more
        than its own length, so consecutive windows are disjoint. Nothing else
        notices: with continuous speech `silent_seconds` never accumulates, so
        no pause ever pins `segment_floor`, and the unalignable window then
        replaced the *entire* accumulated transcript. Measured with an 8 s
        window and 9 s between decodes: "erster teil der nachricht" became
        "und dann kam etwas ganz anderes", i.e. everything but the last window
        was lost, however long the dictation had been.

        The speech in the gap between two disjoint windows was never decoded
        and is gone either way. This only stops that from taking the rest of
        the transcript with it.

        Takes the previous end as an argument because the decode has already
        overwritten `last_window_end` by the time the caller can ask.

        ``pause_explains_the_gap`` separates the two causes, and only one of
        them is a defect. A slow decode leaves undecoded *speech* in the gap.
        A pause leaves undecoded *silence*: the gate returns before the decode,
        so `last_window_end` stops advancing while the buffer keeps growing,
        and any pause longer than the window makes the next one disjoint with
        nothing having been slow at all. Warning there stated three false
        things -- that a partial took longer than the window, that this is why
        they no longer overlap, and that speech was lost -- and because the
        warning latches, an ordinary thinking pause consumed it and the real
        RTF-near-1 condition could never be reported again in that session.
        """
        if previous_window_end <= 0:
            return False
        if session.result.last_window_start < previous_window_end:
            return False
        if not pause_explains_the_gap and not session.result.slow_decode_warned:
            session.result.slow_decode_warned = True
            logger.warning(
                "streaming_decode_slower_than_window: a partial took longer "
                "than stream_partial_window_s=%.1f s, so consecutive windows "
                "no longer overlap and the speech between them is lost. "
                "Choose a smaller model or use batch mode.",
                self.stream_partial_window_s,
            )
        return True

    def _pcm_has_enough_speech_to_append(self, pcm: bytes) -> bool:
        """Is there enough speech in ``pcm`` to append it without a seam?

        Unmeasurable audio returns ``None`` from the meter and is refused:
        appending is the risky direction, so "cannot tell" must fall back to
        the safe aligning path.
        """
        if not pcm:
            return False
        speech_seconds = measure_longest_speech_run_s(
            pcm,
            self.stream_sample_rate,
            self.silence_gate_threshold,
            window_ms=STREAMING_SPEECH_RUN_WINDOW_MS,
        )
        if speech_seconds is None:
            return False
        return speech_seconds >= STREAMING_NEW_SEGMENT_MIN_SPEECH_S

    def _stream_window_has_speech(self, session: _StreamingSession) -> bool:
        """Does the window about to be decoded hold enough speech to append?

        Asked *before* the decode, so the trailing window is the one that will
        be decoded a moment later.
        """
        snapshot, _start, _end = self._trailing_window(
            session, self.stream_partial_window_s
        )
        return self._pcm_has_enough_speech_to_append(snapshot)

    def _decoded_window_has_speech(self, session: _StreamingSession) -> bool:
        """The same question about the window that was ALREADY decoded.

        Asked after the decode, which is the only moment the disjointness of
        two windows can be known -- and by then the trailing window is no
        longer the decoded one: the capture thread kept appending for the
        whole decode, which on this path lasted longer than the window itself,
        so `_stream_window_has_speech` would measure audio the model never
        saw. The recorded byte range is exact.
        """
        start = int(session.result.last_window_start)
        end = int(session.result.last_window_end)
        if end <= start:
            return False
        return self._pcm_has_enough_speech_to_append(
            bytes(session.pcm_buffer[start:end])
        )

    def _warn_once_if_the_room_is_never_quiet(
        self, session: _StreamingSession, quiet_slice: bool
    ) -> None:
        """Say so when the noise floor disables the pause machinery entirely.

        Everything here keys off `silence_gate_threshold`. In a room whose
        floor sits above it -- a fan, HVAC, an open window, or a microphone
        boosted by 10-30 dB in Windows -- no slice is ever quiet, so
        `silent_seconds` never accumulates, `new_segment` never fires and the
        pause never closes off a segment. Nothing else surfaces that.

        The condition is ROLLING, not "no quiet slice ever". A latching flag
        was tried and was wrong twice over: every session starts with a
        moment of silence between the hotkey and the first word, which
        disabled the warning for good, and a fan that starts mid-dictation --
        exactly the case worth reporting -- was never reported at all.
        """
        if quiet_slice:
            session.result.loud_since = None
            return
        now = time.monotonic()
        if session.result.loud_since is None:
            session.result.loud_since = now
            return
        if now - session.result.loud_since < _NOISE_FLOOR_WARN_AFTER_S:
            return
        # Reset the window so a long dictation reports at most once per
        # stretch rather than on every partial.
        session.result.loud_since = now
        if session.result.noise_floor_warned:
            return
        session.result.noise_floor_warned = True
        logger.warning(
            "streaming_noise_floor_above_gate: no audio below "
            "silence_gate_threshold=%.4f for %.0f s. If the room really is "
            "this loud, pause detection and the segment protection are "
            "inactive -- raise the threshold in Settings > Audio && Recording "
            "or reduce microphone gain. Continuous speech without a pause "
            "looks the same from here.",
            self.silence_gate_threshold,
            _NOISE_FLOOR_WARN_AFTER_S,
        )

    def _stream_slice_is_quiet(self, pcm_bytes: bytes) -> bool:
        """Is this stretch of stream audio below the speech threshold?

        Deliberately independent of ``silence_gate_enabled``: the caller uses
        the answer both to skip decoding (which the setting controls) and to
        measure how long the pause has been (which it must not).

        Unmeasurable audio returns ``None`` from the meter and is never treated
        as quiet -- refusing to decode something that could not be measured
        would drop real speech.
        """
        level = measure_peak_windowed_rms_pcm(pcm_bytes, self.stream_sample_rate)
        return level is not None and level < self.silence_gate_threshold

    def _stream_audio_is_silent(self, pcm_bytes: bytes) -> bool:
        """Whether the gate should skip decoding this audio."""
        if not self.silence_gate_enabled:
            return False
        return self._stream_slice_is_quiet(pcm_bytes)

    def _stream_tail_window_is_silent(self, session: _StreamingSession) -> bool:
        """Measure exactly the trailing window the finalizer would decode."""
        snapshot, _start, _end = self._trailing_window(
            session, self.stream_partial_window_s
        )
        if not snapshot:
            return False
        return self._stream_audio_is_silent(snapshot)

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
            if not snapshot:
                return ""
            if max_window_seconds is not None and max_window_seconds > 0:
                max_bytes = int(max_window_seconds * self.stream_sample_rate * 2)
                if max_bytes > 0 and len(snapshot) > max_bytes:
                    snapshot = snapshot[-max_bytes:]
            return self.transcribe_batch(self._pcm16_to_wav_bytes(snapshot))
        snapshot, window_start, window_end = self._trailing_window(
            session, max_window_seconds
        )
        if not snapshot:
            return ""
        # Recorded here rather than computed by the caller: the capture thread
        # keeps appending, so a length read before this call gives a window
        # start earlier than the real one -- the direction that reports an
        # overlap where there is none.
        session.result.last_window_start = window_start
        session.result.last_window_end = window_end
        return self.transcribe_batch(self._pcm16_to_wav_bytes(snapshot))

    def _pcm16_to_wav_bytes(self, pcm_bytes: bytes) -> bytes:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self.stream_sample_rate)
            wav_file.writeframes(pcm_bytes)
        return buffer.getvalue()
