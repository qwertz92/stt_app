from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from stt_app.config import MODEL_REPO_MAP
from stt_app.transcriber import local_webgpu_asr
from stt_app.transcriber.local_webgpu_asr import (
    LocalOnnxWebGpuTranscriber,
    download_webgpu_model_snapshot,
    find_cached_webgpu_models,
    resolve_cached_webgpu_model_path,
)


def _write_required_snapshot(base: Path, model_name: str, snapshot_id: str = "abc123"):
    repo_id = MODEL_REPO_MAP[model_name]
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


def test_webgpu_transcriber_reuses_process_and_reports_cpu_fallback(
    monkeypatch,
    tmp_path,
):
    runner = tmp_path / "runner.mjs"
    runner.write_text("", encoding="utf-8")
    fake_process = _FakeProcess()
    commands = []
    messages = [
        {"ok": True, "device": "cpu", "gpuAvailable": False},
        {
            "id": 1,
            "ok": True,
            "text": "hello world",
            "device": "cpu",
            "gpuAvailable": False,
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

    try:
        text = transcriber.transcribe_batch(b"RIFF")
    finally:
        transcriber.close()

    assert text == "hello world"
    assert transcriber.runtime_device == "cpu"
    assert transcriber.gpu_available is False
    assert "CPU" in transcriber.runtime_warning
    assert commands
    assert commands[0][commands[0].index("--device") + 1] == "cpu"
    requests = [
        json.loads(line)
        for line in fake_process.stdin.getvalue().splitlines()
        if line.strip()
    ]
    assert requests[0]["command"] == "transcribe"
    assert requests[0]["language"] == "en"
    assert Path(requests[0]["audioPath"]).exists() is False
    assert requests[-1]["command"] == "shutdown"


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
