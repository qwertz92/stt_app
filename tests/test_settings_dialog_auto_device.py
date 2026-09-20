"""The benchmark-driven "Auto" device: what it saves, and what it says."""

from __future__ import annotations

import dataclasses
import math

import pytest
from PySide6 import QtCore, QtWidgets

from stt_app.local_benchmark import BenchmarkCase, BenchmarkRun
from stt_app.settings_dialog import SettingsDialog
from stt_app.settings_dialog_helpers import BENCHMARK_GPU_CPU_COMPARISON_LABEL
from stt_app.settings_store import AppSettings, SettingsStore

_COHERE = "cohere-transcribe-03-2026"
_GRANITE = "granite-speech-4.1-2b"
_NEMOTRON = "nemotron-3.5-asr-streaming-0.6b-int4"


class _SecretStore:
    def get_api_key(self, _provider: str) -> None:
        return None


class _Logger:
    def diagnostics_text(self) -> str:
        return ""


def _dialog(store) -> tuple[SettingsDialog, QtWidgets.QApplication]:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = SettingsDialog(
        settings_store=store,
        secret_store=_SecretStore(),
        app_logger=_Logger(),
    )
    return dialog, app


def _real_store(tmp_path, settings: AppSettings) -> SettingsStore:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(settings)
    return store


def _case(model: str, device: str, rtf: float) -> BenchmarkCase:
    return BenchmarkCase(
        model=model,
        device=device,
        compute_type="onnx-q4",
        download_seconds=0.0,
        load_seconds=1.0,
        runs=[
            BenchmarkRun(
                run_index=1,
                seconds=rtf * 10.0,
                audio_duration_seconds=10.0,
                real_time_factor=rtf,
                transcript_chars=4,
                transcript_words=1,
                detected_language="de",
                language_probability=math.nan,
            )
        ],
        error=None,
    )


def _cohere_measured_on_cpu() -> list[BenchmarkCase]:
    return [_case(_COHERE, "webgpu", 0.30), _case(_COHERE, "cpu", 0.10)]


# --- The save path -----------------------------------------------------------


def test_an_untouched_save_keeps_the_measured_devices(tmp_path):
    """`_construct_settings_from_widgets` names every field, and this one has
    no widget: left at its dataclass default it differs from the populated
    baseline, counts as an edit, and erases the measurement on every Save."""
    store = _real_store(
        tmp_path, AppSettings(onnx_auto_preferred_devices={_COHERE: "cpu"})
    )
    dialog, app = _dialog(store)

    dialog._save()

    assert store.load().onnx_auto_preferred_devices == {_COHERE: "cpu"}
    assert dialog._save_status_label.text() == "No settings changes", (
        "a field with no widget was read as an edit and rewrote the file"
    )
    dialog.deleteLater()
    _ = app


def test_a_save_of_another_field_keeps_the_measured_devices(tmp_path):
    store = _real_store(
        tmp_path, AppSettings(onnx_auto_preferred_devices={_COHERE: "cpu"})
    )
    dialog, app = _dialog(store)

    dialog.completion_beep_checkbox.setChecked(
        not dialog.completion_beep_checkbox.isChecked()
    )
    dialog._save()

    assert store.load().onnx_auto_preferred_devices == {_COHERE: "cpu"}
    assert "saved" in dialog._save_status_label.text().lower()
    dialog.deleteLater()
    _ = app


def test_a_benchmark_run_and_a_later_save_in_one_dialog_session_agree(tmp_path):
    """The real sequence: the dialog is opened with nothing measured, a run
    finishes and writes the map through the benchmark path, and the user then
    saves something unrelated. The write and the save read one baseline, or the
    save undoes the run."""
    store = _real_store(tmp_path, AppSettings(model_size=_COHERE, engine="local"))
    dialog, app = _dialog(store)

    dialog._apply_measured_onnx_devices(_cohere_measured_on_cpu())
    dialog.tray_middle_click_checkbox.setChecked(
        not dialog.tray_middle_click_checkbox.isChecked()
    )
    dialog._save()

    assert store.load().onnx_auto_preferred_devices == {_COHERE: "cpu"}
    assert store.load().tray_middle_click_toggle is (
        dialog.tray_middle_click_checkbox.isChecked()
    )
    dialog.deleteLater()
    _ = app


