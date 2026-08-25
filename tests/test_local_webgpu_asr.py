from __future__ import annotations

import io
import json
import queue
import shutil
import subprocess
import sys
import time
import wave
from collections import deque
from fnmatch import fnmatchcase
from pathlib import Path
from types import SimpleNamespace

import pytest

from stt_app.config import (
    GRANITE_4_1_MODEL_SIZES,
    LOCAL_NEMOTRON_MODEL_SIZES,
    LOCAL_ONNX_MODEL_PRECISION,
    LOCAL_ONNX_MODEL_SIZES,
    LOCAL_WEBGPU_MODEL_SIZES,
    MODEL_REPO_MAP,
)
from stt_app.transcriber import local_webgpu_asr
from stt_app.transcriber.base import TranscriptionCanceled, TranscriptionError
from stt_app.transcriber.local_webgpu_asr import (
    LocalOnnxWebGpuTranscriber,
    download_webgpu_model_snapshot,
    find_cached_webgpu_models,
    resolve_cached_webgpu_model_path,
)


def _materialise_required_files(
    repo_id: str,
    local_dir: str,
    allow_patterns: tuple[str, ...] | list[str] | None = None,
) -> None:
    """Give a faked download the files a real one would leave behind.

    ``download_webgpu_model_snapshot`` refuses to report success when the
    weights are absent, because a mirror can serve a repo's metadata without
    its large files and used to leave an unloadable model behind. A stub that
    writes nothing is that same case, so these tests would otherwise assert
    against a failure.

    Only files matching ``allow_patterns`` are written, because that is all a
    real ``snapshot_download`` would fetch. A required file no pattern selects
    is a genuine defect, and the fake has to be able to expose it rather than
    paper over it.
    """
    model_name = next(
        (name for name, repo in MODEL_REPO_MAP.items() if repo == repo_id),
        "",
    )
    required = local_webgpu_asr._REQUIRED_FILES.get(model_name)
    if not required:
        return
    root = Path(local_dir)
    for relative in required:
        if allow_patterns and not any(
            fnmatchcase(relative, pattern) for pattern in allow_patterns
        ):
            continue
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")


def _write_required_snapshot(base: Path, model_name: str, snapshot_id: str = "abc123"):
    repo_id = local_webgpu_asr._repo_id_for_model(model_name)
    assert repo_id is not None
    snapshot = (
        base / f"models--{repo_id.replace('/', '--')}" / "snapshots" / snapshot_id
    )
    for relative in local_webgpu_asr._REQUIRED_FILES[model_name]:
        path = snapshot / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    return snapshot


class _FakeProcess:
    def __init__(self):
        self.stdin = io.StringIO()
        self.stdout = None
        self.stderr = None
        self.wait_calls = 0
        self.terminated = False
        self.killed = False
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.wait_calls += 1
        self.returncode = 0
        return 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def test_find_cached_webgpu_models_detects_complete_q4_snapshots(tmp_path):
    snapshot = _write_required_snapshot(tmp_path, "cohere-transcribe-03-2026")

    assert (
        resolve_cached_webgpu_model_path(
            "cohere-transcribe-03-2026",
            str(tmp_path),
        )
        == snapshot
    )
    assert find_cached_webgpu_models(str(tmp_path)) == ["cohere-transcribe-03-2026"]


def test_selectable_webgpu_models_use_granite_4_1_2b_q4_and_keep_4_0_q4():
    assert "granite-4.0-1b-speech" in LOCAL_WEBGPU_MODEL_SIZES
    assert "granite-4.0-1b-speech" in MODEL_REPO_MAP
    assert LOCAL_ONNX_MODEL_PRECISION["granite-4.0-1b-speech"] == "q4"
    for model_name in GRANITE_4_1_MODEL_SIZES:
        assert model_name in LOCAL_WEBGPU_MODEL_SIZES
        assert model_name in MODEL_REPO_MAP
    # Granite 4.1 2B now ships as a q4 Transformers.js package on the pipeline
    # path; Plus and NAR stay on the raw INT8 graph tier until a verified q4
    # package exists for them.
    assert LOCAL_ONNX_MODEL_PRECISION["granite-speech-4.1-2b"] == "q4"
    assert LOCAL_ONNX_MODEL_PRECISION["granite-speech-4.1-2b-plus"] == "int8"
    assert LOCAL_ONNX_MODEL_PRECISION["granite-speech-4.1-2b-nar"] == "int8"
    assert MODEL_REPO_MAP["granite-speech-4.1-2b"] == (
        "onnx-community/granite-speech-4.1-2b-ONNX"
    )


