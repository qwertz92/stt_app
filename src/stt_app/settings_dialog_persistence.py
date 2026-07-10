"""Settings dialog: persistence mixin (split from settings_dialog.py)."""
from __future__ import annotations

from dataclasses import replace

from PySide6 import QtCore, QtGui, QtWidgets

from .config import (
    CONCURRENT_TRANSCRIPTION_MODE_INSERT,
    DEFAULT_ASSEMBLYAI_MODEL,
    DEFAULT_AZURE_ENDPOINT,
    DEFAULT_AZURE_SPEECH_MODEL,
    DEFAULT_CANCEL_HOTKEY,
    DEFAULT_CONCURRENT_TRANSCRIPTION_MODE,
    DEFAULT_CUSTOM_VOCABULARY,
    DEFAULT_DEEPGRAM_MODEL,
    DEFAULT_DISPLAY_TIMEZONE,
    DEFAULT_ELEVENLABS_MODEL,
    DEFAULT_ENGINE,
    DEFAULT_FUNASR_MODEL,
    DEFAULT_HOTKEY,
    DEFAULT_LANGUAGE_MODE,
    DEFAULT_MODE,
    DEFAULT_OVERLAY_CORNER,
    DEFAULT_INSERT_TARGET,
    DEFAULT_SILENCE_GATE_THRESHOLD,
    DEFAULT_PASTE_MODE,
    DEFAULT_START_BEEP_TONE,
)
from .hotkey import parse_hotkey
from .settings_dialog_helpers import (
    _CONCURRENT_MODE_IMMEDIATE_UI_VALUE,
    _REMOTE_API_KEY_PROVIDERS,
    _app_hotkey_to_qt_hotkey_text,
    _hotkeys_conflict,
    _qt_hotkey_sequence_to_app_hotkey,
)
from .settings_store import AppSettings