# --- Applying a finished run -------------------------------------------------


def test_a_finished_run_stores_the_device_it_measured_as_fastest(tmp_path):
    store = _real_store(tmp_path, AppSettings(model_size=_COHERE, engine="local"))
    dialog, app = _dialog(store)
    emitted: list[int] = []
    dialog.settings_changed.connect(lambda: emitted.append(1))

    sentence = dialog._apply_measured_onnx_devices(_cohere_measured_on_cpu())

    assert store.load().onnx_auto_preferred_devices == {_COHERE: "cpu"}
    # The dialog's own two snapshots move with the file, or the next Save reads
    # the write as an edit to undo.
    assert dialog._loaded_settings.onnx_auto_preferred_devices == {_COHERE: "cpu"}
    assert dialog._populated_settings.onnx_auto_preferred_devices == {_COHERE: "cpu"}
    assert emitted == [1]
    assert "CPU" in sentence
    assert _COHERE in sentence
    # And the note under the picker follows without reopening the dialog.
    assert "Auto starts with CPU" in dialog.local_onnx_device_note_label.text()
    dialog.deleteLater()
    _ = app


def test_a_run_that_measured_nothing_comparable_writes_nothing(tmp_path):
    store = _real_store(tmp_path, AppSettings(model_size=_COHERE, engine="local"))
    dialog, app = _dialog(store)
    emitted: list[int] = []
    dialog.settings_changed.connect(lambda: emitted.append(1))

    assert dialog._apply_measured_onnx_devices([_case(_COHERE, "cpu", 0.1)]) == ""

    assert store.load().onnx_auto_preferred_devices == {}
    assert emitted == []
    dialog.deleteLater()
    _ = app


def test_repeating_a_run_with_the_same_outcome_writes_nothing(tmp_path):
    """A benchmark is re-run to confirm a result, and an unchanged map must not
    rewrite `settings.json` or make the controller reload the model."""
    store = _real_store(
        tmp_path,
        AppSettings(
            model_size=_COHERE,
            engine="local",
            onnx_auto_preferred_devices={_COHERE: "cpu"},
        ),
    )
    dialog, app = _dialog(store)
    emitted: list[int] = []
    dialog.settings_changed.connect(lambda: emitted.append(1))

    assert dialog._apply_measured_onnx_devices(_cohere_measured_on_cpu()) == ""
    assert emitted == []
    dialog.deleteLater()
    _ = app


def test_a_result_for_another_model_is_reported_by_count(tmp_path):
    """The sentence sits on the benchmark status line, which is about the run
    -- so it names the device only when the *selected* model's order moved."""
    store = _real_store(tmp_path, AppSettings(model_size="small", engine="local"))
    dialog, app = _dialog(store)

    sentence = dialog._apply_measured_onnx_devices(_cohere_measured_on_cpu())

    assert store.load().onnx_auto_preferred_devices == {_COHERE: "cpu"}
    assert "1 model" in sentence
    assert "CPU" not in sentence
    dialog.deleteLater()
    _ = app


def _cohere_measured_on_webgpu() -> list[BenchmarkCase]:
    return [_case(_COHERE, "webgpu", 0.08), _case(_COHERE, "cpu", 0.45)]


def test_a_run_that_keeps_the_first_device_does_not_claim_a_change(tmp_path):
    """WebGPU measured and WebGPU kept is the usual outcome on a machine with a
    working GPU. The map gains an entry, so the note can say the order was
    measured -- but nothing was reordered, and "now starts with" would be
    false."""
    store = _real_store(tmp_path, AppSettings(model_size=_COHERE, engine="local"))
    dialog, app = _dialog(store)

    sentence = dialog._apply_measured_onnx_devices(_cohere_measured_on_webgpu())

    assert store.load().onnx_auto_preferred_devices == {_COHERE: "webgpu"}
    assert "now starts" not in sentence
    assert "keeps WebGPU first" in sentence
    dialog.deleteLater()
    _ = app