def test_selectable_local_onnx_models_include_nemotron_int4():
    model_name = "nemotron-3.5-asr-streaming-0.6b-int4"

    assert model_name in LOCAL_NEMOTRON_MODEL_SIZES
    assert model_name in LOCAL_ONNX_MODEL_SIZES
    assert model_name in MODEL_REPO_MAP
    assert LOCAL_ONNX_MODEL_PRECISION[model_name] == "int4"


def test_nemotron_snapshot_is_discovered_by_shared_onnx_inventory(tmp_path):
    model_name = "nemotron-3.5-asr-streaming-0.6b-int4"
    snapshot = _write_required_snapshot(tmp_path, model_name)

    assert resolve_cached_webgpu_model_path(model_name, str(tmp_path)) == snapshot
    assert find_cached_webgpu_models(str(tmp_path)) == [model_name]


def test_download_nemotron_snapshot_uses_root_int4_graph_patterns(
    monkeypatch,
    tmp_path,
):
    calls = []

    def fake_snapshot_download(repo_id, **kwargs):
        calls.append((repo_id, kwargs))
        _materialise_required_files(
            repo_id, kwargs["local_dir"], kwargs.get("allow_patterns")
        )
        return str(tmp_path / "snapshot")

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(snapshot_download=fake_snapshot_download),
    )

    download_webgpu_model_snapshot(
        "nemotron-3.5-asr-streaming-0.6b-int4",
        str(tmp_path),
    )

    _repo_id, kwargs = calls[0]
    assert "*.onnx" in kwargs["allow_patterns"]
    assert "*.onnx.data" in kwargs["allow_patterns"]
    assert "*.json" in kwargs["allow_patterns"]


def test_download_webgpu_model_snapshot_uses_q4_allow_patterns(monkeypatch, tmp_path):
    calls = []

    def fake_snapshot_download(repo_id, **kwargs):
        calls.append((repo_id, kwargs))
        _materialise_required_files(
            repo_id, kwargs["local_dir"], kwargs.get("allow_patterns")
        )
        return str(tmp_path / "snapshot")

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(snapshot_download=fake_snapshot_download),
    )

    result = download_webgpu_model_snapshot(
        "granite-4.0-1b-speech",
        str(tmp_path),
    )

    assert result == str(tmp_path / "snapshot")
    repo_id, kwargs = calls[0]
    assert repo_id == MODEL_REPO_MAP["granite-4.0-1b-speech"]
    assert kwargs["local_dir"] == str(tmp_path / "granite-4.0-1b-speech-ONNX")
    assert kwargs["max_workers"] == 2
    assert "onnx/*_q4.onnx" in kwargs["allow_patterns"]
    assert "onnx/*_q4.onnx_data" in kwargs["allow_patterns"]


def test_download_webgpu_model_snapshot_uses_granite_4_1_2b_q4_patterns(
    monkeypatch,
    tmp_path,
):
    calls = []

    def fake_snapshot_download(repo_id, **kwargs):
        calls.append((repo_id, kwargs))
        _materialise_required_files(
            repo_id, kwargs["local_dir"], kwargs.get("allow_patterns")
        )
        return str(tmp_path / "snapshot")

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(snapshot_download=fake_snapshot_download),
    )

    result = download_webgpu_model_snapshot(
        "granite-speech-4.1-2b",
        str(tmp_path),
    )

    assert result == str(tmp_path / "snapshot")
    repo_id, kwargs = calls[0]
    assert repo_id == MODEL_REPO_MAP["granite-speech-4.1-2b"]
    assert kwargs["local_dir"] == str(tmp_path / "granite-speech-4.1-2b-ONNX")
    assert kwargs["max_workers"] == 2
    assert "onnx/*_q4.onnx" in kwargs["allow_patterns"]
    assert "onnx/*_q4.onnx_data" in kwargs["allow_patterns"]
    assert "chat_template.jinja" in kwargs["allow_patterns"]
    assert "int8/*.onnx" not in kwargs["allow_patterns"]


