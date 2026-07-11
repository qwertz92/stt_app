"""Tests for the ModelScope mirror fallback used when Hugging Face is blocked."""

from __future__ import annotations

import os

import huggingface_hub
import pytest

from stt_app.transcriber import local_faster_whisper, local_webgpu_asr
from stt_app.transcriber import modelscope_mirror as ms


class _FakeResponse:
    def __init__(self, chunks, *, status=200, headers=None):
        self._chunks = iter(chunks)
        self.status = status
        self.headers = headers or {}

    def read(self, _size=-1):
        item = next(self._chunks, b"")
        if isinstance(item, BaseException):
            raise item
        return item

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


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


def test_modelscope_endpoint_must_use_https(monkeypatch):
    monkeypatch.setattr(ms, "MODELSCOPE_ENDPOINT", "http://mirror.invalid")

    with pytest.raises(ms.ModelScopeError, match="HTTPS"):
        ms._api_files_url("org/model", "master")


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "../escape.bin",
        "folder/../../escape.bin",
        "/absolute.bin",
        "C:/absolute.bin",
        r"C:\absolute.bin",
        r"..\escape.bin",
        r"folder\..\escape.bin",
        r"\\server\share\escape.bin",
        "folder//file.bin",
        "./file.bin",
    ],
)
def test_download_rejects_unsafe_server_paths(monkeypatch, tmp_path, unsafe_path):
    monkeypatch.setattr(
        ms,
        "list_repo_files",
        lambda *_args, **_kwargs: [(unsafe_path, 3)],
    )
    monkeypatch.setattr(
        ms,
        "_open",
        lambda *_args, **_kwargs: pytest.fail("unsafe paths must fail before I/O"),
    )

    with pytest.raises(ms.ModelScopeError, match="Unsafe ModelScope repository path"):
        ms.download_repo_to_dir("org/model", tmp_path / "models")

    assert not (tmp_path / "escape.bin").exists()


def test_download_completes_via_incomplete_then_atomic_replace(monkeypatch, tmp_path):
    destination = tmp_path / "models"
    target = destination / "weights.bin"
    incomplete = destination / "weights.bin.incomplete"
    monkeypatch.setattr(
        ms,
        "list_repo_files",
        lambda *_args, **_kwargs: [("weights.bin", 6)],
    )
    monkeypatch.setattr(
        ms,
        "_open",
        lambda *_args, **_kwargs: _FakeResponse([b"abc", b"def"]),
    )
    replace_calls = []
    real_replace = os.replace

    def recording_replace(source, destination_path):
        replace_calls.append((source, destination_path))
        return real_replace(source, destination_path)

    monkeypatch.setattr(ms.os, "replace", recording_replace)

    assert ms.download_repo_to_dir("org/model", destination) == str(destination)

    assert target.read_bytes() == b"abcdef"
    assert not incomplete.exists()
    assert (incomplete.resolve(), target.resolve()) in [
        (source.resolve(), destination_path.resolve())
        for source, destination_path in replace_calls
    ]


def test_interrupted_download_retains_only_resumable_incomplete_file(
    monkeypatch,
    tmp_path,
):
    destination = tmp_path / "models"
    target = destination / "weights.bin"
    incomplete = destination / "weights.bin.incomplete"
    monkeypatch.setattr(
        ms,
        "list_repo_files",
        lambda *_args, **_kwargs: [("weights.bin", 6)],
    )
    responses = [
        _FakeResponse([b"abc", OSError("connection lost")]),
        _FakeResponse(
            [b"def"],
            status=206,
            headers={"Content-Range": "bytes 3-5/6"},
        ),
    ]
    seen_headers = []

    def fake_open(_url, headers=None, **_kwargs):
        seen_headers.append(dict(headers or {}))
        return responses.pop(0)

    monkeypatch.setattr(ms, "_open", fake_open)

    with pytest.raises(ms.ModelScopeError, match="connection lost"):
        ms.download_repo_to_dir("org/model", destination)

    assert not target.exists()
    assert incomplete.read_bytes() == b"abc"

    ms.download_repo_to_dir("org/model", destination)

    assert target.read_bytes() == b"abcdef"
    assert not incomplete.exists()
    assert seen_headers == [{}, {"Range": "bytes=3-"}]