def test_a_run_that_moves_auto_back_to_the_default_device_says_so(tmp_path):
    """The other direction of the same rule: a stored CPU preference that a
    newer run overturns really does move the first device."""
    store = _real_store(
        tmp_path,
        AppSettings(
            model_size=_COHERE,
            engine="local",
            onnx_auto_preferred_devices={_COHERE: "cpu"},
        ),
    )
    dialog, app = _dialog(store)

    sentence = dialog._apply_measured_onnx_devices(_cohere_measured_on_webgpu())

    assert store.load().onnx_auto_preferred_devices == {_COHERE: "webgpu"}
    assert "Auto now starts with WebGPU" in sentence
    dialog.deleteLater()
    _ = app


def test_a_kept_order_for_another_model_is_stored_without_a_sentence(tmp_path):
    """Nothing moved and the selected model was not measured, so the run's
    status line has nothing to add -- but the write still happens and is still
    announced, because the controller's own setters save their whole snapshot
    and would otherwise write the map back as it was."""
    store = _real_store(tmp_path, AppSettings(model_size="small", engine="local"))
    dialog, app = _dialog(store)
    emitted: list[int] = []
    dialog.settings_changed.connect(lambda: emitted.append(1))

    sentence = dialog._apply_measured_onnx_devices(_cohere_measured_on_webgpu())

    assert sentence == ""
    assert store.load().onnx_auto_preferred_devices == {_COHERE: "webgpu"}
    assert emitted == [1]
    dialog.deleteLater()
    _ = app


def test_the_selected_model_and_the_others_are_reported_together(tmp_path):
    store = _real_store(tmp_path, AppSettings(model_size=_COHERE, engine="local"))
    dialog, app = _dialog(store)

    sentence = dialog._apply_measured_onnx_devices(
        [
            *_cohere_measured_on_cpu(),
            _case(_GRANITE, "webgpu", 0.30),
            _case(_GRANITE, "cpu", 0.10),
        ]
    )

    assert "Auto now starts with CPU" in sentence
    assert "1 other model" in sentence
    dialog.deleteLater()
    _ = app


def test_a_second_run_inside_the_noise_band_keeps_the_stored_device(tmp_path):
    """Run 1 moved `auto` to DirectML (15% quicker). Run 2 measures it 5%
    quicker -- still ahead, inside the band. The stored device is what `auto`
    starts with today, so it is what that run has to beat: nothing is written,
    nothing reloads, and the status line does not announce a move back to the
    device the run's own numbers called slower."""
    store = _real_store(tmp_path, AppSettings(model_size=_COHERE, engine="local"))
    dialog, app = _dialog(store)
    emitted: list[int] = []
    dialog.settings_changed.connect(lambda: emitted.append(1))

    first = dialog._apply_measured_onnx_devices(
        [_case(_COHERE, "webgpu", 1.00), _case(_COHERE, "dml", 0.85)]
    )
    assert "Auto now starts with DirectML" in first
    assert emitted == [1]

    second = dialog._apply_measured_onnx_devices(
        [_case(_COHERE, "webgpu", 1.00), _case(_COHERE, "dml", 0.95)]
    )

    assert store.load().onnx_auto_preferred_devices == {_COHERE: "dml"}
    assert second == ""
    assert emitted == [1]
    dialog.deleteLater()
    _ = app


