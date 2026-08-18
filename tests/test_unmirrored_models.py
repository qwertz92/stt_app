"""Behaviour that only shows up on a network which blocks Hugging Face."""

from __future__ import annotations

import pytest

from stt_app.config import (
    CANARY_MODEL_SIZE,
    MODEL_REPO_MAP,
    MODELS_WITHOUT_MODELSCOPE_MIRROR,
    PARAKEET_MODEL_SIZE,
)
from stt_app.transcriber import local_webgpu_asr
from stt_app.transcriber.local_faster_whisper import format_model_download_error


def test_unmirrored_models_are_named_not_guessed():
    """Every entry must be a model the app can actually offer."""
    for name in MODELS_WITHOUT_MODELSCOPE_MIRROR:
        assert name in MODEL_REPO_MAP, f"{name} is not a known model"


@pytest.mark.parametrize(
    "model_name",
    sorted(MODELS_WITHOUT_MODELSCOPE_MIRROR),
)
def test_unmirrored_download_error_does_not_blame_the_connection(model_name):
    """The old wording sent people to debug the one thing that was fine."""
    message = format_model_download_error(model_name, RuntimeError("hub is gone"))
    assert "no ModelScope mirror" in message
    assert "internet connection" not in message
    # It must say what to do instead of only what broke.
    assert "Model Dir" in message or "unrestricted machine" in message


def test_mirrored_model_keeps_the_generic_error():
    message = format_model_download_error("small", RuntimeError("hub is gone"))
    assert "no ModelScope mirror" not in message
    assert "small" in message


@pytest.mark.parametrize("model_name", [PARAKEET_MODEL_SIZE, CANARY_MODEL_SIZE])
def test_onnx_asr_models_report_the_missing_mirror(monkeypatch, tmp_path, model_name):
    """The onnx-asr repos have no ModelScope counterpart.

    Regression test: this path used to end in the generic "check your internet
    connection" text, which is exactly wrong when the proxy denies the whole
    Generative-AI category and no mirror exists.
    """
    from stt_app.transcriber import modelscope_mirror as ms

    # ModelScope is reachable but does not host the repo.
    monkeypatch.setattr(ms, "repo_available", lambda *_a, **_k: False)
    monkeypatch.setattr(ms, "modelscope_fallback_enabled", lambda: True)

    repo_id = MODEL_REPO_MAP[model_name]
    with pytest.raises(RuntimeError) as excinfo:
        local_webgpu_asr._download_onnx_via_modelscope(
            repo_id,
            tmp_path,
            ("*.onnx",),
            RuntimeError("hub unreachable"),
            model_name,
        )
    assert "no ModelScope mirror" in str(excinfo.value)


def test_mirrored_onnx_model_keeps_repo_oriented_error(monkeypatch, tmp_path):
    from stt_app.transcriber import modelscope_mirror as ms

    monkeypatch.setattr(ms, "repo_available", lambda *_a, **_k: False)
    monkeypatch.setattr(ms, "modelscope_fallback_enabled", lambda: True)

    repo_id = MODEL_REPO_MAP["cohere-transcribe-03-2026"]
    with pytest.raises(RuntimeError) as excinfo:
        local_webgpu_asr._download_onnx_via_modelscope(
            repo_id,
            tmp_path,
            ("*.onnx",),
            RuntimeError("hub unreachable"),
            "cohere-transcribe-03-2026",
        )
    assert "no ModelScope mirror" not in str(excinfo.value)
    assert repo_id in str(excinfo.value)


def test_download_is_not_called_finished_without_the_weights(tmp_path):
    """A metadata-only mirror must not pass as a completed download.

    ModelScope's copy of the cohere repo carries the JSON and the tokenizer but
    no ``onnx/`` directory. The transfer used to report success and the model
    then failed much later at load time with an error that said nothing about
    the download.
    """
    layout = local_webgpu_asr._MODEL_LAYOUTS["cohere-transcribe-03-2026"]
    # Everything the mirror does carry, and nothing it does not.
    for relative in ("config.json", "preprocessor_config.json",
                     "processor_config.json", "tokenizer.json"):
        (tmp_path / relative).write_text("{}", encoding="utf-8")

    with pytest.raises(RuntimeError) as excinfo:
        local_webgpu_asr._verify_downloaded_layout(
            "cohere-transcribe-03-2026",
            MODEL_REPO_MAP["cohere-transcribe-03-2026"],
            tmp_path,
            layout,
        )
    message = str(excinfo.value)
    assert "downloaded incompletely" in message
    assert "onnx/encoder_model_q4.onnx" in message
    # It must point at the repo that came up short, not at the network.
    assert MODEL_REPO_MAP["cohere-transcribe-03-2026"] in message


def test_complete_download_passes_verification(tmp_path):
    layout = local_webgpu_asr._MODEL_LAYOUTS["cohere-transcribe-03-2026"]
    for relative in layout.required_files:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")

    local_webgpu_asr._verify_downloaded_layout(
        "cohere-transcribe-03-2026",
        MODEL_REPO_MAP["cohere-transcribe-03-2026"],
        tmp_path,
        layout,
    )


def test_missing_file_list_is_summarised_not_dumped(tmp_path):
    """An empty directory must not produce an unreadable wall of paths."""
    layout = local_webgpu_asr._MODEL_LAYOUTS["cohere-transcribe-03-2026"]
    with pytest.raises(RuntimeError) as excinfo:
        local_webgpu_asr._verify_downloaded_layout(
            "cohere-transcribe-03-2026",
            MODEL_REPO_MAP["cohere-transcribe-03-2026"],
            tmp_path,
            layout,
        )
    assert "more" in str(excinfo.value)
