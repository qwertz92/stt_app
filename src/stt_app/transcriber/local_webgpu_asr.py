from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import (
    CANARY_MODEL_SIZE,
    DEFAULT_LANGUAGE_MODE,
    DOC_MODELS_PATH,
    LOCAL_ONNX_MODEL_PRECISION,
    LOCAL_ONNX_MODEL_SIZES,
    LOCAL_WEBGPU_DEVICE_POLICIES,
    LOCAL_WEBGPU_MODEL_SIZES,
    MODEL_REPO_MAP,
    MODELS_WITHOUT_MODELSCOPE_MIRROR,
    PARAKEET_MODEL_SIZE,
    language_modes_for_selection,
)
from ..model_download_coordinator import run_coordinated_download
from .base import (
    AudioInput,
    ITranscriber,
    ProgressReporter,
    TranscriptionCanceled,
    TranscriptionError,
    canceled_download_is_a_cancel,
)

logger = logging.getLogger(__name__)

_STDERR_MAX_LINES = 256
_MAX_NON_JSON_MESSAGES = 8


class _RuntimeProtocolError(TranscriptionError):
    """The child protocol is no longer safe to reuse."""


@dataclass(frozen=True)
class _NodeProcessState:
    """Reader state that must never be shared across child generations."""

    process: subprocess.Popen[str]
    stdout_queue: queue.Queue[str]
    stderr_lines: deque[str]
    stderr_lock: threading.Lock = field(
        default_factory=threading.Lock,
        compare=False,
        repr=False,
    )


@dataclass(frozen=True)
class _OnnxModelLayout:
    name: str
    precision: str
    allow_patterns: tuple[str, ...]
    required_files: tuple[str, ...]


_BASE_DOWNLOAD_ALLOW_PATTERNS = (
    ".gitattributes",
    "README.md",
    "chat_template.jinja",
    "config.json",
    "generation_config.json",
    "LICENSE",
    "preprocessor_config.json",
    "processor_config.json",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
)

_Q4_DOWNLOAD_ALLOW_PATTERNS = (
    *_BASE_DOWNLOAD_ALLOW_PATTERNS,
    "onnx/*_q4.onnx",
    "onnx/*_q4.onnx_data",
    "onnx/*_q4.onnx_data_*",
)

_NEMOTRON_INT4_DOWNLOAD_ALLOW_PATTERNS = (
    ".gitattributes",
    "README.md",
    "*.json",
    "*.onnx",
    "*.onnx.data",
)

_COHERE_Q4_REQUIRED_FILES = (
    "config.json",
    "preprocessor_config.json",
    "processor_config.json",
    "tokenizer.json",
    "onnx/encoder_model_q4.onnx",
    "onnx/encoder_model_q4.onnx_data",
    "onnx/decoder_model_merged_q4.onnx",
    "onnx/decoder_model_merged_q4.onnx_data",
)

_GRANITE_4_0_Q4_REQUIRED_FILES = (
    "chat_template.jinja",
    "config.json",
    "preprocessor_config.json",
    "processor_config.json",
    "tokenizer.json",
    "onnx/audio_encoder_q4.onnx",
    "onnx/audio_encoder_q4.onnx_data",
    "onnx/embed_tokens_q4.onnx",
    "onnx/embed_tokens_q4.onnx_data",
    "onnx/decoder_model_merged_q4.onnx",
    "onnx/decoder_model_merged_q4.onnx_data",
)

_NEMOTRON_INT4_REQUIRED_FILES = (
    "genai_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "encoder.onnx",
    "encoder.onnx.data",
    "decoder.onnx",
    "decoder.onnx.data",
    "joint.onnx",
    "joint.onnx.data",
    "silero_vad.onnx",
)

_COHERE_Q4_LAYOUT = _OnnxModelLayout(
    name="cohere_q4",
    precision="q4",
    allow_patterns=_Q4_DOWNLOAD_ALLOW_PATTERNS,
    required_files=_COHERE_Q4_REQUIRED_FILES,
)

_GRANITE_4_0_Q4_LAYOUT = _OnnxModelLayout(
    name="granite_4_0_q4",
    precision="q4",
    allow_patterns=_Q4_DOWNLOAD_ALLOW_PATTERNS,
    required_files=_GRANITE_4_0_Q4_REQUIRED_FILES,
)

# Granite Speech 4.1 2B (AR) ships as a Transformers.js q4 package with the same
# component layout as Granite 4.0, so it reuses the 4.0 q4 required-file set.
_GRANITE_4_1_AR_Q4_LAYOUT = _OnnxModelLayout(
    name="granite_4_1_ar_q4",
    precision="q4",
    allow_patterns=_Q4_DOWNLOAD_ALLOW_PATTERNS,
    required_files=_GRANITE_4_0_Q4_REQUIRED_FILES,
)

