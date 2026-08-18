"""Retranscribe a history entry's retained audio from the overlay history.

Reached from the overlay's "Recent Transcriptions" dialog. The entry's own
transcriber and language are preselected, because repeating the run with a
corrected language is the common case; engine and model stay changeable so a
quick "try the bigger model on this one" needs no detour through Settings.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path
from typing import Callable

from PySide6 import QtCore, QtGui, QtWidgets

from .app_icon import load_app_icon
from .config import (
    DEFAULT_ENGINE,
    DEFAULT_LANGUAGE_MODE,
    LANGUAGE_MODE_LABELS,
    VALID_ENGINES,
    language_modes_for_selection,
)
from .settings_dialog_helpers import (
    _ENGINE_LABELS,
    _REMOTE_MODEL_DEFAULTS,
    model_choices_for_engine,
)
from .settings_store import AppSettings, apply_engine_model_selection
from .ui_feedback import (
    BUTTON_FEEDBACK_STYLESHEET,
    reserve_button_width_for_texts,
    set_button_feedback_state,
)

TranscribeCallable = Callable[
    [str, AppSettings, Callable[[str], None] | None],
    "tuple[bool, str]",
]

_PREVIEW_MIN_LINES = 4
_RESULT_MIN_LINES = 6
_DEFAULT_SIZE = QtCore.QSize(640, 620)
_MINIMUM_SIZE = QtCore.QSize(560, 460)


class RetranscribeDialog(QtWidgets.QDialog):
    """Transcribe one history entry's audio again with a chosen setup."""

    _progress_reported = QtCore.Signal(str)
    _run_finished = QtCore.Signal(bool, str)

    def __init__(
        self,
        *,
        entry: object,
        audio_path: Path,
        base_settings: AppSettings,
        transcribe: TranscribeCallable,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._entry = entry
        self._audio_path = Path(audio_path)
        self._base_settings = base_settings
        self._transcribe = transcribe
        self._running = False
        self._elapsed_seconds = 0
        #: True once a run produced a transcript, so the caller can reload.
        self.produced_transcript = False

        self._entry_engine = str(getattr(entry, "engine", "") or "").strip().lower()
        self._entry_model = str(getattr(entry, "model", "") or "").strip()

        self.setWindowTitle("Retranscribe")
        self.setWindowIcon(load_app_icon())
        self.setWindowModality(QtCore.Qt.WindowModal)
        self.setStyleSheet(BUTTON_FEEDBACK_STYLESHEET)
        self.setSizeGripEnabled(True)

        self._elapsed_timer = QtCore.QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._tick_elapsed)

        self._progress_reported.connect(self._on_progress)
        self._run_finished.connect(self._on_finished)

        form = QtWidgets.QFormLayout()
        form.setLabelAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)

        self._audio_label = QtWidgets.QLabel(self._audio_path.name)
        self._audio_label.setToolTip(str(self._audio_path))
        self._audio_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        form.addRow("Audio", self._audio_label)

        self._engine_combo = QtWidgets.QComboBox()
        for value in VALID_ENGINES:
            self._engine_combo.addItem(_ENGINE_LABELS.get(value, value), value)
        self._select_data(
            self._engine_combo,
            self._entry_engine or str(base_settings.engine or DEFAULT_ENGINE),
        )
        self._engine_combo.currentIndexChanged.connect(self._on_engine_changed)
        form.addRow("Engine", self._engine_combo)

        self._model_combo = QtWidgets.QComboBox()
        self._model_combo.currentIndexChanged.connect(self._on_model_changed)
        form.addRow("Model", self._model_combo)

        self._language_combo = QtWidgets.QComboBox()
        form.addRow("Language", self._language_combo)

        # Populate the dependent pickers once the three exist.
        self._populate_models(preferred=self._entry_model)
        self._populate_languages(
            preferred=str(
                getattr(base_settings, "language_mode", DEFAULT_LANGUAGE_MODE)
            )
        )

        self._previous_text = QtWidgets.QPlainTextEdit(
            str(getattr(entry, "text", "") or "")
        )
        self._previous_text.setReadOnly(True)
        self._previous_text.setMinimumHeight(self._text_height(_PREVIEW_MIN_LINES))

        self._result_text = QtWidgets.QPlainTextEdit()
        self._result_text.setReadOnly(True)
        self._result_text.setPlaceholderText(
            "The new transcript appears here. It is saved to history as a "
            "separate entry; the original stays unchanged."
        )
        self._result_text.setMinimumHeight(self._text_height(_RESULT_MIN_LINES))

        # Reserve the status line instead of showing/hiding it: the widgets
        # below must not move when a run starts, reports, or finishes. The
        # ignored width policy keeps a long provider error from widening the
        # dialog; the full text stays available as the tooltip.
        self._status_label = QtWidgets.QLabel("")
        self._status_label.setWordWrap(False)
        self._status_label.setFixedHeight(self.fontMetrics().height())
        self._status_label.setSizePolicy(
            QtWidgets.QSizePolicy.Ignored,
            QtWidgets.QSizePolicy.Fixed,
        )
        self._set_status("")

        self._copy_button = QtWidgets.QPushButton("Copy result")
        self._copy_button.setEnabled(False)
        self._copy_button.clicked.connect(self._copy_result)
        reserve_button_width_for_texts(self._copy_button, ("Copy result", "Copied"))
        self._copy_feedback_timer = QtCore.QTimer(self)
        self._copy_feedback_timer.setSingleShot(True)
        self._copy_feedback_timer.setInterval(1000)
        self._copy_feedback_timer.timeout.connect(self._reset_copy_feedback)

        self._run_button = QtWidgets.QPushButton("Retranscribe")
        self._run_button.setDefault(True)
        self._run_button.clicked.connect(self._start_run)

        self._close_button = QtWidgets.QPushButton("Close")
        self._close_button.clicked.connect(self.close)

        buttons = QtWidgets.QHBoxLayout()
        buttons.setSpacing(6)
        buttons.addWidget(self._copy_button)
        buttons.addStretch(1)
        buttons.addWidget(self._run_button)
        buttons.addWidget(self._close_button)

        # The two transcript views share the extra height a resize provides, so
        # a long transcript benefits from making the dialog bigger.
        splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(_labelled(self._previous_text, "Current transcript"))
        splitter.addWidget(_labelled(self._result_text, "New transcript"))
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)
        root.addLayout(form)
        root.addWidget(splitter, 1)
        root.addWidget(self._status_label)
        root.addLayout(buttons)

        self.setMinimumSize(_MINIMUM_SIZE)
        self.resize(_DEFAULT_SIZE)

    # -- pickers -------------------------------------------------------------

    def selected_engine(self) -> str:
        return str(self._engine_combo.currentData() or DEFAULT_ENGINE)

    def selected_model(self) -> str:
        return str(self._model_combo.currentData() or "")

    def selected_language_mode(self) -> str:
        return str(self._language_combo.currentData() or DEFAULT_LANGUAGE_MODE)

    def _populate_models(self, *, preferred: str = "") -> None:
        engine = self.selected_engine()
        blocker = QtCore.QSignalBlocker(self._model_combo)
        self._model_combo.clear()
        for value, label in model_choices_for_engine(engine):
            self._model_combo.addItem(label, value)
        del blocker
        fallback = (
            str(self._base_settings.model_size or "")
            if engine == DEFAULT_ENGINE
            else _REMOTE_MODEL_DEFAULTS.get(engine, "")
        )
        if not self._select_data(self._model_combo, preferred):
            self._select_data(self._model_combo, fallback)
        self._model_combo.setEnabled(self._model_combo.count() > 1)

    def _populate_languages(self, *, preferred: str = "") -> None:
        modes = language_modes_for_selection(
            self.selected_engine(),
            self.selected_model(),
            "batch",
        )
        blocker = QtCore.QSignalBlocker(self._language_combo)
        self._language_combo.clear()
        for value in modes:
            self._language_combo.addItem(
                LANGUAGE_MODE_LABELS.get(value, value),
                value,
            )
        del blocker
        for candidate in (
            preferred,
            str(getattr(self._base_settings, "language_mode", "")),
            DEFAULT_LANGUAGE_MODE,
        ):
            if self._select_data(self._language_combo, candidate):
                break
        self._language_combo.setEnabled(self._language_combo.count() > 1)

    def _on_engine_changed(self) -> None:
        # Keep the entry's model when the user returns to its engine.
        preferred = (
            self._entry_model
            if self.selected_engine() == self._entry_engine
            else ""
        )
        self._populate_models(preferred=preferred)
        self._populate_languages(preferred=self.selected_language_mode())

    def _on_model_changed(self) -> None:
        self._populate_languages(preferred=self.selected_language_mode())

    @staticmethod
    def _select_data(combo: QtWidgets.QComboBox, value: str) -> bool:
        index = combo.findData(str(value or ""))
        if index < 0:
            return False
        combo.setCurrentIndex(index)
        return True

    def _text_height(self, lines: int) -> int:
        return self.fontMetrics().height() * lines + 12

    # -- run -----------------------------------------------------------------

    def build_settings(self) -> AppSettings:
        engine = self.selected_engine()
        settings = replace(
            self._base_settings,
            engine=engine,
            language_mode=self.selected_language_mode(),
        )
        return apply_engine_model_selection(settings, engine, self.selected_model())

    def _start_run(self) -> None:
        if self._running:
            return
        if not self._audio_path.is_file():
            self._set_status("The audio file is no longer available.", error=True)
            self._run_button.setEnabled(False)
            return
        self._running = True
        self._elapsed_seconds = 0
        self._set_controls_enabled(False)
        self._copy_button.setEnabled(False)
        self._reset_copy_feedback()
        self._result_text.clear()
        self._set_status("Transcribing...")
        self._elapsed_timer.start()

        # Read the widgets on the GUI thread; the worker only sees plain data.
        path = str(self._audio_path)
        settings = self.build_settings()
        transcribe = self._transcribe

        def _run() -> None:
            def _progress(message: str) -> None:
                _emit(self, "_progress_reported", str(message))

            try:
                ok, text = transcribe(path, settings, _progress)
            except Exception as exc:  # noqa: BLE001 - reported to the user
                ok, text = False, str(exc)
            _emit(self, "_run_finished", bool(ok), str(text))

        threading.Thread(
            target=_run,
            name="stt_app_history_retranscription",
            daemon=True,
        ).start()

    def _set_controls_enabled(self, enabled: bool) -> None:
        self._run_button.setEnabled(enabled)
        self._engine_combo.setEnabled(enabled)
        self._model_combo.setEnabled(enabled and self._model_combo.count() > 1)
        self._language_combo.setEnabled(
            enabled and self._language_combo.count() > 1
        )

    def _tick_elapsed(self) -> None:
        self._elapsed_seconds += 1
        self._set_status(f"Transcribing... ({self._elapsed_seconds}s)")

    def _on_progress(self, message: str) -> None:
        text = str(message or "").strip()
        if not text or not self._running:
            return
        self._set_status(f"{text} ({self._elapsed_seconds}s)")

    def _on_finished(self, ok: bool, text: str) -> None:
        self._running = False
        self._elapsed_timer.stop()
        self._set_controls_enabled(True)
        result = str(text or "")
        if not ok or not result.strip():
            self.produced_transcript = False
            self._set_status(f"Failed: {result}", error=True)
            return
        self._result_text.setPlainText(result)
        self._copy_button.setEnabled(True)
        self.produced_transcript = True
        self._set_status("Done. Saved to history as a new entry.", success=True)

    # -- feedback ------------------------------------------------------------

    def _set_status(
        self,
        message: str,
        *,
        error: bool = False,
        success: bool = False,
    ) -> None:
        text = str(message or "")
        self._status_label.setText(text)
        self._status_label.setToolTip(text)
        if error:
            color = "#b71c1c"
        elif success:
            color = "#1b5e20"
        else:
            color = "#555"
        self._status_label.setStyleSheet(f"color: {color};")

    def _copy_result(self) -> None:
        text = self._result_text.toPlainText()
        if not text.strip():
            return
        QtGui.QGuiApplication.clipboard().setText(text)
        self._copy_button.setText("Copied")
        set_button_feedback_state(self._copy_button, "success")
        self._copy_feedback_timer.start()

    def _reset_copy_feedback(self) -> None:
        self._copy_button.setText("Copy result")
        set_button_feedback_state(self._copy_button, None)


def _labelled(content: QtWidgets.QWidget, title: str) -> QtWidgets.QWidget:
    """Pair a section caption with its widget so both scale in a splitter."""
    holder = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(2)
    caption = QtWidgets.QLabel(title)
    caption.setStyleSheet("color: #555;")
    layout.addWidget(caption)
    layout.addWidget(content, 1)
    return holder


def _emit(owner: QtCore.QObject, signal_name: str, *args: object) -> bool:
    """Emit from a worker thread; a closed dialog must not raise."""
    try:
        getattr(owner, signal_name).emit(*args)
    except RuntimeError:
        return False
    return True
