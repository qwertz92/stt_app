"""Every selectable model must be registered in every table that serves it.

Two defects in one session came from a model being added to some tables but not
all of them: Granite Plus demanded a file its repo does not ship (so it was
invisible and unusable), and the onnx-asr models got a new
`LOCAL_MODEL_RUNTIME` value the benchmark dispatcher did not know (so
benchmarking them always failed). A half-registered model should fail here, not
in front of the user.
"""

from __future__ import annotations

import pathlib
import re

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


def test_every_label_entry_is_either_selectable_or_marked_removed():
    """A label for a model nobody can select is either stale or a history aid.

    Granite 4.1 Plus and NAR keep entries on purpose, so a history row recorded
    with one still reads as a name. Anything else left behind here is a table
    the retirement forgot -- the exact class of half-finished change this file
    exists to catch.
    """
    unselectable = set(LOCAL_MODEL_LABELS) - set(config.VALID_MODEL_SIZES)

    assert unselectable == {
        "granite-speech-4.1-2b-plus",
        "granite-speech-4.1-2b-nar",
    }
    for model_name in unselectable:
        assert "removed" in LOCAL_MODEL_LABELS[model_name].lower(), model_name


@pytest.mark.parametrize("model_name", sorted(set(config.VALID_MODEL_SIZES)))
def test_no_selectable_model_is_labelled_as_removed(model_name: str):
    """The other direction: a retired label must never reach a live picker."""
    assert "removed" not in LOCAL_MODEL_LABELS[model_name].lower()


def test_no_picker_label_states_a_size_that_disagrees_with_the_size_table():
    """A hand-written size next to the name drifts away from the measured one.

    `MODEL_ESTIMATED_SIZE_MB` is corrected whenever a real download disagrees
    with it -- that is what drives the download percentage -- and a second
    copy inside the picker label did not follow. `distil-large-v3.5` read
    "~756 MB" against a measured 1516 and `large-v3-turbo` "~809 MB" against
    1622, so the two models a user picks between by size both understated
    themselves by half, and almost every other entry was a few percent out
    from dividing by 1000.
    """
    written = re.compile(r"~\s*([\d.]+)\s*(MB|GB)")
    checked = 0
    for model_name, label in LOCAL_MODEL_LABELS.items():
        match = written.search(label)
        if match is None:
            continue
        stated_mb = float(match.group(1)) * (1024 if match.group(2) == "GB" else 1)
        expected_mb = config.MODEL_ESTIMATED_SIZE_MB.get(model_name)
        assert expected_mb, (
            f"{model_name} states a size in its label but has no entry in "
            "MODEL_ESTIMATED_SIZE_MB"
        )
        assert abs(stated_mb - expected_mb) <= expected_mb * 0.05, (
            f"{model_name}: the label says {match.group(0)} "
            f"({stated_mb:.0f} MB) but the size table says {expected_mb} MB"
        )
        checked += 1

    assert checked >= 10, f"only {checked} labels stated a size; the scan found too few"


def test_the_offline_clone_list_covers_every_model_repository():
    """`docs/models.md`'s clone list is the only route on a blocked network.

    A model missing from it is unreachable for anyone whose proxy blocks
    Hugging Face and whose model has no ModelScope mirror. Nemotron -- the only
    local true-streaming model -- was absent while the surrounding prose
    presented the list as complete.
    """
    doc = (
        pathlib.Path(__file__).resolve().parents[1] / "docs" / "models.md"
    ).read_text(encoding="utf-8")
    listed = set(re.findall(r"git clone https://huggingface\.co/(\S+)", doc))

    missing = {
        name: repo for name, repo in config.MODEL_REPO_MAP.items() if repo not in listed
    }
    assert not missing, (
        "docs/models.md's clone list omits these repositories, so the offline "
        f"route cannot reach them: {missing}"
    )