class _PersistenceMixin:
    def _populate(self, settings: AppSettings) -> None:
        self.hotkey_edit.setKeySequence(
            QtGui.QKeySequence(
                _app_hotkey_to_qt_hotkey_text(settings.hotkey)
            )
        )
        self.cancel_hotkey_edit.setKeySequence(
            QtGui.QKeySequence(
                _app_hotkey_to_qt_hotkey_text(settings.cancel_hotkey)
            )
        )
        # Model Dir must be set before refreshing the model combo so it can
        # scan the correct directory for cached models.
        blocker = QtCore.QSignalBlocker(self.model_dir_edit)
        self.model_dir_edit.setText(settings.model_dir or "")
        del blocker
        self._refresh_model_combo(selected=settings.model_size, cached=[])
        self.vad_checkbox.setChecked(settings.vad_enabled)
        self.keep_microphone_warm_checkbox.setChecked(
            bool(getattr(settings, "keep_microphone_warm", False))
        )
        self.vad_threshold_spin.setValue(float(settings.vad_energy_threshold))
        self.silence_gate_checkbox.setChecked(
            bool(getattr(settings, "silence_gate_enabled", False))
        )
        self.silence_gate_threshold_spin.setValue(
            float(
                getattr(
                    settings,
                    "silence_gate_threshold",
                    DEFAULT_SILENCE_GATE_THRESHOLD,
                )
            )
        )
        self.start_beep_checkbox.setChecked(settings.start_beep_enabled)
        self._select_combo_data(self.start_beep_tone_combo, settings.start_beep_tone)
        self.save_wav_checkbox.setChecked(settings.save_last_wav)
        self.save_all_recordings_checkbox.setChecked(settings.save_all_recordings)
        self.recordings_dir_edit.setText(settings.recordings_dir or "")
        self.recordings_max_spin.setValue(int(settings.recordings_max_count))
        self.history_max_spin.setValue(int(settings.history_max_items))
        self._select_combo_data(
            self.history_timezone_combo,
            str(getattr(settings, "display_timezone", DEFAULT_DISPLAY_TIMEZONE)),
        )
        self._select_combo_data(self.overlay_corner_combo, settings.overlay_corner)
        self.keep_clipboard_checkbox.setChecked(
            settings.keep_transcript_in_clipboard
        )
        self.insecure_key_storage_checkbox.setChecked(
            bool(getattr(settings, "allow_insecure_key_storage", False))
        )
        self.offline_mode_checkbox.setChecked(settings.offline_mode)
        self.keep_onnx_model_loaded_checkbox.setChecked(
            bool(getattr(settings, "keep_onnx_model_loaded", False))
        )
        self._select_combo_data(self.engine_combo, settings.engine)
        self._select_combo_data(self.mode_combo, settings.mode)
        self.streaming_full_final_check.setChecked(
            bool(getattr(settings, "streaming_full_final_transcript", False))
        )
        concurrent_mode = str(
            getattr(
                settings,
                "concurrent_transcription_mode",
                DEFAULT_CONCURRENT_TRANSCRIPTION_MODE,
            )
        )
        if concurrent_mode == CONCURRENT_TRANSCRIPTION_MODE_INSERT and bool(
            getattr(settings, "immediate_background_insert", False)
        ):
            concurrent_mode = _CONCURRENT_MODE_IMMEDIATE_UI_VALUE
        self._select_combo_data(self.concurrent_mode_combo, concurrent_mode)
        self._update_mode_availability()
        self._update_language_availability(preferred_mode=settings.language_mode)
        self.custom_vocabulary_edit.setPlainText(
            str(getattr(settings, "custom_vocabulary", DEFAULT_CUSTOM_VOCABULARY))
        )
        self._update_local_model_runtime_warning()
        self._select_combo_data(self.paste_mode_combo, settings.paste_mode)
        self._select_combo_data(
            self.insert_target_combo,
            str(getattr(settings, "insert_target", DEFAULT_INSERT_TARGET)),
        )
        self._remote_model_values.update(
            {
                "groq": settings.groq_model,
                "openai": settings.openai_model,
                "deepgram": getattr(
                    settings,
                    "deepgram_model",
                    DEFAULT_DEEPGRAM_MODEL,
                ),
                "assemblyai": getattr(
                    settings,
                    "assemblyai_model",
                    DEFAULT_ASSEMBLYAI_MODEL,
                ),
                "elevenlabs": getattr(
                    settings,
                    "elevenlabs_model",
                    DEFAULT_ELEVENLABS_MODEL,
                ),
                "azure": getattr(
                    settings,
                    "azure_speech_model",
                    DEFAULT_AZURE_SPEECH_MODEL,
                ),
                "funasr": getattr(
                    settings,
                    "funasr_model",
                    DEFAULT_FUNASR_MODEL,
                ),
            }
        )
        self._import_model_values.update(
            {
                "local": settings.model_size,
                "groq": settings.groq_model,
                "openai": settings.openai_model,
                "deepgram": getattr(
                    settings,
                    "deepgram_model",
                    DEFAULT_DEEPGRAM_MODEL,
                ),
                "assemblyai": getattr(
                    settings,
                    "assemblyai_model",
                    DEFAULT_ASSEMBLYAI_MODEL,
                ),
                "elevenlabs": getattr(
                    settings,
                    "elevenlabs_model",
                    DEFAULT_ELEVENLABS_MODEL,
                ),
                "azure": getattr(
                    settings,
                    "azure_speech_model",
                    DEFAULT_AZURE_SPEECH_MODEL,
                ),
                "funasr": getattr(
                    settings,
                    "funasr_model",
                    DEFAULT_FUNASR_MODEL,
                ),
            }
        )
        if hasattr(self, "azure_endpoint_edit"):
            blocker = QtCore.QSignalBlocker(self.azure_endpoint_edit)
            self.azure_endpoint_edit.setText(
                getattr(settings, "azure_endpoint", DEFAULT_AZURE_ENDPOINT) or ""
            )
            del blocker
        self._update_remote_model_selector()
        self._select_combo_data(self.test_conn_target_combo, "all-configured")
        if hasattr(self, "import_engine_combo"):
            self._select_combo_data(self.import_engine_combo, settings.engine)
            self._update_import_model_selector()
            self._update_import_engine_note()

        if not self._prime_local_model_views_from_available_cache():
            self._show_local_model_unverified_state(
                "Open Local or Benchmark to verify local model availability in the background."
            )
        self._update_engine_indicator()
        self._refresh_history_list(force=True)
        self._refresh_benchmark_history_list()
        self._apply_secret_store_options()
        self._refresh_provider_key_statuses()
        self._restore_provider_connection_test_labels()

    def _select_combo_data(
        self, combo: QtWidgets.QComboBox, value: str
    ) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _settings_match_loaded_values(self, settings: AppSettings) -> bool:
        loaded = self._loaded_settings
        if settings == loaded:
            return True
        return replace(
            settings,
            recordings_dir=self._recordings_dir_compare_value(settings.recordings_dir),
        ) == replace(
            loaded,
            recordings_dir=self._recordings_dir_compare_value(loaded.recordings_dir),
        )

    def _apply_secret_store_options(self) -> None:
        enabled = self.insecure_key_storage_checkbox.isChecked()
        setter = getattr(self._secret_store, "set_insecure_fallback_enabled", None)
        if callable(setter):
            try:
                setter(enabled)
            except Exception:
                pass
        if enabled:
            self.key_storage_status_label.setStyleSheet("color: #b26a00;")
            self.key_storage_status_label.setText(
                "Insecure key fallback is enabled. "
                "If secure storage fails, keys are saved in plain text."
            )
        elif not self.key_storage_status_label.text().startswith("Could not store"):
            self.key_storage_status_label.setStyleSheet("color: #555;")
            self.key_storage_status_label.setText(
                "Credential Manager only (recommended)."
            )
        self._refresh_provider_key_statuses()

    def _stored_provider_key_states(self) -> dict[str, bool]:
        states: dict[str, bool] = {}
        key_getter = getattr(self._secret_store, "get_api_key", None)
        for provider in _REMOTE_API_KEY_PROVIDERS:
            if not callable(key_getter):
                states[provider] = False
                continue
            try:
                states[provider] = bool(key_getter(provider))
            except Exception:
                states[provider] = False
        return states

    def _persist_provider_key_changes(self) -> tuple[dict[str, bool], list[str], bool]:
        self._apply_secret_store_options()
        errors: list[str] = []
        changed = False
        pending_clear = set(self._provider_pending_clear)

        for provider in _REMOTE_API_KEY_PROVIDERS:
            key_field = self._provider_key_edits.get(provider)
            if key_field is None:
                continue
            label = self._provider_label(provider)
            value = key_field.text().strip()
            if value:
                changed = True
                try:
                    self._secret_store.set_api_key(provider, value)
                    key_field.clear()
                    self._provider_pending_clear.discard(provider)
                    self._clear_provider_connection_test(provider)
                except Exception as exc:
                    errors.append(f"{label}: {exc}")
            elif provider in pending_clear:
                changed = True
                try:
                    self._secret_store.delete_api_key(provider)
                    self._provider_pending_clear.discard(provider)
                    self._clear_provider_connection_test(provider)
                except Exception as exc:
                    errors.append(f"{label} delete: {exc}")

        states = self._stored_provider_key_states()
        self._refresh_provider_key_statuses()
        self._update_import_engine_note()
        return states, errors, changed

    def _show_key_storage_result(self, errors: list[str], changed: bool) -> None:
        if errors:
            self.key_storage_status_label.setStyleSheet("color: #b71c1c;")
            self.key_storage_status_label.setText(
                "Could not store some API keys in Credential Manager. "
                "Enable insecure fallback storage or retry. "
                + " | ".join(errors)
            )
            return
        if changed:
            self.key_storage_status_label.setStyleSheet("color: #1b5e20;")
            self.key_storage_status_label.setText("API key storage updated.")
        else:
            self.key_storage_status_label.setStyleSheet("color: #555;")
            self.key_storage_status_label.setText("No API key changes to save.")

    def _save_api_keys_only(self) -> None:
        key_states, key_storage_errors, changed = self._persist_provider_key_changes()
        metadata_changed = (
            self.insecure_key_storage_checkbox.isChecked()
            != bool(getattr(self._loaded_settings, "allow_insecure_key_storage", False))
        )
        self._show_key_storage_result(key_storage_errors, changed or metadata_changed)
        if key_storage_errors:
            return

        updated = replace(
            self._loaded_settings,
            allow_insecure_key_storage=self.insecure_key_storage_checkbox.isChecked(),
            has_openai_key=key_states["openai"],
            has_deepgram_key=key_states["deepgram"],
            has_assemblyai_key=key_states["assemblyai"],
            has_groq_key=key_states["groq"],
            has_elevenlabs_key=key_states["elevenlabs"],
            has_azure_key=key_states["azure"],
            has_funasr_key=key_states["funasr"],
            azure_endpoint=self.azure_endpoint_edit.text().strip(),
        )
        try:
            self._settings_store.save(updated)
        except Exception as exc:
            self.key_storage_status_label.setStyleSheet("color: #b71c1c;")
            self.key_storage_status_label.setText(
                f"API keys were saved, but key metadata could not be persisted: {exc}"
            )
            return
        self._loaded_settings = updated

    def _construct_settings_from_widgets(
        self,
        *,
        hotkey: str | None = None,
        cancel_hotkey: str | None = None,
        history_limit: int | None = None,
        key_states: dict[str, bool] | None = None,
        model_size: str | None = None,
        engine: str | None = None,
    ) -> AppSettings:
        """Construct an ``AppSettings`` from current widget state.

        Fields that differ between callers are taken from the keyword
        arguments; every other field is read from the widgets exactly once.
        When an argument is ``None`` it falls back to the value implied by
        ``self._loaded_settings`` (or the relevant widget), matching the
        historical behavior of ``_build_current_settings``.

        Must be called on the GUI thread.
        """
        latest_overlay_opacity = int(
            self._settings_store.load().overlay_opacity_percent
        )
        selected_concurrent_mode = str(
            self.concurrent_mode_combo.currentData()
            or DEFAULT_CONCURRENT_TRANSCRIPTION_MODE
        )
        if key_states is None:
            has_openai_key = self._loaded_settings.has_openai_key
            has_deepgram_key = self._loaded_settings.has_deepgram_key
            has_assemblyai_key = self._loaded_settings.has_assemblyai_key
            has_groq_key = self._loaded_settings.has_groq_key
            has_elevenlabs_key = getattr(
                self._loaded_settings, "has_elevenlabs_key", False
            )
            has_azure_key = getattr(self._loaded_settings, "has_azure_key", False)
            has_funasr_key = getattr(self._loaded_settings, "has_funasr_key", False)
        else:
            has_openai_key = key_states["openai"]
            has_deepgram_key = key_states["deepgram"]
            has_assemblyai_key = key_states["assemblyai"]
            has_groq_key = key_states["groq"]
            has_elevenlabs_key = key_states["elevenlabs"]
            has_azure_key = key_states["azure"]
            has_funasr_key = key_states["funasr"]
        return AppSettings(
            hotkey=hotkey if hotkey is not None else self._loaded_settings.hotkey,
            cancel_hotkey=(
                cancel_hotkey
                if cancel_hotkey is not None
                else self._loaded_settings.cancel_hotkey
            ),
            model_size=(
                model_size
                if model_size is not None
                else str(
                    self.model_combo.currentData() or self._loaded_settings.model_size
                )
            ),
            language_mode=str(
                self.language_combo.currentData() or DEFAULT_LANGUAGE_MODE
            ),
            custom_vocabulary=self.custom_vocabulary_edit.toPlainText(),
            vad_enabled=self.vad_checkbox.isChecked(),
            keep_microphone_warm=(
                self.keep_microphone_warm_checkbox.isChecked()
            ),
            silence_gate_enabled=self.silence_gate_checkbox.isChecked(),
            silence_gate_threshold=float(
                self.silence_gate_threshold_spin.value()
            ),
            vad_energy_threshold=float(self.vad_threshold_spin.value()),
            save_last_wav=self.save_wav_checkbox.isChecked(),
            save_all_recordings=self.save_all_recordings_checkbox.isChecked(),
            recordings_dir=self._effective_recordings_dir(),
            recordings_max_count=int(self.recordings_max_spin.value()),
            history_max_items=(
                history_limit
                if history_limit is not None
                else int(self.history_max_spin.value())
            ),
            display_timezone=str(
                self.history_timezone_combo.currentData() or DEFAULT_DISPLAY_TIMEZONE
            ),
            overlay_opacity_percent=latest_overlay_opacity,
            keep_transcript_in_clipboard=self.keep_clipboard_checkbox.isChecked(),
            allow_insecure_key_storage=self.insecure_key_storage_checkbox.isChecked(),
            offline_mode=self.offline_mode_checkbox.isChecked(),
            keep_onnx_model_loaded=self.keep_onnx_model_loaded_checkbox.isChecked(),
            start_beep_enabled=self.start_beep_checkbox.isChecked(),
            start_beep_tone=str(
                self.start_beep_tone_combo.currentData() or DEFAULT_START_BEEP_TONE
            ),
            overlay_corner=str(
                self.overlay_corner_combo.currentData() or DEFAULT_OVERLAY_CORNER
            ),
            model_dir=self.model_dir_edit.text().strip(),
            engine=str(engine or self.engine_combo.currentData() or DEFAULT_ENGINE),
            mode=str(self.mode_combo.currentData() or DEFAULT_MODE),
            streaming_full_final_transcript=(
                self.streaming_full_final_check.isChecked()
            ),
            concurrent_transcription_mode=(
                CONCURRENT_TRANSCRIPTION_MODE_INSERT
                if selected_concurrent_mode == _CONCURRENT_MODE_IMMEDIATE_UI_VALUE
                else selected_concurrent_mode
            ),
            immediate_background_insert=(
                selected_concurrent_mode == _CONCURRENT_MODE_IMMEDIATE_UI_VALUE
            ),
            paste_mode=str(
                self.paste_mode_combo.currentData() or DEFAULT_PASTE_MODE
            ),
            insert_target=str(
                self.insert_target_combo.currentData() or DEFAULT_INSERT_TARGET
            ),
            has_openai_key=has_openai_key,
            has_deepgram_key=has_deepgram_key,
            has_assemblyai_key=has_assemblyai_key,
            has_groq_key=has_groq_key,
            has_elevenlabs_key=has_elevenlabs_key,
            has_azure_key=has_azure_key,
            has_funasr_key=has_funasr_key,
            groq_model=self._remote_model_value_for_provider("groq"),
            openai_model=self._remote_model_value_for_provider("openai"),
            deepgram_model=self._remote_model_value_for_provider("deepgram"),
            assemblyai_model=self._remote_model_value_for_provider("assemblyai"),
            elevenlabs_model=self._remote_model_value_for_provider("elevenlabs"),
            azure_speech_model=self._remote_model_value_for_provider("azure"),
            azure_endpoint=self.azure_endpoint_edit.text().strip(),
            funasr_model=self._remote_model_value_for_provider("funasr"),
        )

    def _build_current_settings(
        self,
        *,
        engine_override: str | None = None,
        model_override: str | None = None,
    ) -> AppSettings:
        """Construct an ``AppSettings`` from current widget state.

        Delegates the widget reads to ``_construct_settings_from_widgets``
        and then applies the engine/model override resolution.
        """
        settings = self._construct_settings_from_widgets(engine=engine_override)
        effective_engine = str(
            engine_override or self.engine_combo.currentData() or DEFAULT_ENGINE
        )
        return self._apply_engine_model_selection(
            settings,
            effective_engine,
            str(model_override or ""),
        )

    def _save(self) -> None:
        hotkey = _qt_hotkey_sequence_to_app_hotkey(
            self.hotkey_edit.keySequence()
        )
        hotkey = hotkey or DEFAULT_HOTKEY
        cancel_hotkey = _qt_hotkey_sequence_to_app_hotkey(
            self.cancel_hotkey_edit.keySequence()
        )
        cancel_hotkey = cancel_hotkey or DEFAULT_CANCEL_HOTKEY
        try:
            parse_hotkey(hotkey)
        except ValueError as exc:
            QtWidgets.QMessageBox.critical(
                self,
                "Invalid hotkey",
                f"The hotkey is invalid: {exc}",
            )
            return
        try:
            parse_hotkey(cancel_hotkey)
        except ValueError as exc:
            QtWidgets.QMessageBox.critical(
                self,
                "Invalid cancel hotkey",
                f"The cancel hotkey is invalid: {exc}",
            )
            return
        if _hotkeys_conflict(hotkey, cancel_hotkey):
            QtWidgets.QMessageBox.critical(
                self,
                "Hotkey conflict",
                "Cancel hotkey must not be identical to, subset of, or superset "
                "of the main recording hotkey.",
            )
            return

        requested_history_limit = int(self.history_max_spin.value())
        current_history_count = self._history_store.count()
        history_limit_changed = (
            requested_history_limit != int(self._loaded_settings.history_max_items)
        )
        if (
            history_limit_changed
            and
            requested_history_limit > 0
            and current_history_count > requested_history_limit
        ):
            to_delete = current_history_count - requested_history_limit
            answer = QtWidgets.QMessageBox.question(
                self,
                "Reduce history size",
                (
                    f"Reducing the history limit to {requested_history_limit} will "
                    f"delete {to_delete} oldest entr{'y' if to_delete == 1 else 'ies'}.\n\n"
                    "Do you want to continue?"
                ),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if answer != QtWidgets.QMessageBox.Yes:
                return

        key_states, key_storage_errors, key_storage_changed = (
            self._persist_provider_key_changes()
        )
        if key_storage_errors or key_storage_changed:
            self._show_key_storage_result(key_storage_errors, key_storage_changed)

        settings = self._construct_settings_from_widgets(
            hotkey=hotkey,
            cancel_hotkey=cancel_hotkey,
            history_limit=requested_history_limit,
            key_states=key_states,
            model_size=str(self.model_combo.currentData()),
        )

        settings_changed = not self._settings_match_loaded_values(settings)
        if not settings_changed and not key_storage_changed:
            if not key_storage_errors:
                self._set_bottom_status("No settings changes")
                self._save_status_timer.start()
            return

        if history_limit_changed and requested_history_limit > 0:
            self._history_store.apply_max_items(requested_history_limit)
        if settings_changed:
            self._settings_store.save(settings)
            self._loaded_settings = settings
        self._set_bottom_status(
            "\u2713 Settings saved" if settings_changed else "\u2713 API keys saved"
        )
        self._save_status_timer.start()
        self.settings_changed.emit()
