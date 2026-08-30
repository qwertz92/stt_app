from __future__ import annotations

import io
import json
import queue
import re
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
    GRANITE_4_1_REPO_MAP,
    LOCAL_NEMOTRON_MODEL_SIZES,
    LOCAL_ONNX_MODEL_PRECISION,
    LOCAL_ONNX_MODEL_SIZES,
    LOCAL_WEBGPU_MODEL_SIZES,
    MODEL_REPO_MAP,
    PARAKEET_MODEL_SIZE,
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
    for model_name in GRANITE_4_1_REPO_MAP:
        assert model_name in LOCAL_WEBGPU_MODEL_SIZES
        assert model_name in MODEL_REPO_MAP
    # Granite 4.1 2B ships as a q4 Transformers.js package on the pipeline
    # path. The raw INT8 graph tier (Plus, NAR) was retired on 2026-08-26, so
    # every selectable ONNX model now runs through that one pipeline.
    assert LOCAL_ONNX_MODEL_PRECISION["granite-speech-4.1-2b"] == "q4"
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


@pytest.mark.parametrize("model_name", LOCAL_ONNX_MODEL_SIZES)
def test_every_required_file_is_covered_by_the_download_allow_patterns(model_name):
    """A required file the download never fetches is unrecoverable at runtime:
    the snapshot check fails, an online retry re-downloads, the allow-pattern
    for a nonexistent file matches nothing, and the same check fails again.
    This is what made the (since retired) Granite 4.1 Plus permanently
    unusable."""
    layout = local_webgpu_asr._MODEL_LAYOUTS[model_name]

    for relative in layout.required_files:
        assert any(
            fnmatchcase(relative, pattern) for pattern in layout.allow_patterns
        ), f"{model_name}: '{relative}' is required but no allow-pattern fetches it"


