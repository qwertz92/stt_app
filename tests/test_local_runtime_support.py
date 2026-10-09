"""Running on a platform where faster-whisper's CTranslate2 cannot be imported.

Windows ARM64 has no CTranslate2 build (`pyproject.toml`, `[tool.uv]`). Nothing
here runs on ARM: every test makes the answer of `find_spec` say what that
platform would, which is exactly the one input the app reads.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from PySide6 import QtWidgets
from test_settings_dialog_regions import _dialog

from stt_app import local_benchmark, local_runtime_support
from stt_app.config import (
    DEFAULT_MODEL_SIZE,
    FASTER_WHISPER_MODEL_SIZES,
    LOCAL_ONNX_MODEL_SIZES,
)
from stt_app.settings_dialog_helpers import local_model_label
from stt_app.settings_store import AppSettings
from stt_app.transcriber import local_faster_whisper
from stt_app.transcriber.base import TranscriptionError
from stt_app.transcriber.local_faster_whisper import LocalFasterWhisperTranscriber

_SRC = Path(__file__).resolve().parents[1] / "src"
_FOUND = object()
_REASON = "The Whisper models cannot run on this test machine."


# Held from import time: a test replaces the module attribute, and the cache
# has to be cleared on the real function whatever a test left in its place.
_REAL_ANSWER = local_runtime_support.faster_whisper_unavailable_reason


@pytest.fixture(autouse=True)
def _forget_the_cached_answer():
    _REAL_ANSWER.cache_clear()
    yield
    _REAL_ANSWER.cache_clear()


def _modules_found(monkeypatch, *, missing: tuple[str, ...] = ()) -> None:
    monkeypatch.setattr(
        local_runtime_support.importlib.util,
        "find_spec",
        lambda name: None if name in missing else _FOUND,
    )


def _arm64(monkeypatch, is_arm64: bool) -> None:
    monkeypatch.setattr(local_runtime_support, "_is_windows_arm64", lambda: is_arm64)


def _without_the_runtime(monkeypatch) -> None:
    """Every faster-whisper model answers with `_REASON`; nothing else does."""
    monkeypatch.setattr(
        local_runtime_support, "faster_whisper_unavailable_reason", lambda: _REASON
    )


def test_both_packages_present_means_available(monkeypatch):
    _modules_found(monkeypatch)
    assert local_runtime_support.faster_whisper_unavailable_reason() is None


def test_a_missing_ctranslate2_on_windows_arm64_says_it_has_no_build(monkeypatch):
    _modules_found(monkeypatch, missing=("ctranslate2",))
    _arm64(monkeypatch, True)
    reason = local_runtime_support.faster_whisper_unavailable_reason()
    assert reason is not None
    assert "no Windows ARM64 build" in reason
    assert "cloud engine" in reason


def test_a_missing_package_elsewhere_says_to_install_it(monkeypatch):
    _modules_found(monkeypatch, missing=("ctranslate2",))
    _arm64(monkeypatch, False)
    reason = local_runtime_support.faster_whisper_unavailable_reason()
    assert reason is not None
    assert "'ctranslate2'" in reason and "uv sync" in reason
    assert "ARM64" not in reason


def test_a_missing_faster_whisper_is_named(monkeypatch):
    _modules_found(monkeypatch, missing=("faster_whisper",))
    _arm64(monkeypatch, True)
    reason = local_runtime_support.faster_whisper_unavailable_reason()
    assert reason is not None
    assert "'faster_whisper'" in reason


def test_a_lookup_that_fails_does_not_lock_the_runtime_out(monkeypatch):
    # `find_spec` raises ValueError for a module whose `__spec__` is None. That
    # is not "missing": the import itself would report the real error, and
    # answering "unavailable" here would refuse a runtime that may work.
    def _raises(_name):
        raise ValueError("ctranslate2.__spec__ is None")

    monkeypatch.setattr(local_runtime_support.importlib.util, "find_spec", _raises)
    assert local_runtime_support.faster_whisper_unavailable_reason() is None


def test_only_faster_whisper_models_are_ever_unavailable(monkeypatch):
    _without_the_runtime(monkeypatch)
    for name in FASTER_WHISPER_MODEL_SIZES:
        assert local_runtime_support.unavailable_reason(name) == _REASON
    for name in LOCAL_ONNX_MODEL_SIZES:
        assert local_runtime_support.unavailable_reason(name) is None
    assert local_runtime_support.unavailable_reason("a-model-from-the-future") is None


def test_the_default_model_does_not_depend_on_ctranslate2(monkeypatch):
    # A first run on ARM64 starts with the default model and nothing stored,
    # so the default must be a runtime that platform has.
    _without_the_runtime(monkeypatch)
    assert local_runtime_support.unavailable_reason(DEFAULT_MODEL_SIZE) is None


def test_a_picker_row_of_an_unavailable_model_is_marked(monkeypatch):
    _without_the_runtime(monkeypatch)
    assert local_model_label("small").endswith(
        local_runtime_support.UNAVAILABLE_LABEL_SUFFIX
    )
    assert not local_model_label(DEFAULT_MODEL_SIZE).endswith(
        local_runtime_support.UNAVAILABLE_LABEL_SUFFIX
    )


def test_a_picker_row_is_unmarked_where_the_runtime_exists(monkeypatch):
    _modules_found(monkeypatch)
    for name in FASTER_WHISPER_MODEL_SIZES:
        assert local_runtime_support.UNAVAILABLE_LABEL_SUFFIX not in (
            local_model_label(name)
        )


def test_a_transcription_refuses_before_it_downloads_anything(monkeypatch):
    _without_the_runtime(monkeypatch)
    downloads: list[str] = []
    monkeypatch.setattr(
        LocalFasterWhisperTranscriber,
        "_coordinated_download_if_missing",
        lambda self: downloads.append(self.model_size),
    )
    transcriber = LocalFasterWhisperTranscriber(model_size="small")

    with pytest.raises(TranscriptionError, match="cannot run on this test machine"):
        transcriber.transcribe_batch(b"\x00\x00" * 1600)
    with pytest.raises(TranscriptionError, match="cannot run on this test machine"):
        transcriber.preload_model()

    assert downloads == []
    assert not transcriber.is_model_loaded


def test_a_factory_handed_in_by_the_caller_is_not_second_guessed(monkeypatch):
    # The factory supplies its own runtime, so the environment's packages say
    # nothing about it. This is also what keeps every fake-model test of the
    # suite independent of what is installed on the machine running it.
    _without_the_runtime(monkeypatch)
    monkeypatch.setattr(
        LocalFasterWhisperTranscriber,
        "_coordinated_download_if_missing",
        lambda s: None,
    )
    marker = object()
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small", model_factory=lambda *_a, **_k: marker
    )
    assert transcriber._ensure_model() is marker


def test_the_default_factory_is_what_the_guard_recognises():
    # The guard compares against this name; a rename that left the guard
    # comparing against nothing would silently drop it.
    assert (
        LocalFasterWhisperTranscriber(model_size="small")._model_factory
        is local_faster_whisper._default_model_factory
    )


def test_a_benchmark_case_reports_the_reason_not_a_missing_module(monkeypatch):
    _without_the_runtime(monkeypatch)
    cases = local_benchmark.run_benchmark_cases(
        audio_path="unused.wav", model_names=["small"]
    )
    assert len(cases) == 1
    assert cases[0].error == _REASON
    assert not cases[0].runs


def test_the_silero_graph_is_still_found_when_ctranslate2_cannot_be_imported(
    tmp_path,
):
    """The ARM64 install keeps faster-whisper but not CTranslate2, and the
    speech check reads the graph faster-whisper ships. Checked in a fresh
    interpreter where importing ctranslate2 fails the way it would there."""
    env = {
        **os.environ,
        "PYTHONPATH": str(_SRC),
        "APPDATA": str(tmp_path),
        "LOCALAPPDATA": str(tmp_path),
        "HF_HUB_OFFLINE": "1",
    }
    code = (
        "import sys\n"
        "sys.modules['ctranslate2'] = None\n"
        "from stt_app import silero_vad\n"
        "session = silero_vad._build_session()\n"
        "print(type(session).__name__)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "InferenceSession"


@pytest.fixture
def _qt_app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _combo_row(combo, value: str) -> str:
    return combo.itemText(combo.findData(value))


def test_the_settings_dialog_marks_explains_and_never_downloads_them(
    tmp_path, monkeypatch, _qt_app
):
    _without_the_runtime(monkeypatch)
    dialog, _store, _app = _dialog(tmp_path, AppSettings(model_size="small"))
    started: list[list[str]] = []
    monkeypatch.setattr(
        dialog, "_start_local_model_download", lambda names: started.append(names)
    )

    # The stored selection is still there as itself, marked, and the note
    # under the picker says why instead of describing a working model.
    assert str(dialog.model_combo.currentData()) == "small"
    assert _combo_row(dialog.model_combo, "small").endswith("[unavailable]")
    assert dialog.local_model_runtime_warning_label.text() == _REASON

    # The Models tab says so on the row, and no download button reaches them.
    dialog._refresh_local_models_list([])
    rows = {
        dialog.local_models_list.item(i).data(0x100): dialog.local_models_list.item(i)
        for i in range(dialog.local_models_list.count())
    }
    assert "cannot run on this PC" in rows["small"].text()
    assert _REASON in rows["small"].toolTip()
    assert "cannot run on this PC" not in rows[DEFAULT_MODEL_SIZE].text()

    missing = dialog._missing_downloadable_models()
    assert DEFAULT_MODEL_SIZE in missing
    assert not set(missing) & set(FASTER_WHISPER_MODEL_SIZES)

    rows["small"].setSelected(True)
    dialog._download_selected_local_models()
    assert started == []
    assert dialog.local_models_action_label.text() == _REASON

    dialog.close()
    dialog.deleteLater()


def test_the_lock_leaves_ctranslate2_out_of_a_windows_arm64_install():
    """Without the `[tool.uv]` override, `uv sync` on Windows ARM64 stops at
    ctranslate2 (no wheel, no sdist) and installs nothing at all. The marker on
    faster-whisper's ctranslate2 edge is what the sync reads."""
    root = _SRC.parent
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    overrides = project["tool"]["uv"]["override-dependencies"]
    assert any(line.startswith("ctranslate2") and "ARM64" in line for line in overrides)

    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    faster_whisper = next(
        package for package in lock["package"] if package["name"] == "faster-whisper"
    )
    edge = next(
        dependency
        for dependency in faster_whisper["dependencies"]
        if dependency["name"] == "ctranslate2"
    )
    assert "ARM64" in edge.get("marker", "")