def test_download_webgpu_model_snapshot_uses_granite_4_1_plus_int8_patterns(
    monkeypatch,
    tmp_path,
):
    calls = []

    def fake_snapshot_download(repo_id, **kwargs):
        calls.append((repo_id, kwargs))
        _materialise_required_files(
            repo_id, kwargs["local_dir"], kwargs.get("allow_patterns")
        )
        return str(tmp_path / "snapshot")

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(snapshot_download=fake_snapshot_download),
    )

    result = download_webgpu_model_snapshot(
        "granite-speech-4.1-2b-plus",
        str(tmp_path),
    )

    assert result == str(tmp_path / "snapshot")
    repo_id, kwargs = calls[0]
    assert repo_id == MODEL_REPO_MAP["granite-speech-4.1-2b-plus"]
    assert kwargs["local_dir"] == str(tmp_path / "ibm-granite-speech-4.1-2b-plus-onnx")
    assert kwargs["max_workers"] == 2
    assert "int8/*.onnx" in kwargs["allow_patterns"]
    assert "int8/*.onnx_data" in kwargs["allow_patterns"]
    assert "chat_template.jinja" in kwargs["allow_patterns"]
    assert "onnx/*_q4.onnx" not in kwargs["allow_patterns"]


def test_download_webgpu_model_snapshot_uses_granite_4_1_nar_int8_patterns(
    monkeypatch,
    tmp_path,
):
    calls = []

    def fake_snapshot_download(repo_id, **kwargs):
        calls.append((repo_id, kwargs))
        _materialise_required_files(
            repo_id, kwargs["local_dir"], kwargs.get("allow_patterns")
        )
        return str(tmp_path / "snapshot")

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(snapshot_download=fake_snapshot_download),
    )

    download_webgpu_model_snapshot("granite-speech-4.1-2b-nar", str(tmp_path))

    repo_id, kwargs = calls[0]
    assert repo_id == MODEL_REPO_MAP["granite-speech-4.1-2b-nar"]
    assert kwargs["local_dir"] == str(tmp_path / "ibm-granite-speech-4.1-2b-nar-onnx")
    assert "int8/editor.onnx" not in kwargs["allow_patterns"]
    assert "int8/*.onnx" in kwargs["allow_patterns"]
    assert "int8/*.onnx_data" in kwargs["allow_patterns"]
    assert "chat_template.jinja" not in kwargs["allow_patterns"]
    assert "test_fixtures/*" in kwargs["allow_patterns"]
    assert "onnx/*_q4.onnx" not in kwargs["allow_patterns"]


@pytest.mark.parametrize("model_name", LOCAL_ONNX_MODEL_SIZES)
def test_download_destination_matches_the_local_dir_actually_downloaded_into(
    monkeypatch,
    tmp_path,
    model_name,
):
    """Download progress is derived from growth of the destination directory, so
    the advertised destination and the directory `snapshot_download` writes into
    must never drift apart."""
    calls = []

    def fake_snapshot_download(repo_id, **kwargs):
        calls.append((repo_id, kwargs))
        _materialise_required_files(
            repo_id, kwargs["local_dir"], kwargs.get("allow_patterns")
        )
        return str(tmp_path / "snapshot")

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(snapshot_download=fake_snapshot_download),
    )

    download_webgpu_model_snapshot(model_name, str(tmp_path))

    _, kwargs = calls[0]
    destination = local_webgpu_asr.webgpu_download_destination(
        model_name, str(tmp_path)
    )
    assert destination is not None
    assert kwargs["local_dir"] == str(destination)


def test_download_destination_is_unknown_for_an_unmapped_model():
    assert local_webgpu_asr.webgpu_download_destination("not-a-model") is None


def test_required_file_validation_accepts_granite_4_1_2b_q4_snapshot(tmp_path):
    snapshot = _write_required_snapshot(tmp_path, "granite-speech-4.1-2b")

    assert (
        resolve_cached_webgpu_model_path("granite-speech-4.1-2b", str(tmp_path))
        == snapshot
    )
    assert find_cached_webgpu_models(str(tmp_path)) == ["granite-speech-4.1-2b"]


def test_required_file_validation_accepts_granite_4_1_nar_int8_snapshot(tmp_path):
    snapshot = _write_required_snapshot(tmp_path, "granite-speech-4.1-2b-nar")

    assert (
        resolve_cached_webgpu_model_path("granite-speech-4.1-2b-nar", str(tmp_path))
        == snapshot
    )
    assert find_cached_webgpu_models(str(tmp_path)) == ["granite-speech-4.1-2b-nar"]


def test_required_file_validation_rejects_incomplete_granite_4_1_snapshot(tmp_path):
    snapshot = _write_required_snapshot(tmp_path, "granite-speech-4.1-2b")
    (snapshot / "onnx/decoder_model_merged_q4.onnx_data").unlink()

    assert (
        resolve_cached_webgpu_model_path(
            "granite-speech-4.1-2b",
            str(tmp_path),
        )
        is None
    )