def test_explicit_cpu_policy_does_not_report_failed_gpu_fallback():
    transcriber = LocalOnnxWebGpuTranscriber(
        model_size="granite-speech-4.1-2b",
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

    def _no_child(*_args, **_kwargs):
        raise AssertionError("a canceled job must not spawn the Node runtime")

    # Sandboxed on purpose: without this a regression would launch a real
    # Node process instead of failing, and the assertion below only observes
    # a proxy for that.
    monkeypatch.setattr(local_webgpu_asr.subprocess, "Popen", _no_child)
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


def test_the_reader_keeps_polling_while_it_waits_not_only_on_entry():
    """A check that is already true on entry proves only the first poll.

    The real case is a Cancel pressed *during* a transcription that has been
    running for seconds: the check turns true long after the reader started
    waiting, and it is the per-iteration poll that has to notice.
    """
    transcriber = LocalOnnxWebGpuTranscriber(model_size="cohere-transcribe-03-2026")
    state = local_webgpu_asr._NodeProcessState(
        _FakeProcess(),
        queue.Queue(),
        deque(maxlen=local_webgpu_asr._STDERR_MAX_LINES),
    )
    polls = []

    def cancel_after_three_polls() -> bool:
        polls.append(1)
        return len(polls) > 3

    transcriber.set_cancel_check(cancel_after_three_polls)

    started = time.monotonic()
    with pytest.raises(TranscriptionCanceled):
        transcriber._read_json_message(state, time.monotonic() + 30)
    elapsed = time.monotonic() - started

    assert len(polls) == 4
    # Four iterations of the 0.25 s queue poll, nowhere near the 30 s deadline.
    assert 0.5 < elapsed < 5.0


def test_a_cancel_reaches_the_real_reader_and_kills_the_child(monkeypatch, tmp_path):
    """The same case as above, through the unmocked read path.

    ``test_a_cancel_stops_waiting_for_the_child_and_kills_it`` replaces
    ``_read_json_message`` entirely, so it can only prove what the caller does
    with the exception. Here the handshake is answered through the real queue
    and the transcribe response never arrives, which is what a running
    transcription looks like from the parent side.
    """
    runner = tmp_path / "runner.mjs"
    runner.write_text("", encoding="utf-8")
    process = _FakeProcess()

    def answer_handshake(self, state):
        state.stdout_queue.put(
            json.dumps({"ok": True, "device": "cpu", "gpuAvailable": False})
        )

    monkeypatch.setattr(
        LocalOnnxWebGpuTranscriber, "_ensure_snapshot", lambda self: tmp_path
    )
    monkeypatch.setattr(
        LocalOnnxWebGpuTranscriber, "_start_reader_threads", answer_handshake
    )
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
    transcriber.request_timeout_s = 30
    transcriber.set_cancel_check(lambda: "transcribe" in process.stdin.getvalue())

    started = time.monotonic()
    with pytest.raises(TranscriptionCanceled):
        transcriber.transcribe_batch(b"RIFF")

    assert time.monotonic() - started < 5.0
    assert process.stdin.getvalue().count("transcribe") == 1
    assert process.terminated is True
    assert transcriber._process_state is None


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


def _probe_imports(monkeypatch) -> set[str]:
    """The package names `_run_transformers_import_probe` actually imports."""
    captured: list[list[str]] = []

    def fake_run(command, **_kwargs):
        captured.append(list(command))
        raise AssertionError("the probe must not be executed here")

    monkeypatch.setattr(local_webgpu_asr.subprocess, "run", fake_run)
    with pytest.raises(AssertionError):
        local_webgpu_asr._run_transformers_import_probe("node", Path("."))
    script = _strip_js_comments(captured[0][-1])
    return set(_IMPORT_SPECIFIER.findall(script))


BACKTICK = chr(96)

# A tiny JS lexer, because a bare pattern reads comments, template literals
# and regex literals as code: a commented-out `import "old-pkg"`, an error
# message containing ` from "..."`, and a regex holding `/*` all changed what
# the scan found. Strings are kept -- the specifier lives in one -- while
# comments, template literals and regex literals are blanked.
#
# The regex-literal alternative must come *before* the comment ones, or
# `/[/*]/` starts a block comment that swallows the file up to the next `*/`.
# A leading `/` is a regex only where an expression may start, which is what
# the single-character lookbehind approximates; after an operand it is
# division and is left alone.
_JS_TOKEN = re.compile(
    r'"(?:[^"\\\n]|\\.)*"'
    r"|'(?:[^'\\\n]|\\.)*'"
    r"|`(?:[^`\\]|\\.)*`"
    r"|(?<=[=(,:\[!&|?{};])[ \t]*"
    r"/(?:[^/\\\n\[]|\\.|\[(?:[^\]\\]|\\.)*\])+/[gimsuyvd]*"
    r"|//[^\n]*"
    r"|/\*[\s\S]*?\*/",
)


def _strip_js_comments(source: str) -> str:
    """Blank comments, template literals and regex literals; keep strings."""

    def replace(match: re.Match[str]) -> str:
        text = match.group(0)
        stripped = text.lstrip(" \t")
        if stripped.startswith(("//", "/*")):
            # Spaces, not "", so `a/*c*/from "x"` does not become `afrom`.
            return " " * len(text)
        if stripped.startswith("/"):
            return " " * len(text)  # regex literal
        if stripped.startswith("`"):
            return text[: len(text) - len(stripped)] + "``"
        return text

    return _JS_TOKEN.sub(replace, source)


# Every ES-module form that can pull in a package. A single-line
# `import ... from` pattern misses four of them, and the one the runner
# actually uses is the dynamic `import()` -- so a Prettier reflow of that one
# line used to silence the guard completely.
_IMPORT_SPECIFIER = re.compile(
    r"(?:"
    # Named / default / re-export. The middle may span lines but must not
    # cross a statement boundary or contain `(` or a backtick, which is what
    # keeps `export function f() { ... from "x" ... }` out.
    r"(?:^|;)[ \t]*(?:import|export)\s[^;(]*?\sfrom\s*"
    # Side-effect import. Also after `;`, so `import "a"; import "b";` is two.
    r"|(?:^|;)[ \t]*import\s*"
    # Dynamic import(), with any whitespace or newline before the specifier.
    r"|\bimport\s*\(\s*"
    r")"
    r"""['\"]([^'\"]+)['\"]""",
    re.MULTILINE,
)

# Forms kept out of the parametrize list so the test source stays scannable.
_MULTILINE_IMPORT = '''import {
  a,
  b,
} from "pkg-b";'''
_MULTILINE_DYNAMIC_IMPORT = '''const m = await import(
  "pkg-x"
);'''
_COMMENTED_OUT_IMPORT = '''/*
import "old-pkg";
*/
import "pkg-y";'''
_STRING_THAT_LOOKS_LIKE_AN_IMPORT = '''export default 1;
const s = " from 'ghost'";'''
# Discriminating on purpose. A URL in a string does not test string handling:
# without it the `//` is eaten as a line comment, but the damage stops at the
# newline and the next line's import is still found. On one line it does.
_COMMENT_MARKER_IN_A_STRING = '''const u = "a//b"; import "pkg-u";'''
# Likewise, a template literal inside `export function f()` is already blocked
# by the `(` in the named-import bound, so it proves nothing about blanking.
_TEMPLATE_LITERAL_BANNER = (
    '''export const banner = ''' + BACKTICK + '''built from "nowhere"''' + BACKTICK + ''';'''
)
# A regex literal holding `/*` starts a block comment for a naive lexer, which
# then swallows everything up to the next real `*/` -- including the import.
_REGEX_WITH_A_COMMENT_MARKER = '''const token = /[/*]/;
import "pkg-r";
/* an ordinary block comment further down */'''
_TEMPLATE_LITERAL_FROM = '''export function f() {
  return `copied from "not-a-pkg"`;
}'''


def _runner_imports() -> set[str]:
    """The bare package specifiers `webgpu_asr_runner.mjs` imports."""
    runner = (
        Path(local_webgpu_asr.__file__).resolve().parents[1] / "webgpu_asr_runner.mjs"
    )
    source = _strip_js_comments(runner.read_text(encoding="utf-8"))
    specifiers = set(_IMPORT_SPECIFIER.findall(source))
    return {
        name
        for name in specifiers
        if not name.startswith(("node:", ".", "/"))
    }


def test_the_runtime_probe_only_imports_declared_dependencies(monkeypatch):
    """Probing an undeclared package makes the repair unreachable.

    The probe's failure branch runs `npm install`, which installs exactly what
    `package.json` asks for. `@huggingface/tokenizers` and `onnxruntime-node`
    resolve today only because npm hoists them out of
    `@huggingface/transformers`; probing for them meant that if that hoist ever
    changed, every ONNX dictation would end in "run npm install" and the
    reinstall could never fix it.
    """
    root = Path(__file__).resolve().parents[1]
    declared = set(json.loads((root / "package.json").read_text("utf-8"))["dependencies"])
    probed = _probe_imports(monkeypatch)

    # An empty set is a subset of everything, so both halves are asserted.
    assert probed
    assert probed <= declared


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('import { x } from "pkg-a";', {"pkg-a"}),
        (_MULTILINE_IMPORT, {"pkg-b"}),
        ('import "pkg-c";', {"pkg-c"}),
        ('export { z } from "pkg-d";', {"pkg-d"}),
        ('const m = await import("pkg-e");', {"pkg-e"}),
        ('import { readFileSync } from "node:fs";', {"node:fs"}),
        (_MULTILINE_DYNAMIC_IMPORT, {"pkg-x"}),
        ('const m = await import (/* why not */ "pkg-z");', {"pkg-z"}),
        ('import "pkg-1"; import "pkg-2";', {"pkg-1", "pkg-2"}),
        (_COMMENTED_OUT_IMPORT, {"pkg-y"}),
        (_TEMPLATE_LITERAL_FROM, set()),
        (_COMMENT_MARKER_IN_A_STRING, {"pkg-u"}),
        (_TEMPLATE_LITERAL_BANNER, set()),
        (_REGEX_WITH_A_COMMENT_MARKER, {"pkg-r"}),
        (_STRING_THAT_LOOKS_LIKE_AN_IMPORT, set()),
    ],
    ids=[
        "named",
        "multiline",
        "side-effect",
        "re-export",
        "dynamic",
        "builtin",
        "multiline-dynamic",
        "spaced-dynamic-with-comment",
        "two-on-one-line",
        "commented-out",
        "template-literal",
        "comment-marker-in-a-string",
        "template-literal-banner",
        "regex-literal-with-a-comment-marker",
        "string-that-looks-like-an-import",
    ],
)
def test_the_import_scanner_sees_every_module_form(source, expected):
    """The dependency guards below are only as good as this pattern.

    A single-line `import ... from` pattern misses a multi-line named import,
    a side-effect import and a re-export -- three ordinary ways to add the
    dependency the guards exist to catch.
    """
    assert set(_IMPORT_SPECIFIER.findall(_strip_js_comments(source))) == expected


