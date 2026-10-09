"""The startup notice for recordings an earlier session did not transcribe.

Lists every recording in the `UnfinishedRecordingStore` with when it was
recorded, how long it is and its size, and lets the user transcribe all or
some of them into history, reveal them in Explorer to listen first, delete
them, or keep the files and stop being asked. Revealing never closes the
notice: the owner listens to the files before deciding.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from .app_icon import load_app_icon
from .dialog_style import apply_dialog_style, make_label_selectable, styled_message_box
from .history_audio import reveal_paths_in_file_manager
from .settings_dialog_helpers import (
    _THREAD_START_ERRORS,
    _emit_background_signal,
    exception_reason,
)
from .ui_feedback import reserve_button_width_for_texts
from .unfinished_recordings import UnfinishedRecording, UnfinishedRecordingStore

TranscribeRecording = Callable[
    [UnfinishedRecording, Callable[[str], None] | None], "tuple[bool, str]"
]

_DIALOG_WIDTH = 680
_VISIBLE_ROWS = 6
_STATUS_LINES = 3

_PENDING = "pending"
_QUEUED = "queued"
_RUNNING = "running"
_DONE = "done"
_FAILED = "failed"
_DELETED = "deleted"
_MOVED = "moved"
# Rows an action can still take: not yet handled, or handled and failed.
_ACTIONABLE = (_PENDING, _FAILED)

_COLUMNS = ("Recorded", "Length", "Size", "Status")
_LATER_TEXT = "Ask again next start"
_CLOSE_TEXT = "Close"


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    total = round(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def format_size(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "unknown"
    if size_bytes < 1024 * 1024:
        return f"{max(1, round(size_bytes / 1024))} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def _confirm_delete(parent: QtWidgets.QWidget, count: int) -> bool:
    noun = "this recording" if count == 1 else f"these {count} recordings"
    box = styled_message_box(
        icon=QtWidgets.QMessageBox.Warning,
        title="Delete recordings",
        text=f"Delete {noun}? They are not in History and cannot be restored.",
        buttons=QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        default_button=QtWidgets.QMessageBox.No,
        parent=parent,
    )
    return box.exec() == QtWidgets.QMessageBox.Yes


class UnfinishedRecordingsDialog(QtWidgets.QDialog):
    """One fixed-size window for the whole decision.

    The table shows a fixed number of rows and scrolls past them, the note
    never changes after the window opens, and the status line has its height
    reserved, so nothing moves while rows are transcribed or deleted.
    """

    _row_started = QtCore.Signal(str)
    _row_progress = QtCore.Signal(str, str)
    _row_finished = QtCore.Signal(str, bool, str)
    _run_finished = QtCore.Signal()

    def __init__(
        self,
        *,
        recordings: Sequence[UnfinishedRecording],
        store: UnfinishedRecordingStore,
        transcribe: TranscribeRecording,
        keep_dir: Path,
        reveal: Callable[[Sequence[Path]], bool] = reveal_paths_in_file_manager,
        confirm_delete: Callable[[QtWidgets.QWidget, int], bool] = _confirm_delete,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._recordings = list(recordings)
        self._store = store
        self._transcribe = transcribe
        self._keep_dir = Path(keep_dir)
        self._reveal = reveal
        self._confirm_delete = confirm_delete
        self._states = {item.recording_id: _PENDING for item in self._recordings}
        self._running = False

        self.setWindowTitle("Unfinished recordings")
        self.setWindowIcon(load_app_icon())
        apply_dialog_style(self)

        count = len(self._recordings)
        intro = QtWidgets.QLabel(
            f"{count} recording{'s' if count != 1 else ''} from an earlier session "
            f"{'was' if count == 1 else 'were'} not transcribed."
        )
        intro_font = intro.font()
        intro_font.setBold(True)
        intro.setFont(intro_font)

        self.table = QtWidgets.QTableWidget(count, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(QtCore.Qt.ElideRight)
        header = self.table.horizontalHeader()
        for column in range(len(_COLUMNS) - 1):
            header.setSectionResizeMode(column, QtWidgets.QHeaderView.ResizeToContents)
        header.setStretchLastSection(True)
        for row, recording in enumerate(self._recordings):
            recorded = (
                recording.recorded_at.strftime("%Y-%m-%d %H:%M:%S")
                if recording.recorded_at is not None
                else "unknown"
            )
            cells = (
                recorded,
                format_duration(recording.duration_s),
                format_size(recording.size_bytes),
                "Not transcribed",
            )
            for column, text in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(text)
                item.setToolTip(str(recording.path) if column == 0 else text)
                self.table.setItem(row, column, item)
        self.table.itemSelectionChanged.connect(self._update_buttons)

        note = QtWidgets.QLabel(
            "Transcripts are saved to History; nothing is pasted. Show in folder "
            "opens Explorer with the recordings selected, so you can listen to "
            "them first; this window stays open.\n"
            f"Keep files and close moves the remaining recordings to {self._keep_dir}"
            " and does not ask again.\n"
            f"Ask again next start leaves them in {self._store.directory}."
        )
        note.setWordWrap(True)
        note.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
        make_label_selectable(note)

        self.status_label = QtWidgets.QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
        make_label_selectable(self.status_label)

        self.transcribe_selected_button = QtWidgets.QPushButton(
            "Transcribe selected", self
        )
        self.transcribe_all_button = QtWidgets.QPushButton("Transcribe all", self)
        self.transcribe_all_button.setProperty("primary", True)
        self.reveal_button = QtWidgets.QPushButton("Show in folder", self)
        self.delete_button = QtWidgets.QPushButton("Delete selected...", self)
        self.keep_button = QtWidgets.QPushButton("Keep files and close", self)
        self.later_button = QtWidgets.QPushButton(_LATER_TEXT, self)
        # "Close" once nothing is left to ask about; the width stays that of
        # the wider label.
        reserve_button_width_for_texts(self.later_button, (_LATER_TEXT, _CLOSE_TEXT))
        self.transcribe_selected_button.clicked.connect(self._transcribe_selected)
        self.transcribe_all_button.clicked.connect(self._transcribe_all)
        self.reveal_button.clicked.connect(self._reveal_rows)
        self.delete_button.clicked.connect(self._delete_selected)
        self.keep_button.clicked.connect(self._keep_and_close)
        self.later_button.clicked.connect(self.reject)

        row_actions = QtWidgets.QHBoxLayout()
        row_actions.addWidget(self.transcribe_all_button)
        row_actions.addWidget(self.transcribe_selected_button)
        row_actions.addWidget(self.reveal_button)
        row_actions.addStretch(1)
        row_actions.addWidget(self.delete_button)
        closing = QtWidgets.QHBoxLayout()
        closing.addStretch(1)
        closing.addWidget(self.keep_button)
        closing.addWidget(self.later_button)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.table)
        layout.addLayout(row_actions)
        layout.addWidget(note)
        layout.addWidget(self.status_label)
        layout.addLayout(closing)

        self._row_started.connect(self._on_row_started)
        self._row_progress.connect(self._on_row_progress)
        self._row_finished.connect(self._on_row_finished)
        self._run_finished.connect(self._on_run_finished)

        self._fix_size(layout, note)
        self._update_buttons()

    def _fix_size(self, layout: QtWidgets.QVBoxLayout, note: QtWidgets.QLabel) -> None:
        self.ensurePolished()
        margins = layout.contentsMargins()
        text_width = _DIALOG_WIDTH - margins.left() - margins.right()
        note.setFixedHeight(note.heightForWidth(text_width))
        lines = self.status_label.fontMetrics().lineSpacing() * _STATUS_LINES
        self.status_label.setFixedHeight(lines + 4)
        row_height = self.table.verticalHeader().defaultSectionSize()
        frame = self.table.frameWidth() * 2
        self.table.setFixedHeight(
            self.table.horizontalHeader().sizeHint().height()
            + row_height * _VISIBLE_ROWS
            + frame
        )
        layout.activate()
        self.setFixedSize(_DIALOG_WIDTH, layout.sizeHint().height())

    # -- rows ---------------------------------------------------------------

    def _row_of(self, recording_id: str) -> int:
        for row, recording in enumerate(self._recordings):
            if recording.recording_id == recording_id:
                return row
        return -1

    def _set_status_cell(self, recording_id: str, text: str) -> None:
        row = self._row_of(recording_id)
        if row < 0:
            return
        item = self.table.item(row, len(_COLUMNS) - 1)
        item.setText(text)
        item.setToolTip(text)

    def _selected(self) -> list[UnfinishedRecording]:
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        return [self._recordings[row] for row in rows]

    def _actionable(
        self, recordings: Sequence[UnfinishedRecording]
    ) -> list[UnfinishedRecording]:
        return [
            item
            for item in recordings
            if self._states[item.recording_id] in _ACTIONABLE
        ]

    def _update_buttons(self) -> None:
        remaining = self._actionable(self._recordings)
        selected = self._actionable(self._selected())
        idle = not self._running
        self.transcribe_all_button.setEnabled(idle and bool(remaining))
        self.transcribe_selected_button.setEnabled(idle and bool(selected))
        self.delete_button.setEnabled(idle and bool(selected))
        self.keep_button.setEnabled(idle and bool(remaining))
        self.later_button.setText(_LATER_TEXT if remaining else _CLOSE_TEXT)
        self.reveal_button.setEnabled(
            any(item.path.is_file() for item in self._selected() or self._recordings)
        )

    def show_problem(self, text: str) -> None:
        """A problem found while gathering the recordings, shown as an error."""
        self._set_status(text, error=True)

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setToolTip(text)
        self.status_label.setStyleSheet("color: #b71c1c;" if error else "")

    # -- actions --------------------------------------------------------------

    def _transcribe_all(self) -> None:
        self._start_run(self._actionable(self._recordings))

    def _transcribe_selected(self) -> None:
        self._start_run(self._actionable(self._selected()))

    def _start_run(self, recordings: list[UnfinishedRecording]) -> None:
        if self._running or not recordings:
            return
        self._running = True
        for item in recordings:
            self._states[item.recording_id] = _QUEUED
            self._set_status_cell(item.recording_id, "Waiting")
        self._set_status(f"Transcribing {len(recordings)} recording(s)...")
        self._update_buttons()
        transcribe = self._transcribe

        def _run() -> None:
            # A window closed meanwhile only stops the reports: every
            # transcript still goes to history.
            emit = _emit_background_signal
            for item in recordings:
                emit(self, "_row_started", item.recording_id)

                def _progress(message: str, recording_id=item.recording_id) -> None:
                    emit(self, "_row_progress", recording_id, str(message))

                try:
                    ok, text = transcribe(item, _progress)
                except Exception as exc:
                    ok, text = False, exception_reason(exc)
                emit(self, "_row_finished", item.recording_id, bool(ok), str(text))
            emit(self, "_run_finished")

        try:
            threading.Thread(
                target=_run, name="stt_app_unfinished_recordings", daemon=True
            ).start()
        except _THREAD_START_ERRORS as exc:
            for item in recordings:
                self._states[item.recording_id] = _PENDING
                self._set_status_cell(item.recording_id, "Not transcribed")
            self._running = False
            self._set_status(
                f"The transcription could not start: {exception_reason(exc)}",
                error=True,
            )
            self._update_buttons()

    def _on_row_started(self, recording_id: str) -> None:
        self._states[recording_id] = _RUNNING
        self._set_status_cell(recording_id, "Transcribing...")

    def _on_row_progress(self, recording_id: str, message: str) -> None:
        if self._states.get(recording_id) == _RUNNING and message.strip():
            self._set_status_cell(recording_id, message.strip())

    def _on_row_finished(self, recording_id: str, ok: bool, text: str) -> None:
        if ok:
            self._states[recording_id] = _DONE
            self._set_status_cell(recording_id, "Transcribed, in History")
        else:
            self._states[recording_id] = _FAILED
            self._set_status_cell(recording_id, f"Failed: {text}")
        self._update_buttons()

    def _on_run_finished(self) -> None:
        self._running = False
        failed = sum(1 for state in self._states.values() if state == _FAILED)
        done = sum(1 for state in self._states.values() if state == _DONE)
        if failed:
            self._set_status(
                f"{done} transcribed into History, {failed} failed; a failed "
                "recording stays here. Point at its status for the error.",
                error=True,
            )
        else:
            self._set_status(f"{done} transcribed into History.")
        self._update_buttons()

    def _reveal_rows(self) -> None:
        chosen = self._selected() or self._recordings
        paths = [item.path for item in chosen if item.path.is_file()]
        if not paths:
            self._set_status("These recordings are no longer on disk.", error=True)
            return
        if not self._reveal(paths):
            self._set_status(
                f"Explorer could not be opened for {paths[0]}.", error=True
            )

    def _delete_selected(self) -> None:
        chosen = self._actionable(self._selected())
        if not chosen or not self._confirm_delete(self, len(chosen)):
            return
        errors = []
        for item in chosen:
            try:
                self._store.discard(item)
            except OSError as exc:
                errors.append(f"{item.path.name}: {exc}")
                continue
            self._states[item.recording_id] = _DELETED
            self._set_status_cell(item.recording_id, "Deleted")
        if errors:
            self._set_status("Not deleted: " + "; ".join(errors), error=True)
        else:
            self._set_status(f"{len(chosen)} recording(s) deleted.")
        self._update_buttons()

    def _keep_and_close(self) -> None:
        errors = []
        for item in self._actionable(self._recordings):
            try:
                self._store.move_to(item, self._keep_dir)
            except OSError as exc:
                errors.append(f"{item.path.name}: {exc}")
                continue
            self._states[item.recording_id] = _MOVED
            self._set_status_cell(item.recording_id, f"Moved to {self._keep_dir}")
        if errors:
            self._set_status("Not moved: " + "; ".join(errors), error=True)
            self._update_buttons()
            return
        self.accept()
