"""Settings dialog: remote mixin (split from settings_dialog.py)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from PySide6 import QtCore, QtWidgets

from .config import (
    CUSTOM_API_MODE_CHAT,
    CUSTOM_API_MODE_TRANSCRIPTIONS,
    CUSTOM_KEY_COMMAND_TTL_S,
    DEFAULT_CUSTOM_API_MODE,
    DEFAULT_ENGINE,
    DEFAULT_LANGUAGE_MODE,
    SPEECHMATICS_MELIA_UNAVAILABLE_TEXT,
    speechmatics_model_available_in,
)
from .dialog_style import make_label_selectable
from .settings_dialog_helpers import (
    _REMOTE_PROVIDER_GRID_SPACING_PX,
    _REMOTE_PROVIDER_LABEL_EXTRA_PX,
    _REMOTE_PROVIDERS,
    _REMOTE_REGION_CHOICES,
    WrappedStatusLabel,
    _emit_background_signal,
    _remote_provider_label,
    _WheelPassthroughComboBox,
)

# The key field's minimum on the Providers tab. Every other column is sized
# by its caption, so this decides the page's minimum width with them.
_PROVIDER_KEY_FIELD_MIN_WIDTH_PX = 140
# How far a row that belongs to the provider above it (a region, Azure's
# endpoint) indents its label.
_PROVIDER_SUB_ROW_INDENT_PX = 12
_REGION_ROW_LABEL = "Region"
_AZURE_ENDPOINT_ROW_LABEL = "Endpoint"
_CUSTOM_ENDPOINT_ROW_LABELS = ("Base URL", "API Style", "Key Command")
_CUSTOM_KEY_ROW_LABEL = "API Key"
_TEST_OK_MARK = "✓"
_TEST_FAILED_MARK = "✗"
_TEST_OK_COLOR = "#1b5e20"
# Key sources `SecretStore.get_api_key` answers with a key; "insecure-
# disabled" holds one it does not hand out while the fallback is off.
_USABLE_KEY_SOURCES = frozenset({"keyring", "legacy-keyring", "insecure"})
_TEST_FAILED_COLOR = "#b71c1c"


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
    custom_endpoint: str = ""
    custom_api_mode: str = DEFAULT_CUSTOM_API_MODE
    custom_key_command: str = ""
    # The provider's data-residency region, for the providers that have one
    # (`_REMOTE_REGION_CHOICES`); empty for the rest.
    region: str = ""


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


def _custom_transcriber_factory(**kwargs: object) -> object:
    from .transcriber.custom_endpoint_provider import CustomEndpointTranscriber

    return CustomEndpointTranscriber(**kwargs)


def _funasr_transcriber_factory(**kwargs: object) -> object:
    from .transcriber.funasr_provider import FunAsrTranscriber

    return FunAsrTranscriber(**kwargs)


def _speechmatics_transcriber_factory(**kwargs: object) -> object:
    from .transcriber.speechmatics_provider import SpeechmaticsTranscriber

    return SpeechmaticsTranscriber(**kwargs)


def _mistral_transcriber_factory(**kwargs: object) -> object:
    from .transcriber.mistral_provider import MistralTranscriber

    return MistralTranscriber(**kwargs)


# Maps provider name to (lazy transcriber factory, extra snapshot fields the
# factory needs besides api_key and model).
_CONNECTION_TESTER_FACTORIES: dict[
    str,
    tuple[Callable[..., object], tuple[str, ...]],
] = {
    # A region is passed so the test asks the host the dictation uses: a test
    # against the US host would pass for a key the EU host then refuses.
    "assemblyai": (_assemblyai_transcriber_factory, ("region",)),
    "groq": (_groq_transcriber_factory, ()),
    "openai": (_openai_transcriber_factory, ()),
    "deepgram": (_deepgram_transcriber_factory, ("region",)),
    "elevenlabs": (_elevenlabs_transcriber_factory, ("language_mode",)),
    "azure": (_azure_transcriber_factory, ("language_mode", "endpoint")),
    "funasr": (_funasr_transcriber_factory, ("language_mode",)),
    # Built as the dictation builds it: the language decides how Melia 1 is
    # asked, and Melia 1 in the Australian region is refused at construction.
    "speechmatics": (
        _speechmatics_transcriber_factory,
        ("language_mode", "region"),
    ),
    "mistral": (_mistral_transcriber_factory, ("language_mode",)),
    "custom": (_custom_transcriber_factory, ("custom",)),
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
    factory, extra_fields = factory_entry
    if not snapshot.api_key and not (
        "custom" in extra_fields and snapshot.custom_key_command
    ):
        return None, "No API key entered. Enter a key above first."
    kwargs: dict[str, object] = {
        "api_key": snapshot.api_key,
        "model": snapshot.model,
    }
    if "language_mode" in extra_fields:
        kwargs["language_mode"] = snapshot.language_mode
    if "region" in extra_fields:
        kwargs["region"] = snapshot.region
    if "endpoint" in extra_fields:
        if not snapshot.azure_endpoint:
            return (
                None,
                ("No Azure endpoint entered. Enter the resource endpoint above first."),
            )
        kwargs["endpoint"] = snapshot.azure_endpoint
    if "custom" in extra_fields:
        if not snapshot.custom_endpoint:
            return None, "No custom endpoint URL entered. Enter it above first."
        kwargs["endpoint"] = snapshot.custom_endpoint
        kwargs["api_mode"] = snapshot.custom_api_mode
        kwargs["key_command"] = snapshot.custom_key_command
    try:
        transcriber = factory(**kwargs)
    except Exception as exc:
        return None, str(exc)
    return transcriber.test_connection, None


class _RemoteProvidersMixin:
    def _build_remote_tab(self) -> None:
        """The Providers tab: one compact row per cloud provider, the custom
        endpoint in a group of its own, one shared connection-test line.

        Each row is name, key field, Test, Remove, the last test's mark and
        the key-source badge; a region or Azure's endpoint is an indented row
        right under its provider. The per-provider "Last test" lines this
        replaced reserved two lines each and made the tab scroll by 219 px at
        the default text size; the result now lives in the mark's tooltip and
        in the one line under the groups.
        """
        tab, content = self._create_scroll_tab()
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        label_width = self._remote_provider_label_width()
        badge_width = self._provider_status_badge_width()

        cloud_box = QtWidgets.QGroupBox("Cloud providers")
        cloud_layout = QtWidgets.QVBoxLayout(cloud_box)
        cloud_layout.setContentsMargins(10, 10, 10, 10)
        cloud_layout.setSpacing(6)
        cloud_intro = QtWidgets.QLabel(
            "Keys are stored in Windows Credential Manager. Type a key only to "
            "replace the stored one; Remove deletes it on Save."
        )
        cloud_intro.setWordWrap(True)
        self._style_note_label(cloud_intro)
        cloud_layout.addWidget(cloud_intro)
        cloud_grid = self._new_provider_grid(label_width)
        row = 0
        for provider in _REMOTE_PROVIDERS:
            if provider.name == "custom":
                continue
            self._add_provider_key_row(
                cloud_grid, row, provider.name, provider.title, label_width, badge_width
            )
            row += 1
            if provider.name in _REMOTE_REGION_CHOICES:
                combo = self._build_region_combo(provider.name)
                self._add_provider_sub_row(
                    cloud_grid,
                    row,
                    _REGION_ROW_LABEL,
                    combo,
                    label_width,
                    own_width=True,
                )
                row += 1
            if provider.name == "azure":
                self.azure_endpoint_edit = QtWidgets.QLineEdit()
                self.azure_endpoint_edit.setPlaceholderText(
                    "https://<resource>.cognitiveservices.azure.com"
                )
                self.azure_endpoint_edit.setToolTip(
                    "Required for Azure LLM Speech. Copy the endpoint from your "
                    "Azure Speech / Foundry resource (Keys and Endpoint). The "
                    "region must support LLM Speech."
                )
                self.azure_endpoint_edit.textChanged.connect(
                    lambda _text: self._update_remote_model_note()
                )
                self._add_provider_sub_row(
                    cloud_grid,
                    row,
                    _AZURE_ENDPOINT_ROW_LABEL,
                    self.azure_endpoint_edit,
                    label_width,
                )
                row += 1
        cloud_layout.addLayout(cloud_grid)
        layout.addWidget(cloud_box)

        self.assemblyai_key_edit = self._provider_key_edits["assemblyai"]
        self.groq_key_edit = self._provider_key_edits["groq"]
        self.openai_key_edit = self._provider_key_edits["openai"]
        self.deepgram_key_edit = self._provider_key_edits["deepgram"]
        self.elevenlabs_key_edit = self._provider_key_edits["elevenlabs"]
        self.azure_key_edit = self._provider_key_edits["azure"]
        self.funasr_key_edit = self._provider_key_edits["funasr"]
        self.speechmatics_key_edit = self._provider_key_edits["speechmatics"]
        self.mistral_key_edit = self._provider_key_edits["mistral"]

        layout.addWidget(self._build_custom_endpoint_group(label_width, badge_width))

        # One line for every test, under both groups: the result of the test
        # just run, or on opening the most recent stored one. Two lines are
        # reserved, so a long failure moves nothing; the whole message is the
        # tooltip.
        self.test_conn_button = QtWidgets.QPushButton("Test All Configured")
        self.test_conn_button.setToolTip(
            "Test every provider that has a key. A typed key is tested before "
            "the stored one; each row's Test button tests that provider alone."
        )
        self.test_conn_button.clicked.connect(
            lambda _checked=False: self._test_connection("all-configured")
        )
        self.test_conn_result = WrappedStatusLabel("")
        self.test_conn_result.setWordWrap(True)
        make_label_selectable(self.test_conn_result)
        self._reserve_dynamic_hint_height(self.test_conn_result)
        test_row = QtWidgets.QHBoxLayout()
        test_row.setContentsMargins(0, 0, 0, 0)
        test_row.setSpacing(_REMOTE_PROVIDER_GRID_SPACING_PX)
        test_row.addWidget(self.test_conn_button, 0, QtCore.Qt.AlignTop)
        test_row.addWidget(self.test_conn_result, 1)
        layout.addLayout(test_row)

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
        layout.addWidget(self.insecure_key_storage_checkbox)

        self.key_storage_status_label = WrappedStatusLabel("")
        make_label_selectable(self.key_storage_status_label)
        self.key_storage_status_label.setWordWrap(True)
        self._style_note_label(self.key_storage_status_label)
        self.save_api_keys_button = QtWidgets.QPushButton("Save API Keys")
        self.save_api_keys_button.setToolTip(
            "Store the keys and this tab's endpoints and regions without "
            "applying the other settings."
        )
        self.save_api_keys_button.clicked.connect(self._save_api_keys_only)
        save_row = QtWidgets.QHBoxLayout()
        save_row.setContentsMargins(0, 0, 0, 0)
        save_row.setSpacing(_REMOTE_PROVIDER_GRID_SPACING_PX)
        save_row.addWidget(self.save_api_keys_button, 0, QtCore.Qt.AlignTop)
        save_row.addWidget(self.key_storage_status_label, 1, QtCore.Qt.AlignVCenter)
        layout.addLayout(save_row)

        self._refresh_provider_key_statuses()

        layout.addStretch(1)
        # "Providers": the keys, the regions, Azure's endpoint and the custom
        # endpoint all live here; "API Keys" named only the first of them.
        self.tabs.addTab(tab, "Providers")

    def _remote_provider_label_width(self) -> int:
        """The Providers tab's name column: wide enough for every row label,
        a sub-row's indent included, in both groups."""
        metrics = self.fontMetrics()
        widths = [
            metrics.horizontalAdvance(text)
            for text in (
                *(p.title for p in _REMOTE_PROVIDERS if p.name != "custom"),
                *_CUSTOM_ENDPOINT_ROW_LABELS,
                _CUSTOM_KEY_ROW_LABEL,
            )
        ]
        widths.extend(
            metrics.horizontalAdvance(text) + _PROVIDER_SUB_ROW_INDENT_PX
            for text in (_REGION_ROW_LABEL, _AZURE_ENDPOINT_ROW_LABEL)
        )
        return max(widths) + _REMOTE_PROVIDER_LABEL_EXTRA_PX

    @staticmethod
    def _new_provider_grid(label_width: int) -> QtWidgets.QGridLayout:
        """A grid with the Providers tab's columns: name, key field, Test,
        Remove, test mark, badge. Both groups use it, so their columns line
        up: every column but the key field is sized by the same captions."""
        grid = QtWidgets.QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(_REMOTE_PROVIDER_GRID_SPACING_PX)
        grid.setVerticalSpacing(4)
        grid.setColumnMinimumWidth(0, label_width)
        grid.setColumnStretch(1, 1)
        return grid

    def _add_provider_key_row(
        self,
        grid: QtWidgets.QGridLayout,
        row: int,
        provider: str,
        title: str,
        label_width: int,
        badge_width: int,
    ) -> None:
        key_field = QtWidgets.QLineEdit()
        key_field.setEchoMode(QtWidgets.QLineEdit.Password)
        key_field.setMinimumWidth(_PROVIDER_KEY_FIELD_MIN_WIDTH_PX)
        key_field.textChanged.connect(
            lambda _text, p=provider: self._on_provider_key_changed(p)
        )
        test_button = QtWidgets.QPushButton("Test")
        test_button.setToolTip(
            f"Test the {_remote_provider_label(provider)} key: the typed one, "
            "else the stored one."
        )
        test_button.clicked.connect(
            lambda _checked=False, p=provider: self._test_connection(p)
        )
        remove_button = QtWidgets.QPushButton("Remove")
        remove_button.setToolTip("Delete the stored key for this provider on Save.")
        remove_button.clicked.connect(
            lambda _checked=False, p=provider: self._mark_provider_key_for_clear(p)
        )
        self._match_field_button_height(key_field, test_button, remove_button)

        # The last test's outcome as one glyph, its text on hover. Fixed
        # width, so a mark appearing moves nothing beside it.
        mark = QtWidgets.QLabel("")
        mark.setAlignment(QtCore.Qt.AlignCenter)
        mark.setFixedWidth(
            max(
                mark.fontMetrics().horizontalAdvance(glyph)
                for glyph in (_TEST_OK_MARK, _TEST_FAILED_MARK)
            )
            + 4
        )

        status_badge = QtWidgets.QLabel("Not configured")
        status_badge.setAlignment(QtCore.Qt.AlignCenter | QtCore.Qt.AlignVCenter)
        status_badge.setFixedWidth(badge_width)
        status_badge.setSizePolicy(
            QtWidgets.QSizePolicy.Fixed,
            QtWidgets.QSizePolicy.Fixed,
        )
        status_badge.setStyleSheet(
            "padding: 2px 8px; border: 1px solid #bbb; border-radius: 9px;"
            " color: #555; background: #f2f2f2;"
        )

        title_label = QtWidgets.QLabel(title)
        title_label.setFixedWidth(label_width)
        title_label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        grid.addWidget(
            title_label, row, 0, QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter
        )
        grid.addWidget(key_field, row, 1)
        grid.addWidget(test_button, row, 2)
        grid.addWidget(remove_button, row, 3)
        grid.addWidget(mark, row, 4)
        grid.addWidget(status_badge, row, 5)

        self._provider_key_edits[provider] = key_field
        self._provider_status_labels[provider] = status_badge
        self._provider_test_marks[provider] = mark
        self._provider_test_buttons[provider] = test_button
        self._provider_remove_buttons[provider] = remove_button

    @staticmethod
    def _add_provider_sub_row(
        grid: QtWidgets.QGridLayout,
        row: int,
        text: str,
        field: QtWidgets.QWidget,
        label_width: int,
        *,
        own_width: bool = False,
    ) -> None:
        """A setting that belongs to the row above it, label indented.

        The field takes the key field's column and so ends where the key
        fields do. ``own_width`` keeps it at its own, narrower width,
        left-aligned (a region combo).
        """
        label = QtWidgets.QLabel(text)
        label.setIndent(_PROVIDER_SUB_ROW_INDENT_PX)
        label.setFixedWidth(label_width)
        label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        label.setStyleSheet("color: #555;")
        grid.addWidget(label, row, 0, QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        if own_width:
            grid.addWidget(field, row, 1, QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        else:
            grid.addWidget(field, row, 1)

    def _build_region_combo(self, provider: str) -> QtWidgets.QComboBox:
        """Where the provider processes the audio; always enabled whatever
        the engine, so a pick moves nothing."""
        choices = _REMOTE_REGION_CHOICES[provider]
        combo = _WheelPassthroughComboBox()
        for value, label, guarantee in choices:
            combo.addItem(label, value)
            combo.setItemData(combo.count() - 1, guarantee, QtCore.Qt.ToolTipRole)
        tooltip = (
            f"Where {_remote_provider_label(provider)} processes the audio. "
            "Dictation, audio imports and the connection test all use this "
            "region.\n"
            + "\n".join(f"{label}: {guarantee}" for _value, label, guarantee in choices)
        )
        if provider == "speechmatics":
            # This sentence was a visible note under the region rows; with the
            # rows under their providers it belongs to this combo alone.
            tooltip += (
                "\nMelia 1 runs in the EU and US only; pick Enhanced or "
                "Standard on the Transcription tab for Australia."
            )
        combo.setToolTip(tooltip)
        # A region can make the selected model unavailable (Melia 1 in au1).
        combo.currentIndexChanged.connect(
            lambda _index: self._update_remote_model_note()
        )
        self._provider_region_combos[provider] = combo
        return combo

    def _build_custom_endpoint_group(
        self, label_width: int, badge_width: int
    ) -> QtWidgets.QGroupBox:
        """Base URL, API style, key command and static key of the custom
        endpoint. Its model is picked on the Transcription tab, where the
        Refresh button lists what the endpoint offers."""
        box = QtWidgets.QGroupBox("Custom endpoint (OpenAI-compatible)")
        box_layout = QtWidgets.QVBoxLayout(box)
        box_layout.setContentsMargins(10, 10, 10, 10)
        box_layout.setSpacing(6)
        intro = QtWidgets.QLabel(
            "Any OpenAI-compatible server, such as a LiteLLM or vLLM gateway or "
            "a local speech server. Its model is picked on the Transcription tab."
        )
        intro.setWordWrap(True)
        self._style_note_label(intro)
        box_layout.addWidget(intro)

        self.custom_endpoint_edit = QtWidgets.QLineEdit()
        self.custom_endpoint_edit.setPlaceholderText(
            "https://llm-gateway.example.com/v1"
        )
        self.custom_endpoint_edit.setToolTip(
            "Base URL of an OpenAI-compatible API, used as given (nothing is "
            "appended), e.g. https://llm-gateway.example.com/v1 or "
            "http://localhost:8000/v1."
        )
        self.custom_endpoint_edit.textChanged.connect(self._on_custom_endpoint_changed)
        self.custom_key_command_edit = QtWidgets.QLineEdit()
        self.custom_key_command_edit.setPlaceholderText(
            "Optional, e.g. token-helper --print"
        )
        self.custom_key_command_edit.setToolTip(
            "Runs when a request needs a key; its output is used as the Bearer "
            f"token for {CUSTOM_KEY_COMMAND_TTL_S / 60:.0f} minutes and "
            "overrides the stored key. Run without a shell, e.g. "
            "wsl.exe -e /path/to/token-helper."
        )
        # A command alone makes the endpoint testable and runnable.
        self.custom_key_command_edit.textChanged.connect(
            lambda _text: self._on_provider_key_changed("custom")
        )
        self.custom_api_mode_combo = _WheelPassthroughComboBox()
        self.custom_api_mode_combo.addItem(
            "OpenAI transcription API (/audio/transcriptions)",
            CUSTOM_API_MODE_TRANSCRIPTIONS,
        )
        self.custom_api_mode_combo.addItem(
            "Chat completions with audio input (multimodal LLM)",
            CUSTOM_API_MODE_CHAT,
        )
        # Sized by a minimum content length, not by its longest caption: the
        # dialog's minimum width follows every page's minimum and only grows.
        self.custom_api_mode_combo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon
        )
        self.custom_api_mode_combo.setMinimumContentsLength(8)
        self.custom_api_mode_combo.setToolTip(
            "Speech servers and speech models answer the transcription API. "
            "A gateway that routes audio only to a multimodal LLM needs the "
            "chat style, whose transcript is less deterministic and may "
            "paraphrase."
        )
        grid = self._new_provider_grid(label_width)
        for row, (text, field) in enumerate(
            zip(
                _CUSTOM_ENDPOINT_ROW_LABELS,
                (
                    self.custom_endpoint_edit,
                    self.custom_api_mode_combo,
                    self.custom_key_command_edit,
                ),
                strict=True,
            )
        ):
            label = QtWidgets.QLabel(text)
            label.setFixedWidth(label_width)
            label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
            grid.addWidget(label, row, 0, QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
            grid.addWidget(field, row, 1)
        self._add_provider_key_row(
            grid,
            len(_CUSTOM_ENDPOINT_ROW_LABELS),
            "custom",
            _CUSTOM_KEY_ROW_LABEL,
            label_width,
            badge_width,
        )
        self.custom_key_edit = self._provider_key_edits["custom"]
        box_layout.addLayout(grid)
        return box

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

    def _key_source_after_save(self, provider: str) -> str:
        """The stored key's source as Save would leave it.

        The store hands a plain-text key out only while its insecure fallback
        is enabled, and the checkbox applies on Save, so an unsaved tick
        changes what the store will report: a stored fallback key reads as
        "insecure" with the box checked and "insecure-disabled" without it.
        The key's Test button is not judged this way: a test runs now, against
        the store's present state.
        """
        source = self._stored_key_source(provider)
        if source in {"insecure", "insecure-disabled"}:
            return (
                "insecure"
                if self.insecure_key_storage_checkbox.isChecked()
                else "insecure-disabled"
            )
        return source

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
        source = self._stored_key_source(provider)
        self._refresh_provider_row_controls(provider, key_field, typed_value, source)
        source = self._key_source_after_save(provider)
        # The Transcription tab warns while the selected engine has no key.
        if provider == self.engine_combo.currentData():
            self._update_remote_model_note()
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

    def _refresh_provider_row_controls(
        self,
        provider: str,
        key_field: QtWidgets.QLineEdit,
        typed_value: str,
        source: str,
    ) -> None:
        """The row's placeholder, Test and Remove follow what it holds.

        Test needs a key to test (typed, stored and not marked for removal,
        or the custom endpoint's key command) and no test running; Remove
        needs something to remove. Only enabled states and the placeholder
        change, so nothing in the row moves.
        """
        pending = provider in self._provider_pending_clear
        stored = source != "none"
        usable_stored = source in _USABLE_KEY_SOURCES and not pending
        if pending:
            placeholder = "Removed on Save; type a key to keep one"
        elif stored:
            placeholder = "Stored; type a new key to replace it"
        elif provider == "custom":
            placeholder = "API key (optional with a key command)"
        else:
            placeholder = "API key"
        key_field.setPlaceholderText(placeholder)
        runnable = bool(typed_value) or usable_stored
        if provider == "custom" and hasattr(self, "custom_key_command_edit"):
            runnable = runnable or bool(self.custom_key_command_edit.text().strip())
        test_button = self._provider_test_buttons.get(provider)
        if test_button is not None:
            test_button.setEnabled(
                runnable and self._active_connection_test_thread is None
            )
        remove_button = self._provider_remove_buttons.get(provider)
        if remove_button is not None:
            remove_button.setEnabled(bool(typed_value) or (stored and not pending))

    def _refresh_provider_key_statuses(self) -> None:
        for provider in self._provider_key_edits:
            self._refresh_provider_key_status(provider)

    def _remote_engine_setup_issue(self, engine: str) -> str | None:
        """What stops dictation with this remote engine, or None.

        Judged on what Save would leave: a typed key counts (Save stores it),
        a key marked for removal does not, and a stored one counts only when
        the store hands it out. Shown on the Transcription tab in place of the
        model note (`_update_remote_model_note`).
        """
        key_field = self._provider_key_edits.get(engine)
        if key_field is None:
            return None
        label = self._provider_label(engine)
        if engine == "custom" and not self.custom_endpoint_edit.text().strip():
            return (
                "No base URL for the custom endpoint yet: enter it on the "
                "Providers tab, or dictation with this engine fails."
            )
        # An "insecure-disabled" key sits in the file but the store does not
        # hand it out; whether the fallback is enabled is the checkbox's value
        # as Save would apply it (the row's Test button reads the store's
        # present state instead, as a test runs now).
        has_key = bool(key_field.text().strip()) or (
            engine not in self._provider_pending_clear
            and self._key_source_after_save(engine) in _USABLE_KEY_SOURCES
        )
        if engine == "custom":
            has_key = has_key or bool(self.custom_key_command_edit.text().strip())
        if not has_key:
            if engine in self._provider_pending_clear:
                return (
                    f"The {label} key is removed on Save, and dictation with "
                    "this engine then fails. Enter a key on the Providers tab."
                )
            if engine == "custom":
                # The endpoint refuses to start without either (see
                # CustomEndpointTranscriber), even for a server without auth.
                return (
                    "No key or key command for the custom endpoint yet: enter "
                    "one on the Providers tab (any placeholder such as 'none' "
                    "for a server without authentication)."
                )
            return (
                f"No {label} API key yet: enter one on the Providers tab, or "
                "dictation with this engine fails."
            )
        if engine == "azure" and not self.azure_endpoint_edit.text().strip():
            return (
                "Azure also needs its endpoint: enter it on the Providers tab, "
                "or dictation with this engine fails."
            )
        if engine == "speechmatics" and not speechmatics_model_available_in(
            self._remote_model_value_for_provider(engine), self._region_shown(engine)
        ):
            # The transcriber refuses the pair before any upload.
            return (
                f"{SPEECHMATICS_MELIA_UNAVAILABLE_TEXT} Pick eu1 or us1 on the "
                "Providers tab, or the Enhanced or Standard model."
            )
        return None

    def _mark_provider_key_for_clear(self, provider: str) -> None:
        key_field = self._provider_key_edits.get(provider)
        if key_field is None:
            return
        key_field.clear()
        self._provider_pending_clear.add(provider)
        self._refresh_provider_key_status(provider)
        self._update_import_engine_note()
        # A pending removal is an unsaved change that no widget value shows
        # (the field is empty before and after), so ask for the refresh here.
        self._schedule_unsaved_changes_refresh()

    def _import_engine_credential_issue(self, engine: str) -> str | None:
        """Explain why an import cannot safely use this provider credential."""
        engine_name = str(engine or "").strip().lower()
        if engine_name == DEFAULT_ENGINE:
            return None
        key_field = self._provider_key_edits.get(engine_name)
        if key_field is None:
            return f"No API key configured for {self._provider_label(engine_name)}."
        if (
            engine_name == "custom"
            and not key_field.text().strip()
            and engine_name not in self._provider_pending_clear
            and self.custom_key_command_edit.text().strip()
        ):
            return None
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
                "Local import transcription stays independent from the model selected on the Transcription tab."
            )
            return
        credential_issue = self._import_engine_credential_issue(engine)
        if credential_issue is None:
            self.import_engine_note.setStyleSheet("color: #555;")
            model_text = f" using model '{selected_model}'." if selected_model else "."
            self.import_engine_note.setText(
                f"Import transcription will use {self._provider_label(engine)}{model_text}"
            )
            return
        self.import_engine_note.setStyleSheet("color: #b71c1c;")
        self.import_engine_note.setText(credential_issue)

    def _test_connection(self, target: str = "all-configured") -> None:
        """Test one provider (a row's Test) or every configured one."""
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
            if not snapshot.api_key and not snapshot.custom_key_command:
                self._set_test_connection_feedback(
                    f"No API key entered for {self._provider_label(provider)}.",
                    "#b71c1c",
                )
                return
            snapshots[provider] = snapshot

        self._connection_test_id += 1
        test_id = self._connection_test_id
        self.test_conn_button.setEnabled(False)
        for button in self._provider_test_buttons.values():
            button.setEnabled(False)
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
        # `Thread.start()` can raise `RuntimeError` when the interpreter
        # cannot create another thread. The busy marker is already set at
        # that point, and nothing clears it but the completion signal that
        # will never arrive -- so the dialog stays busy for the rest of the
        # session: the control stays disabled and `reload_from_store` is
        # deferred forever, silently.
        try:
            worker.start()
        except RuntimeError as exc:
            self._end_connection_test()
            self._set_test_connection_feedback(
                f"Could not start the connection test: {exc}", "#b71c1c"
            )

    def _providers_for_connection_target(self, target: str) -> list[str]:
        normalized = str(target or "").strip().lower()
        remote_providers = tuple(provider.name for provider in _REMOTE_PROVIDERS)
        if normalized == "all-configured":
            configured: list[str] = []
            for provider in remote_providers:
                key_field = self._provider_key_edits.get(provider)
                if key_field is None:
                    continue
                if self._resolve_api_key(provider, key_field) or (
                    provider == "custom" and self.custom_key_command_edit.text().strip()
                ):
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
            region=(
                self._region_shown(provider)
                if provider in self._provider_region_combos
                else ""
            ),
            **(
                {
                    "custom_endpoint": self.custom_endpoint_edit.text().strip(),
                    "custom_api_mode": str(
                        self.custom_api_mode_combo.currentData()
                        or DEFAULT_CUSTOM_API_MODE
                    ),
                    "custom_key_command": (self.custom_key_command_edit.text().strip()),
                }
                if provider == "custom"
                else {}
            ),
        )

    def _region_shown(self, provider: str) -> str:
        """The region the provider's selector shows (its first, default
        choice if it shows none)."""
        combo = self._provider_region_combos[provider]
        return str(combo.currentData() or _REMOTE_REGION_CHOICES[provider][0][0])

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

    def _end_connection_test(self) -> None:
        """No test runs any more: the Test buttons follow their rows again."""
        self._active_connection_test_thread = None
        self.test_conn_button.setEnabled(True)
        self._refresh_provider_key_statuses()

    @QtCore.Slot(int, bool, str)
    def _on_connection_test_finished(self, test_id: int, ok: bool, msg: str) -> None:
        details = self._connection_test_details.pop(test_id, {})
        if test_id != self._connection_test_id:
            return
        self._end_connection_test()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")  # noqa: DTZ005 (shown to the user in their own time zone)
        for provider, (provider_ok, provider_msg) in details.items():
            self._remember_provider_connection_test(
                provider,
                ok=provider_ok,
                message=provider_msg,
                timestamp=timestamp,
            )

        if len(details) > 1:
            # Only the failures are named, by their short row titles: every
            # row carries its own mark, and "Name: Fail | ..." under the long
            # labels for all ten providers needed three lines of the two the
            # line reserves.
            failed = [
                provider.title
                for provider in _REMOTE_PROVIDERS
                if provider.name in details and not details[provider.name][0]
            ]
            color = _TEST_OK_COLOR if ok else "#b26a00"
            if failed:
                msg = f"{msg} Failed: {', '.join(failed)}."
            self._set_test_connection_feedback(msg, color)
            return
        if details:
            # One provider: its name leads, since every row reports here.
            provider = next(iter(details))
            msg = f"{self._provider_label(provider)}: {msg}"
        if ok:
            self._set_test_connection_feedback(f"{_TEST_OK_MARK} {msg}", _TEST_OK_COLOR)
        else:
            self._set_test_connection_feedback(
                f"{_TEST_FAILED_MARK} {msg}", _TEST_FAILED_COLOR
            )

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
        self._apply_provider_connection_test_mark(provider)

    def _clear_provider_connection_test(self, provider: str) -> None:
        self._provider_test_history.pop(provider, None)
        try:
            self._provider_connection_test_store.clear_result(provider)
        except Exception:
            self._settings_perf_logger.exception(
                "Failed to clear %s connection test result", provider
            )
        self._apply_provider_connection_test_mark(provider)
        # The shared line may describe the key just replaced or removed.
        self._show_latest_connection_test()

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
        for provider in self._provider_test_marks:
            self._apply_provider_connection_test_mark(provider)
        self._show_latest_connection_test()

    @staticmethod
    def _connection_test_text(
        provider_label: str, result: tuple[bool, str, str]
    ) -> str:
        ok, message, timestamp = result
        marker = _TEST_OK_MARK if ok else _TEST_FAILED_MARK
        return f"Last test ({timestamp}): {provider_label} {marker} {message}"

    def _apply_provider_connection_test_mark(self, provider: str) -> None:
        mark = self._provider_test_marks.get(provider)
        if mark is None:
            return
        result = self._provider_test_history.get(provider)
        if result is None:
            mark.setText("")
            mark.setToolTip("Not tested since the key was last saved.")
            return
        ok = result[0]
        mark.setText(_TEST_OK_MARK if ok else _TEST_FAILED_MARK)
        mark.setStyleSheet(
            f"color: {_TEST_OK_COLOR if ok else _TEST_FAILED_COLOR}; font-weight: bold;"
        )
        mark.setToolTip(
            self._connection_test_text(self._provider_label(provider), result)
        )

    def _show_latest_connection_test(self) -> None:
        """Put the most recent stored test result on the shared line.

        Used when the dialog opens and when a key's result is cleared;
        a test just run reports itself instead. A running test owns the line.
        """
        if self._active_connection_test_thread is not None:
            return
        if not self._provider_test_history:
            self._set_test_connection_feedback("", "#555")
            return
        # The timestamps are "%Y-%m-%d %H:%M:%S", so they sort as text.
        provider, result = max(
            self._provider_test_history.items(), key=lambda item: item[1][2]
        )
        self._set_test_connection_feedback(
            self._connection_test_text(self._provider_label(provider), result),
            _TEST_OK_COLOR if result[0] else _TEST_FAILED_COLOR,
        )
