"""The Run Benchmark window keeps its Run/Cancel row still and in view.

Measured before the row left the scroll area (860x880 window, 9 pt): opening
"Show Run Options" pushed Run Benchmark from y=778 to y=1073, below the
812 px viewport; an inventory scan landing with a 13th model moved it by
20 px; a long audio path wrapping its status line moved it by 9 px; and with
twelve models the button's lower 19 px sat below the viewport at the
default size, so the window's one primary action needed a scroll to reach.
"""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets
from test_benchmark_run_progress import _dialog

_MANY_MODELS = [
    "tiny",
    "base",
    "small",
    "medium",
    "large-v3",
    "large-v3-turbo",
    "distil-large-v3.5",
    "parakeet-tdt-0.6b-v3",
    "canary-1b-v2",
    "cohere-transcribe-03-2026",
    "granite-speech-5.0-470m-turboctc",
    "nemotron-3.5-asr-0.6b",
]


def _settle(app: QtWidgets.QApplication) -> None:
    for _ in range(10):
        app.processEvents()


def _shown_window(tmp_path, models):
    dialog, app = _dialog(tmp_path, models)
    window = dialog.benchmark_window
    window.resize(860, 880)
    window.show()
    _settle(app)
    return dialog, app, window


def _rect_in(window: QtWidgets.QWidget, widget: QtWidgets.QWidget) -> QtCore.QRect:
    return QtCore.QRect(widget.mapTo(window, QtCore.QPoint(0, 0)), widget.size())


def test_run_and_cancel_stay_put_and_visible_whatever_the_window_shows(tmp_path):
    dialog, app, window = _shown_window(tmp_path, ["small", "tiny"])
    buttons = (dialog.run_benchmark_button, dialog.cancel_benchmark_button)

    def geometry():
        _settle(app)
        return [_rect_in(window, button) for button in buttons]

    before = geometry()
    for rect in before:
        assert window.rect().contains(rect), f"{rect} lies outside the window"
    # Outside the scrolling content: no scroll position can hide them.
    for button in buttons:
        assert not dialog.benchmark_setup_scroll.isAncestorOf(button)

    dialog.benchmark_options_toggle.click()
    assert geometry() == before, "opening the run options moved Run/Cancel"
    dialog.benchmark_options_toggle.click()

    dialog._refresh_benchmark_model_list(cached=_MANY_MODELS)
    assert geometry() == before, "more installed models moved Run/Cancel"

    long_path = tmp_path / ("a_rather_long_recording_name_" * 6 + ".wav")
    long_path.write_bytes(b"RIFF")
    dialog._set_benchmark_audio_path(str(long_path))
    assert geometry() == before, "a long audio path moved Run/Cancel"
    window.hide()


def test_a_long_audio_path_moves_nothing_below_it(tmp_path):
    """The status line under the audio field wrapped to a second line, and
    every row below it -- the model list, the case list -- moved down 9 px."""
    dialog, app, window = _shown_window(tmp_path, ["small", "tiny"])
    label = dialog.benchmark_audio_status_label
    models = dialog.benchmark_models_list

    def geometry():
        _settle(app)
        return (label.height(), _rect_in(window, models).top())

    before = geometry()
    long_path = tmp_path / ("a_rather_long_recording_name_" * 6 + ".wav")
    long_path.write_bytes(b"RIFF")
    dialog._set_benchmark_audio_path(str(long_path))

    assert geometry() == before
    assert label.toolTip() == label.text()
    window.hide()


def test_the_audio_line_says_why_a_typed_path_cannot_run(tmp_path):
    """Typing a path left the line at "No audio sample selected." while Run
    stayed disabled, so nothing said what was wrong with the path."""
    dialog, app, _window = _shown_window(tmp_path, ["small"])
    label = dialog.benchmark_audio_status_label

    assert label.text() == "No audio sample selected."

    missing = tmp_path / "missing.wav"
    dialog.benchmark_audio_edit.setText(str(missing))

    assert "not found" in label.text().lower()
    assert dialog.run_benchmark_button.isEnabled() is False

    present = tmp_path / "sample.wav"
    present.write_bytes(b"RIFF")
    dialog.benchmark_audio_edit.setText(str(present))

    assert label.text() == f"Selected: {present}"
    assert dialog.run_benchmark_button.isEnabled() is True

    dialog.benchmark_audio_edit.setText("")

    assert label.text() == "No audio sample selected."
    assert dialog.run_benchmark_button.isEnabled() is False
    _ = app


def test_a_quoted_path_from_explorer_is_accepted_and_used_unquoted(
    monkeypatch, tmp_path
):
    """Explorer's "Copy as path" wraps the path in double quotes, and the
    quoted text is no file: the line said "File not found" and Run stayed
    disabled for a file that exists."""
    seen: dict[str, object] = {}

    def _fake_run(**kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr("stt_app.settings_dialog.run_benchmark_cases", _fake_run)
    monkeypatch.setattr(
        "stt_app.settings_dialog_benchmark.collect_benchmark_environment",
        lambda: None,
    )
    dialog, app, _window = _shown_window(tmp_path, ["small"])
    present = tmp_path / "sample.wav"
    present.write_bytes(b"RIFF")

    dialog.benchmark_audio_edit.setText(f' "{present}" ')

    assert dialog.benchmark_audio_status_label.text() == f"Selected: {present}"
    assert dialog.run_benchmark_button.isEnabled() is True

    dialog._run_local_benchmark()
    thread = dialog._active_benchmark_thread
    if thread is not None:
        thread.join(5)
    _settle(app)

    assert seen.get("audio_path") == str(present)
    _ = app
