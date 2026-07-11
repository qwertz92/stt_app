"""Settings dialog: remote mixin (split from settings_dialog.py)."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from PySide6 import QtCore, QtWidgets

from .config import DEFAULT_ENGINE, DEFAULT_LANGUAGE_MODE
from .settings_dialog_helpers import (
    _emit_background_signal,
    _remote_provider_label,
    _REMOTE_PROVIDER_GRID_SPACING_PX,
    _REMOTE_PROVIDERS,
    _WheelPassthroughComboBox,
)


@dataclass(frozen=True)
class _ConnectionTestSnapshot:
    """Widget values for one provider test, captured on the GUI thread.

    The connection-test worker thread must never read Qt widgets, so every
    value it needs is snapshotted into this plain dataclass before the
    thread starts.
    """

    api_key: str
    model: str
    language_mode: str
    azure_endpoint: str = ""


def _assemblyai_transcriber_factory(**kwargs: object) -> object:
    from .transcriber.assemblyai_provider import AssemblyAITranscriber

    return AssemblyAITranscriber(**kwargs)


def _groq_transcriber_factory(**kwargs: object) -> object:
    from .transcriber.groq_provider import GroqTranscriber

    return GroqTranscriber(**kwargs)


def _openai_transcriber_factory(**kwargs: object) -> object:
    from .transcriber.openai_provider import OpenAITranscriber

    return OpenAITranscriber(**kwargs)


def _deepgram_transcriber_factory(**kwargs: object) -> object:
    from .transcriber.deepgram_provider import DeepgramTranscriber

    return DeepgramTranscriber(**kwargs)


def _elevenlabs_transcriber_factory(**kwargs: object) -> object:
    from .transcriber.elevenlabs_provider import ElevenLabsTranscriber

    return ElevenLabsTranscriber(**kwargs)


def _azure_transcriber_factory(**kwargs: object) -> object:
    from .transcriber.azure_provider import AzureLlmSpeechTranscriber

    return AzureLlmSpeechTranscriber(**kwargs)


def _funasr_transcriber_factory(**kwargs: object) -> object:
    from .transcriber.funasr_provider import FunAsrTranscriber

    return FunAsrTranscriber(**kwargs)


# Maps provider name to (lazy transcriber factory, extra snapshot fields the
# factory needs besides api_key and model).
_CONNECTION_TESTER_FACTORIES: dict[
    str,
    tuple[Callable[..., object], tuple[str, ...]],
] = {
    "assemblyai": (_assemblyai_transcriber_factory, ()),
    "groq": (_groq_transcriber_factory, ()),
    "openai": (_openai_transcriber_factory, ()),
    "deepgram": (_deepgram_transcriber_factory, ()),
    "elevenlabs": (_elevenlabs_transcriber_factory, ("language_mode",)),
    "azure": (_azure_transcriber_factory, ("language_mode", "endpoint")),
    "funasr": (_funasr_transcriber_factory, ("language_mode",)),
}


def _build_connection_tester(
    provider: str,
    snapshot: _ConnectionTestSnapshot,
) -> tuple[Callable[[], tuple[bool, str]] | None, str | None]:
    """Build the ``test_connection`` callable for *provider* from *snapshot*.

    Safe to call from the worker thread: it only reads the snapshot, never
    Qt widgets. Returns ``(tester, None)`` on success and
    ``(None, error_text)`` — or ``(None, None)`` for an unknown provider —
    otherwise.
    """
    factory_entry = _CONNECTION_TESTER_FACTORIES.get(provider)
    if factory_entry is None:
        return None, None
    if not snapshot.api_key:
        return None, "No API key entered. Enter a key above first."
    factory, extra_fields = factory_entry
    kwargs: dict[str, object] = {
        "api_key": snapshot.api_key,
        "model": snapshot.model,
    }
    if "language_mode" in extra_fields:
        kwargs["language_mode"] = snapshot.language_mode
    if "endpoint" in extra_fields:
        if not snapshot.azure_endpoint:
            return (
                None,
                "No Azure endpoint entered. "
                "Enter the resource endpoint above first.",
            )
        kwargs["endpoint"] = snapshot.azure_endpoint
    try:
        transcriber = factory(**kwargs)
    except Exception as exc:
        return None, str(exc)
    return transcriber.test_connection, None


class _RemoteProvidersMixin:
    def _build_remote_tab(self) -> None:
        tab, content = self._create_scroll_tab()
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        # API keys
        provider_box = QtWidgets.QGroupBox("Remote Provider API Keys")
        provider_layout = QtWidgets.QVBoxLayout(provider_box)
        provider_layout.setContentsMargins(10, 10, 10, 10)
        provider_layout.setSpacing(6)
        provider_rows = tuple(
            (provider.name, provider.title) for provider in _REMOTE_PROVIDERS
        )
        provider_intro = QtWidgets.QLabel(
            "Enter a key only when you want to replace the stored one. The status badge shows whether the app already has a usable key."
        )
        provider_intro.setWordWrap(True)
        self._style_note_label(provider_intro)
        provider_layout.addWidget(provider_intro)

        provider_label_width = self._remote_provider_label_width(provider_rows)
        status_badge_width = self._provider_status_badge_width()
        provider_grid = QtWidgets.QGridLayout()
        provider_grid.setContentsMargins(0, 0, 0, 0)
        provider_grid.setHorizontalSpacing(_REMOTE_PROVIDER_GRID_SPACING_PX)
        provider_grid.setVerticalSpacing(3)
        provider_grid.setColumnMinimumWidth(0, provider_label_width)
        provider_grid.setColumnStretch(1, 1)
        provider_grid.setColumnStretch(2, 0)
        provider_grid.setColumnStretch(3, 0)

        grid_row = 0
        for provider, title in provider_rows:
            key_field = QtWidgets.QLineEdit()
            key_field.setEchoMode(QtWidgets.QLineEdit.Password)
            key_field.setPlaceholderText(
                "Enter new key to update; use Clear saved to remove the stored key."
            )
            key_field.setMinimumWidth(180)
            key_field.textChanged.connect(
                lambda _text, p=provider: self._on_provider_key_changed(p)
            )
            clear_button = QtWidgets.QPushButton("Clear saved")
            clear_button.setToolTip("Delete the stored key for this provider on Save.")
            clear_button.setMinimumWidth(78)
            clear_button.setMaximumWidth(88)
            self._match_field_button_height(key_field, clear_button)
            clear_button.clicked.connect(
                lambda _checked=False, p=provider: self._mark_provider_key_for_clear(p)
            )

            status_badge = QtWidgets.QLabel("Not configured")
            status_badge.setAlignment(
                QtCore.Qt.AlignCenter | QtCore.Qt.AlignVCenter
            )
            status_badge.setFixedWidth(status_badge_width)
            status_badge.setSizePolicy(
                QtWidgets.QSizePolicy.Fixed,
                QtWidgets.QSizePolicy.Fixed,
            )
            status_badge.setStyleSheet(
                "padding: 2px 8px; border: 1px solid #bbb; border-radius: 9px;"
                " color: #555; background: #f2f2f2;"
            )

            title_label = QtWidgets.QLabel(title)
            title_label.setFixedWidth(provider_label_width)
            title_label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)

            last_test_label = QtWidgets.QLabel("Last test: never.")
            last_test_label.setWordWrap(True)
            self._style_provider_last_test_label(last_test_label)
            provider_grid.addWidget(
                title_label,
                grid_row,
                0,
                QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
            )
            provider_grid.addWidget(key_field, grid_row, 1)
            provider_grid.addWidget(clear_button, grid_row, 2)
            provider_grid.addWidget(status_badge, grid_row, 3)
            provider_grid.addWidget(last_test_label, grid_row + 1, 1, 1, 3)
            provider_grid.setRowMinimumHeight(
                grid_row + 1,
                max(1, self.fontMetrics().height()),
            )
            grid_row += 2

            self._provider_key_edits[provider] = key_field
            self._provider_status_labels[provider] = status_badge
            self._provider_last_test_labels[provider] = last_test_label

        self.assemblyai_key_edit = self._provider_key_edits["assemblyai"]
        self.groq_key_edit = self._provider_key_edits["groq"]
        self.openai_key_edit = self._provider_key_edits["openai"]
        self.deepgram_key_edit = self._provider_key_edits["deepgram"]
        self.elevenlabs_key_edit = self._provider_key_edits["elevenlabs"]
        self.azure_key_edit = self._provider_key_edits["azure"]
        self.funasr_key_edit = self._provider_key_edits["funasr"]

        # Azure additionally needs a per-resource endpoint (no other provider
        # does), so it gets a dedicated, non-secret text field here.
        self.azure_endpoint_edit = QtWidgets.QLineEdit()
        self.azure_endpoint_edit.setPlaceholderText(
            "https://<resource>.cognitiveservices.azure.com"
        )
        self.azure_endpoint_edit.setMinimumWidth(180)
        azure_endpoint_hint = QtWidgets.QLabel(
            "Required for Azure LLM Speech. Copy the endpoint from your Azure "
            "Speech / Foundry resource (Keys and Endpoint). The region must "
            "support LLM Speech."
        )
        azure_endpoint_hint.setWordWrap(True)
        self._style_note_label(azure_endpoint_hint)
        azure_endpoint_label = QtWidgets.QLabel("Azure Endpoint")
        azure_endpoint_label.setFixedWidth(provider_label_width)
        azure_endpoint_label.setAlignment(
            QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter
        )
        provider_grid.addWidget(
            azure_endpoint_label,
            grid_row,
            0,
            QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
        )
        provider_grid.addWidget(self.azure_endpoint_edit, grid_row, 1, 1, 3)
        provider_grid.addWidget(azure_endpoint_hint, grid_row + 1, 1, 1, 3)
        grid_row += 2

        provider_note = QtWidgets.QLabel(
            "Status badges show where each key is currently sourced from."
        )
        self._style_note_label(provider_note)
        provider_grid.addWidget(provider_note, grid_row, 1, 1, 3)
        grid_row += 1

        self.insecure_key_storage_checkbox = QtWidgets.QCheckBox(
            "Allow insecure local API key fallback (plain text)"
        )
        self.insecure_key_storage_checkbox.setToolTip(
            "Use only if Credential Manager/keyring is blocked. "
            "Keys are then stored unencrypted in the app-data folder."
        )
        self.insecure_key_storage_checkbox.toggled.connect(
            lambda _checked: self._refresh_secret_store_options_ui()
        )
        provider_grid.addWidget(self.insecure_key_storage_checkbox, grid_row, 1, 1, 3)
        grid_row += 1

        self.key_storage_status_label = QtWidgets.QLabel("")
        self.key_storage_status_label.setWordWrap(True)
        self._style_note_label(self.key_storage_status_label)
        self.save_api_keys_button = QtWidgets.QPushButton("Save API Keys")
        self.save_api_keys_button.setToolTip(
            "Store entered API keys without applying all settings or refreshing the app."
        )
        self.save_api_keys_button.clicked.connect(self._save_api_keys_only)
        provider_grid.addWidget(
            self.save_api_keys_button,
            grid_row,
            0,
            QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
        )
        provider_grid.addWidget(self.key_storage_status_label, grid_row, 1, 1, 3)
        grid_row += 1

        self.test_conn_target_combo = _WheelPassthroughComboBox()
        self.test_conn_target_combo.addItem(
            "All configured providers (Recommended)",
            "all-configured",
        )
        for provider in _REMOTE_PROVIDERS:
            self.test_conn_target_combo.addItem(
                f"{provider.title} only", provider.name
            )
        self.test_conn_target_combo.setToolTip(
            "Choose which provider to test. "
            "This is independent from the transcription engine selection."
        )
        connection_target_label = QtWidgets.QLabel("Connection Target")
        connection_target_label.setFixedWidth(provider_label_width)
        connection_target_label.setAlignment(
            QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter
        )
        provider_grid.addWidget(
            connection_target_label,
            grid_row,
            0,
            QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
        )
        provider_grid.addWidget(self.test_conn_target_combo, grid_row, 1, 1, 3)
        grid_row += 1

        # Test connection
        self.test_conn_button = QtWidgets.QPushButton("Run Connection Test")
        self.test_conn_button.setToolTip(
            "Test one provider or all configured providers. "
            "Typed key input is preferred over stored key."
        )
        self.test_conn_button.clicked.connect(self._test_connection)
        self.test_conn_result = QtWidgets.QLabel("")
        self.test_conn_result.setWordWrap(True)
        provider_grid.addWidget(
            self.test_conn_button,
            grid_row,
            0,
            QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
        )
        provider_grid.addWidget(self.test_conn_result, grid_row, 1, 1, 3)
        provider_layout.addLayout(provider_grid)

        self._refresh_provider_key_statuses()

        layout.addWidget(provider_box)
        layout.addStretch(1)
        self.tabs.addTab(tab, "Remote")

    def _on_provider_key_changed(self, provider: str) -> None:
        key_field = self._provider_key_edits.get(provider)
        if key_field is not None and key_field.text().strip():
            self._provider_pending_clear.discard(provider)
        self._refresh_provider_key_status(provider)
        self._update_import_engine_note()

    def _provider_label(self, provider: str) -> str:
        return _remote_provider_label(provider)

    def _stored_key_source(self, provider: str) -> str:
        source_getter = getattr(self._secret_store, "get_api_key_source", None)
        if callable(source_getter):
            try:
                value = str(source_getter(provider) or "none").strip().lower()
                return value or "none"
            except Exception:
                pass

        key_getter = getattr(self._secret_store, "get_api_key", None)
        if not callable(key_getter):
            return "none"
        try:
            return "keyring" if key_getter(provider) else "none"
        except Exception:
            return "none"

    def _set_provider_status_badge(
        self,
        provider: str,
        text: str,
        *,
        text_color: str,
        background: str,
        border: str,
        tooltip: str = "",
    ) -> None:
        badge = self._provider_status_labels.get(provider)
        if badge is None:
            return
        badge.setText(text)
        badge.setToolTip(tooltip)
        badge.setStyleSheet(
            "padding: 2px 8px; border-radius: 9px; "
            f"border: 1px solid {border}; "
            f"color: {text_color}; "
            f"background: {background};"
        )

    def _refresh_provider_key_status(self, provider: str) -> None:
        key_field = self._provider_key_edits.get(provider)
        if key_field is None:
            return

        typed_value = key_field.text().strip()
        if typed_value:
            self._set_provider_status_badge(
                provider,
                "Unsaved input",
                text_color="#0d47a1",
                background="#e3f2fd",
                border="#90caf9",
                tooltip="A new key is typed here and will be stored on Save.",
            )
            return

        if provider in self._provider_pending_clear:
            self._set_provider_status_badge(
                provider,
                "Will clear on Save",
                text_color="#b26a00",
                background="#fff3e0",
                border="#ffcc80",
                tooltip="The stored key will be deleted when settings are saved.",
            )
            return

        source = self._stored_key_source(provider)
        if source in {"keyring", "legacy-keyring"}:
            label = "Stored securely"
            tooltip = "Stored securely in Windows Credential Manager."
            if source == "legacy-keyring":
                label = "Secure (legacy)"
                tooltip = "Stored securely under the legacy keyring entry."
            self._set_provider_status_badge(
                provider,
                label,
                text_color="#1b5e20",
                background="#e8f5e9",
                border="#a5d6a7",
                tooltip=tooltip,
            )
            return

        if source == "insecure":
            self._set_provider_status_badge(
                provider,
                "Stored insecurely",
                text_color="#7a4a00",
                background="#fff3e0",
                border="#ffcc80",
                tooltip="Stored in the plain-text fallback file.",
            )
            return

        if source == "insecure-disabled":
            self._set_provider_status_badge(
                provider,
                "Insecure disabled",
                text_color="#7a4a00",
                background="#fff8e1",
                border="#ffe082",
                tooltip=(
                    "A plain-text fallback key exists, but insecure fallback "
                    "storage is currently disabled."
                ),
            )
            return

        self._set_provider_status_badge(
            provider,
            "Not configured",
            text_color="#555",
            background="#f2f2f2",
            border="#bbb",
            tooltip="No stored key is configured for this provider.",
        )

    def _refresh_provider_key_statuses(self) -> None:
        for provider in self._provider_key_edits:
            self._refresh_provider_key_status(provider)

    def _mark_provider_key_for_clear(self, provider: str) -> None:
        key_field = self._provider_key_edits.get(provider)
        if key_field is None:
            return
        key_field.clear()
        self._provider_pending_clear.add(provider)
        self._refresh_provider_key_status(provider)
        self._update_import_engine_note()

    def _import_engine_credential_issue(self, engine: str) -> str | None:
        """Explain why an import cannot safely use this provider credential."""
        engine_name = str(engine or "").strip().lower()
        if engine_name == DEFAULT_ENGINE:
            return None
        key_field = self._provider_key_edits.get(engine_name)
        if key_field is None:
            return f"No API key configured for {self._provider_label(engine_name)}."
        if key_field.text().strip():
            return (
                f"A new {self._provider_label(engine_name)} API key is typed but "
                "not saved. Save API keys before starting the import."
            )
        if engine_name in self._provider_pending_clear:
            return (
                f"The stored {self._provider_label(engine_name)} API key is marked "
                "for deletion. Save or re-enter the key before starting the import."
            )
        if self._resolve_api_key(engine_name, key_field):
            return None
        return f"No API key configured for {self._provider_label(engine_name)}."

    def _update_import_engine_note(self) -> None:
        if not hasattr(self, "import_engine_combo"):
            return
        engine = str(self.import_engine_combo.currentData() or DEFAULT_ENGINE)
        selected_model = (
            str(self.import_model_combo.currentData() or "")
            if hasattr(self, "import_model_combo")
            else ""
        )
        if engine == DEFAULT_ENGINE:
            self.import_engine_note.setStyleSheet("color: #555;")
            self.import_engine_note.setText(
                "Local import transcription stays independent from the main Local tab selection."
            )
            return
        credential_issue = self._import_engine_credential_issue(engine)
        if credential_issue is None:
            self.import_engine_note.setStyleSheet("color: #555;")
            model_text = (
                f" using model '{selected_model}'."
                if selected_model
                else "."
            )
            self.import_engine_note.setText(
                f"Import transcription will use {self._provider_label(engine)}{model_text}"
            )
            return
        self.import_engine_note.setStyleSheet("color: #b71c1c;")
        self.import_engine_note.setText(credential_issue)

    def _test_connection(self) -> None:
        """Test connectivity for one provider or all configured providers."""
        target = str(
            self.test_conn_target_combo.currentData() or "all-configured"
        )
        providers = self._providers_for_connection_target(target)
        if not providers:
            self._set_test_connection_feedback(
                "No configured provider keys found. Enter a key first.",
                "#b71c1c",
            )
            return

        # Snapshot every widget value the worker needs while still on the
        # GUI thread; the worker must never touch Qt widgets.
        snapshots: dict[str, _ConnectionTestSnapshot] = {}
        for provider in providers:
            key_field = self._provider_key_edits.get(provider)
            if key_field is None:
                self._set_test_connection_feedback(
                    f"Unsupported provider: {provider}",
                    "#b71c1c",
                )
                return
            snapshot = self._connection_test_snapshot(provider, key_field)
            if not snapshot.api_key:
                self._set_test_connection_feedback(
                    f"No API key entered for {self._provider_label(provider)}.",
                    "#b71c1c",
                )
                return
            snapshots[provider] = snapshot

        self._connection_test_id += 1
        test_id = self._connection_test_id
        self.test_conn_button.setEnabled(False)
        self.test_conn_target_combo.setEnabled(False)
        if len(providers) == 1:
            provider_label = self._provider_label(providers[0])
            self._set_test_connection_feedback(
                f"Testing {provider_label}...",
                "#555",
            )
        else:
            self._set_test_connection_feedback(
                "Testing all configured providers...",
                "#555",
            )
        worker = threading.Thread(
            target=self._run_connection_test_worker,
            args=(test_id, snapshots),
            name="stt_app_settings_connection_test",
            daemon=True,
        )
        self._active_connection_test_thread = worker
        worker.start()

    def _providers_for_connection_target(self, target: str) -> list[str]:
        normalized = str(target or "").strip().lower()
        remote_providers = tuple(provider.name for provider in _REMOTE_PROVIDERS)
        if normalized == "all-configured":
            configured: list[str] = []
            for provider in remote_providers:
                key_field = self._provider_key_edits.get(provider)
                if key_field is None:
                    continue
                if self._resolve_api_key(provider, key_field):
                    configured.append(provider)
            return configured
        if normalized in remote_providers:
            return [normalized]
        return []

    def _connection_test_snapshot(
        self,
        provider: str,
        key_field: QtWidgets.QLineEdit,
    ) -> _ConnectionTestSnapshot:
        """Capture the widget values one provider test needs (GUI thread)."""
        return _ConnectionTestSnapshot(
            api_key=self._resolve_api_key(provider, key_field),
            model=self._remote_model_value_for_provider(provider),
            language_mode=str(
                self.language_combo.currentData() or DEFAULT_LANGUAGE_MODE
            ),
            azure_endpoint=(
                self._resolve_azure_endpoint() if provider == "azure" else ""
            ),
        )

    def _resolve_api_key(self, provider: str, key_field: QtWidgets.QLineEdit) -> str:
        api_key = key_field.text().strip()
        if api_key:
            return api_key
        key_getter = getattr(self._secret_store, "get_api_key", None)
        if not callable(key_getter):
            return ""
        try:
            return str(key_getter(provider) or "")
        except Exception:
            return ""

    def _resolve_azure_endpoint(self) -> str:
        """Return the typed Azure endpoint, or the stored one as fallback."""
        typed = self.azure_endpoint_edit.text().strip()
        if typed:
            return typed
        return str(getattr(self._loaded_settings, "azure_endpoint", "") or "").strip()

    def _run_connection_test_worker(
        self,
        test_id: int,
        snapshots: dict[str, _ConnectionTestSnapshot],
    ) -> None:
        results: dict[str, tuple[bool, str]] = {}
        for provider, snapshot in snapshots.items():
            tester, error_text = _build_connection_tester(provider, snapshot)
            if tester is None:
                if error_text:
                    results[provider] = (False, error_text)
                else:
                    results[provider] = (
                        False,
                        f"Connection test not implemented for {provider}.",
                    )
                continue
            try:
                ok, msg = tester()
            except Exception as exc:
                ok, msg = False, f"Test failed: {exc}"
            results[provider] = (bool(ok), str(msg))

        self._connection_test_details[test_id] = results
        success_count = sum(1 for provider_ok, _ in results.values() if provider_ok)
        total_count = len(results)
        all_ok = total_count > 0 and success_count == total_count
        if total_count <= 1:
            if total_count == 1:
                only_provider = next(iter(results))
                summary = results[only_provider][1]
            else:
                summary = "No providers tested."
        else:
            summary = f"{success_count}/{total_count} provider tests passed."
        _emit_background_signal(
            self,
            "connection_test_finished",
            test_id,
            all_ok,
            summary,
        )

    @QtCore.Slot(int, bool, str)
    def _on_connection_test_finished(self, test_id: int, ok: bool, msg: str) -> None:
        details = self._connection_test_details.pop(test_id, {})
        if test_id != self._connection_test_id:
            return
        self.test_conn_button.setEnabled(True)
        self.test_conn_target_combo.setEnabled(True)
        self._active_connection_test_thread = None
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for provider, (provider_ok, provider_msg) in details.items():
            self._remember_provider_connection_test(
                provider,
                ok=provider_ok,
                message=provider_msg,
                timestamp=timestamp,
            )

        if len(details) > 1:
            parts = []
            for provider in (provider.name for provider in _REMOTE_PROVIDERS):
                if provider not in details:
                    continue
                provider_ok, _provider_msg = details[provider]
                marker = "OK" if provider_ok else "Fail"
                parts.append(f"{self._provider_label(provider)}: {marker}")
            color = "#1b5e20" if ok else "#b26a00"
            joined = " | ".join(parts)
            self._set_test_connection_feedback(f"{msg} {joined}", color)
            return

        if ok:
            self._set_test_connection_feedback(f"\u2713 {msg}", "#1b5e20")
        else:
            self._set_test_connection_feedback(f"\u2717 {msg}", "#b71c1c")

    def _set_test_connection_feedback(self, text: str, color: str) -> None:
        self.test_conn_result.setText(text)
        self.test_conn_result.setStyleSheet(f"color: {color};")

    def _remember_provider_connection_test(
        self,
        provider: str,
        *,
        ok: bool,
        message: str,
        timestamp: str,
    ) -> None:
        self._provider_test_history[provider] = (bool(ok), str(message), timestamp)
        try:
            self._provider_connection_test_store.save_result(
                provider,
                ok=ok,
                message=message,
                checked_at=timestamp,
            )
        except Exception:
            self._settings_perf_logger.exception(
                "Failed to persist %s connection test result", provider
            )
        self._apply_provider_connection_test_label(provider)

    def _clear_provider_connection_test(self, provider: str) -> None:
        self._provider_test_history.pop(provider, None)
        try:
            self._provider_connection_test_store.clear_result(provider)
        except Exception:
            self._settings_perf_logger.exception(
                "Failed to clear %s connection test result", provider
            )
        self._apply_provider_connection_test_label(provider)

    def _restore_provider_connection_test_labels(self) -> None:
        try:
            results = self._provider_connection_test_store.load_all()
        except Exception:
            self._settings_perf_logger.exception(
                "Failed to load provider connection test results"
            )
            results = {}
        for provider, result in results.items():
            self._provider_test_history[provider] = (
                result.ok,
                result.message,
                result.checked_at,
            )
        for provider in self._provider_last_test_labels:
            self._apply_provider_connection_test_label(provider)

    def _apply_provider_connection_test_label(self, provider: str) -> None:
        last_label = self._provider_last_test_labels.get(provider)
        if last_label is None:
            return
        result = self._provider_test_history.get(provider)
        if result is None:
            self._style_provider_last_test_label(last_label)
            last_label.setText("Last test: never.")
            return
        ok, message, timestamp = result
        marker = "\u2713" if ok else "\u2717"
        color = "#1b5e20" if ok else "#b71c1c"
        self._style_provider_last_test_label(last_label, color=color)
        last_label.setText(f"Last test ({timestamp}): {marker} {message}")
