"""Tests for the ModelScope mirror fallback used when Hugging Face is blocked."""

from __future__ import annotations

import os
from pathlib import Path

import huggingface_hub
import pytest

from stt_app.transcriber import local_faster_whisper, local_webgpu_asr
from stt_app.transcriber import modelscope_mirror as ms


@pytest.fixture(autouse=True)
def _allow_the_mirror(monkeypatch):
    """This file tests the fallback, so the kill switch must be off.

    The suite-wide cache isolation in `conftest.py` sets
    `STT_APP_DISABLE_MODELSCOPE` because the mirror is plain urllib and does
    not read `HF_HUB_OFFLINE`; here the requests are faked, so the fallback is
    exactly what is under test.
    """
    monkeypatch.delenv("STT_APP_DISABLE_MODELSCOPE", raising=False)


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
    incomplete = destination / f"weights.bin{ms._PARTIAL_SUFFIX}"
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
    incomplete = destination / f"weights.bin{ms._PARTIAL_SUFFIX}"
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
    incomplete = destination / f"weights.bin{ms._PARTIAL_SUFFIX}"
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
    incomplete = destination / f"weights.bin{ms._PARTIAL_SUFFIX}"
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
    incomplete = destination / f"weights.bin{ms._PARTIAL_SUFFIX}"
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
    assert not (destination / f"weights.bin{ms._PARTIAL_SUFFIX}").exists()


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


def test_onnx_falls_back_to_modelscope(monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise OSError("huggingface blocked by proxy")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", boom)
    monkeypatch.setattr(ms, "repo_available", lambda *a, **k: True)

    model_name = "nemotron-3.5-asr-streaming-0.6b-int4"
    destination = tmp_path / "onnx-community" / (
        "nemotron-3.5-asr-streaming-0.6b-onnx-int4"
    )
    monkeypatch.setattr(
        local_webgpu_asr,
        "webgpu_download_destination",
        lambda *_a, **_k: destination,
    )

    called = {}

    def fake_download(repo_id, dest_dir, allow_patterns=None, **kwargs):
        called["repo_id"] = repo_id
        called["allow_patterns"] = allow_patterns
        # A mirror that answers but delivers nothing is a different case, and
        # test_onnx_fallback_rejects_a_weightless_mirror covers it. Here the
        # transfer genuinely succeeds, so produce what the layout requires.
        layout = local_webgpu_asr._MODEL_LAYOUTS[model_name]
        for relative in layout.required_files:
            target = Path(dest_dir) / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x", encoding="utf-8")
        return str(dest_dir)

    monkeypatch.setattr(ms, "download_repo_to_dir", fake_download)

    result = local_webgpu_asr.download_webgpu_model_snapshot(model_name)
    assert result.endswith("nemotron-3.5-asr-streaming-0.6b-onnx-int4")
    assert (
        called["repo_id"] == "onnx-community/nemotron-3.5-asr-streaming-0.6b-onnx-int4"
    )
    assert called["allow_patterns"]  # non-empty tuple was forwarded


def test_onnx_fallback_rejects_a_weightless_mirror(monkeypatch, tmp_path):
    """A mirror carrying only metadata must not count as a finished download.

    ModelScope hosts onnx-community/cohere-transcribe-03-2026-ONNX but its copy
    has no ``onnx/`` directory, so the fallback "succeeded" and left an
    unloadable model behind.
    """
    def boom(*args, **kwargs):
        raise OSError("huggingface blocked by proxy")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", boom)
    monkeypatch.setattr(ms, "repo_available", lambda *a, **k: True)

    destination = tmp_path / "cohere"
    monkeypatch.setattr(
        local_webgpu_asr,
        "webgpu_download_destination",
        lambda *_a, **_k: destination,
    )

    def metadata_only(repo_id, dest_dir, allow_patterns=None, **kwargs):
        for relative in ("config.json", "preprocessor_config.json",
                         "processor_config.json", "tokenizer.json"):
            target = Path(dest_dir) / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}", encoding="utf-8")
        return str(dest_dir)

    monkeypatch.setattr(ms, "download_repo_to_dir", metadata_only)

    with pytest.raises(RuntimeError, match="downloaded incompletely"):
        local_webgpu_asr.download_webgpu_model_snapshot("cohere-transcribe-03-2026")


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


def test_foreign_incomplete_is_discarded_not_resumed(monkeypatch, tmp_path):
    """A Hugging Face leftover must never be treated as a resumable transfer.

    Regression test: huggingface_hub parks aborted downloads as
    ``<name>.incomplete`` in the very same directory. Resuming one of those
    appended mirror bytes onto a foreign prefix and produced a file of exactly
    the right length whose contents were two downloads glued together.
    """
    destination = tmp_path / "models"
    destination.mkdir()
    target = destination / "weights.bin"
    foreign = destination / "weights.bin.incomplete"
    foreign.write_bytes(b"XXX")

    requests: list[dict] = []

    def _capture(url, headers=None, timeout=60):
        requests.append(dict(headers or {}))
        return _FakeResponse([b"abc", b"def"])

    monkeypatch.setattr(
        ms,
        "list_repo_files",
        lambda *_args, **_kwargs: [("weights.bin", 6)],
    )
    monkeypatch.setattr(ms, "_open", _capture)

    ms.download_repo_to_dir("org/model", destination)

    # Whole object requested: no Range header derived from the foreign prefix.
    assert requests and "Range" not in requests[0]
    assert target.read_bytes() == b"abcdef"
    assert not foreign.exists()