def test_a_comparison_that_measured_one_device_says_why_nothing_changed(tmp_path):
    """The machine with no usable GPU: the run the note asks for ends with one
    failed GPU case and one CPU case. Nothing is stored, rightly -- and the
    status line has to say so, or the note's promise silently does nothing."""
    store = _real_store(tmp_path, AppSettings(model_size=_COHERE, engine="local"))
    dialog, app = _dialog(store)
    emitted: list[int] = []
    dialog.settings_changed.connect(lambda: emitted.append(1))
    failed = _case(_COHERE, "gpu", 0.0)
    failed.error = "No GPU device could load the model"

    sentence = dialog._apply_measured_onnx_devices(
        [failed, _case(_COHERE, "cpu", 0.10)]
    )

    assert store.load().onnx_auto_preferred_devices == {}
    assert emitted == []
    assert "unchanged" in sentence
    assert "only CPU could be measured" in sentence
    assert _COHERE in sentence

    # Another model's failed comparison is not this line's business.
    other = _case(_GRANITE, "gpu", 0.0)
    other.error = "No GPU device could load the model"
    assert (
        dialog._apply_measured_onnx_devices([other, _case(_GRANITE, "cpu", 0.10)])
        == ""
    )
    dialog.deleteLater()
    _ = app


def test_a_refused_write_is_reported_and_never_claimed_as_applied(tmp_path):
    store = _real_store(tmp_path, AppSettings(model_size=_COHERE, engine="local"))
    dialog, app = _dialog(store)
    emitted: list[int] = []
    dialog.settings_changed.connect(lambda: emitted.append(1))

    def refuse(_settings):
        raise OSError(13, "Permission denied")

    store.save = refuse

    sentence = dialog._apply_measured_onnx_devices(_cohere_measured_on_cpu())

    assert "could not be saved" in sentence
    assert emitted == [], "a failed write announced a settings change"
    assert dialog._loaded_settings.onnx_auto_preferred_devices == {}
    dialog.deleteLater()
    _ = app


@pytest.mark.parametrize(
    ("success", "status"),
    [
        (True, "completed"),
        (True, "completed_with_errors"),
        (False, "failed"),
        (True, "canceled"),
    ],
)
def test_only_a_finished_run_decides_the_device_order(tmp_path, success, status):
    """A canceled run measured whatever it got to before the user stopped it,
    and a failed one stopped somewhere unknown; neither is a comparison the
    app may act on by itself."""
    store = _real_store(tmp_path, AppSettings(model_size=_COHERE, engine="local"))
    dialog, app = _dialog(store)

    dialog._on_benchmark_finished(
        success,
        "Benchmark finished.",
        {
            "cases": _cohere_measured_on_cpu(),
            "status": status,
            "options": None,
        },
    )

    expected = {_COHERE: "cpu"} if status.startswith("completed") else {}
    assert store.load().onnx_auto_preferred_devices == expected
    dialog.deleteLater()
    _ = app


def test_the_success_line_names_what_the_run_changed(tmp_path):
    store = _real_store(tmp_path, AppSettings(model_size=_COHERE, engine="local"))
    dialog, app = _dialog(store)

    dialog._on_benchmark_finished(
        True,
        "Benchmark finished.",
        {
            "cases": _cohere_measured_on_cpu(),
            "status": "completed",
            "options": None,
        },
    )

    status = dialog.benchmark_status_label.text()
    assert "Benchmark finished" in status
    assert "CPU" in status
    dialog.deleteLater()
    _ = app


def test_a_run_with_one_failed_model_still_reports_what_it_measured(tmp_path):
    """One model erroring does not undo the comparison another model got, and
    that write has already happened -- so the errors line has to say so too."""
    store = _real_store(tmp_path, AppSettings(model_size=_COHERE, engine="local"))
    dialog, app = _dialog(store)
    failed = _case(_GRANITE, "webgpu", 0.0)
    failed.error = "WebGPU session failed"

    dialog._on_benchmark_finished(
        True,
        "Benchmark finished.",
        {
            "cases": [*_cohere_measured_on_cpu(), failed],
            "status": "completed",
            "options": None,
        },
    )

    status = dialog.benchmark_status_label.text()
    assert "completed with errors" in status
    assert "CPU" in status
    assert store.load().onnx_auto_preferred_devices == {_COHERE: "cpu"}
    dialog.deleteLater()
    _ = app