def test_the_runtime_probe_covers_everything_the_runner_imports(monkeypatch):
    """The other direction: a package the runner needs must be probed.

    The probe exists to turn a missing dependency into one actionable message
    instead of a crash mid-dictation, so it has to check the full set.
    """
    needed = _runner_imports()

    assert needed
    assert needed <= _probe_imports(monkeypatch)


def test_a_model_in_the_default_cache_is_found_with_a_model_dir_set(
    tmp_path, monkeypatch
):
    """The ONNX side searched one root while delete spanned two.

    `scripts/download_model.py` writes into the default cache. Setting a Model
    Dir afterwards made the Local tab report the model as missing and the
    preload fetch it again -- and Delete, which always looked in both roots,
    would then remove the copy the scan had never listed. It matters most for
    the default model, which is one of these.
    """
    default_cache = tmp_path / "default-cache"
    model_dir = tmp_path / "model-dir"
    model_dir.mkdir()
    snapshot = _write_required_snapshot(default_cache, PARAKEET_MODEL_SIZE)
    monkeypatch.setattr(
        local_webgpu_asr, "_default_hf_cache_dir", lambda: str(default_cache)
    )

    assert local_webgpu_asr.find_cached_webgpu_models(str(model_dir)) == [
        PARAKEET_MODEL_SIZE
    ]
    assert (
        local_webgpu_asr.resolve_cached_webgpu_model_path(
            PARAKEET_MODEL_SIZE, str(model_dir)
        )
        == snapshot
    )
    # ...while the download destination stays the configured Model Dir, so a
    # progress bar still measures the directory a download writes into.
    assert local_webgpu_asr.webgpu_download_destination(
        PARAKEET_MODEL_SIZE, str(model_dir)
    ) == model_dir / "parakeet-tdt-0.6b-v3-onnx"