def test_webgpu_transcriber_defaults_auto_language_to_german():
    transcriber = LocalOnnxWebGpuTranscriber(
        model_size="cohere-transcribe-03-2026",
        language_mode="auto",
    )

    assert transcriber._language_arg() == "de"


def test_granite_webgpu_transcriber_allows_auto_language():
    transcriber = LocalOnnxWebGpuTranscriber(
        model_size="granite-4.0-1b-speech",
        language_mode="auto",
    )

    assert transcriber._language_arg() == ""


def test_granite_4_1_transcriber_allows_auto_and_french_language():
    auto_transcriber = LocalOnnxWebGpuTranscriber(
        model_size="granite-speech-4.1-2b",
        language_mode="auto",
    )
    french_transcriber = LocalOnnxWebGpuTranscriber(
        model_size="granite-speech-4.1-2b",
        language_mode="fr",
    )

    assert auto_transcriber._language_arg() == ""
    assert french_transcriber._language_arg() == "fr"


def test_granite_4_1_transcriber_defaults_to_int8_dtype():
    transcriber = LocalOnnxWebGpuTranscriber(
        model_size="granite-speech-4.1-2b-nar",
        language_mode="en",
    )

    assert transcriber.dtype == "int8"
    assert transcriber.device == "cpu"
    assert "CPU preferred for this model" in transcriber.runtime_status_text()

    transcriber._set_runtime_status("cpu", False, [])

    assert "preferred for this model" in transcriber.runtime_status_text()
    assert "intentionally uses CPU" in transcriber.runtime_warning
    assert "fallback was not available" not in transcriber.runtime_status_text()


def test_granite_4_1_plus_prefers_cpu_like_nar():
    """Plus shares NAR's conformer encoder, whose block-local attention no GPU
    execution provider here can run. Its WebGPU session still creates fine and
    only fails at inference, so without this preference every dictation paid a
    doomed WebGPU load plus a failed attempt before falling back."""
    transcriber = LocalOnnxWebGpuTranscriber(
        model_size="granite-speech-4.1-2b-plus",
        language_mode="en",
    )

    assert transcriber.device == "cpu"
    assert "CPU preferred for this model" in transcriber.runtime_status_text()


def test_granite_4_1_plus_explicit_gpu_target_bypasses_cpu_preference():
    transcriber = LocalOnnxWebGpuTranscriber(
        model_size="granite-speech-4.1-2b-plus",
        language_mode="en",
        device="webgpu",
    )

    assert transcriber.device == "webgpu"


def test_granite_4_1_plus_required_files_match_the_published_repo():
    """The Plus export ships `processor_config.json`; only the NAR export ships a
    flat `preprocessor_config.json`. Requiring the NAR name made a fully
    downloaded Plus invisible and unusable."""
    plus_required = set(local_webgpu_asr._REQUIRED_FILES["granite-speech-4.1-2b-plus"])
    nar_required = set(local_webgpu_asr._REQUIRED_FILES["granite-speech-4.1-2b-nar"])

    assert "processor_config.json" in plus_required
    assert "preprocessor_config.json" not in plus_required
    assert "preprocessor_config.json" in nar_required


@pytest.mark.parametrize("model_name", LOCAL_ONNX_MODEL_SIZES)
def test_every_required_file_is_covered_by_the_download_allow_patterns(model_name):
    """A required file the download never fetches is unrecoverable at runtime:
    the snapshot check fails, an online retry re-downloads, the allow-pattern
    for a nonexistent file matches nothing, and the same check fails again.
    This is what made Granite 4.1 Plus permanently unusable."""
    layout = local_webgpu_asr._MODEL_LAYOUTS[model_name]

    for relative in layout.required_files:
        assert any(
            fnmatchcase(relative, pattern) for pattern in layout.allow_patterns
        ), f"{model_name}: '{relative}' is required but no allow-pattern fetches it"


def test_granite_4_1_nar_explicit_gpu_target_bypasses_cpu_preference():
    transcriber = LocalOnnxWebGpuTranscriber(
        model_size="granite-speech-4.1-2b-nar",
        language_mode="en",
        device="dml",
    )

    assert transcriber.device == "dml"


def test_explicit_cpu_policy_does_not_report_failed_gpu_fallback():
    transcriber = LocalOnnxWebGpuTranscriber(
        model_size="granite-speech-4.1-2b-plus",
        language_mode="en",
        device="cpu",
    )

    transcriber._set_runtime_status("cpu", False, [])

    assert transcriber.runtime_status_text() == (
        "ONNX runtime active on CPU (selected device policy)."
    )
    assert "CPU device policy is selected" in transcriber.runtime_warning
    assert "fallback was not available" not in transcriber.runtime_status_text()