def test_a_shutting_down_dialog_writes_no_settings(tmp_path):
    """`shutdown()` runs from `aboutToQuit` and delivers the benchmark's queued
    signals from there. A write started on that road reaches a controller that
    is already tearing down, and `settings_changed` would ask it to reload a
    model while the app is quitting."""
    store = _real_store(tmp_path, AppSettings(model_size=_COHERE, engine="local"))
    dialog, app = _dialog(store)
    emitted: list[int] = []
    dialog.settings_changed.connect(lambda: emitted.append(1))
    dialog._shutdown_started = True

    dialog._on_benchmark_finished(
        True,
        "Benchmark finished.",
        {
            "cases": _cohere_measured_on_cpu(),
            "status": "completed",
            "options": None,
        },
    )

    assert store.load().onnx_auto_preferred_devices == {}
    assert emitted == []
    dialog.deleteLater()
    _ = app


# --- The note under the picker -----------------------------------------------


def _note_for(dialog, model: str, measured: dict[str, str], policy: str) -> str:
    dialog._populated_settings = dataclasses.replace(
        dialog._populated_settings, onnx_auto_preferred_devices=measured
    )
    dialog.engine_combo.setCurrentIndex(dialog.engine_combo.findData("local"))
    index = dialog.model_combo.findData(model)
    assert index >= 0, model
    dialog.model_combo.setCurrentIndex(index)
    dialog.local_onnx_device_combo.setCurrentIndex(
        dialog.local_onnx_device_combo.findData(policy)
    )
    # The baseline moved without a widget moving, which in the app happens
    # when a finished benchmark writes the map -- and that path refreshes the
    # row itself. Whether it really does is pinned where it belongs, by
    # `test_a_finished_run_stores_the_device_it_measured_as_fastest`.
    dialog._update_local_onnx_device_row()
    QtWidgets.QApplication.processEvents()
    return dialog.local_onnx_device_note_label.text()


def test_the_note_says_which_device_auto_will_start_with(tmp_path):
    store = _real_store(tmp_path, AppSettings())
    dialog, app = _dialog(store)
    dialog.show()

    measured = _note_for(dialog, _COHERE, {_COHERE: "cpu"}, "auto")
    assert "Auto starts with CPU" in measured
    assert "benchmark" in measured
    # The map is merged across runs, so an entry can be older than the last
    # run -- which may have measured other models only.
    assert "last benchmark" not in measured

    # Measured *and* already the first device: the order is the normal one, so
    # the note must not claim anything was reordered.
    # Also not "confirmed as the fastest": the first device stands as well when
    # another one was quicker by less than the minimum gain.
    confirmed = _note_for(dialog, _COHERE, {_COHERE: "webgpu"}, "auto")
    assert "starts with" not in confirmed
    assert "confirmed" not in confirmed
    assert "last benchmark" not in confirmed
    assert "nothing clearly faster than WebGPU" in confirmed

    unmeasured = _note_for(dialog, _COHERE, {}, "auto")
    assert BENCHMARK_GPU_CPU_COMPARISON_LABEL in unmeasured
    assert "WebGPU, then DirectML, then CPU" in unmeasured

    # Another model's measurement says nothing about this one.
    other = _note_for(dialog, _COHERE, {_GRANITE: "cpu"}, "auto")
    assert other == unmeasured
    dialog.deleteLater()
    _ = app


def test_the_nemotron_note_offers_the_comparison_it_can_actually_run(tmp_path):
    store = _real_store(tmp_path, AppSettings())
    dialog, app = _dialog(store)
    dialog.show()

    unmeasured = _note_for(dialog, _NEMOTRON, {}, "auto")
    assert "DirectML and CPU only" in unmeasured
    assert BENCHMARK_GPU_CPU_COMPARISON_LABEL in unmeasured

    measured = _note_for(dialog, _NEMOTRON, {_NEMOTRON: "cpu"}, "auto")
    assert "Auto starts with CPU" in measured

    # WebGPU is not a provider this runtime has, so a stored entry naming it
    # changes nothing and must not be announced as a measurement.
    unreachable = _note_for(dialog, _NEMOTRON, {_NEMOTRON: "webgpu"}, "auto")
    assert unreachable == unmeasured
    dialog.deleteLater()
    _ = app