def test_a_machine_without_a_gpu_stops_restarting_the_child(monkeypatch, tmp_path):
    """The `auto` policy tries webgpu, then dml on Windows, then cpu, so a
    machine with no usable GPU reports two `fallbackErrors` and `device: cpu`
    for every single request. That made `_should_restart_after_cpu_fallback`
    permanently true and killed the child after *each* transcription, so
    `keep_onnx_model_loaded` -- on by default -- bought nothing on exactly the
    machines that need it most and every dictation paid a fresh Node start plus
    a full ONNX model load. One retry still covers a GPU that was merely busy.
    """
    runner = tmp_path / "runner.mjs"
    runner.write_text("", encoding="utf-8")
    processes: list[_FakeProcess] = []

    cpu_fallback = {
        "device": "cpu",
        "gpuAvailable": False,
        "fallbackErrors": [
            "webgpu: no adapter",
            "dml: DirectML is unavailable",
        ],
    }
    messages = [
        {"ok": True, **cpu_fallback},
        {"id": 1, "ok": True, "text": "one", **cpu_fallback},
        {"ok": True, **cpu_fallback},
        {"id": 2, "ok": True, "text": "two", **cpu_fallback},
        {"id": 3, "ok": True, "text": "three", **cpu_fallback},
        {"id": 4, "ok": True, "text": "four", **cpu_fallback},
    ]

    def _spawn(command, **kwargs):
        process = _FakeProcess()
        processes.append(process)
        return process

    monkeypatch.setattr(
        LocalOnnxWebGpuTranscriber, "_ensure_snapshot", lambda self: tmp_path
    )
    monkeypatch.setattr(
        LocalOnnxWebGpuTranscriber, "_start_reader_threads", lambda self, process: None
    )
    monkeypatch.setattr(
        LocalOnnxWebGpuTranscriber,
        "_read_json_message",
        lambda self, state, deadline: messages.pop(0),
    )
    monkeypatch.setattr(
        local_webgpu_asr, "_ensure_js_runtime_available", lambda node_path, runner: None
    )
    monkeypatch.setattr(local_webgpu_asr.subprocess, "Popen", _spawn)

    transcriber = LocalOnnxWebGpuTranscriber(
        model_size="cohere-transcribe-03-2026",
        language_mode="en",
        device="auto",
        node_path="node",
        runner_path=runner,
    )

    assert transcriber.transcribe_batch(b"RIFF") == "one"
    assert transcriber.is_model_loaded is False, "the one retry has to happen"

    assert transcriber.transcribe_batch(b"RIFF") == "two"
    assert transcriber.is_model_loaded is True, (
        "the second fallback in a row restarted the child again"
    )
    assert transcriber.transcribe_batch(b"RIFF") == "three"
    assert transcriber.transcribe_batch(b"RIFF") == "four"
    assert transcriber.is_model_loaded is True

    assert len(processes) == 2, (
        f"four transcriptions started {len(processes)} Node processes"
    )
    transcriber.close()