def test_checksum_mismatch_rejects_and_removes_partial(monkeypatch, tmp_path):
    """Size matching is not proof of a clean transfer; the digest decides."""
    destination = tmp_path / "models"
    target = destination / "weights.bin"
    partial = destination / f"weights.bin{ms._PARTIAL_SUFFIX}"

    monkeypatch.setattr(
        ms,
        "list_repo_files",
        lambda *_args, **_kwargs: [("weights.bin", 6, "0" * 64)],
    )
    monkeypatch.setattr(
        ms,
        "_open",
        lambda *_args, **_kwargs: _FakeResponse([b"abc", b"def"]),
    )

    with pytest.raises(ms.ModelScopeError, match="checksum mismatch"):
        ms.download_repo_to_dir("org/model", destination)

    # A file that failed verification must not survive as a resume candidate.
    assert not target.exists()
    assert not partial.exists()


def test_checksum_match_publishes_file(monkeypatch, tmp_path):
    import hashlib

    destination = tmp_path / "models"
    target = destination / "weights.bin"
    digest = hashlib.sha256(b"abcdef").hexdigest()

    monkeypatch.setattr(
        ms,
        "list_repo_files",
        lambda *_args, **_kwargs: [("weights.bin", 6, digest)],
    )
    monkeypatch.setattr(
        ms,
        "_open",
        lambda *_args, **_kwargs: _FakeResponse([b"abc", b"def"]),
    )

    ms.download_repo_to_dir("org/model", destination)

    assert target.read_bytes() == b"abcdef"


def test_list_repo_files_exposes_checksum(monkeypatch):
    payload = {
        "Success": True,
        "Data": {
            "Files": [
                {
                    "Type": "blob",
                    "Path": "model.bin",
                    "Size": 6,
                    "Sha256": "A" * 64,
                },
                {
                    "Type": "blob",
                    "Path": "config.json",
                    "Size": 2,
                    "Sha256": "not-a-digest",
                },
            ]
        },
    }
    monkeypatch.setattr(
        ms,
        "_open",
        lambda *_args, **_kwargs: _FakeJsonResponse(payload),
    )
    files = {entry[0]: entry[2] for entry in ms.list_repo_files("org/model")}
    assert files["model.bin"] == "a" * 64
    # A malformed digest is dropped rather than trusted.
    assert files["config.json"] is None


class _FakeJsonResponse:
    def __init__(self, payload):
        import json

        self._data = json.dumps(payload).encode("utf-8")

    def read(self, _size=-1):
        data, self._data = self._data, b""
        return data

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_partial_suffix_is_distinct_from_huggingface(tmp_path):
    """The whole fix rests on not sharing a name with huggingface_hub."""
    assert ms._PARTIAL_SUFFIX != ".incomplete"
    assert ".incomplete" in ms._FOREIGN_PARTIAL_SUFFIXES
    # And the literal name, so renaming the constant cannot silently
    # reintroduce the collision.
    assert ms._PARTIAL_SUFFIX == ".ms-part"


def test_existing_destination_with_wrong_checksum_is_replaced(monkeypatch, tmp_path):
    """A published file of the right length but wrong content must not stand.

    This is the exact production state the append bug left behind: correct
    size, wrong bytes. Trusting size alone kept it forever.
    """
    import hashlib

    destination = tmp_path / "models"
    destination.mkdir()
    target = destination / "weights.bin"
    target.write_bytes(b"XXXXXX")  # right length, wrong content
    digest = hashlib.sha256(b"abcdef").hexdigest()

    monkeypatch.setattr(
        ms,
        "list_repo_files",
        lambda *_args, **_kwargs: [("weights.bin", 6, digest)],
    )
    monkeypatch.setattr(
        ms,
        "_open",
        lambda *_args, **_kwargs: _FakeResponse([b"abc", b"def"]),
    )

    ms.download_repo_to_dir("org/model", destination)

    assert target.read_bytes() == b"abcdef"


def test_existing_destination_with_matching_checksum_is_not_refetched(
    monkeypatch, tmp_path
):
    import hashlib

    destination = tmp_path / "models"
    destination.mkdir()
    target = destination / "weights.bin"
    target.write_bytes(b"abcdef")
    digest = hashlib.sha256(b"abcdef").hexdigest()

    monkeypatch.setattr(
        ms,
        "list_repo_files",
        lambda *_args, **_kwargs: [("weights.bin", 6, digest)],
    )
    monkeypatch.setattr(
        ms,
        "_open",
        lambda *_args, **_kwargs: pytest.fail("a verified file must not refetch"),
    )

    ms.download_repo_to_dir("org/model", destination)

    assert target.read_bytes() == b"abcdef"


def test_resume_still_works_without_a_published_digest(monkeypatch, tmp_path):
    """Resume must survive for repos ModelScope publishes no checksum for.

    Dropping resume there would restart multi-GB transfers on the flaky proxied
    links that need it most.
    """
    destination = tmp_path / "models"
    destination.mkdir()
    target = destination / "weights.bin"
    partial = destination / f"weights.bin{ms._PARTIAL_SUFFIX}"
    partial.write_bytes(b"abc")

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
