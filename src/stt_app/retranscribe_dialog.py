"""Retranscribe a history entry's retained audio from the overlay history.

Reached from the overlay's "Recent Transcriptions" dialog. The entry's own
transcriber and language are preselected, because repeating the run with a
corrected language is the common case; engine and model stay changeable so a
quick "try the bigger model on this one" needs no detour through Settings.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from .app_icon import load_app_icon
from .config import (
    CANARY_MODEL_SIZE,
    DEFAULT_ENGINE,
    DEFAULT_LANGUAGE_MODE,
    LANGUAGE_MODE_LABELS,
    VALID_ENGINES,
    language_modes_for_selection,
)
from .dialog_style import make_label_selectable
from .settings_dialog_helpers import (
    _ENGINE_LABELS,
    _REMOTE_MODEL_DEFAULTS,
    LOCAL_MODEL_LABELS,
    local_model_short_label,
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
_CANARY_LANGUAGE_WARNING = (
    "Canary cannot detect the language. Pick the one actually spoken - with "
    "the wrong one it translates instead of transcribing."
)
# The widest name the substitution sentence can carry, so the reserved height
# below is measured against the real worst case rather than today's wording.
_LONGEST_MODEL_NAME = max(
    (local_model_short_label(name) for name in LOCAL_MODEL_LABELS),
    key=len,
    default="",
)

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
        self._language_note = QtWidgets.QLabel("")
        self._language_note.setWordWrap(True)
        self._language_note.setStyleSheet("color: #b71c1c; font-size: 11px;")
        # Reserved height so showing or hiding the note never moves the widgets
        # below it, but a *minimum* rather than a fixed one: the dialog is
        # deliberately resizable, and at its narrowest a too-small reservation
        # would clip the warning instead of growing.
        #
        # Three lines, not two: the note can carry a retired-model
        # substitution *and* the Canary language warning at once, which
        # measured 45 px at the default width against the 38 px that two lines
        # of the dialog font reserve -- so the buttons below moved by 7 px when
        # the model changed. (The multiplier is in the dialog's font while the
        # label renders at 11 px, which is why two dialog lines were already
        # worth about two and a half label lines.)
        self._minimum_note_height = self.fontMetrics().lineSpacing() * 3 + 6
        self._language_note.setMinimumHeight(self._minimum_note_height)
        # `heightForWidth` explicitly, not by accident: the dialog is
        # resizable down to `_MINIMUM_SIZE`, and at 560 px the worst-case note
        # needs 60 px against the 54 px reserved above, so without it the last
        # sentence is cut off with no scrollbar and no ellipsis -- and the last
        # sentence is the Canary warning, whose absence costs a translated
        # transcript. A word-wrapping QLabel happens to restore this flag
        # inside `setText`, which is why it works today; passing a policy
        # object with the flag already set stops that from being load-bearing
        # Qt-internal behaviour.
        note_policy = QtWidgets.QSizePolicy(
            QtWidgets.QSizePolicy.Preferred,
            QtWidgets.QSizePolicy.Minimum,
        )
        note_policy.setHeightForWidth(True)
        self._language_note.setSizePolicy(note_policy)
        self._language_note.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
        # The longest note this dialog can ever show, so the reservation can
        # be re-measured whenever the width changes. Composed from the same
        # sentences `_update_substitution_note` builds.
        #
        # The name is the longer of the widest *known* label and this entry's
        # own: `local_model_short_label` returns an unrecognised id verbatim,
        # and `_entry_model` comes from a history entry -- arbitrary text
        # after a History import, or a retired id whose label was deleted (the
        # longest raw local id, `nemotron-3.5-asr-streaming-0.6b-int4`, is
        # already 6 characters longer than the longest label). Measured with a
        # 63-character id, a labels-only worst case reserved 60 px for a note
        # that took 75 at the dialog's own minimum width, so changing the
        # model moved everything below it by 15 px.
        widest_name = max(
            (_LONGEST_MODEL_NAME, local_model_short_label(self._entry_model)),
            key=len,
        )
        self._worst_case_note = " ".join(
            (
                f"This entry was recorded with '{widest_name}', which "
                f"this version no longer offers, so {widest_name} was "
                f"chosen instead.",
                "This entry's language (auto) is unavailable here, so de was "
                "chosen.",
                _CANARY_LANGUAGE_WARNING,
            )
        )
        language_box = QtWidgets.QWidget()
        language_layout = QtWidgets.QVBoxLayout(language_box)
        language_layout.setContentsMargins(0, 0, 0, 0)
        language_layout.setSpacing(2)
        language_layout.addWidget(self._language_combo)
        language_layout.addWidget(self._language_note)
        form.addRow("Language", language_box)

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
        make_label_selectable(self._status_label)
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
        self._update_substitution_note(preferred)

    def resizeEvent(self, event) -> None:
        """Keep the note's reservation matched to the current width.

        The reservation is what stops the widgets below moving when the note's
        *text* changes, and a line count only covers the width it was measured
        at. The dialog is resizable down to `_MINIMUM_SIZE` with a size grip,
        and at 560 px the worst-case note needs 60 px against the 54 px three
        lines reserve -- so changing the model there moved everything below it.
        Re-measuring the worst case at the live width keeps the reservation
        correct at every size, at the cost of the note area growing when the
        user narrows the dialog, which is a resize they asked for.
        """
        super().resizeEvent(event)
        self._reserve_note_height()

    def showEvent(self, event) -> None:
        # The first resize arrives before the form layout has given the note
        # its real width, so the reservation computed there would be measured
        # against a stale one.
        super().showEvent(event)
        self._reserve_note_height()

    def _reserve_note_height(self) -> None:
        note = getattr(self, "_language_note", None)
        if note is None:
            return
        # No `layout().activate()` here. Measured across six sequences
        # (resize before show, show then resize to 320, `showMaximized`, a
        # raised minimum width, hide/resize/reshow, a font change) it changed
        # the read width in none of them, so it bought nothing.
        #
        # Two reads are stale and both are corrected by the `showEvent` that
        # must follow: the construction-time one, which returns the QWidget
        # default of 640 rather than anything `width() <= 0` would catch, and
        # a resize while the dialog is hidden -- Qt does not lay out hidden
        # widgets, so `activate()` would not have fixed that one either.
        width = note.width()
        if width <= 0:
            return
        # Measured through the label itself rather than a bare QFontMetrics:
        # the note renders at the stylesheet's 11 px and wraps under the
        # label's own margins, so anything measured beside it disagrees with
        # what is actually laid out.
        original = note.text()
        try:
            note.setText(self._worst_case_note)
            needed = note.heightForWidth(width)
        finally:
            note.setText(original)
        reserved = max(self._minimum_note_height, needed)
        # Only on a change: `setMinimumHeight` can trigger another resize, and
        # this runs from `resizeEvent`.
        if note.minimumHeight() != reserved:
            note.setMinimumHeight(reserved)

    def _update_substitution_note(self, requested: str = "") -> None:
        """Say whenever this run will not repeat the entry exactly.

        Two ways that happens, both silent before:

        * The entry's **model** was removed from the app (Granite 4.1 Plus and
          NAR on 2026-08-26), so the picker quietly offers another one and the
          new transcript is not comparable with the old.
        * The entry's **language** is unavailable for the chosen model. Canary
          has no auto-detect, so an entry recorded with `auto` (the app
          default) lands on the first offered language, and running an English
          recording that way stores a German *translation* as a new entry.
        """
        if not hasattr(self, "_language_note"):
            return
        parts: list[str] = []
        if (
            self._entry_model
            and self.selected_engine() == self._entry_engine
            and self._model_combo.findData(self._entry_model) < 0
        ):
            parts.append(
                # The names the user recognises, not the raw settings ids --
                # the same reason the streaming tooltip stopped quoting them.
                f"This entry was recorded with "
                f"'{local_model_short_label(self._entry_model)}', which this "
                f"version no longer offers, so "
                f"{local_model_short_label(self.selected_model())} was chosen "
                f"instead."
            )
        if self.selected_model() == CANARY_MODEL_SIZE:
            selected = self.selected_language_mode()
            if requested and requested != selected:
                parts.append(
                    f"This entry's language ({requested}) is unavailable "
                    f"here, so {selected} was chosen."
                )
            parts.append(_CANARY_LANGUAGE_WARNING)
        self._language_note.setText(" ".join(parts))

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
            except Exception as exc:
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
