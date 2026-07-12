"""Settings dialog: importtab mixin (split from settings_dialog.py)."""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6 import QtCore, QtGui, QtWidgets

from .app_paths import recordings_dir
from .config import DEFAULT_ENGINE, VALID_ENGINES
from .settings_dialog_helpers import (
    _emit_background_signal,
    _ENGINE_LABELS,
    _set_transcriber_progress_callback,
    _WheelPassthroughComboBox,
)
from .settings_store import AppSettings
from .ui_feedback import set_button_feedback_state


class _ImportTabMixin:
    def _build_import_tab(self) -> None:
        tab, content = self._create_scroll_tab()
        self._import_tab = tab
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        import_box = QtWidgets.QGroupBox("Import Audio File")
        import_box.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Expanding,
        )
        import_layout = QtWidgets.QVBoxLayout(import_box)

        import_controls = QtWidgets.QWidget()
        import_controls_layout = QtWidgets.QVBoxLayout(import_controls)
        import_controls_layout.setContentsMargins(0, 0, 0, 0)
        import_controls_layout.setSpacing(6)

        import_hint = QtWidgets.QLabel(
            "Transcribe an existing audio file and select the transcription service "
            "and model directly here (useful after failures or for external recordings)."
        )
        import_hint.setWordWrap(True)
        self._style_note_label(import_hint)
        import_controls_layout.addWidget(import_hint)

        self.import_engine_combo = _WheelPassthroughComboBox()
        for value in VALID_ENGINES:
            self.import_engine_combo.addItem(
                _ENGINE_LABELS.get(value, value),
                value,
            )
        self.import_engine_note = QtWidgets.QLabel("")
        self.import_engine_note.setWordWrap(True)
        self._style_note_label(self.import_engine_note)
        self.import_engine_combo.currentIndexChanged.connect(
            self._on_import_engine_changed
        )
        import_controls_layout.addWidget(QtWidgets.QLabel("Import Service"))
        import_controls_layout.addWidget(self.import_engine_combo)
        import_controls_layout.addWidget(self.import_engine_note)

        self.import_model_combo = _WheelPassthroughComboBox()
        self.import_model_note = QtWidgets.QLabel("")
        self.import_model_note.setWordWrap(True)
        self._style_note_label(self.import_model_note)
        self.import_model_combo.currentIndexChanged.connect(
            self._on_import_model_changed
        )
        import_controls_layout.addWidget(QtWidgets.QLabel("Import Model"))
        import_controls_layout.addWidget(self.import_model_combo)
        import_controls_layout.addWidget(self.import_model_note)

        import_buttons = QtWidgets.QHBoxLayout()
        self._configure_button_row(import_buttons)
        self.import_file_button = QtWidgets.QPushButton("Choose file...")
        self.import_file_button.clicked.connect(self._choose_import_file)
        self.import_last_recording_button = QtWidgets.QPushButton(
            "Use last recording"
        )
        self.import_last_recording_button.clicked.connect(
            self._select_last_recording_file
        )
        self.import_start_button = QtWidgets.QPushButton("Start transcription")
        self.import_start_button.setEnabled(False)
        self.import_start_button.clicked.connect(
            self._transcribe_selected_import_file
        )
        import_buttons.addWidget(self.import_file_button)
        import_buttons.addWidget(self.import_last_recording_button)
        import_buttons.addWidget(self.import_start_button)
        import_buttons.addStretch(1)
        import_controls_layout.addLayout(import_buttons)

        self.import_selected_file_label = QtWidgets.QLabel("No file selected.")
        self.import_selected_file_label.setWordWrap(True)
        self.import_selected_file_label.setStyleSheet("color: #555;")
        import_controls_layout.addWidget(self.import_selected_file_label)

        import_result = QtWidgets.QWidget()
        import_result_layout = QtWidgets.QVBoxLayout(import_result)
        import_result_layout.setContentsMargins(0, 0, 0, 0)
        import_result_layout.setSpacing(6)

        import_result_header = QtWidgets.QHBoxLayout()
        self._configure_button_row(import_result_header)
        import_result_header.addWidget(QtWidgets.QLabel("Result"))
        import_result_header.addStretch(1)
        self.import_copy_button = QtWidgets.QPushButton("Copy result")
        self.import_copy_button.setEnabled(False)
        self.import_copy_button.clicked.connect(self._copy_import_result)
        import_result_header.addWidget(self.import_copy_button)
        import_result_layout.addLayout(import_result_header)

        self.import_result_label = QtWidgets.QLabel("")
        self.import_result_label.setWordWrap(True)
        self.import_result_label.setTextInteractionFlags(
            QtCore.Qt.TextSelectableByMouse | QtCore.Qt.TextSelectableByKeyboard
        )
        import_result_layout.addWidget(self.import_result_label)

        self.import_result_text = QtWidgets.QPlainTextEdit()
        self.import_result_text.setReadOnly(True)
        self.import_result_text.setMinimumHeight(self.fontMetrics().height() * 12)
        self.import_result_text.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Expanding,
        )
        import_result_layout.addWidget(self.import_result_text, 1)

        self.import_splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.import_splitter.setChildrenCollapsible(False)
        self.import_splitter.addWidget(import_controls)
        self.import_splitter.addWidget(import_result)
        self.import_splitter.setStretchFactor(0, 0)
        self.import_splitter.setStretchFactor(1, 1)
        self.import_splitter.setSizes([320, 420])
        import_layout.addWidget(self.import_splitter, 1)

        self._selected_import_file_path = ""
        self._import_file_dialog: QtWidgets.QFileDialog | None = None

        layout.addWidget(import_box, 1)
        self.tabs.addTab(tab, "Import Audio")

    def _browse_recordings_dir(self) -> None:
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Select recordings directory",
            self.recordings_dir_edit.text() or str(recordings_dir()),
        )
        if path:
            self.recordings_dir_edit.setText(path)

    def _open_recordings_dir(self) -> None:
        target = self._effective_recordings_dir()
        Path(target).mkdir(parents=True, exist_ok=True)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(target))
        refresher = getattr(self._controller, "refresh_hotkey_registration", None)
        if callable(refresher):
            QtCore.QTimer.singleShot(500, refresher)

    def _effective_recordings_dir(self) -> str:
        text = self.recordings_dir_edit.text().strip()
        if text:
            return text
        return str(recordings_dir())

    @staticmethod
    def _recordings_dir_compare_value(value: str) -> str:
        text = str(value or "").strip()
        return text or str(recordings_dir())

    def _recordings_file_dialog_dir(self) -> str:
        target = self._effective_recordings_dir()
        try:
            Path(target).mkdir(parents=True, exist_ok=True)
        except OSError:
            return str(recordings_dir())
        return target

    def _import_file_dialog_dir(self) -> str:
        selected = str(self._selected_import_file_path or "").strip()
        if selected:
            parent = Path(selected).parent
            if parent.is_dir():
                return str(parent)
        return self._recordings_file_dialog_dir()

    def _archived_recordings_dir_for_selection(self) -> str | None:
        if not self.save_all_recordings_checkbox.isChecked():
            return None
        return self._effective_recordings_dir()

    def _copy_import_result(self) -> None:
        text = self.import_result_text.toPlainText()
        if not text:
            return
        QtGui.QGuiApplication.clipboard().setText(text)
        self.import_copy_button.setText("Copied")
        set_button_feedback_state(self.import_copy_button, "success")
        self._import_copy_feedback_timer.start()

    def _reset_import_copy_feedback(self) -> None:
        self.import_copy_button.setText("Copy result")
        set_button_feedback_state(self.import_copy_button, None)

    def _set_selected_import_file(self, path: str) -> None:
        selected = str(path or "").strip()
        self._selected_import_file_path = selected
        if selected and Path(selected).is_file():
            self.import_selected_file_label.setText(f"Selected: {selected}")
            self.import_selected_file_label.setStyleSheet("color: #1b5e20;")
            self.import_start_button.setEnabled(True)
        elif selected:
            self.import_selected_file_label.setText(
                f"Selected file does not exist: {selected}"
            )
            self.import_selected_file_label.setStyleSheet("color: #b71c1c;")
            self.import_start_button.setEnabled(False)
        else:
            self.import_selected_file_label.setText("No file selected.")
            self.import_selected_file_label.setStyleSheet("color: #555;")
            self.import_start_button.setEnabled(False)

    def _choose_import_file(self) -> None:
        if (
            self._import_file_dialog is not None
            and self._import_file_dialog.isVisible()
        ):
            self._import_file_dialog.raise_()
            self._import_file_dialog.activateWindow()
            return
        dialog = QtWidgets.QFileDialog(
            self,
            "Select audio file",
            self._import_file_dialog_dir(),
        )
        dialog.setNameFilter(
            "Audio files (*.wav *.mp3 *.m4a *.flac *.ogg *.opus *.webm);;All files (*)"
        )
        dialog.setFileMode(QtWidgets.QFileDialog.ExistingFile)
        dialog.setOption(QtWidgets.QFileDialog.DontUseNativeDialog, True)
        dialog.setModal(False)
        dialog.setWindowModality(QtCore.Qt.NonModal)
        available = dialog.screen().availableGeometry()
        target = QtCore.QSize(1000, 700).boundedTo(
            QtCore.QSize(
                max(720, available.width() - 80),
                max(500, available.height() - 80),
            )
        )
        dialog.setMinimumSize(720, 500)
        dialog.resize(target)
        dialog.fileSelected.connect(self._set_selected_import_file)
        dialog.finished.connect(self._on_import_file_dialog_finished)
        self._import_file_dialog = dialog
        dialog.show()

    def _on_import_file_dialog_finished(self, _result: int) -> None:
        if self._import_file_dialog is not None:
            self._import_file_dialog.deleteLater()
            self._import_file_dialog = None

    def _select_last_recording_file(self) -> bool:
        path = self._last_recording_store.selectable_path(
            self._archived_recordings_dir_for_selection()
        )
        if path is None:
            self.import_result_label.setText(
                "No last recording is currently available."
            )
            self.import_result_label.setStyleSheet("color: #b71c1c;")
            self.import_result_text.clear()
            self.import_copy_button.setEnabled(False)
            self._reset_import_copy_feedback()
            return False
        self._set_selected_import_file(str(path))
        self.import_result_label.setText(
            "Last recording loaded. Choose a provider and start transcription."
        )
        self.import_result_label.setStyleSheet("color: #555;")
        return True

    def prepare_last_recording_import(self) -> bool:
        import_index = self.tabs.indexOf(self._import_tab)
        if import_index >= 0:
            self.tabs.setCurrentIndex(import_index)
        return self._select_last_recording_file()

    def _transcribe_selected_import_file(self) -> None:
        path = self._selected_import_file_path
        if not path:
            self.import_result_label.setText("Select a file first.")
            self.import_result_label.setStyleSheet("color: #b71c1c;")
            return
        if not Path(path).is_file():
            self.import_result_label.setText(
                f"Selected file no longer exists: {path}"
            )
            self.import_result_label.setStyleSheet("color: #b71c1c;")
            self.import_start_button.setEnabled(False)
            return
        self._start_import_transcription(path)

    def _start_import_transcription(self, path: str) -> None:
        self._import_progress_started_at = datetime.now()
        self._set_import_progress("Preparing transcription...")
        self.import_result_label.setStyleSheet("color: #555;")
        self.import_result_text.clear()
        self.import_copy_button.setEnabled(False)
        self._reset_import_copy_feedback()
        self.import_file_button.setEnabled(False)
        self.import_last_recording_button.setEnabled(False)
        self.import_start_button.setEnabled(False)
        self.import_engine_combo.setEnabled(False)
        self.import_model_combo.setEnabled(False)
        self._import_progress_timer.start()

        # Build settings on the GUI thread — widgets must not be accessed
        # from background threads.
        import_engine = str(
            self.import_engine_combo.currentData() or DEFAULT_ENGINE
        )
        import_model = str(self.import_model_combo.currentData() or "")
        credential_issue = self._import_engine_credential_issue(import_engine)
        if credential_issue is not None:
            self._import_progress_timer.stop()
            self._import_progress_message = ""
            self._import_progress_started_at = None
            detail = f"Failed: {credential_issue}"
            if self._last_recording_store.is_managed_audio_path(path):
                detail = (
                    f"{detail} The last recording stays available. "
                    "Fix the provider settings and try again."
                )
            self.import_result_label.setText(detail)
            self.import_result_label.setStyleSheet("color: #b71c1c;")
            self.import_result_text.setPlainText(detail)
            self.import_copy_button.setEnabled(bool(detail))
            self.import_file_button.setEnabled(True)
            self.import_last_recording_button.setEnabled(True)
            self.import_start_button.setEnabled(
                bool(self._selected_import_file_path)
            )
            self.import_engine_combo.setEnabled(True)
            self.import_model_combo.setEnabled(True)
            return
        settings = self._build_current_settings(
            engine_override=import_engine,
            model_override=import_model,
        )

        def _run() -> None:
            def _progress(text: str) -> None:
                _emit_background_signal(
                    self,
                    "import_transcription_progress",
                    str(text),
                )

            try:
                _progress(f"Sending audio to {self._provider_label(import_engine)}...")
                ok, text = self._transcribe_import_file(
                    path,
                    settings,
                    progress_callback=_progress,
                )
            except Exception as exc:
                ok, text = False, str(exc)
            _emit_background_signal(
                self,
                "import_transcription_finished",
                bool(ok),
                str(text),
            )

        threading.Thread(
            target=_run,
            name="stt_app_import_file_transcription",
            daemon=True,
        ).start()

    def _set_import_progress(self, text: str) -> None:
        self._import_progress_message = str(text or "").strip()
        self._refresh_import_progress_label()

    def _refresh_import_progress_label(self) -> None:
        message = self._import_progress_message.strip()
        if not message:
            return
        if self._import_progress_started_at is None:
            self.import_result_label.setText(message)
            return
        elapsed = int(
            (datetime.now() - self._import_progress_started_at).total_seconds()
        )
        self.import_result_label.setText(f"{message} ({elapsed}s)")

    def _on_import_transcription_progress(self, text: str) -> None:
        self._set_import_progress(text)

    def _transcribe_import_file(
        self,
        path: str,
        settings: AppSettings,
        progress_callback: Callable[[str], None] | None = None,
    ) -> tuple[bool, str]:
        from .transcriber import create_transcriber

        if self._controller is not None:
            if progress_callback is not None:
                return self._controller.transcribe_audio_file(
                    path,
                    settings_override=settings,
                    progress_callback=progress_callback,
                )
            return self._controller.transcribe_audio_file(
                path,
                settings_override=settings,
            )

        transcriber = create_transcriber(settings, secret_store=self._secret_store)
        try:
            if progress_callback is not None:
                _set_transcriber_progress_callback(transcriber, progress_callback)
            text = transcriber.transcribe_batch(path)
        finally:
            if hasattr(transcriber, "close"):
                try:
                    transcriber.close()
                except Exception:
                    logger = getattr(self, "_settings_perf_logger", None)
                    if logger is not None:
                        logger.exception(
                            "Failed to close imported-transcription runtime"
                        )
        return True, str(text or "").strip()

    def _finish_import_transcription(self, ok: bool, text: str) -> None:
        self._import_progress_timer.stop()
        self._import_progress_message = ""
        self._import_progress_started_at = None
        self.import_file_button.setEnabled(True)
        self.import_last_recording_button.setEnabled(True)
        self.import_start_button.setEnabled(bool(self._selected_import_file_path))
        self.import_engine_combo.setEnabled(True)
        self.import_model_combo.setEnabled(True)
        if ok:
            self.import_result_label.setText("Transcription finished.")
            self.import_result_label.setStyleSheet("color: #1b5e20;")
            self.import_result_text.setPlainText(text)
            self.import_copy_button.setEnabled(bool(text))
            self._reset_import_copy_feedback()
            self._refresh_history_list(force=True)
            return
        detail = f"Failed: {text}"
        if self._last_recording_store.is_managed_audio_path(
            self._selected_import_file_path
        ):
            detail = (
                f"{detail} The last recording remains available. "
                "Fix the settings and try again."
            )
        self.import_result_label.setText(detail)
        self.import_result_label.setStyleSheet("color: #b71c1c;")
        self.import_result_text.setPlainText(detail)
        self.import_copy_button.setEnabled(bool(detail))
        self._reset_import_copy_feedback()