def test_a_gpu_that_comes_back_re_arms_the_retry(monkeypatch, tmp_path):
    """The cap is per fallback event, not per process: once a GPU load has
    succeeded, the next fallback is a new event and gets its own retry."""
    runner = tmp_path / "runner.mjs"
    runner.write_text("", encoding="utf-8")
    processes: list[_FakeProcess] = []

    cpu_fallback = {
        "device": "cpu",
        "gpuAvailable": False,
        "fallbackErrors": ["webgpu: adapter busy"],
    }
    on_gpu = {"device": "webgpu", "gpuAvailable": True, "fallbackErrors": []}
    messages = [
        {"ok": True, **cpu_fallback},
        {"id": 1, "ok": True, "text": "one", **cpu_fallback},
        # restart 1
        {"ok": True, **on_gpu},
        {"id": 2, "ok": True, "text": "two", **on_gpu},
        # the adapter goes away again on the next request
        {"id": 3, "ok": True, "text": "three", **cpu_fallback},
        # restart 2
        {"ok": True, **cpu_fallback},
        {"id": 4, "ok": True, "text": "four", **cpu_fallback},
    ]

    def _spawn(command, **kwargs):
        process = _FakeProcess()
        processes.append(process)
        return process

    monkeypatch.setattr(
        LocalOnnxWebGpuTranscriber, "_ensure_snapshot", lambda self: tmp_path
    )
    monkeypatch.setattr(
        LocalOnnxWebGpuTranscriber, "_start_reader_threads", lambda self, process: None
    )
    monkeypatch.setattr(
        LocalOnnxWebGpuTranscriber,
        "_read_json_message",
        lambda self, state, deadline: messages.pop(0),
    )
    monkeypatch.setattr(
        local_webgpu_asr, "_ensure_js_runtime_available", lambda node_path, runner: None
    )
    monkeypatch.setattr(local_webgpu_asr.subprocess, "Popen", _spawn)

    transcriber = LocalOnnxWebGpuTranscriber(
        model_size="cohere-transcribe-03-2026",
        language_mode="en",
        device="auto",
        node_path="node",
        runner_path=runner,
    )

    assert transcriber.transcribe_batch(b"RIFF") == "one"
    assert transcriber.is_model_loaded is False
    assert transcriber.transcribe_batch(b"RIFF") == "two"
    assert transcriber.runtime_device == "webgpu"
    assert transcriber.is_model_loaded is True

    assert transcriber.transcribe_batch(b"RIFF") == "three"
    assert transcriber.is_model_loaded is False, (
        "a fallback after a working GPU has to be retried again"
    )
    assert transcriber.transcribe_batch(b"RIFF") == "four"
    assert len(processes) == 3
    transcriber.close()


def test_the_stdout_reader_finishes_even_when_nobody_drains_it():
    """The queue is bounded at 128 and only `_read_json_message` drains it, so
    a child that keeps writing between requests -- or one discarded after a
    protocol timeout with lines still buffered in the pipe -- parked this
    thread inside `Queue.put` for the rest of the process's life, holding the
    `Popen` and all three pipe handles with it. Every restart leaked another
    set, and the CPU-fallback restart made that once per dictation.
    """
    transcriber = LocalOnnxWebGpuTranscriber(model_size="cohere-transcribe-03-2026")
    lines = [f'{{"id": {index}, "ok": true}}' for index in range(500)]

    class _Process:
        stdout = io.StringIO("\n".join(lines) + "\n")
        stderr = None

    state = local_webgpu_asr._NodeProcessState(
        process=_Process(),
        stdout_queue=queue.Queue(maxsize=128),
        stderr_lines=deque(maxlen=64),
    )

    transcriber._start_reader_threads(state)

    # The thread itself cannot be asserted on: it may well have finished before
    # `threading.enumerate()` is reached. What it leaves behind can. A reader
    # parked in `put` stops at the first 128 lines and the newest never arrives.
    deadline = time.monotonic() + 5.0
    snapshot: list[str] = []
    while time.monotonic() < deadline:
        snapshot = list(state.stdout_queue.queue)
        if snapshot and snapshot[-1] == lines[-1]:
            break
        time.sleep(0.01)

    assert snapshot, "the reader produced nothing at all"
    assert snapshot[-1] == lines[-1], (
        "the newest line never arrived, so the reader is parked in put() with "
        f"the pipe held open; it stopped at {snapshot[-1]!r}"
    )
    assert len(snapshot) == 128, f"the bound was not kept: {len(snapshot)}"
    assert snapshot[0] == lines[-128], f"more than the oldest was dropped: {snapshot[0]!r}"
