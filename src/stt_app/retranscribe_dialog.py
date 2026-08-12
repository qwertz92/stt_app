"""Retranscribe a history entry's retained audio with a different language.

Reached from the overlay's "Recent Transcriptions" dialog. Picking the wrong
dictation language is the common case this exists for, so the language is the
only control: engine and model stay the ones the entry was produced with.
Settings > History > Retranscribe... remains the path for changing those.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path
from typing import Callable

from PySide6 import QtCore, QtGui, QtWidgets

from .app_icon import load_app_icon
from .config import (
    DEFAULT_LANGUAGE_MODE,
    LANGUAGE_MODE_LABELS,
    language_modes_for_selection,
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

_PREVIEW_HEIGHT_LINES = 4
_RESULT_HEIGHT_LINES = 6


class RetranscribeDialog(QtWidgets.QDialog):
    """Transcribe one history entry's audio again with a chosen language."""

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

        self._engine = str(getattr(entry, "engine", "") or "").strip()
        self._model = str(getattr(entry, "model", "") or "").strip()

        self.setWindowTitle("Retranscribe")
        self.setWindowIcon(load_app_icon())
        self.setWindowModality(QtCore.Qt.WindowModal)
        self.setStyleSheet(BUTTON_FEEDBACK_STYLESHEET)

        self._elapsed_timer = QtCore.QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._tick_elapsed)

        self._progress_reported.connect(self._on_progress)
        self._run_finished.connect(self._on_finished)

        form = QtWidgets.QFormLayout()
        form.setLabelAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)

        provider = f"{self._engine} · {self._model}" if self._model else self._engine
        self._provider_label = QtWidgets.QLabel(provider or "unknown transcriber")
        self._provider_label.setToolTip(
            "The entry is transcribed again with the same engine and model. "
            "Use Settings > History > Retranscribe... to change those."
        )
        form.addRow("Transcriber", self._provider_label)

        self._audio_label = QtWidgets.QLabel(self._audio_path.name)
        self._audio_label.setToolTip(str(self._audio_path))
        self._audio_label.setTextInteractionFlags(
            QtCore.Qt.TextSelectableByMouse
        )
        form.addRow("Audio", self._audio_label)

        self._language_combo = QtWidgets.QComboBox()
        self._populate_languages()
        form.addRow("Language", self._language_combo)

        self._previous_text = QtWidgets.QPlainTextEdit(
            str(getattr(entry, "text", "") or "")
        )
        self._previous_text.setReadOnly(True)
        self._previous_text.setFixedHeight(
            self._text_height(_PREVIEW_HEIGHT_LINES)
        )

        self._result_text = QtWidgets.QPlainTextEdit()
        self._result_text.setReadOnly(True)
        self._result_text.setPlaceholderText(
            "The new transcript appears here. It is saved to history as a "
            "separate entry; the original stays unchanged."
        )
        self._result_text.setFixedHeight(self._text_height(_RESULT_HEIGHT_LINES))

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
        reserve_button_width_for_texts(
            self._copy_button,
            ("Copy result", "Copied"),
        )
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

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)
        root.addLayout(form)
        root.addWidget(_section_label("Current transcript"))
        root.addWidget(self._previous_text)
        root.addWidget(_section_label("New transcript"))
        root.addWidget(self._result_text)
        root.addWidget(self._status_label)
        root.addLayout(buttons)

        self.setMinimumWidth(560)
        self.adjustSize()
        # A fixed height keeps the dialog from resizing around its own content
        # while a run reports progress.
        self.setFixedHeight(self.sizeHint().height())

    # -- setup ---------------------------------------------------------------

    def _populate_languages(self) -> None:
        modes = language_modes_for_selection(self._engine, self._model, "batch")
        for value in modes:
            self._language_combo.addItem(
                LANGUAGE_MODE_LABELS.get(value, value),
                value,
            )
        current = str(
            getattr(self._base_settings, "language_mode", DEFAULT_LANGUAGE_MODE)
        )
        index = self._language_combo.findData(current)
        if index < 0:
            index = self._language_combo.findData(DEFAULT_LANGUAGE_MODE)
        if index >= 0:
            self._language_combo.setCurrentIndex(index)
        self._language_combo.setEnabled(self._language_combo.count() > 1)

    def _text_height(self, lines: int) -> int:
        return self.fontMetrics().height() * lines + 12

    # -- run -----------------------------------------------------------------

    def selected_language_mode(self) -> str:
        return str(self._language_combo.currentData() or DEFAULT_LANGUAGE_MODE)

    def build_settings(self) -> AppSettings:
        settings = replace(
            self._base_settings,
            engine=self._engine or self._base_settings.engine,
            language_mode=self.selected_language_mode(),
        )
        return apply_engine_model_selection(
            settings,
            self._engine or self._base_settings.engine,
            self._model,
        )

    def _start_run(self) -> None:
        if self._running:
            return
        if not self._audio_path.is_file():
            self._set_status(
                "The audio file is no longer available.",
                error=True,
            )
            self._run_button.setEnabled(False)
            return
        self._running = True
        self._elapsed_seconds = 0
        self._run_button.setEnabled(False)
        self._language_combo.setEnabled(False)
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
        self._run_button.setEnabled(True)
        self._language_combo.setEnabled(self._language_combo.count() > 1)
        result = str(text or "")
        if not ok:
            self._set_status(f"Failed: {result}", error=True)
            return
        self._result_text.setPlainText(result)
        self._copy_button.setEnabled(bool(result.strip()))
        self.produced_transcript = bool(result.strip())
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


def _section_label(text: str) -> QtWidgets.QLabel:
    label = QtWidgets.QLabel(text)
    label.setStyleSheet("color: #555;")
    return label


def _emit(owner: QtCore.QObject, signal_name: str, *args: object) -> bool:
    """Emit from a worker thread; a closed dialog must not raise."""
    try:
        getattr(owner, signal_name).emit(*args)
    except RuntimeError:
        return False
    return True
