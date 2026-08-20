"""Every selectable model must be registered in every table that serves it.

Two defects in one session came from a model being added to some tables but not
all of them: Granite Plus demanded a file its repo does not ship (so it was
invisible and unusable), and the onnx-asr models got a new
`LOCAL_MODEL_RUNTIME` value the benchmark dispatcher did not know (so
benchmarking them always failed). A half-registered model should fail here, not
in front of the user.
"""

from __future__ import annotations

import pytest

from stt_app import config
from stt_app.settings_dialog_helpers import LOCAL_MODEL_LABELS
from stt_app.transcriber import local_webgpu_asr


@pytest.mark.parametrize("model_name", config.VALID_MODEL_SIZES)
def test_model_is_registered_everywhere(model_name: str):
    assert model_name in config.MODEL_REPO_MAP
    assert model_name in config.LOCAL_MODEL_RUNTIME
    assert model_name in LOCAL_MODEL_LABELS
    # A missing size estimate silently degrades the progress bar to "N MB
    # cached" with no percentage.
    assert config.MODEL_ESTIMATED_SIZE_MB.get(model_name, 0) > 0
    # Every model must offer at least one language mode, or its picker is empty.
    assert config.language_modes_for_selection("local", model_name)


@pytest.mark.parametrize("model_name", config.LOCAL_ONNX_MODEL_SIZES)
def test_local_onnx_model_has_a_layout_and_a_download_destination(model_name: str):
    assert model_name in local_webgpu_asr._MODEL_LAYOUTS
    assert local_webgpu_asr.webgpu_download_destination(model_name) is not None


def test_a_model_that_cannot_stream_is_declared_batch_only():
    """`supports_streaming` drives the UI; a model whose runtime has no
    streaming path must say so or the Mode picker offers a mode that fails."""
    for model_name in config.LOCAL_WEBGPU_MODEL_SIZES + config.LOCAL_ONNX_ASR_MODEL_SIZES:
        assert config.supports_streaming("local", model_name) is False, model_name