def test_a_pinned_device_note_ignores_the_measurement(tmp_path):
    store = _real_store(tmp_path, AppSettings())
    dialog, app = _dialog(store)
    dialog.show()

    for policy in ("cpu", "gpu", "webgpu", "dml"):
        note = _note_for(dialog, _COHERE, {_COHERE: "cpu"}, policy)
        assert "benchmark" not in note.lower(), policy
    dialog.deleteLater()
    _ = app


def test_the_notes_for_models_without_a_device_say_what_decides_instead(tmp_path):
    """Both used to be wrong: onnx-asr was called "the fastest local option"
    (`tiny` is quicker), and faster-whisper was said to have "its own device
    setting", which this app has no such thing as."""
    store = _real_store(tmp_path, AppSettings())
    dialog, app = _dialog(store)
    dialog.show()

    parakeet = _note_for(dialog, "parakeet-tdt-0.6b-v3", {}, "auto")
    assert "fastest" not in parakeet
    assert "onnx-asr" in parakeet

    # A different CPU-only runtime, so it must not be named after onnx-asr --
    # and it must not fall through to the Whisper note either, which points
    # at CUDA and at three models this one is not.
    granite_ctc = _note_for(dialog, "granite-speech-5.0-470m-turboctc", {}, "auto")
    assert "always runs on the CPU through ONNX Runtime" in granite_ctc
    assert "onnx-asr" not in granite_ctc
    assert "CUDA" not in granite_ctc

    whisper = _note_for(dialog, "small", {}, "auto")
    assert "its own device setting" not in whisper
    assert "CUDA" in whisper
    dialog.deleteLater()
    _ = app


def test_the_runtime_note_no_longer_restates_a_device_order_that_can_move(
    tmp_path,
):
    """The note beside the model combo said "Auto tries WebGPU, then DirectML,
    then falls back to CPU". With a measured preference that is simply false,
    and it is the wrong place to repeat the order anyway -- the ONNX Device row
    below it owns that."""
    store = _real_store(tmp_path, AppSettings())
    dialog, app = _dialog(store)
    dialog.show()

    def runtime_note(model: str) -> str:
        dialog.engine_combo.setCurrentIndex(dialog.engine_combo.findData("local"))
        dialog.model_combo.setCurrentIndex(dialog.model_combo.findData(model))
        QtWidgets.QApplication.processEvents()
        return dialog.local_model_runtime_warning_label.text()

    cohere = runtime_note(_COHERE)
    assert "Auto tries" not in cohere
    assert "ONNX Device" in cohere

    nemotron = runtime_note(_NEMOTRON)
    assert "Auto tries" not in nemotron
    assert "ONNX Device" in nemotron

    # AGENTS.md: never "fastest" flat -- `tiny` is quicker than Parakeet.
    parakeet = runtime_note("parakeet-tdt-0.6b-v3")
    assert "fastest" not in parakeet
    assert "recommended default" in parakeet
    dialog.deleteLater()
    _ = app


def test_the_benchmark_device_note_says_what_a_comparison_also_decides(tmp_path):
    """The run is where the measurement comes from, so that is where the user
    has to learn that it changes daily dictation too."""
    store = _real_store(tmp_path, AppSettings())
    dialog, app = _dialog(store)
    dialog._open_benchmark_window()
    QtWidgets.QApplication.processEvents()

    notes = [
        label.text()
        for label in dialog.benchmark_window.findChildren(QtWidgets.QLabel)
        if "Whisper models pick their" in label.text()
    ]

    assert notes, "the benchmark ONNX Device note was not found"
    # The note points at the tab by its visible title, which is
    # "Transcription" since the rename.
    assert "Settings > Transcription" in notes[0]
    dialog.benchmark_window.close()
    dialog.deleteLater()
    _ = app