def test_webgpu_transcriber_reuses_process_and_reports_cpu_fallback(
    monkeypatch,
    tmp_path,
):
    runner = tmp_path / "runner.mjs"
    runner.write_text("", encoding="utf-8")
    fake_process = _FakeProcess()
    commands = []
    messages = [
        {
            "ok": True,
            "device": "cpu",
            "gpuAvailable": False,
            "fallbackErrors": [
                "webgpu: Failed to create WebGPU session",
                "dml: DirectML is unavailable",
            ],
        },
        {
            "id": 1,
            "ok": True,
            "text": "hello world",
            "device": "cpu",
            "gpuAvailable": False,
            "fallbackErrors": [
                "webgpu: Failed to create WebGPU session",
                "dml: DirectML is unavailable",
            ],
        },
    ]

    monkeypatch.setattr(
        LocalOnnxWebGpuTranscriber,
        "_ensure_snapshot",
        lambda self: tmp_path,
    )
    monkeypatch.setattr(
        LocalOnnxWebGpuTranscriber,
        "_start_reader_threads",
        lambda self, process: None,
    )
    monkeypatch.setattr(
        LocalOnnxWebGpuTranscriber,
        "_read_json_message",
        lambda self, state, deadline: messages.pop(0),
    )
    monkeypatch.setattr(
        local_webgpu_asr,
        "_ensure_js_runtime_available",
        lambda node_path, runner: None,
    )
    monkeypatch.setattr(
        local_webgpu_asr.subprocess,
        "Popen",
        lambda command, **kwargs: commands.append(command) or fake_process,
    )

    transcriber = LocalOnnxWebGpuTranscriber(
        model_size="cohere-transcribe-03-2026",
        language_mode="en",
        device="cpu",
        node_path="node",
        runner_path=runner,
    )
    progress: list[str] = []
    transcriber.set_progress_callback(progress.append)

    try:
        text = transcriber.transcribe_batch(b"RIFF")
        assert transcriber.is_model_loaded is True
    finally:
        transcriber.close()

    assert text == "hello world"
    assert transcriber.runtime_device == "cpu"
    assert transcriber.gpu_available is False
    assert "CPU" in transcriber.runtime_warning
    assert "webgpu: Failed to create WebGPU session" in transcriber.runtime_details_text
    assert "DirectML is unavailable" in transcriber.runtime_warning
    assert any("Starting ONNX runtime" in item for item in progress)
    assert any("ONNX runtime active on CPU" in item for item in progress)
    assert commands
    assert commands[0][commands[0].index("--device") + 1] == "cpu"
    assert commands[0][commands[0].index("--dtype") + 1] == "q4"
    requests = [
        json.loads(line)
        for line in fake_process.stdin.getvalue().splitlines()
        if line.strip()
    ]
    assert requests[0]["command"] == "transcribe"
    assert requests[0]["language"] == "en"
    assert Path(requests[0]["audioPath"]).exists() is False
    assert requests[-1]["command"] == "shutdown"


def test_granite_4_1_2b_transcriber_passes_q4_precision_to_node(
    monkeypatch,
    tmp_path,
):
    runner = tmp_path / "runner.mjs"
    runner.write_text("", encoding="utf-8")
    fake_process = _FakeProcess()
    commands = []
    messages = [
        {"ok": True, "device": "cpu", "gpuAvailable": False},
    ]

    monkeypatch.setattr(
        LocalOnnxWebGpuTranscriber,
        "_ensure_snapshot",
        lambda self: tmp_path,
    )
    monkeypatch.setattr(
        LocalOnnxWebGpuTranscriber,
        "_start_reader_threads",
        lambda self, process: None,
    )
    monkeypatch.setattr(
        LocalOnnxWebGpuTranscriber,
        "_read_json_message",
        lambda self, state, deadline: messages.pop(0),
    )
    monkeypatch.setattr(
        local_webgpu_asr,
        "_ensure_js_runtime_available",
        lambda node_path, runner: None,
    )
    monkeypatch.setattr(
        local_webgpu_asr.subprocess,
        "Popen",
        lambda command, **kwargs: commands.append(command) or fake_process,
    )

    transcriber = LocalOnnxWebGpuTranscriber(
        model_size="granite-speech-4.1-2b",
        language_mode="en",
        device="cpu",
        node_path="node",
        runner_path=runner,
    )
    try:
        transcriber.preload_model()
    finally:
        transcriber.close()

    assert commands
    assert commands[0][commands[0].index("--dtype") + 1] == "q4"


