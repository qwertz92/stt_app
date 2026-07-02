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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import (
    DEFAULT_LANGUAGE_MODE,
    DOC_MODELS_PATH,
    LOCAL_ONNX_MODEL_PRECISION,
    LOCAL_ONNX_MODEL_SIZES,
    LOCAL_WEBGPU_DEVICE_POLICIES,
    LOCAL_WEBGPU_MODEL_SIZES,
    MODEL_REPO_MAP,
    language_modes_for_selection,
)
from .base import AudioInput, ITranscriber, ProgressReporter, TranscriptionError

logger = logging.getLogger(__name__)

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

_GRANITE_4_1_INT8_BASE_DOWNLOAD_ALLOW_PATTERNS = (
    ".gitattributes",
    "README.md",
    "LICENSE",
    "granite_export_metadata.json",
    "preprocessor_config.json",
    "processor_config.json",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "test_fixtures/*",
    "int8/*.onnx",
    "int8/*.onnx_data",
)

_GRANITE_4_1_AR_INT8_DOWNLOAD_ALLOW_PATTERNS = (
    *_GRANITE_4_1_INT8_BASE_DOWNLOAD_ALLOW_PATTERNS,
    "chat_template.jinja",
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

_GRANITE_4_1_AR_INT8_REQUIRED_FILES = (
    "chat_template.jinja",
    "granite_export_metadata.json",
    "preprocessor_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "int8/encoder.onnx",
    "int8/encoder.onnx_data",
    "int8/embed_tokens.onnx",
    "int8/embed_tokens.onnx_data",
    "int8/prompt_encode.onnx",
    "int8/prompt_encode.onnx_data",
    "int8/decode_step.onnx",
    "int8/decode_step.onnx_data",
)

_GRANITE_4_1_NAR_INT8_REQUIRED_FILES = (
    "granite_export_metadata.json",
    "preprocessor_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "int8/encoder.onnx",
    "int8/encoder.onnx_data",
    "int8/embed_tokens.onnx",
    "int8/embed_tokens.onnx_data",
    "int8/editor.onnx",
    "int8/editor.onnx_data",
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

_GRANITE_4_1_AR_INT8_LAYOUT = _OnnxModelLayout(
    name="granite_4_1_ar_int8",
    precision="int8",
    allow_patterns=_GRANITE_4_1_AR_INT8_DOWNLOAD_ALLOW_PATTERNS,
    required_files=_GRANITE_4_1_AR_INT8_REQUIRED_FILES,
)

_GRANITE_4_1_NAR_INT8_LAYOUT = _OnnxModelLayout(
    name="granite_4_1_nar_int8",
    precision="int8",
    allow_patterns=_GRANITE_4_1_INT8_BASE_DOWNLOAD_ALLOW_PATTERNS,
    required_files=_GRANITE_4_1_NAR_INT8_REQUIRED_FILES,
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
    "granite-speech-4.1-2b-plus": _GRANITE_4_1_AR_INT8_LAYOUT,
    "granite-speech-4.1-2b-nar": _GRANITE_4_1_NAR_INT8_LAYOUT,
    "nemotron-3.5-asr-streaming-0.6b-int4": _NEMOTRON_INT4_LAYOUT,
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


def _model_cache_dirs(model_name: str, model_dir: str = "") -> list[Path]:
    repo_id = _repo_id_for_model(model_name)
    if repo_id is None:
        return []

    if model_dir and model_dir.strip():
        search_dirs = [model_dir.strip()]
    else:
        search_dirs = [_default_hf_cache_dir()]

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


def resolve_cached_webgpu_model_path(model_name: str, model_dir: str = "") -> Path | None:
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
    if repo_id is None or layout is None:
        raise ValueError(f"Unknown local ONNX model '{model_name}'.")

    base_dir = (
        Path(model_dir.strip())
        if model_dir and model_dir.strip()
        else Path(_default_hf_cache_dir())
    )
    repo_basename = repo_id.rsplit("/", 1)[-1]
    local_dir = base_dir / repo_basename

    kwargs: dict[str, object] = {
        "allow_patterns": layout.allow_patterns,
        # Use a real local folder instead of the Hugging Face blob/snapshot
        # cache for these large ONNX models. The normal cache relies on
        # symlinks, which can fail on Windows without Developer Mode/admin
        # privileges (WinError 1314).
        "local_dir": str(local_dir),
        "max_workers": 2,
    }

    try:
        return str(snapshot_download(repo_id, **kwargs))
    except Exception as hf_error:
        # Hugging Face may be unreachable (e.g. a corporate proxy blocking the
        # whole "Generative AI and ML Applications" category). Fall back to the
        # ModelScope mirror, which hosts the same repo IDs and serves the LFS
        # weights from its own CDN. The flat local_dir layout is identical to
        # what snapshot_download produces, so the app finds it unchanged.
        return _download_onnx_via_modelscope(
            repo_id, local_dir, layout.allow_patterns, hf_error
        )


def _download_onnx_via_modelscope(
    repo_id: str,
    local_dir: Path,
    allow_patterns: tuple[str, ...],
    hf_error: Exception,
) -> str:
    from . import modelscope_mirror as ms

    if not ms.modelscope_fallback_enabled() or not ms.repo_available(repo_id):
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
    configured = os.environ.get("STT_APP_NODE_PATH", "").strip()
    if configured:
        return configured

    for name in ("node", "node.exe"):
        resolved = shutil.which(name)
        if resolved:
            return resolved

    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
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
    return subprocess.run(
        [
            node_path,
            "--input-type=module",
            "-e",
            (
                "await import('@huggingface/transformers'); "
                "await import('@huggingface/tokenizers'); "
                "await import('onnxruntime-node')"
            ),
        ],
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
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
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
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

        detail = (
            probe_error
            or ((probe.stderr or probe.stdout or "").strip() if probe is not None else "")
        )
        install_hint = (
            "The app tried to install the JavaScript runtime automatically, but "
            "the import still failed."
            if source_root is not None and npm_path
            else "Install Node.js and run npm install, or use the packaged app with bundled JavaScript dependencies."
        )
        raise TranscriptionError(
            "The ONNX JavaScript runtime is not available. "
            f"{install_hint}"
            + (f"\n{detail}" if detail else "")
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
        self.language_mode = language_mode
        self.device = device
        self.dtype = str(dtype or LOCAL_ONNX_MODEL_PRECISION.get(model_size) or "q4")
        self.offline_mode = offline_mode
        self.model_dir = (model_dir or "").strip()
        self.node_path = node_path
        self.runner_path = Path(runner_path) if runner_path is not None else None
        self.startup_timeout_s = max(1.0, float(startup_timeout_s))
        self.request_timeout_s = max(1.0, float(request_timeout_s))

        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._stdout_queue: queue.Queue[str] = queue.Queue()
        # Cap retained stderr to avoid unbounded memory growth in a
        # long-running tray app with a chatty Node.js runtime. Only the last
        # few lines are ever consumed (``_stderr_tail``).
        self._stderr_lines: deque[str] = deque(maxlen=256)
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
        return (
            "ONNX runtime active on CPU. WebGPU/DirectML GPU fallback was not "
            "available or did not load."
        )

    def _language_arg(self) -> str:
        mode = (self.language_mode or DEFAULT_LANGUAGE_MODE).strip().lower()
        supported_modes = language_modes_for_selection("local", self.model_size)
        if mode in supported_modes and mode != DEFAULT_LANGUAGE_MODE:
            return mode
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
            download_webgpu_model_snapshot(self.model_size, self.model_dir)
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

    def _start_reader_threads(self, process: subprocess.Popen[str]) -> None:
        def _read_stdout() -> None:
            if process.stdout is None:
                return
            for line in process.stdout:
                self._stdout_queue.put(line.rstrip("\r\n"))

        def _read_stderr() -> None:
            if process.stderr is None:
                return
            for line in process.stderr:
                stripped = line.rstrip("\r\n")
                if stripped:
                    self._stderr_lines.append(stripped)

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

    def _stderr_tail(self) -> str:
        # ``deque`` has no slice support; take the last 12 via list().
        return "\n".join(list(self._stderr_lines)[-12:]).strip()

    def _read_json_message(self, timeout_s: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        skipped: list[str] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                detail = self._stderr_tail()
                if skipped:
                    detail = f"{detail}\nNon-JSON output: {' | '.join(skipped[-3:])}".strip()
                raise TranscriptionError(
                    "Timed out waiting for ONNX/WebGPU runtime response."
                    + (f"\n{detail}" if detail else "")
                )
            try:
                line = self._stdout_queue.get(timeout=min(0.25, remaining))
            except queue.Empty:
                if self._process is not None and self._process.poll() is not None:
                    detail = self._stderr_tail()
                    raise TranscriptionError(
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
                continue
            if isinstance(payload, dict):
                return payload
            skipped.append(line)

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
            raise TranscriptionError(f"Failed to start ONNX/WebGPU runtime: {exc}") from exc

        self._process = process
        self._stdout_queue = queue.Queue()
        self._stderr_lines = []
        self._start_reader_threads(process)

        try:
            ready = self._read_json_message(self.startup_timeout_s)
        except Exception:
            self.close()
            raise
        if not bool(ready.get("ok")):
            detail = str(ready.get("error") or self._stderr_tail())
            self.close()
            raise TranscriptionError(f"ONNX/WebGPU runtime failed to load: {detail}")

        self._set_runtime_status(
            ready.get("device"),
            ready.get("gpuAvailable"),
            ready.get("fallbackErrors"),
        )
        self._emit_progress(self.runtime_status_text())

    def _ensure_process(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        self.close()
        self._start_process()

    def preload_model(self) -> None:
        with self._lock:
            self._ensure_process()

    @property
    def is_model_loaded(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def transcribe_batch(self, audio_source: AudioInput) -> str:
        temp_path: Path | None = None
        restart_after_cpu_fallback = False
        try:
            if isinstance(audio_source, bytes):
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
                    handle.write(audio_source)
                    temp_path = Path(handle.name)
                audio_path = temp_path
            else:
                audio_path = Path(audio_source)

            with self._lock:
                self._ensure_process()
                process = self._process
                if process is None or process.stdin is None:
                    raise TranscriptionError("ONNX/WebGPU runtime is not available.")
                self._emit_progress(
                    f"Transcribing with {self.runtime_status_text()}"
                )

                self._request_id += 1
                request_id = self._request_id
                request = {
                    "id": request_id,
                    "command": "transcribe",
                    "audioPath": str(audio_path),
                    "language": self._language_arg(),
                    "maxNewTokens": 1024,
                }
                process.stdin.write(json.dumps(request) + "\n")
                process.stdin.flush()

                while True:
                    response = self._read_json_message(self.request_timeout_s)
                    if response.get("id") != request_id:
                        continue
                    if not bool(response.get("ok")):
                        raise TranscriptionError(
                            "ONNX/WebGPU transcription failed: "
                            f"{response.get('error') or self._stderr_tail()}"
                        )
                    previous_device = self._runtime_device
                    self._set_runtime_status(
                        response.get("device") or self._runtime_device,
                        response.get("gpuAvailable", self._gpu_available),
                        response.get("fallbackErrors"),
                    )
                    if self._runtime_device != previous_device:
                        self._emit_progress(self.runtime_status_text())
                    restart_after_cpu_fallback = (
                        self._should_restart_after_cpu_fallback()
                    )
                    if restart_after_cpu_fallback:
                        self._emit_progress(
                            "ONNX runtime fell back to CPU; restarting before "
                            "the next request so WebGPU/DirectML can be retried."
                        )
                    return str(response.get("text") or "").strip()
        except TranscriptionError:
            raise
        except Exception as exc:
            raise TranscriptionError(f"Local ONNX/WebGPU transcription failed: {exc}") from exc
        finally:
            if restart_after_cpu_fallback:
                self.close()
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is None:
            try:
                if process.stdin is not None:
                    process.stdin.write(json.dumps({"command": "shutdown"}) + "\n")
                    process.stdin.flush()
            except Exception:
                pass
            try:
                process.wait(timeout=2.0)
            except Exception:
                try:
                    process.terminate()
                    process.wait(timeout=2.0)
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