@pytest.mark.pixel_exact
def test_every_device_note_fits_its_two_reserved_lines(tmp_path):
    """The reservation is the label's `minimumHeight`, and `heightForWidth` is
    floored by it -- so measuring through the real label answers the
    reservation whatever the text is. Measure through a bare polished copy at
    the width the label really has when the dialog is at its minimum width.
    """
    store = _real_store(tmp_path, AppSettings())
    dialog, app = _dialog(store)
    dialog.show()
    QtWidgets.QApplication.processEvents()
    if round(dialog.font().pointSizeF() * 4) != 36:
        pytest.skip(
            "Measured for the 9 pt default font; this session runs at "
            f"{dialog.font().pointSizeF()} pt."
        )

    dialog.resize(dialog.minimumWidth(), dialog.height())
    QtWidgets.QApplication.processEvents()
    label = dialog.local_onnx_device_note_label
    # `_style_field_hint_label` pins every field hint to a 460 px minimum, so
    # that is the narrowest this label can ever be -- narrower than it actually
    # is at the dialog's own minimum width, whether that is the 581 px it
    # answers before the Benchmark tab has been painted or the 611 px after.
    # Measuring there is the worst case and needs no window arithmetic.
    width = label.minimumWidth()
    assert 0 < width <= label.width()

    probe = QtWidgets.QLabel(dialog)
    probe.setWordWrap(True)
    probe.setStyleSheet(label.styleSheet())
    probe.setFont(label.font())
    probe.ensurePolished()
    reserved = label.height()

    texts: list[tuple[str, str]] = []
    for model in (_COHERE, _NEMOTRON, "parakeet-tdt-0.6b-v3", "small"):
        for measured in ({}, {model: "cpu"}, {model: "webgpu"}, {model: "dml"}):
            texts.extend(
                (
                    f"{model}/{measured}/{policy}",
                    _note_for(dialog, model, measured, policy),
                )
                for policy in ("auto", "cpu", "gpu", "webgpu", "dml")
            )
    dialog.engine_combo.setCurrentIndex(dialog.engine_combo.findData("openai"))
    QtWidgets.QApplication.processEvents()
    texts.append(("remote", dialog.local_onnx_device_note_label.text()))

    too_tall: list[str] = []
    for label_text, text in texts:
        probe.setText(text)
        if probe.heightForWidth(width) > reserved:
            too_tall.append(
                f"{label_text}: {probe.heightForWidth(width)} px > {reserved} px "
                f"at {width} px -- {text!r}"
            )
    assert not too_tall, "\n".join(too_tall)
    dialog.deleteLater()
    _ = app


def test_the_note_is_also_in_the_tooltip(tmp_path):
    """Two lines at the dialog's minimum width are not much room, so the whole
    sentence has to be readable without widening the window."""
    store = _real_store(tmp_path, AppSettings())
    dialog, app = _dialog(store)
    dialog.show()

    for model, measured in ((_COHERE, {_COHERE: "cpu"}), (_NEMOTRON, {})):
        note = _note_for(dialog, model, measured, "auto")
        assert dialog.local_onnx_device_note_label.toolTip() == note
    dialog.deleteLater()
    _ = app


def test_the_note_never_moves_the_fields_below_it(tmp_path):
    """The existing row test covers the model families; the measured states are
    new text in the same reserved area and must not move anything either."""
    store = _real_store(tmp_path, AppSettings())
    dialog, app = _dialog(store)
    dialog.show()

    def language_y() -> int:
        return dialog.language_combo.mapTo(dialog, QtCore.QPoint()).y()

    _note_for(dialog, _COHERE, {}, "auto")
    baseline = language_y()
    seen = set()
    for model in (_COHERE, _NEMOTRON, "parakeet-tdt-0.6b-v3", "small"):
        for measured in ({}, {model: "cpu"}, {model: "webgpu"}):
            for policy in ("auto", "cpu", "dml"):
                _note_for(dialog, model, measured, policy)
                seen.add(language_y())

    assert seen == {baseline}
    dialog.deleteLater()
    _ = app