def test_webgpu_transcriber_closes_process_when_startup_response_fails(
    monkeypatch,
    tmp_path,
):
    runner = tmp_path / "runner.mjs"
    runner.write_text("", encoding="utf-8")
    fake_process = _FakeProcess()

    monkeypatch.setattr(
        LocalOnnxWebGpuTranscriber,
        "_ensure_snapshot",
        lambda self: tmp_path,
    )
    monkeypatch.setattr(
        LocalOnnxWebGpuTranscriber,
        "_start_reader_threads",
        lambda self, process: None,
    )
    monkeypatch.setattr(
        LocalOnnxWebGpuTranscriber,
        "_read_json_message",
        lambda self, state, deadline: (_ for _ in ()).throw(
            local_webgpu_asr.TranscriptionError("startup timeout")
        ),
    )
    monkeypatch.setattr(
        local_webgpu_asr,
        "_ensure_js_runtime_available",
        lambda node_path, runner: None,
    )
    monkeypatch.setattr(
        local_webgpu_asr.subprocess,
        "Popen",
        lambda command, **kwargs: fake_process,
    )

    transcriber = LocalOnnxWebGpuTranscriber(
        model_size="cohere-transcribe-03-2026",
        language_mode="en",
        node_path="node",
        runner_path=runner,
    )

    with pytest.raises(local_webgpu_asr.TranscriptionError, match="startup timeout"):
        transcriber.preload_model()

    assert fake_process.wait_calls == 1
    assert fake_process.terminated is True
    assert fake_process.stdin.getvalue() == ""
    assert transcriber.is_model_loaded is False


def test_webgpu_transcriber_restarts_after_auto_cpu_fallback(
    monkeypatch,
    tmp_path,
):
    runner = tmp_path / "runner.mjs"
    runner.write_text("", encoding="utf-8")
    fake_process = _FakeProcess()
    messages = [
        {
            "ok": True,
            "device": "cpu",
            "gpuAvailable": False,
            "fallbackErrors": [
                "webgpu: adapter unavailable after resume",
                "dml: DirectML is unavailable",
            ],
        },
        {
            "id": 1,
            "ok": True,
            "text": "hello world",
            "device": "cpu",
            "gpuAvailable": False,
            "fallbackErrors": [
                "webgpu: adapter unavailable after resume",
                "dml: DirectML is unavailable",
            ],
        },
    ]

    monkeypatch.setattr(
        LocalOnnxWebGpuTranscriber,
        "_ensure_snapshot",
        lambda self: tmp_path,
    )
    monkeypatch.setattr(
        LocalOnnxWebGpuTranscriber,
        "_start_reader_threads",
        lambda self, process: None,
    )
    monkeypatch.setattr(
        LocalOnnxWebGpuTranscriber,
        "_read_json_message",
        lambda self, state, deadline: messages.pop(0),
    )
    monkeypatch.setattr(
        local_webgpu_asr,
        "_ensure_js_runtime_available",
        lambda node_path, runner: None,
    )
    monkeypatch.setattr(
        local_webgpu_asr.subprocess,
        "Popen",
        lambda command, **kwargs: fake_process,
    )

    transcriber = LocalOnnxWebGpuTranscriber(
        model_size="cohere-transcribe-03-2026",
        language_mode="en",
        device="auto",
        node_path="node",
        runner_path=runner,
    )
    progress: list[str] = []
    transcriber.set_progress_callback(progress.append)

    text = transcriber.transcribe_batch(b"RIFF")

    assert text == "hello world"
    assert transcriber.runtime_device == "cpu"
    assert transcriber.is_model_loaded is False
    assert fake_process.wait_calls == 1
    requests = [
        json.loads(line)
        for line in fake_process.stdin.getvalue().splitlines()
        if line.strip()
    ]
    assert requests[-1] == {"command": "shutdown"}
    assert any("restarting before the next request" in item for item in progress)


def test_json_reader_uses_process_local_queue_and_absolute_deadline():
    transcriber = LocalOnnxWebGpuTranscriber(model_size="cohere-transcribe-03-2026")
    stale_process = _FakeProcess()
    current_process = _FakeProcess()
    stale = local_webgpu_asr._NodeProcessState(
        stale_process,
        queue.Queue(),
        deque(maxlen=local_webgpu_asr._STDERR_MAX_LINES),
    )
    current = local_webgpu_asr._NodeProcessState(
        current_process,
        queue.Queue(),
        deque(maxlen=local_webgpu_asr._STDERR_MAX_LINES),
    )
    stale.stdout_queue.put('{"id": 1, "ok": true}')
    current.stdout_queue.put('{"id": 2, "ok": true}')

    message = transcriber._read_json_message(current, time.monotonic() + 0.2)

    assert message["id"] == 2
    assert stale.stdout_queue.qsize() == 1

    started = time.monotonic()
    with pytest.raises(TranscriptionError, match="Timed out"):
        transcriber._read_json_message(current, time.monotonic() + 0.02)
    assert time.monotonic() - started < 0.2