# NeMo exports served by the pure-Python onnx-asr runtime. Both repos also ship
# fp32 graphs (2.4 GB / 3.3 GB of `.onnx.data`); the allow-patterns deliberately
# fetch only the int8 tier, which is what the app runs.
_ONNX_ASR_INT8_BASE_ALLOW_PATTERNS = (
    ".gitattributes",
    "README.md",
    "config.json",
    "vocab.txt",
    "*.int8.onnx",
)

_PARAKEET_INT8_LAYOUT = _OnnxModelLayout(
    name="parakeet_tdt_int8",
    precision="int8",
    allow_patterns=(*_ONNX_ASR_INT8_BASE_ALLOW_PATTERNS, "nemo128.onnx"),
    required_files=(
        "config.json",
        "vocab.txt",
        "encoder-model.int8.onnx",
        "decoder_joint-model.int8.onnx",
    ),
)

_CANARY_INT8_LAYOUT = _OnnxModelLayout(
    name="canary_aed_int8",
    precision="int8",
    allow_patterns=_ONNX_ASR_INT8_BASE_ALLOW_PATTERNS,
    required_files=(
        "config.json",
        "vocab.txt",
        "encoder-model.int8.onnx",
        # AED, so a plain decoder — Parakeet's TDT ships a fused decoder_joint.
        "decoder-model.int8.onnx",
    ),
)

_NEMOTRON_INT4_LAYOUT = _OnnxModelLayout(
    name="nemotron_int4",
    precision="int4",
    allow_patterns=_NEMOTRON_INT4_DOWNLOAD_ALLOW_PATTERNS,
    required_files=_NEMOTRON_INT4_REQUIRED_FILES,
)

_MODEL_LAYOUTS: dict[str, _OnnxModelLayout] = {
    "cohere-transcribe-03-2026": _COHERE_Q4_LAYOUT,
    "granite-4.0-1b-speech": _GRANITE_4_0_Q4_LAYOUT,
    "granite-speech-4.1-2b": _GRANITE_4_1_AR_Q4_LAYOUT,
    "nemotron-3.5-asr-streaming-0.6b-int4": _NEMOTRON_INT4_LAYOUT,
    PARAKEET_MODEL_SIZE: _PARAKEET_INT8_LAYOUT,
    CANARY_MODEL_SIZE: _CANARY_INT8_LAYOUT,
}
_REQUIRED_FILES: dict[str, tuple[str, ...]] = {
    model_name: layout.required_files for model_name, layout in _MODEL_LAYOUTS.items()
}

_ACCELERATED_DEVICES = {"webgpu", "dml", "cuda", "gpu", "webnn-gpu"}
_RUNTIME_DEVICE_LABELS = {
    "webgpu": "WebGPU",
    "dml": "DirectML GPU",
    "cuda": "CUDA GPU",
    "gpu": "GPU",
    "webnn-gpu": "WebNN GPU",
    "cpu": "CPU",
}
_DEVICE_POLICY_LABELS = {
    "auto": "Auto (WebGPU -> DirectML -> CPU)",
    "gpu": "GPU only (WebGPU -> DirectML)",
    "webgpu": "WebGPU only",
    "dml": "DirectML only",
    "cpu": "CPU only",
}
_JS_RUNTIME_READY: set[tuple[str, str]] = set()
_JS_RUNTIME_LOCK = threading.Lock()


def _default_hf_cache_dir() -> str:
    hf_home = os.environ.get("HF_HOME", "")
    if hf_home:
        return os.path.join(hf_home, "hub")
    hf_cache = os.environ.get("HF_HUB_CACHE", "")
    if hf_cache:
        return hf_cache
    return os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub")


def _repo_id_for_model(model_name: str) -> str | None:
    return MODEL_REPO_MAP.get(model_name)


def webgpu_download_destination(model_name: str, model_dir: str = "") -> Path | None:
    """Return the exact directory a download for this model writes into.

    Single source of truth for `download_webgpu_model_snapshot` and for download
    progress measurement. These models use a flat `local_dir` layout rather than
    the HuggingFace blob/snapshot cache, so the sibling `models--<repo>` folder
    is *not* the download target even when it exists: an unrelated full-repo copy
    left there (e.g. fp32 weights pulled by a conversion experiment) must never be
    mistaken for this download's progress.
    """
    repo_id = _repo_id_for_model(model_name)
    if repo_id is None or model_name not in _MODEL_LAYOUTS:
        return None
    base_dir = (
        Path(model_dir.strip())
        if model_dir and model_dir.strip()
        else Path(_default_hf_cache_dir())
    )
    return base_dir / repo_id.rsplit("/", 1)[-1]


