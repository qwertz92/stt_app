"""Tests for the ModelScope mirror fallback used when Hugging Face is blocked."""

from __future__ import annotations

import huggingface_hub
import pytest

from stt_app.transcriber import local_faster_whisper, local_webgpu_asr
from stt_app.transcriber import modelscope_mirror as ms


@pytest.mark.parametrize(
    ("path", "patterns", "expected"),
    [
        ("onnx/audio_encoder_q4.onnx", ("onnx/*_q4.onnx", "onnx/*_q4.onnx_data"), True),
        ("onnx/audio_encoder_q4.onnx_data", ("onnx/*_q4.onnx_data",), True),
        # Other precisions must be rejected so we never pull multi-GB extras.
        ("onnx/audio_encoder_fp16.onnx_data", ("onnx/*_q4.onnx_data",), False),
        ("onnx/audio_encoder_quantized.onnx", ("onnx/*_q4.onnx",), False),
        # Nemotron root-level patterns.
        ("encoder.onnx.data", ("*.json", "*.onnx", "*.onnx.data"), True),
        ("genai_config.json", ("*.json",), True),
        ("encoder.onnx.data", ("*.onnx",), False),
        # No patterns means "take everything".
        ("anything.bin", None, True),
    ],
)
def test_matches(path, patterns, expected):
    assert ms._matches(path, patterns) is expected


def test_fallback_enabled_default_and_opt_out(monkeypatch):
    monkeypatch.delenv("STT_APP_DISABLE_MODELSCOPE", raising=False)
    assert ms.modelscope_fallback_enabled() is True
    for value in ("1", "true", "YES", "on"):
        monkeypatch.setenv("STT_APP_DISABLE_MODELSCOPE", value)
        assert ms.modelscope_fallback_enabled() is False


def test_faster_whisper_falls_back_to_modelscope(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("huggingface blocked by proxy")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", boom)
    monkeypatch.setattr(ms, "repo_available", lambda *a, **k: True)

    called = {}

    def fake_download(repo_id, cache_dir, allow_patterns=None, **kwargs):
        called["repo_id"] = repo_id
        return "/fake/snapshot"

    monkeypatch.setattr(ms, "download_faster_whisper_to_cache", fake_download)

    result = local_faster_whisper.download_model_snapshot("small")
    assert result == "/fake/snapshot"
    assert called["repo_id"] == "Systran/faster-whisper-small"


def test_onnx_falls_back_to_modelscope(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("huggingface blocked by proxy")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", boom)
    monkeypatch.setattr(ms, "repo_available", lambda *a, **k: True)

    called = {}

    def fake_download(repo_id, dest_dir, allow_patterns=None, **kwargs):
        called["repo_id"] = repo_id
        called["allow_patterns"] = allow_patterns
        return str(dest_dir)

    monkeypatch.setattr(ms, "download_repo_to_dir", fake_download)

    result = local_webgpu_asr.download_webgpu_model_snapshot(
        "nemotron-3.5-asr-streaming-0.6b-int4"
    )
    assert result.endswith("nemotron-3.5-asr-streaming-0.6b-onnx-int4")
    assert called["repo_id"] == "onnx-community/nemotron-3.5-asr-streaming-0.6b-onnx-int4"
    assert called["allow_patterns"]  # non-empty tuple was forwarded


def test_no_fallback_when_disabled(monkeypatch):
    monkeypatch.setenv("STT_APP_DISABLE_MODELSCOPE", "1")

    def boom(*args, **kwargs):
        raise OSError("huggingface blocked by proxy")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", boom)

    def must_not_call(*args, **kwargs):
        raise AssertionError("ModelScope must not be used when disabled")

    monkeypatch.setattr(ms, "repo_available", must_not_call)
    monkeypatch.setattr(ms, "download_faster_whisper_to_cache", must_not_call)

    with pytest.raises(RuntimeError):
        local_faster_whisper.download_model_snapshot("small")


def test_default_node_path_strips_surrounding_quotes(monkeypatch, tmp_path):
    # `setx STT_APP_NODE_PATH "..."` can store the literal quotes; the resolved
    # path must not include them or subprocess fails with WinError 2.
    node = tmp_path / "node.exe"
    node.write_text("")
    monkeypatch.setenv("STT_APP_NODE_PATH", f'"{node}"')
    assert local_webgpu_asr._default_node_path() == str(node)
    monkeypatch.setenv("STT_APP_NODE_PATH", str(node))
    assert local_webgpu_asr._default_node_path() == str(node)


def test_npm_beside_node(tmp_path):
    assert local_webgpu_asr._npm_beside_node(None) is None
    node = tmp_path / "node.exe"
    node.write_text("")
    assert local_webgpu_asr._npm_beside_node(str(node)) is None
    (tmp_path / "npm.cmd").write_text("")
    assert local_webgpu_asr._npm_beside_node(str(node)) == str(tmp_path / "npm.cmd")