def test_reader_retains_only_bounded_process_local_stderr():
    transcriber = LocalOnnxWebGpuTranscriber(model_size="cohere-transcribe-03-2026")
    process = _FakeProcess()
    process.stdout = io.StringIO('{"ok": true}\n')
    process.stderr = io.StringIO(
        "".join(f"diagnostic-{index}\n" for index in range(400))
    )
    state = local_webgpu_asr._NodeProcessState(
        process,
        queue.Queue(),
        deque(maxlen=local_webgpu_asr._STDERR_MAX_LINES),
    )

    transcriber._start_reader_threads(state)
    assert transcriber._read_json_message(state, time.monotonic() + 1)["ok"] is True
    deadline = time.monotonic() + 1
    while len(state.stderr_lines) < local_webgpu_asr._STDERR_MAX_LINES:
        assert time.monotonic() < deadline
        time.sleep(0.001)

    assert len(state.stderr_lines) == local_webgpu_asr._STDERR_MAX_LINES
    assert state.stderr_lines[0] == "diagnostic-144"
    assert state.stderr_lines[-1] == "diagnostic-399"
    assert "diagnostic-388" in transcriber._stderr_tail(state)
    assert "diagnostic-387" not in transcriber._stderr_tail(state)


def test_a_cancel_stops_waiting_for_the_child_and_kills_it(monkeypatch, tmp_path):
    """Cancel used to do nothing here: the request had already been written, so
    the Node child kept transcribing -- CPU busy, model still in memory -- while
    the parent blocked on the response. Killing it is what actually stops the
    work."""
    runner = tmp_path / "runner.mjs"
    runner.write_text("", encoding="utf-8")
    process = _FakeProcess()
    read_count = 0

    def fake_read(self, state, deadline):
        nonlocal read_count
        read_count += 1
        if read_count == 1:  # startup handshake
            return {"ok": True, "device": "cpu", "gpuAvailable": False}
        # Reproduce the real reader's cancel poll rather than blocking forever.
        self._raise_if_canceled()
        raise AssertionError("the cancelled wait must not reach the response")

    monkeypatch.setattr(
        LocalOnnxWebGpuTranscriber, "_ensure_snapshot", lambda self: tmp_path
    )
    monkeypatch.setattr(
        LocalOnnxWebGpuTranscriber, "_start_reader_threads", lambda self, state: None
    )
    monkeypatch.setattr(LocalOnnxWebGpuTranscriber, "_read_json_message", fake_read)
    monkeypatch.setattr(
        local_webgpu_asr, "_ensure_js_runtime_available", lambda node_path, runner: None
    )
    monkeypatch.setattr(
        local_webgpu_asr.subprocess, "Popen", lambda command, **kwargs: process
    )
    transcriber = LocalOnnxWebGpuTranscriber(
        model_size="cohere-transcribe-03-2026",
        device="cpu",
        node_path="node",
        runner_path=runner,
    )
    # Cancel only once the request is in flight -- that is the case the fix is
    # about; a cancel before it is covered by the next test.
    transcriber.set_cancel_check(lambda: "transcribe" in process.stdin.getvalue())

    with pytest.raises(TranscriptionCanceled):
        transcriber.transcribe_batch(b"RIFF")

    assert process.stdin.getvalue().count("transcribe") == 1
    assert process.terminated is True
    assert transcriber._process_state is None


def test_a_cancel_before_the_request_starts_no_child(monkeypatch, tmp_path):
    runner = tmp_path / "runner.mjs"
    runner.write_text("", encoding="utf-8")
    started: list[object] = []
    monkeypatch.setattr(
        local_webgpu_asr,
        "_ensure_js_runtime_available",
        lambda node_path, runner: started.append(runner),
    )
    transcriber = LocalOnnxWebGpuTranscriber(
        model_size="cohere-transcribe-03-2026",
        device="cpu",
        node_path="node",
        runner_path=runner,
    )
    transcriber.set_cancel_check(lambda: True)

    with pytest.raises(TranscriptionCanceled):
        transcriber.transcribe_batch(b"RIFF")

    assert started == []


