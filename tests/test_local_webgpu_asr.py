from __future__ import annotations

import io
import json
import sys
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
from stt_app.transcriber.local_webgpu_asr import (
    LocalOnnxWebGpuTranscriber,
    download_webgpu_model_snapshot,
    find_cached_webgpu_models,
    resolve_cached_webgpu_model_path,
)


def _write_required_snapshot(base: Path, model_name: str, snapshot_id: str = "abc123"):
    repo_id = local_webgpu_asr._repo_id_for_model(model_name)
    assert repo_id is not None
    snapshot = (
        base
        / f"models--{repo_id.replace('/', '--')}"
        / "snapshots"
        / snapshot_id
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

    def poll(self):
        return None

    def wait(self, timeout=None):
        self.wait_calls += 1
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
    assert kwargs["local_dir"] == str(
        tmp_path / "ibm-granite-speech-4.1-2b-plus-onnx"
    )
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
        return str(tmp_path / "snapshot")

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(snapshot_download=fake_snapshot_download),
    )

    download_webgpu_model_snapshot("granite-speech-4.1-2b-nar", str(tmp_path))

    repo_id, kwargs = calls[0]
    assert repo_id == MODEL_REPO_MAP["granite-speech-4.1-2b-nar"]
    assert kwargs["local_dir"] == str(
        tmp_path / "ibm-granite-speech-4.1-2b-nar-onnx"
    )
    assert "int8/editor.onnx" not in kwargs["allow_patterns"]
    assert "int8/*.onnx" in kwargs["allow_patterns"]
    assert "int8/*.onnx_data" in kwargs["allow_patterns"]
    assert "chat_template.jinja" not in kwargs["allow_patterns"]
    assert "test_fixtures/*" in kwargs["allow_patterns"]
    assert "onnx/*_q4.onnx" not in kwargs["allow_patterns"]


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

    assert resolve_cached_webgpu_model_path(
        "granite-speech-4.1-2b",
        str(tmp_path),
    ) is None


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
        lambda self, timeout_s: messages.pop(0),
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
        lambda self, timeout_s: messages.pop(0),
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
        lambda self, timeout_s: (_ for _ in ()).throw(
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
    assert json.loads(fake_process.stdin.getvalue().strip()) == {"command": "shutdown"}
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
        lambda self, timeout_s: messages.pop(0),
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
