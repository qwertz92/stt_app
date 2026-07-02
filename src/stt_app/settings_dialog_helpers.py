"""Shared widgets, constants and pure helpers for the settings dialog."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6 import QtCore, QtGui, QtWidgets

from .benchmark_history import BenchmarkHistoryEntry
from .config import (
    ASSEMBLYAI_MODELS,
    AZURE_SPEECH_MODELS,
    DEEPGRAM_MODELS,
    DEFAULT_ASSEMBLYAI_MODEL,
    DEFAULT_AZURE_SPEECH_MODEL,
    DEFAULT_DEEPGRAM_MODEL,
    DEFAULT_ELEVENLABS_MODEL,
    DEFAULT_FUNASR_MODEL,
    DEFAULT_GROQ_MODEL,
    DEFAULT_OPENAI_MODEL,
    ELEVENLABS_MODELS,
    FUNASR_MODELS,
    GROQ_MODELS,
    OPENAI_MODELS,
)
from .local_benchmark import _format_seconds


def _emit_background_signal(
    owner: QtCore.QObject,
    signal_name: str,
    *args: object,
) -> bool:
    try:
        getattr(owner, signal_name).emit(*args)
    except RuntimeError:
        return False
    return True


class _WheelPassthroughComboBox(QtWidgets.QComboBox):
    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        view = self.view()
        if view is not None and view.isVisible():
            super().wheelEvent(event)
            return
        event.ignore()


class _WheelPassthroughSpinBox(QtWidgets.QSpinBox):
    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        event.ignore()


class _WheelPassthroughDoubleSpinBox(QtWidgets.QDoubleSpinBox):
    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        event.ignore()


_REMOTE_MODEL_LABELS: dict[str, str] = {
    "whisper-large-v3": "whisper-large-v3 (best quality, $0.111/hr)",
    "whisper-large-v3-turbo": "whisper-large-v3-turbo (faster, $0.04/hr)",
    "gpt-4o-mini-transcribe": "gpt-4o-mini-transcribe (fast, low cost)",
    "gpt-4o-transcribe": "gpt-4o-transcribe (higher quality)",
    "whisper-1": "whisper-1 (legacy whisper model)",
    "nova-3": "nova-3 (current default)",
    "nova-2": "nova-2 (older generation)",
    "universal-3-pro": "universal-3-pro (highest accuracy, falls back to universal-2)",
    "universal-2": "universal-2 (fast, broad language coverage)",
    "scribe_v2": "scribe_v2 (current default, highest published accuracy)",
    "scribe_v1": "scribe_v1 (legacy batch model)",
    "mai-transcribe-1.5": "mai-transcribe-1.5 (current default, 42 languages)",
    "mai-transcribe-1": "mai-transcribe-1 (first generation, fewer languages)",
    "fun-asr-realtime": "fun-asr-realtime (31 languages; no German)",
}


_REMOTE_MODEL_CHOICES: dict[str, tuple[tuple[str, str], ...]] = {
    "groq": tuple((value, _REMOTE_MODEL_LABELS.get(value, value)) for value in GROQ_MODELS),
    "openai": tuple(
        (value, _REMOTE_MODEL_LABELS.get(value, value)) for value in OPENAI_MODELS
    ),
    "deepgram": tuple(
        (value, _REMOTE_MODEL_LABELS.get(value, value)) for value in DEEPGRAM_MODELS
    ),
    "assemblyai": tuple(
        (value, _REMOTE_MODEL_LABELS.get(value, value))
        for value in ASSEMBLYAI_MODELS
    ),
    "elevenlabs": tuple(
        (value, _REMOTE_MODEL_LABELS.get(value, value))
        for value in ELEVENLABS_MODELS
    ),
    "azure": tuple(
        (value, _REMOTE_MODEL_LABELS.get(value, value))
        for value in AZURE_SPEECH_MODELS
    ),
    "funasr": tuple(
        (value, _REMOTE_MODEL_LABELS.get(value, value))
        for value in FUNASR_MODELS
    ),
}


_ENGINE_LABELS: dict[str, str] = {
    "local": "Local (faster-whisper / ONNX)",
    "assemblyai": "Remote (AssemblyAI)",
    "groq": "Remote (Groq)",
    "openai": "Remote (OpenAI)",
    "deepgram": "Remote (Deepgram)",
    "elevenlabs": "Remote (ElevenLabs)",
    "azure": "Remote (Azure LLM Speech)",
    "funasr": "Remote (Fun-ASR / Alibaba)",
}


_HISTORY_TIMEZONE_LABELS: dict[str, str] = {
    "local": "Local time",
    "utc": "UTC",
}


_OVERLAY_CORNER_LABELS: dict[str, str] = {
    "top-right": "Top Right",
    "top-left": "Top Left",
    "bottom-right": "Bottom Right",
    "bottom-left": "Bottom Left",
}


_MODE_LABELS: dict[str, str] = {
    "batch": "Batch",
    "streaming": "Streaming (Experimental)",
}


_CONCURRENT_MODE_LABELS: dict[str, str] = {
    "insert": "Queue & insert into its window",
    "history": "Queue & save to history only",
    "cancel": "Cancel the running transcription",
}


_PASTE_MODE_LABELS: dict[str, str] = {
    "auto": "Auto (SendInput -> WM_PASTE)",
    "wm_paste": "WM_PASTE only",
    "send_input": "SendInput only",
}


_START_BEEP_TONE_LABELS: dict[str, str] = {
    "soft": "Soft beep",
    "high": "High beep",
    "chime": "Two-tone chime",
    "system": "System notification",
}


_DEFAULT_SETTINGS_DIALOG_SIZE = QtCore.QSize(780, 960)


_DIALOG_SCREEN_MARGIN = 48


_COMPACT_LIST_ITEM_STYLESHEET = "QListWidget::item { padding: 0px 4px; }"


_COMPACT_LIST_ROW_EXTRA_PX = 4


_COMPACT_TABLE_ROW_EXTRA_PX = 4


_LOCAL_MODEL_AUTO_REFRESH_DELAY_MS = 150


_PROVIDER_STATUS_BADGE_TEXTS = (
    "Not configured",
    "Unsaved input",
    "Will clear on Save",
    "Stored securely",
    "Secure (legacy)",
    "Stored insecurely",
    "Insecure disabled",
)


_PROVIDER_STATUS_BADGE_HORIZONTAL_PADDING_PX = 16


_REMOTE_PROVIDER_LABEL_EXTRA_PX = 18


_REMOTE_PROVIDER_GRID_SPACING_PX = 12


_GENERAL_FORM_LABEL_EXTRA_PX = 12


_ACTION_ROW_SPACING_PX = 8


_INLINE_FIELD_BUTTON_SPACING_PX = 6


_REMOTE_MODEL_DEFAULTS: dict[str, str] = {
    "groq": DEFAULT_GROQ_MODEL,
    "openai": DEFAULT_OPENAI_MODEL,
    "deepgram": DEFAULT_DEEPGRAM_MODEL,
    "assemblyai": DEFAULT_ASSEMBLYAI_MODEL,
    "elevenlabs": DEFAULT_ELEVENLABS_MODEL,
    "azure": DEFAULT_AZURE_SPEECH_MODEL,
    "funasr": DEFAULT_FUNASR_MODEL,
}


@dataclass(frozen=True)
class _RemoteProviderInfo:
    """One row in the canonical remote-provider table.

    ``title`` is the short label used for the Remote-tab row and the
    "<title> only" connection-test target; ``label`` is the longer display
    label used in status/import text (falls back to ``name`` elsewhere via
    :func:`_remote_provider_label`).
    """

    name: str
    title: str
    label: str


# Single source of truth for the 7 remote providers and their UI order.
# Every other provider-name list/order in the settings dialog derives from
# this tuple instead of repeating it.
_REMOTE_PROVIDERS: tuple[_RemoteProviderInfo, ...] = (
    _RemoteProviderInfo("assemblyai", "AssemblyAI", "AssemblyAI"),
    _RemoteProviderInfo("groq", "Groq", "Groq"),
    _RemoteProviderInfo("openai", "OpenAI", "OpenAI"),
    _RemoteProviderInfo("deepgram", "Deepgram", "Deepgram"),
    _RemoteProviderInfo("elevenlabs", "ElevenLabs", "ElevenLabs"),
    _RemoteProviderInfo("azure", "Azure", "Azure LLM Speech"),
    _RemoteProviderInfo("funasr", "Fun-ASR", "Fun-ASR (Alibaba)"),
)


_REMOTE_PROVIDER_LABELS: dict[str, str] = {
    provider.name: provider.label for provider in _REMOTE_PROVIDERS
}


def _remote_provider_label(name: str) -> str:
    return _REMOTE_PROVIDER_LABELS.get(name, name)


_REMOTE_API_KEY_PROVIDERS: tuple[str, ...] = tuple(
    provider.name for provider in _REMOTE_PROVIDERS
)


_LOCAL_MODEL_SCAN_SESSION_CACHE: dict[str, list[str]] = {}


_LOCAL_MODEL_SCAN_SESSION_VERIFIED_DIRS: set[str] = set()


def _set_transcriber_progress_callback(
    transcriber: object,
    callback: Callable[[str], None],
) -> None:
    setter = getattr(transcriber, "set_progress_callback", None)
    if callable(setter):
        setter(callback)


def _benchmark_status_text(status: str) -> str:
    labels = {
        "running": "Running",
        "completed": "Completed",
        "completed_with_errors": "Completed with errors",
        "canceled": "Canceled",
        "failed": "Failed",
    }
    return labels.get(str(status or "").strip().lower(), str(status or ""))


def _benchmark_history_label(entry: BenchmarkHistoryEntry) -> str:
    models = ", ".join(entry.options.model_names[:3])
    if len(entry.options.model_names) > 3:
        models = f"{models}, ..."
    fastest = min(
        (case for case in entry.cases if case.error is None and case.runs),
        key=lambda case: case.avg_seconds,
        default=None,
    )
    speed = ""
    if fastest is not None:
        speed = f" | fastest {fastest.model} {_format_seconds(fastest.avg_seconds)}"
    status = _benchmark_status_text(entry.status)
    return f"{entry.created_at} | {status} | {models or 'no models'}{speed}"


def _qt_hotkey_sequence_to_app_hotkey(
    sequence: QtGui.QKeySequence,
) -> str:
    text = sequence.toString(QtGui.QKeySequence.PortableText)
    return _qt_hotkey_text_to_app_hotkey(text)


def _qt_hotkey_text_to_app_hotkey(text: str) -> str:
    if not text:
        return ""

    first = text.split(",")[0].strip()
    if not first:
        return ""

    token_map = {
        "CTRL": "Ctrl",
        "ALT": "Alt",
        "SHIFT": "Shift",
        "META": "Win",
        "ESCAPE": "Esc",
        "RETURN": "Enter",
    }
    tokens = [token.strip() for token in first.split("+") if token.strip()]
    normalized: list[str] = []
    for token in tokens:
        upper = token.upper()
        if upper in token_map:
            normalized.append(token_map[upper])
            continue
        if len(token) == 1:
            normalized.append(token.upper())
            continue
        normalized.append(token)

    return "+".join(normalized)


def _app_hotkey_to_qt_hotkey_text(text: str) -> str:
    if not text:
        return ""

    token_map = {
        "WIN": "Meta",
        "ESC": "Escape",
    }
    tokens = [token.strip() for token in text.split("+") if token.strip()]
    normalized: list[str] = []
    for token in tokens:
        upper = token.upper()
        normalized.append(token_map.get(upper, token))
    return "+".join(normalized)


def _hotkeys_conflict(first: str, second: str) -> bool:
    left = _hotkey_token_set(first)
    right = _hotkey_token_set(second)
    if not left or not right:
        return False
    if left == right:
        return True
    return left.issubset(right) or right.issubset(left)


def _hotkey_token_set(value: str) -> set[str]:
    return {
        token.strip().upper()
        for token in str(value or "").split("+")
        if token.strip()
    }