def test_the_response_reader_polls_the_cancel_check():
    transcriber = LocalOnnxWebGpuTranscriber(model_size="cohere-transcribe-03-2026")
    process = _FakeProcess()
    state = local_webgpu_asr._NodeProcessState(
        process,
        queue.Queue(),
        deque(maxlen=local_webgpu_asr._STDERR_MAX_LINES),
    )
    transcriber.set_cancel_check(lambda: True)
    started = time.monotonic()
    with pytest.raises(TranscriptionCanceled):
        transcriber._read_json_message(state, time.monotonic() + 30)
    # Not a timeout: it returns immediately rather than after the deadline.
    assert time.monotonic() - started < 1.0


def test_protocol_timeout_kills_child_and_next_request_starts_fresh(
    monkeypatch,
    tmp_path,
):
    runner = tmp_path / "runner.mjs"
    runner.write_text("", encoding="utf-8")
    processes = [_FakeProcess(), _FakeProcess()]
    process_iter = iter(processes)
    read_count = 0

    def fake_read(self, state, deadline):
        nonlocal read_count
        read_count += 1
        if read_count in {1, 3}:
            return {"ok": True, "device": "cpu", "gpuAvailable": False}
        if read_count == 2:
            raise local_webgpu_asr._RuntimeProtocolError("request timeout")
        return {"id": 2, "ok": True, "text": "recovered"}

    monkeypatch.setattr(
        LocalOnnxWebGpuTranscriber,
        "_ensure_snapshot",
        lambda self: tmp_path,
    )
    monkeypatch.setattr(
        LocalOnnxWebGpuTranscriber,
        "_start_reader_threads",
        lambda self, state: None,
    )
    monkeypatch.setattr(LocalOnnxWebGpuTranscriber, "_read_json_message", fake_read)
    monkeypatch.setattr(
        local_webgpu_asr,
        "_ensure_js_runtime_available",
        lambda node_path, runner: None,
    )
    monkeypatch.setattr(
        local_webgpu_asr.subprocess,
        "Popen",
        lambda command, **kwargs: next(process_iter),
    )
    transcriber = LocalOnnxWebGpuTranscriber(
        model_size="cohere-transcribe-03-2026",
        device="cpu",
        node_path="node",
        runner_path=runner,
    )

    with pytest.raises(TranscriptionError, match="request timeout"):
        transcriber.transcribe_batch(b"RIFF")
    assert processes[0].terminated is True
    assert processes[0].stdin.getvalue().count("transcribe") == 1

    try:
        assert transcriber.transcribe_batch(b"RIFF") == "recovered"
    finally:
        transcriber.close()

    assert processes[1].stdin.getvalue().count("transcribe") == 1
    assert '"id": 2' in processes[1].stdin.getvalue()


def test_node_wav_and_protocol_parsers_reject_malformed_bounds(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed.")
    runner = (
        Path(local_webgpu_asr.__file__).resolve().parents[1] / "webgpu_asr_runner.mjs"
    )
    valid = tmp_path / "valid.wav"
    with wave.open(str(valid), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(b"\x01\x00" * 160)
    truncated = tmp_path / "truncated.wav"
    malformed = bytearray(valid.read_bytes())
    data_offset = malformed.index(b"data")
    declared_size = int.from_bytes(
        malformed[data_offset + 4 : data_offset + 8], "little"
    )
    malformed[data_offset + 4 : data_offset + 8] = (declared_size + 100).to_bytes(
        4, "little"
    )
    truncated.write_bytes(malformed)

    script = """
      import { pathToFileURL } from 'node:url';
      const runtime = await import(pathToFileURL(process.argv[1]).href);
      const result = {};
      result.validSamples = runtime.decodeWavFile(process.argv[2], 16000).length;
      try { runtime.decodeWavFile(process.argv[3], 16000); }
      catch (error) { result.wavError = String(error.message); }
      result.command = runtime.parseProtocolRequestLine('{"command":"shutdown"}').command;
      try { runtime.parseProtocolRequestLine('[]'); }
      catch (error) { result.protocolError = String(error.message); }
      console.log(JSON.stringify(result));
    """
    completed = subprocess.run(
        [
            node,
            "--input-type=module",
            "-e",
            script,
            str(runner),
            str(valid),
            str(truncated),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    result = json.loads(completed.stdout)

    assert result["validSamples"] == 160
    assert "exceeds the file bounds" in result["wavError"]
    assert result["command"] == "shutdown"
    assert result["protocolError"] == "Protocol request must be a JSON object."