def _model_cache_dirs(model_name: str, model_dir: str = "") -> list[Path]:
    repo_id = _repo_id_for_model(model_name)
    if repo_id is None:
        return []

    # Both roots, the way the faster-whisper side has always searched --
    # a configured Model Dir *and* the default cache. Only one root meant a
    # model fetched by `scripts/download_model.py` (which writes to the
    # default cache) was invisible to the inventory and to the loader as soon
    # as a Model Dir was set, so the Local tab reported it as missing and the
    # preload downloaded it again. Delete already spanned both, so the app
    # would then remove the copy the scan had never listed.
    #
    # Not used by `webgpu_download_destination`, which stays the single root
    # a download writes into.
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
        for path in (base / folder_name, base / repo_basename):
            if path in seen:
                continue
            seen.add(path)
            dirs.append(path)
    return dirs


def _has_required_files(directory: Path, required_files: tuple[str, ...]) -> bool:
    if not directory.is_dir():
        return False
    return all((directory / relative).is_file() for relative in required_files)


def _valid_snapshot_path(model_name: str, cache_dir: Path) -> Path | None:
    layout = _MODEL_LAYOUTS.get(model_name)
    if layout is None:
        return None

    if _has_required_files(cache_dir, layout.required_files):
        return cache_dir

    snapshots_dir = cache_dir / "snapshots"
    if not snapshots_dir.is_dir():
        return None

    try:
        snapshots = sorted(
            (path for path in snapshots_dir.iterdir() if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None

    for snapshot in snapshots:
        if _has_required_files(snapshot, layout.required_files):
            return snapshot
    return None


def resolve_cached_webgpu_model_root(
    model_name: str, model_dir: str = ""
) -> Path | None:
    """Cache root holding a *complete* snapshot of this model, for sizing.

    Returns the root rather than the snapshot directory because a HuggingFace
    snapshot entry may be a symlink into `blobs/`, which a size sum has to skip.
    Requiring a valid snapshot is what keeps an unrelated copy of the same repo
    out of the measurement. The case that proved it: `convert_granite_nar_q4.py`
    pulled the (now retired) NAR repo's fp32 weights with `cache_dir=`, 9.4 GB
    under `models--<repo>` carrying none of the required files, and sizing that
    made the real download report 100% while it was half done.
    """
    for root in _model_cache_dirs(model_name, model_dir):
        if _valid_snapshot_path(model_name, root) is not None:
            return root
    return None


def resolve_cached_webgpu_model_path(
    model_name: str, model_dir: str = ""
) -> Path | None:
    for root in _model_cache_dirs(model_name, model_dir):
        snapshot = _valid_snapshot_path(model_name, root)
        if snapshot is not None:
            return snapshot
    return None


def find_cached_webgpu_models(model_dir: str = "") -> list[str]:
    found: set[str] = set()
    for model_name in LOCAL_ONNX_MODEL_SIZES:
        if resolve_cached_webgpu_model_path(model_name, model_dir) is not None:
            found.add(model_name)
    return [model_name for model_name in LOCAL_ONNX_MODEL_SIZES if model_name in found]


def download_webgpu_model_snapshot(model_name: str, model_dir: str = "") -> str:
    try:
        from huggingface_hub import snapshot_download  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "huggingface_hub is not installed. Install dependencies and try again."
        ) from exc

    repo_id = _repo_id_for_model(model_name)
    layout = _MODEL_LAYOUTS.get(model_name)
    local_dir = webgpu_download_destination(model_name, model_dir)
    if repo_id is None or layout is None or local_dir is None:
        raise ValueError(f"Unknown local ONNX model '{model_name}'.")

    kwargs: dict[str, object] = {
        "allow_patterns": layout.allow_patterns,
        # Use a real local folder instead of the Hugging Face blob/snapshot
        # cache for these large ONNX models. The normal cache relies on
        # symlinks, which can fail on Windows without Developer Mode/admin
        # privileges (WinError 1314).
        "local_dir": str(local_dir),
        # Parallel *files*, which barely matters for these repos: every ONNX
        # model is dominated by one weight file (Parakeet 652 of 671 MB,
        # Cohere 2016 of 2128 MB), so extra workers have nothing to do.
        # Measured against the Parakeet snapshot on a ~70 Mbit/s line, two runs
        # each: 2 workers 76.7 s / 77.6 s, 8 workers 76.6 s / 76.4 s -- a 0.9 %
        # difference, i.e. noise. Kept at 2 because raising it buys nothing and
        # costs another concurrent writer per download.
        "max_workers": 2,
    }

    try:
        path = str(snapshot_download(repo_id, **kwargs))
    except Exception as hf_error:
        # Hugging Face may be unreachable (e.g. a corporate proxy blocking the
        # whole "Generative AI and ML Applications" category). Fall back to the
        # ModelScope mirror, which hosts the same repo IDs and serves the LFS
        # weights from its own CDN. The flat local_dir layout is identical to
        # what snapshot_download produces, so the app finds it unchanged.
        path = _download_onnx_via_modelscope(
            repo_id, local_dir, layout.allow_patterns, hf_error, model_name
        )
    _verify_downloaded_layout(model_name or repo_id, repo_id, local_dir, layout)
    return path


def _verify_downloaded_layout(
    label: str,
    repo_id: str,
    local_dir: Path,
    layout: _OnnxModelLayout,
) -> None:
    """Refuse to call a download finished while the weights are absent.

    A mirror can carry a repo's metadata without its large files: ModelScope's
    copy of onnx-community/cohere-transcribe-03-2026-ONNX holds the JSON and
    the tokenizer but no ``onnx/`` directory at all. The transfer then reports
    success, the model is left unloadable, and the failure surfaces much later
    as a runtime error that says nothing about the download.
    """
    missing = [
        relative
        for relative in layout.required_files
        if not (local_dir / relative).is_file()
    ]
    if not missing:
        return
    shown = ", ".join(missing[:4])
    if len(missing) > 4:
        shown = f"{shown}, and {len(missing) - 4} more"
    raise RuntimeError(
        f"'{label}' downloaded incompletely: {len(missing)} required file(s) "
        f"are missing ({shown}). The source that answered does not carry the "
        f"weights for '{repo_id}'. Download the model on an unrestricted "
        f"machine and point 'Model Dir' at it. See {DOC_MODELS_PATH}."
    )


def _download_onnx_via_modelscope(
    repo_id: str,
    local_dir: Path,
    allow_patterns: tuple[str, ...],
    hf_error: Exception,
    model_name: str = "",
) -> str:
    from . import modelscope_mirror as ms

    if not ms.modelscope_fallback_enabled() or not ms.repo_available(repo_id):
        if model_name in MODELS_WITHOUT_MODELSCOPE_MIRROR:
            from .local_faster_whisper import format_model_download_error

            raise RuntimeError(
                format_model_download_error(model_name, hf_error)
            ) from hf_error
        raise RuntimeError(
            f"Model download for '{repo_id}' failed: {hf_error}. See {DOC_MODELS_PATH}."
        ) from hf_error

    logger.warning(
        "Hugging Face download failed for %s (%s); trying ModelScope mirror.",
        repo_id,
        hf_error,
    )
    try:
        path = ms.download_repo_to_dir(
            repo_id, local_dir, allow_patterns=allow_patterns
        )
    except Exception as ms_error:
        raise RuntimeError(
            f"Model download for '{repo_id}' failed on Hugging Face ({hf_error}) "
            f"and on the ModelScope mirror ({ms_error})."
        ) from ms_error
    logger.info("Downloaded %s from ModelScope mirror.", repo_id)
    return path


def _default_runner_path() -> Path:
    bundled_root = getattr(sys, "_MEIPASS", "")
    if bundled_root:
        bundled = Path(str(bundled_root)) / "stt_app" / "webgpu_asr_runner.mjs"
        if bundled.is_file():
            return bundled
    return Path(__file__).resolve().parents[1] / "webgpu_asr_runner.mjs"


def _default_node_path() -> str | None:
    # Strip surrounding quotes: a value set via `setx STT_APP_NODE_PATH "..."`
    # can store the literal quotes, which then make subprocess fail with
    # WinError 2 (the quoted string is not a real file path).
    configured = os.environ.get("STT_APP_NODE_PATH", "").strip().strip('"')
    if configured:
        return configured

    for name in ("node", "node.exe"):
        resolved = shutil.which(name)
        if resolved:
            return resolved

    program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    candidate = Path(program_files) / "nodejs" / "node.exe"
    if candidate.is_file():
        return str(candidate)
    return None


def _npm_beside_node(node_path: str | None) -> str | None:
    """Locate npm next to the resolved node executable.

    A portable/unzipped Node.js install (used when the machine-wide MSI is
    blocked by policy and the app is pointed at it via STT_APP_NODE_PATH) ships
    npm in the same directory as node but is not on PATH, so shutil.which finds
    neither. Deriving npm from the node location keeps the auto-install working.
    """
    if not node_path:
        return None
    node_dir = Path(node_path).parent
    for name in ("npm.cmd", "npm"):
        candidate = node_dir / name
        if candidate.is_file():
            return str(candidate)
    return None


def _find_source_package_root(runner: Path) -> Path | None:
    for directory in (runner.parent, *runner.parents):
        if (
            (directory / "package.json").is_file()
            and (directory / "package-lock.json").is_file()
            and (directory / ".git").exists()
        ):
            return directory
    return None


def _run_transformers_import_probe(
    node_path: str,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    """Check the one package `webgpu_asr_runner.mjs` imports.

    It must stay in step with both the runner and `package.json`. The probe
    used to also import `@huggingface/tokenizers` and `onnxruntime-node`,
    which the runner stopped importing when the raw Granite graph paths were
    retired and which `package.json` never declared -- they resolve today only
    because npm hoists them out of `@huggingface/transformers`. Probing for an
    undeclared package makes the repair unreachable: `npm install` installs
    what `package.json` asks for, so if the hoist ever changed, the probe
    would fail, the reinstall would not fix it, and every ONNX dictation would
    end in "run npm install" forever.
    """
    return subprocess.run(
        [
            node_path,
            "--input-type=module",
            "-e",
            "await import('@huggingface/transformers')",
        ],
        cwd=str(cwd),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def _ensure_js_runtime_available(node_path: str, runner: Path) -> None:
    cache_key = (str(Path(node_path)), str(runner.parent))
    with _JS_RUNTIME_LOCK:
        if cache_key in _JS_RUNTIME_READY:
            return

        probe: subprocess.CompletedProcess[str] | None = None
        probe_error = ""
        try:
            probe = _run_transformers_import_probe(node_path, runner.parent)
        except Exception as exc:
            probe_error = str(exc)

        if probe is not None and probe.returncode == 0:
            _JS_RUNTIME_READY.add(cache_key)
            return

        source_root = _find_source_package_root(runner)
        npm_path = (
            shutil.which("npm")
            or shutil.which("npm.cmd")
            or _npm_beside_node(node_path)
        )
        if source_root is not None and npm_path:
            try:
                install = subprocess.run(
                    [npm_path, "install"],
                    cwd=str(source_root),
                    text=True,
                    capture_output=True,
                    timeout=300,
                    check=False,
                )
            except Exception as exc:
                probe_error = str(exc)
            else:
                if install.returncode == 0:
                    try:
                        probe = _run_transformers_import_probe(node_path, runner.parent)
                    except Exception as exc:
                        probe_error = str(exc)
                    else:
                        if probe.returncode == 0:
                            _JS_RUNTIME_READY.add(cache_key)
                            return
                elif install.stderr or install.stdout:
                    probe_error = (install.stderr or install.stdout).strip()

        detail = probe_error or (
            (probe.stderr or probe.stdout or "").strip() if probe is not None else ""
        )
        install_hint = (
            "The app tried to install the JavaScript runtime automatically, but "
            "the import still failed."
            if source_root is not None and npm_path
            else "Install Node.js and run npm install, or use the packaged app with bundled JavaScript dependencies."
        )
        raise TranscriptionError(
            "The ONNX JavaScript runtime is not available. "
            f"{install_hint}" + (f"\n{detail}" if detail else "")
        )


class LocalOnnxWebGpuTranscriber(ProgressReporter, ITranscriber):
    """Selectable local ONNX ASR through a persistent Transformers.js process."""

    def __init__(
        self,
        model_size: str,
        language_mode: str = DEFAULT_LANGUAGE_MODE,
        device: str = "auto",
        dtype: str = "",
        offline_mode: bool = False,
        model_dir: str = "",
        node_path: str | None = None,
        runner_path: str | Path | None = None,
        startup_timeout_s: float = 180.0,
        request_timeout_s: float = 600.0,
    ) -> None:
        device = str(device or "auto").strip().lower()
        if model_size not in LOCAL_WEBGPU_MODEL_SIZES:
            raise ValueError(f"Unsupported ONNX/WebGPU model '{model_size}'.")
        if device not in LOCAL_WEBGPU_DEVICE_POLICIES:
            raise ValueError(
                "Unsupported ONNX/WebGPU device policy "
                f"'{device}'. Use one of: {', '.join(LOCAL_WEBGPU_DEVICE_POLICIES)}."
            )
        ProgressReporter.__init__(self)
        self.model_size = model_size
        # Needs self.model_size, so this must run after it is assigned above.
        self.set_language_mode(language_mode)
        self.device = device
        self.dtype = str(dtype or LOCAL_ONNX_MODEL_PRECISION.get(model_size) or "q4")
        self.offline_mode = offline_mode
        self.model_dir = (model_dir or "").strip()
        self.node_path = node_path
        self.runner_path = Path(runner_path) if runner_path is not None else None
        self.startup_timeout_s = max(1.0, float(startup_timeout_s))
        self.request_timeout_s = max(1.0, float(request_timeout_s))

        # Serializes process lifecycle and stdin requests. ``close`` can be
        # called by another thread, so protecting writes only in
        # ``transcribe_batch`` is insufficient.
        self._lock = threading.RLock()
        self._process_state: _NodeProcessState | None = None
        self._request_id = 0
        self._runtime_device = ""
        self._gpu_available = False
        self._runtime_fallback_details: list[str] = []
        self.runtime_warning = ""

    @property
    def runtime_device(self) -> str:
        return self._runtime_device

    @property
    def gpu_available(self) -> bool:
        return self._gpu_available

    @property
    def runtime_details_text(self) -> str:
        if not self._runtime_fallback_details:
            return ""
        return "Fallback attempts: " + "; ".join(self._runtime_fallback_details)

    def runtime_status_text(self) -> str:
        if not self._runtime_device:
            policy = _DEVICE_POLICY_LABELS.get(self.device, self.device)
            return f"ONNX runtime not loaded yet. Device policy: {policy}."
        label = _RUNTIME_DEVICE_LABELS.get(self._runtime_device, self._runtime_device)
        if self._runtime_device in _ACCELERATED_DEVICES:
            return f"ONNX runtime active on {label}."
        if self.device == "cpu":
            return "ONNX runtime active on CPU (selected device policy)."
        return (
            "ONNX runtime active on CPU. WebGPU/DirectML GPU fallback was not "
            "available or did not load."
        )

    def _normalize_language_mode(self, mode: str) -> str:
        normalized = (mode or DEFAULT_LANGUAGE_MODE).strip().lower()
        supported_modes = language_modes_for_selection("local", self.model_size)
        if normalized not in supported_modes:
            normalized = DEFAULT_LANGUAGE_MODE
        return normalized

    def _language_arg(self) -> str:
        if self._language_mode != DEFAULT_LANGUAGE_MODE:
            return self._language_mode
        if self.model_size != "cohere-transcribe-03-2026":
            return ""
        # Cohere requires an explicit language. German is the safer default for
        # this app's primary user workflow when Auto reaches this provider.
        return "de"

    def _ensure_snapshot(self) -> Path:
        snapshot = resolve_cached_webgpu_model_path(self.model_size, self.model_dir)
        if snapshot is not None:
            return snapshot
        if self.offline_mode:
            raise TranscriptionError(
                f"ONNX/WebGPU model '{self.model_size}' is not cached locally. "
                f"Disable Offline mode or download it first. See {DOC_MODELS_PATH}."
            )
        try:
            # Through the single slot: a cache miss here is a real download and
            # must not race the preload path or the Local tab's queue.
            with canceled_download_is_a_cancel():
                run_coordinated_download(
                    self.model_size,
                    self.model_dir,
                    lambda: download_webgpu_model_snapshot(
                        self.model_size, self.model_dir
                    ),
                    # `_is_cancel_requested`, not the raw attribute: a check that
                    # raises must never fail the work, and the coordinator re-raises
                    # whatever escapes it.
                    cancel_check=self._is_cancel_requested,
                )
        except TranscriptionCanceled:
            raise
        except Exception as exc:
            raise TranscriptionError(
                f"Failed to download ONNX/WebGPU model '{self.model_size}': {exc}"
            ) from exc
        snapshot = resolve_cached_webgpu_model_path(self.model_size, self.model_dir)
        if snapshot is None:
            layout = _MODEL_LAYOUTS.get(self.model_size)
            precision = layout.precision if layout is not None else "required"
            raise TranscriptionError(
                f"Downloaded '{self.model_size}', but no complete {precision} "
                "ONNX snapshot "
                "was found."
            )
        return snapshot

    def _set_runtime_status(
        self,
        device: object,
        gpu_available: object,
        fallback_details: object = None,
    ) -> None:
        self._runtime_device = str(device or "")
        self._gpu_available = bool(gpu_available)
        if isinstance(fallback_details, list):
            self._runtime_fallback_details = [
                str(detail).strip()
                for detail in fallback_details
                if str(detail).strip()
            ]
        if self._runtime_device not in _ACCELERATED_DEVICES:
            if self.device == "cpu":
                self.runtime_warning = (
                    "The CPU device policy is selected. This model may be much "
                    "slower than the CTranslate2 Whisper models."
                )
            else:
                self.runtime_warning = (
                    "No WebGPU or DirectML GPU runtime was selected. This model is "
                    "running on CPU and may be much slower than the CTranslate2 "
                    "Whisper models."
                )
            if self.runtime_details_text:
                self.runtime_warning = (
                    f"{self.runtime_warning} {self.runtime_details_text}"
                )
        else:
            self.runtime_warning = ""

    def _should_restart_after_cpu_fallback(self) -> bool:
        return (
            self.device in {"auto", "gpu"}
            and self._runtime_device == "cpu"
            and bool(self._runtime_fallback_details)
        )

    def _node_executable(self) -> str:
        node_path = self.node_path or _default_node_path()
        if not node_path:
            raise TranscriptionError(
                "Node.js is required for Cohere/Granite ONNX/WebGPU local models. "
                "Install Node.js 22+ or set STT_APP_NODE_PATH to node.exe."
            )
        return node_path

    def _runner_file(self) -> Path:
        runner = self.runner_path or _default_runner_path()
        if not runner.is_file():
            raise TranscriptionError(f"ONNX/WebGPU runner not found: {runner}")
        return runner

    def _start_reader_threads(self, state: _NodeProcessState) -> None:
        process = state.process

        def _read_stdout() -> None:
            if process.stdout is None:
                return
            for line in process.stdout:
                state.stdout_queue.put(line.rstrip("\r\n"))

        def _read_stderr() -> None:
            if process.stderr is None:
                return
            for line in process.stderr:
                stripped = line.rstrip("\r\n")
                if stripped:
                    with state.stderr_lock:
                        state.stderr_lines.append(stripped)

        threading.Thread(
            target=_read_stdout,
            name="stt_app_webgpu_asr_stdout",
            daemon=True,
        ).start()
        threading.Thread(
            target=_read_stderr,
            name="stt_app_webgpu_asr_stderr",
            daemon=True,
        ).start()

    def _stderr_tail(self, state: _NodeProcessState | None = None) -> str:
        state = state or self._process_state
        if state is None:
            return ""
        # ``deque`` has no slice support; take the last 12 via list().
        with state.stderr_lock:
            return "\n".join(list(state.stderr_lines)[-12:]).strip()

    def _read_json_message(
        self,
        state: _NodeProcessState,
        deadline: float,
    ) -> dict[str, Any]:
        skipped: list[str] = []
        while True:
            # The child owns a whole Node process; without this poll Cancel
            # could not stop a running transcription, and the runtime kept a
            # core busy and its model in memory until the request finished.
            self._raise_if_canceled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                detail = self._stderr_tail(state)
                if skipped:
                    detail = (
                        f"{detail}\nNon-JSON output: {' | '.join(skipped[-3:])}".strip()
                    )
                raise _RuntimeProtocolError(
                    "Timed out waiting for ONNX/WebGPU runtime response."
                    + (f"\n{detail}" if detail else "")
                )
            try:
                line = state.stdout_queue.get(timeout=min(0.25, remaining))
            except queue.Empty:
                if state.process.poll() is not None:
                    detail = self._stderr_tail(state)
                    raise _RuntimeProtocolError(
                        "ONNX/WebGPU runtime exited unexpectedly."
                        + (f"\n{detail}" if detail else "")
                    ) from None
                continue
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                skipped.append(line)
                if len(skipped) >= _MAX_NON_JSON_MESSAGES:
                    detail = " | ".join(skipped[-3:])
                    raise _RuntimeProtocolError(
                        "ONNX/WebGPU runtime protocol was poisoned by repeated "
                        f"non-JSON output: {detail}"
                    ) from None
                continue
            if isinstance(payload, dict):
                return payload
            skipped.append(line)
            if len(skipped) >= _MAX_NON_JSON_MESSAGES:
                raise _RuntimeProtocolError(
                    "ONNX/WebGPU runtime protocol returned repeated non-object messages."
                )

    def _start_process(self) -> None:
        snapshot = self._ensure_snapshot()
        node_path = self._node_executable()
        runner = self._runner_file()
        _ensure_js_runtime_available(node_path, runner)
        policy = _DEVICE_POLICY_LABELS.get(self.device, self.device)
        self._emit_progress(f"Starting ONNX runtime for {self.model_size}: {policy}.")
        command = [
            node_path,
            str(runner),
            "--server",
            "--model",
            self.model_size,
            "--model-path",
            str(snapshot),
            "--device",
            self.device,
            "--dtype",
            self.dtype,
        ]
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except Exception as exc:
            raise TranscriptionError(
                f"Failed to start ONNX/WebGPU runtime: {exc}"
            ) from exc

        state = _NodeProcessState(
            process=process,
            stdout_queue=queue.Queue(maxsize=128),
            stderr_lines=deque(maxlen=_STDERR_MAX_LINES),
        )
        self._process_state = state
        self._start_reader_threads(state)

        try:
            ready = self._read_json_message(
                state,
                time.monotonic() + self.startup_timeout_s,
            )
        except Exception:
            self._discard_process_locked(state)
            raise
        if not bool(ready.get("ok")):
            detail = str(ready.get("error") or self._stderr_tail(state))
            self._discard_process_locked(state)
            raise TranscriptionError(f"ONNX/WebGPU runtime failed to load: {detail}")

        self._set_runtime_status(
            ready.get("device"),
            ready.get("gpuAvailable"),
            ready.get("fallbackErrors"),
        )
        self._emit_progress(self.runtime_status_text())

    def _ensure_process(self) -> None:
        state = self._process_state
        if state is not None and state.process.poll() is None:
            return
        if state is not None:
            self._discard_process_locked(state)
        self._start_process()

    def preload_model(self) -> None:
        with self._lock:
            self._ensure_process()

    @property
    def is_model_loaded(self) -> bool:
        state = self._process_state
        return state is not None and state.process.poll() is None

    def transcribe_batch(self, audio_source: AudioInput) -> str:
        temp_path: Path | None = None
        restart_after_cpu_fallback = False
        self._raise_if_canceled()
        try:
            if isinstance(audio_source, bytes):
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
                    # The path is claimed before the write, not after: the
                    # file already exists once `NamedTemporaryFile` returns,
                    # so a write that fails (a full disk, a quota) left
                    # `temp_path` None and the cleanup below skipped a real
                    # file -- once per failed dictation, in %TEMP%, forever.
                    temp_path = Path(handle.name)
                    handle.write(audio_source)
                audio_path = temp_path
            else:
                audio_path = Path(audio_source)

            with self._lock:
                self._ensure_process()
                state = self._process_state
                if state is None or state.process.stdin is None:
                    raise TranscriptionError("ONNX/WebGPU runtime is not available.")
                process = state.process
                self._emit_progress(f"Transcribing with {self.runtime_status_text()}")

                self._request_id += 1
                request_id = self._request_id
                request = {
                    "id": request_id,
                    "command": "transcribe",
                    "audioPath": str(audio_path),
                    "language": self._language_arg(),
                    "maxNewTokens": 1024,
                }
                try:
                    process.stdin.write(json.dumps(request) + "\n")
                    process.stdin.flush()
                except Exception as exc:
                    self._discard_process_locked(state)
                    raise TranscriptionError(
                        f"ONNX/WebGPU runtime request write failed: {exc}"
                    ) from exc

                deadline = time.monotonic() + self.request_timeout_s
                try:
                    response = self._read_json_message(state, deadline)
                    if response.get("id") != request_id:
                        raise _RuntimeProtocolError(
                            "ONNX/WebGPU runtime returned an unexpected response id "
                            f"({response.get('id')!r}, expected {request_id})."
                        )
                except (_RuntimeProtocolError, TranscriptionCanceled):
                    # The child is still working on this request and will write
                    # its response later, so the stream cannot be reused. Killing
                    # it is also what actually frees the CPU and the loaded
                    # model, which is the point of pressing Cancel.
                    self._discard_process_locked(state)
                    raise

                if not bool(response.get("ok")):
                    raise TranscriptionError(
                        "ONNX/WebGPU transcription failed: "
                        f"{response.get('error') or self._stderr_tail(state)}"
                    )
                previous_device = self._runtime_device
                self._set_runtime_status(
                    response.get("device") or self._runtime_device,
                    response.get("gpuAvailable", self._gpu_available),
                    response.get("fallbackErrors"),
                )
                if self._runtime_device != previous_device:
                    self._emit_progress(self.runtime_status_text())
                restart_after_cpu_fallback = self._should_restart_after_cpu_fallback()
                if restart_after_cpu_fallback:
                    self._emit_progress(
                        "ONNX runtime fell back to CPU; restarting before "
                        "the next request so WebGPU/DirectML can be retried."
                    )
                return str(response.get("text") or "").strip()
        except (TranscriptionError, TranscriptionCanceled):
            raise
        except Exception as exc:
            raise TranscriptionError(
                f"Local ONNX/WebGPU transcription failed: {exc}"
            ) from exc
        finally:
            if restart_after_cpu_fallback:
                self.close()
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def close(self) -> None:
        with self._lock:
            state = self._process_state
            if state is None:
                return
            self._process_state = None
            self._terminate_process(state, graceful=True)

    def _discard_process_locked(self, state: _NodeProcessState) -> None:
        """Kill a process whose request/response stream cannot be trusted."""
        if self._process_state is state:
            self._process_state = None
        self._terminate_process(state, graceful=False)

    @staticmethod
    def _terminate_process(state: _NodeProcessState, *, graceful: bool) -> None:
        process = state.process
        if process.poll() is not None:
            return
        if graceful:
            try:
                if process.stdin is not None:
                    process.stdin.write(json.dumps({"command": "shutdown"}) + "\n")
                    process.stdin.flush()
            except Exception:
                pass
        try:
            if graceful:
                process.wait(timeout=2.0)
            else:
                process.terminate()
                process.wait(timeout=2.0)
            return
        except Exception:
            pass
        try:
            process.kill()
            process.wait(timeout=2.0)
        except Exception:
            pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
