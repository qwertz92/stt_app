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
from decimal import Decimal

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


_WRITTEN_SIZE = re.compile(r"~\s*([\d.]+)\s*(MB|GB)")


def _label_size(label: str):
    """The size a picker label states, and the slack its format allows.

    Shared with `test_the_label_tolerance_survives_binary_floating_point` so
    the arithmetic that test pins is the arithmetic the table check runs, not
    a second copy of it.

    `Decimal`, not `float`: `float("4.03") * 1000` is 4030.0000000000005, so a
    correctly derived label for a 4025 MB model is 5.000000000000455 out and
    fails a 5 MB bound. No entry is in range today -- the largest is 3091 --
    which is why the first model that reached it would have been the one to
    find this.
    """
    match = _WRITTEN_SIZE.search(label)
    if match is None:
        return None, None, None
    is_gb = match.group(2) == "GB"
    stated_mb = Decimal(match.group(1)) * (1000 if is_gb else 1)
    return stated_mb, Decimal(5) if is_gb else Decimal(0), match.group(0)


def test_no_picker_label_states_a_size_that_disagrees_with_the_size_table():
    """A hand-written size next to the name drifts away from the measured one.

    `MODEL_ESTIMATED_SIZE_MB` is corrected whenever a real download disagrees
    with it -- that is what drives the download percentage -- and a second
    copy inside the picker label did not follow. `distil-large-v3.5` read
    "~756 MB" against a measured 1516 and `large-v3-turbo` "~809 MB" against
    1622, so the two models a user picks between by size both understated
    themselves by half.

    **The GB conversion here is decimal, and that is the point of the test.**
    `MODEL_ESTIMATED_SIZE_MB` states its unit ("decimal megabytes (MB), not
    MiB") and `model_download_progress` converts it with `* 1_000_000`, so a
    label is only consistent with the bar the user watches if it divides by
    1000. Dividing by 1024 here once made the test agree with a label that
    divided by 1024 too, so the pair was self-consistent and wrong: it passed
    while `large-v3` advertised "~3.0 GB" for a 3.09 GB download.

    The tolerance follows the format the label actually uses, because that is
    the only slack the derivation can introduce. Below 1000 MB the label
    restates the table's integer, so it must match *exactly*; above it, two
    decimals of GB can move the stated value by at most 5 MB (0.005 GB).
    Nothing looser is defensible: `tiny` was once hand-written at 75 MB
    against a real 78, and 5%, a flat 5 MB and `max(5, 0.5%)` all accept that
    -- only the exact comparison below rejects it. `max(5, 0.5%)` was also
    looser than the 5 MB it was meant to express for every GB model, reaching
    15.5 MB for `large-v3`.

    Measured against the current table: every MB label is exact and the worst
    GB label (`distil-large-v3.5`) is 4 MB out, so both bounds are real
    constraints rather than decoration.
    """
    checked = set()
    for model_name, label in LOCAL_MODEL_LABELS.items():
        stated_mb, tolerance_mb, written_size = _label_size(label)
        if stated_mb is None:
            continue
        expected_mb = config.MODEL_ESTIMATED_SIZE_MB.get(model_name)
        assert expected_mb, (
            f"{model_name} states a size in its label but has no entry in "
            "MODEL_ESTIMATED_SIZE_MB"
        )
        assert abs(stated_mb - expected_mb) <= tolerance_mb, (
            f"{model_name}: the label says {written_size} "
            f"({stated_mb:.0f} MB) but the size table says {expected_mb} MB"
        )
        checked.add(model_name)

    # Not a floor: every model the table sizes must actually carry that size
    # in its label. A count threshold passed while three of the thirteen
    # silently stopped stating one.
    #
    # Retired models are not filtered out here. They are already excluded by
    # having no size entry -- verified: the two labels reading "(removed)"
    # are absent from `MODEL_ESTIMATED_SIZE_MB` -- so a `"removed" not in
    # label` filter excluded nothing and only served to *hide* the case worth
    # catching, a retirement that left the size entry behind.
    sized = {
        name for name in config.MODEL_ESTIMATED_SIZE_MB if name in LOCAL_MODEL_LABELS
    }
    assert checked == sized, (
        f"labels stating no size for a model the table sizes: {sorted(sized - checked)}"
    )


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


@pytest.mark.parametrize(
    ("count", "expects"),
    [
        (1, "These folders will be deleted"),
        (8, "These folders will be deleted"),
        (9, "9 folders will be deleted"),
        (52, "52 folders will be deleted"),
    ],
)
def test_the_delete_confirmation_stays_bounded(count: int, expects: str):
    """A `QMessageBox` does not scroll, and a long path does not wrap.

    The Local tab uses `ExtendedSelection`, so "every installed model" is one
    Ctrl+A away: 13 models x up to 4 cache folders each is 52 lines sized to
    the longest absolute path, i.e. a dialog taller than the screen with its
    Yes/No buttons off the bottom. Below the cap every folder is still named,
    because saying *which disk* is the whole reason the list exists.
    """
    from stt_app.settings_dialog_local import (
        _MAX_LISTED_DELETE_FOLDERS,
        _describe_doomed_folders,
    )

    doomed = [
        rf"C:\Users\someone\AppData\Local\models\root{index % 3}\models--org--model-{index}"
        for index in range(count)
    ]
    text = _describe_doomed_folders(doomed)

    assert expects in text
    lines = text.strip().splitlines()
    # One heading plus the entries. Above the cap the entries are parent
    # directories, of which there are three here however many models there are.
    assert len(lines) <= _MAX_LISTED_DELETE_FOLDERS + 2, (
        f"{count} folders produced {len(lines)} lines:\n{text}"
    )
    if count <= _MAX_LISTED_DELETE_FOLDERS:
        for path in doomed:
            assert path in text


def test_the_delete_confirmation_says_nothing_when_there_is_nothing_to_say():
    from stt_app.settings_dialog_local import _describe_doomed_folders

    assert _describe_doomed_folders([]) == ""


def test_the_label_tolerance_survives_binary_floating_point():
    """A correct label must not fail the bound because of the parse.

    `float("4.03") * 1000` is 4030.0000000000005 -- 5.000000000000455 away
    from 4025, which a 5 MB bound rejects. The label is right; the arithmetic
    was wrong. Nothing in the table reaches that size today, so this pins the
    comparison itself rather than any current row, through the same helper the
    table check uses.
    """
    for megabytes in (4025, 4065, 2128, 3091, 1029):
        label = f"model (~{megabytes / 1000:.2f} GB, ONNX)"
        stated_mb, tolerance_mb, written_size = _label_size(label)
        assert stated_mb is not None, label
        assert abs(stated_mb - megabytes) <= tolerance_mb, (
            f"{written_size} is a correct two-decimal label for "
            f"{megabytes} MB but was rejected: stated {stated_mb}"
        )