def test_resume_restarts_when_server_ignores_range(monkeypatch, tmp_path):
    destination = tmp_path / "models"
    destination.mkdir()
    incomplete = destination / "weights.bin.incomplete"
    incomplete.write_bytes(b"abc")
    monkeypatch.setattr(
        ms,
        "list_repo_files",
        lambda *_args, **_kwargs: [("weights.bin", 6)],
    )
    seen_headers = []

    def fake_open(_url, headers=None, **_kwargs):
        seen_headers.append(dict(headers or {}))
        return _FakeResponse([b"abcdef"], status=200)

    monkeypatch.setattr(ms, "_open", fake_open)

    ms.download_repo_to_dir("org/model", destination)

    assert (destination / "weights.bin").read_bytes() == b"abcdef"
    assert not incomplete.exists()
    assert seen_headers == [{"Range": "bytes=3-"}]


def test_resume_rejects_mismatched_content_range(monkeypatch, tmp_path):
    destination = tmp_path / "models"
    destination.mkdir()
    incomplete = destination / "weights.bin.incomplete"
    incomplete.write_bytes(b"abc")
    monkeypatch.setattr(
        ms,
        "list_repo_files",
        lambda *_args, **_kwargs: [("weights.bin", 6)],
    )
    monkeypatch.setattr(
        ms,
        "_open",
        lambda *_args, **_kwargs: _FakeResponse(
            [b"def"],
            status=206,
            headers={"Content-Range": "bytes 2-4/6"},
        ),
    )

    with pytest.raises(ms.ModelScopeError, match="requested byte range"):
        ms.download_repo_to_dir("org/model", destination)

    assert incomplete.read_bytes() == b"abc"
    assert not (destination / "weights.bin").exists()


def test_resume_rolls_back_body_that_disagrees_with_content_range(
    monkeypatch,
    tmp_path,
):
    destination = tmp_path / "models"
    destination.mkdir()
    incomplete = destination / "weights.bin.incomplete"
    incomplete.write_bytes(b"abc")
    monkeypatch.setattr(
        ms,
        "list_repo_files",
        lambda *_args, **_kwargs: [("weights.bin", 6)],
    )
    monkeypatch.setattr(
        ms,
        "_open",
        lambda *_args, **_kwargs: _FakeResponse(
            [b"de"],
            status=206,
            headers={"Content-Range": "bytes 3-5/6"},
        ),
    )

    with pytest.raises(ms.ModelScopeError, match="does not match Content-Range"):
        ms.download_repo_to_dir("org/model", destination)

    assert incomplete.read_bytes() == b"abc"
    assert not (destination / "weights.bin").exists()


def test_legacy_partial_final_name_is_migrated_before_resume(monkeypatch, tmp_path):
    destination = tmp_path / "models"
    destination.mkdir()
    target = destination / "weights.bin"
    target.write_bytes(b"abc")
    monkeypatch.setattr(
        ms,
        "list_repo_files",
        lambda *_args, **_kwargs: [("weights.bin", 6)],
    )
    monkeypatch.setattr(
        ms,
        "_open",
        lambda *_args, **_kwargs: _FakeResponse(
            [b"def"],
            status=206,
            headers={"Content-Range": "bytes 3-5/6"},
        ),
    )

    ms.download_repo_to_dir("org/model", destination)

    assert target.read_bytes() == b"abcdef"
    assert not (destination / "weights.bin.incomplete").exists()


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
    assert (
        called["repo_id"] == "onnx-community/nemotron-3.5-asr-streaming-0.6b-onnx-int4"
    )
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
