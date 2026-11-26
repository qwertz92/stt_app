"""Shared widgets, constants and pure helpers for the settings dialog."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6 import QtCore, QtGui, QtWidgets

from . import local_runtime_support
from .benchmark_history import BenchmarkHistoryEntry
from .config import (
    ASSEMBLYAI_MODELS,
    ASSEMBLYAI_REGION_AUTO,
    ASSEMBLYAI_REGION_EU,
    ASSEMBLYAI_REGION_US,
    AZURE_SPEECH_MODELS,
    DEEPGRAM_MODELS,
    DEEPGRAM_REGION_EU,
    DEEPGRAM_REGION_GLOBAL,
    DEFAULT_ASSEMBLYAI_MODEL,
    DEFAULT_AZURE_SPEECH_MODEL,
    DEFAULT_DEEPGRAM_MODEL,
    DEFAULT_ELEVENLABS_MODEL,
    DEFAULT_ENGINE,
    DEFAULT_FUNASR_MODEL,
    DEFAULT_GROQ_MODEL,
    DEFAULT_META_MODEL,
    DEFAULT_MISTRAL_MODEL,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_OPENROUTER_MODEL,
    DEFAULT_SPEECHMATICS_MODEL,
    ELEVENLABS_MODELS,
    ENGINE_LABELS,
    FUNASR_MODELS,
    GROQ_MODELS,
    LOCAL_ONNX_MODEL_PRECISION,
    META_MODELS,
    MISTRAL_MODELS,
    MODEL_ESTIMATED_SIZE_MB,
    OPENAI_MODELS,
    SPEECHMATICS_MODELS,
    VALID_MODEL_SIZES,
)
from .local_benchmark import _format_seconds
from .settings_store import _REMOTE_MODEL_FIELDS, LISTED_MODEL_ENGINES

# What `Thread.start()` raises when the worker cannot be created: the OS refuses
# the thread (`RuntimeError`) or the interpreter cannot allocate its
# bookkeeping (`MemoryError`). Every worker start in the dialog guards both,
# or its busy marker stays set for the life of the dialog.
_THREAD_START_ERRORS = (RuntimeError, MemoryError)


def exception_reason(exc: BaseException) -> str:
    """An exception's text, or its type name when it has none.

    `str(MemoryError())` is empty, so "Could not start ...: {exc}" ended in a
    bare colon exactly in the case the guards above exist for.
    """
    return str(exc) or type(exc).__name__


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


class MinimumHeightHintListWidget(QtWidgets.QListWidget):
    """A list whose preferred height is its minimum height.

    A settings page is a `QScrollArea` whose content never gets less height
    than its layout's preferred one (wrapped labels make the layout
    height-for-width, and the area then uses the preferred, not the minimum,
    height). A list that prefers all its rows would therefore push the page
    into a scroll bar before it gives up any of them; one that prefers its
    minimum and expands up to its maximum gives up rows first.
    """

    def sizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(super().sizeHint().width(), self.minimumHeight())


class ContentCappedGroupBox(QtWidgets.QGroupBox):
    """A group box that grows no taller than its content.

    Its `flexible_child` prefers its minimum height but expands up to its
    maximum; a box in a layout with spare height would take that spare height
    into itself as empty space once the child is at its maximum. The cap is the
    box's preferred height with the child at its maximum, kept up to date on
    every layout request (a status line appearing changes it).
    """

    def __init__(self, title: str) -> None:
        super().__init__(title)
        self.flexible_child: QtWidgets.QWidget | None = None

    def event(self, event: QtCore.QEvent) -> bool:
        handled = super().event(event)
        if event.type() in (
            QtCore.QEvent.Type.LayoutRequest,
            QtCore.QEvent.Type.Resize,
            QtCore.QEvent.Type.Show,
        ):
            self._update_height_cap()
        return handled

    def _update_height_cap(self) -> None:
        child = self.flexible_child
        if child is None:
            return
        # At the width it has now: wrapped notes take more lines when narrow,
        # which the plain size hint (taken at the preferred width) misses.
        width = self.width()
        height = (
            self.heightForWidth(width)
            if self.hasHeightForWidth()
            else self.sizeHint().height()
        )
        cap = height - child.sizeHint().height() + child.maximumHeight()
        if cap != self.maximumHeight():
            self.setMaximumHeight(cap)


class ElidingLabel(QtWidgets.QLabel):
    """A one-line label that shortens its text to fit instead of clipping it.

    A `QLabel` with word wrap off cuts a long line at its right edge with
    nothing to show that it did, and its `minimumSizeHint` still reports the
    full text width -- so the benchmark header's status label showed 77% of a
    failure message with no ellipsis and no tooltip, and raised the settings
    dialog's own minimum width from 492 px to 1109 px the moment that message
    appeared. Word wrap is not the answer for a label that shares a fixed-height
    row with a button, because growing it moves the button.

    It is one line whatever the text holds: a failed save's exception message
    can span several lines, and shown as such the bottom status label grew
    taller than the Save and Close buttons beside it and moved them up --
    7 px at three lines, 8 px more per line after (measured).

    `text()` keeps returning the full string, so callers and tests read what was
    set rather than what happened to fit, and the whole message stays readable
    in the tooltip. Copying goes through `copy_message`, which puts the
    message as written on the clipboard: `QLabel` copies from its own text
    control, which holds the elided text.
    """

    # Which end of a too-long text is cut; a path keeps both ends (middle).
    elide_mode = QtCore.Qt.ElideRight

    def __init__(self, text: str = "", parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._full_text = ""
        self.setWordWrap(False)
        # Ignored, not Expanding: the layout must give this label the space that
        # is left over, never widen the window to fit the text.
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        self.setText(text)

    def setText(self, text: str) -> None:
        self._full_text = str(text)
        self.setToolTip(self._full_text)
        self._apply_elide()

    def text(self) -> str:
        return self._full_text

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        if event.matches(QtGui.QKeySequence.Copy):
            self.copy_message()
            event.accept()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event: QtGui.QContextMenuEvent) -> None:
        menu = self._context_menu()
        menu.exec(event.globalPos())
        menu.deleteLater()

    def _context_menu(self) -> QtWidgets.QMenu:
        """`QLabel`'s own menu copies through its text control, which holds
        the elided text; this one copies through `copy_message`."""
        menu = QtWidgets.QMenu(self)
        copy_action = menu.addAction("Copy")
        copy_action.setEnabled(bool(self._full_text))
        copy_action.triggered.connect(self.copy_message)
        select_all = menu.addAction("Select All")
        select_all.setEnabled(bool(self._full_text))
        select_all.triggered.connect(self._select_all)
        return menu

    def _select_all(self) -> None:
        self.setSelection(0, len(QtWidgets.QLabel.text(self)))

    def copy_message(self) -> None:
        """Put the selected part, or the whole message, on the clipboard.

        `QLabel` copies from its own text control, which holds the elided
        text: a failed save's message, the one text worth pasting into a bug
        report, came back as its first 37 characters and an ellipsis, and
        the tooltip that holds the rest cannot be copied. A part of the
        painted text copies as selected; the whole of it, or no selection,
        copies the message as written.
        """
        selected = self.selectedText()
        painted = QtWidgets.QLabel.text(self)
        text = selected if selected and selected != painted else self._full_text
        QtGui.QGuiApplication.clipboard().setText(text)

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._apply_elide()

    def _apply_elide(self) -> None:
        single_line = " ".join(self._full_text.split())
        margins = self.contentsMargins()
        available = self.width() - margins.left() - margins.right()
        if available <= 0 or not single_line:
            super().setText(single_line)
            return
        super().setText(
            self.fontMetrics().elidedText(single_line, self.elide_mode, available)
        )


class WrappedStatusLabel(QtWidgets.QLabel):
    """A word-wrapped status line whose whole text is also its tooltip.

    No wrapped label may widen the dialog (`_let_wrapped_labels_narrow`), so
    one unbroken token in a status line -- a cache path, a URL in a
    provider's error -- is cut off at the label's edge: 110-140 px of a
    120-character path at the default width (measured). The tooltip is where
    the rest can be read.
    """

    def setText(self, text: str) -> None:
        super().setText(text)
        self.setToolTip(text)


class CollapsingStatusLabel(WrappedStatusLabel):
    """A `WrappedStatusLabel` that takes no room while it has nothing to say.

    For a line below the controls it reports on: its text appearing moves
    nothing above it, so it needs no reserved space.
    """

    def __init__(self, text: str = "") -> None:
        super().__init__(text)
        self.setVisible(bool(text))

    def setText(self, text: str) -> None:
        super().setText(text)
        self.setVisible(bool(text))


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
    "gpt-transcribe": "gpt-transcribe (current default, recommended by OpenAI)",
    "nova-3": "nova-3 (current default)",
    "assemblyai-stt-3-7": "assemblyai-stt-3-7 (latest, highest accuracy; 32 languages)",
    "universal-2": "universal-2 (fast, broad language coverage)",
    "scribe_v3": "scribe_v3 (current default, highest published accuracy)",
    "mai-transcribe-2": "mai-transcribe-2 (current default, 60 languages)",
    "fun-asr-realtime": "fun-asr-realtime (30 languages)",
    "melia-1": "melia-1 (current default; Auto detects the language)",
    "enhanced": "enhanced (highest accuracy; pick a language)",
    "voxtral-mini-transcribe-3-0": (
        "voxtral-mini-transcribe-3-0 (current default, $0.21/hr)"
    ),
    "voxtral-small-transcribe-3": (
        "voxtral-small-transcribe-3 (slightly more accurate, $0.24/hr)"
    ),
    "muse-voice-transcribe-1.0": "muse-voice-transcribe-1.0 ($0.18/hr, 25 languages)",
}


_REMOTE_MODEL_CHOICES: dict[str, tuple[tuple[str, str], ...]] = {
    "groq": tuple(
        (value, _REMOTE_MODEL_LABELS.get(value, value)) for value in GROQ_MODELS
    ),
    "openai": tuple(
        (value, _REMOTE_MODEL_LABELS.get(value, value)) for value in OPENAI_MODELS
    ),
    "deepgram": tuple(
        (value, _REMOTE_MODEL_LABELS.get(value, value)) for value in DEEPGRAM_MODELS
    ),
    "assemblyai": tuple(
        (value, _REMOTE_MODEL_LABELS.get(value, value)) for value in ASSEMBLYAI_MODELS
    ),
    "elevenlabs": tuple(
        (value, _REMOTE_MODEL_LABELS.get(value, value)) for value in ELEVENLABS_MODELS
    ),
    "azure": tuple(
        (value, _REMOTE_MODEL_LABELS.get(value, value)) for value in AZURE_SPEECH_MODELS
    ),
    "funasr": tuple(
        (value, _REMOTE_MODEL_LABELS.get(value, value)) for value in FUNASR_MODELS
    ),
    "speechmatics": tuple(
        (value, _REMOTE_MODEL_LABELS.get(value, value)) for value in SPEECHMATICS_MODELS
    ),
    "mistral": tuple(
        (value, _REMOTE_MODEL_LABELS.get(value, value)) for value in MISTRAL_MODELS
    ),
    "meta": tuple(
        (value, _REMOTE_MODEL_LABELS.get(value, value)) for value in META_MODELS
    ),
}


# The data-residency region choices of each provider that has them, as
# (value, label, what the vendor guarantees for it); the field each is
# stored in is `settings_store._REMOTE_REGION_FIELDS`, the hosts and the
# vendor sentences behind each guarantee are beside the host tables in
# `config.py`. The vendor's default endpoint comes first and says so. A
# label claims no more than the vendor does: AssemblyAI's default streaming
# host routes to the US or the EU, so it is "Automatic", not "US", and
# Deepgram's default is its "global" endpoint (review of 2026-10-01).
_REMOTE_REGION_CHOICES: dict[str, tuple[tuple[str, str, str], ...]] = {
    "assemblyai": (
        (
            ASSEMBLYAI_REGION_AUTO,
            "Automatic (default)",
            (
                "AssemblyAI's default endpoints: batch is processed in the US; "
                "streaming goes to the nearest location, in the US or EU."
            ),
        ),
        (
            ASSEMBLYAI_REGION_US,
            "US only",
            "Batch and streaming are processed only in the US.",
        ),
        (
            ASSEMBLYAI_REGION_EU,
            "EU only",
            "Batch and streaming are processed only in the EU.",
        ),
    ),
    "deepgram": (
        (
            DEEPGRAM_REGION_GLOBAL,
            "Global (default)",
            (
                "Deepgram's default global endpoint; Deepgram states no "
                "processing location for it."
            ),
        ),
        (
            DEEPGRAM_REGION_EU,
            "EU",
            (
                "Deepgram's EU endpoint, which routes the traffic through the "
                "EU. It takes the same API key."
            ),
        ),
    ),
    "speechmatics": (
        ("eu1", "EU (default)", "Jobs are created in Speechmatics' EU region."),
        ("us1", "US", "Jobs are created in Speechmatics' US region."),
        (
            "au1",
            "Australia",
            (
                "Jobs are created in Speechmatics' Australian region; Melia 1 "
                "is not offered there."
            ),
        ),
    ),
}


_ENGINE_CLOUD_HEADER = "Cloud"


def fill_engine_combo(combo: QtWidgets.QComboBox, engines: tuple[str, ...]) -> None:
    """The local engine, a separator, a "Cloud" header, then the providers.

    One filler for the three engine pickers (Transcription, Import Audio and
    the retranscribe dialog), so they list the engines identically. The
    header is disabled and not selectable: `QComboBox` skips such an item on
    the arrow keys and the wheel, and it carries no data, so `findData` and
    `currentData` only ever see engine ids.
    """
    remote = [engine for engine in engines if engine != DEFAULT_ENGINE]
    if DEFAULT_ENGINE in engines:
        combo.addItem(ENGINE_LABELS.get(DEFAULT_ENGINE, DEFAULT_ENGINE), DEFAULT_ENGINE)
        if remote:
            combo.insertSeparator(combo.count())
    if remote:
        combo.addItem(_ENGINE_CLOUD_HEADER)
        model = combo.model()
        header = model.item(combo.count() - 1) if hasattr(model, "item") else None
        if header is not None:
            header.setEnabled(False)
            header.setSelectable(False)
    for engine in remote:
        combo.addItem(ENGINE_LABELS.get(engine, engine), engine)


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


# UI choices for the "While busy" combo. The first element is the
# stored value: "insert"/"history"/"cancel" map to concurrent_transcription_mode
# directly; "insert_immediate" is Insert mode with immediate_background_insert.
_CONCURRENT_MODE_UI_CHOICES: tuple[tuple[str, str], ...] = (
    ("insert", "Finish previous; insert all results when idle"),
    ("insert_immediate", "Finish previous; insert each result when ready"),
    ("history", "Finish previous; save its result to history only"),
    ("cancel", "Cancel previous; keep any result that already finished"),
)
_CONCURRENT_MODE_IMMEDIATE_UI_VALUE = "insert_immediate"


# The Run Benchmark window's device choice that measures a model on a GPU and
# on the CPU, i.e. the one a user has to pick for `auto` to learn anything.
# Named once because the Transcription tab's note tells them to pick it by
# name, and a second copy of that string would drift the moment the combo is
# reworded.
BENCHMARK_GPU_CPU_COMPARISON_LABEL = "GPU + CPU comparison"

# How the device values read in a sentence. The transcriber has its own,
# longer labels for a *running* runtime ("DirectML GPU"); these are the names
# the pickers use.
ONNX_DEVICE_LABELS: dict[str, str] = {
    "webgpu": "WebGPU",
    "dml": "DirectML",
    "cpu": "CPU",
}


def onnx_device_label(device: str) -> str:
    return ONNX_DEVICE_LABELS.get(device, device)


def onnx_device_order_text(order: tuple[str, ...]) -> str:
    """ "WebGPU, then DirectML, then CPU" -- one rendering of a device chain."""
    return ", then ".join(onnx_device_label(device) for device in order)


_PASTE_MODE_LABELS: dict[str, str] = {
    "auto": "Auto (SendInput -> WM_PASTE)",
    "wm_paste": "WM_PASTE only",
    "send_input": "SendInput only",
}


_INSERT_TARGET_LABELS: dict[str, str] = {
    "recording_window": "Window focused when the recording started",
    "current_window": "Window focused when the transcript is ready",
}


_START_BEEP_TONE_LABELS: dict[str, str] = {
    "soft": "Soft beep",
    "high": "High beep",
    "chime": "Two-tone chime",
    "system": "System notification",
}


def _approximate_size_text(model_name: str) -> str:
    """The download size, taken from the one table that measures it.

    Never hand-written next to the name. `MODEL_ESTIMATED_SIZE_MB` is
    corrected whenever a real download disagrees with it, and a second copy in
    the picker label does not follow: `distil-large-v3.5` read "~756 MB"
    against a measured 1516, and `large-v3-turbo` "~809 MB" against 1622, so
    the two models the label was meant to help choose between both understated
    themselves by half.

    **The table is decimal megabytes** -- it says so, and
    `model_download_progress` turns each entry into bytes with `* 1_000_000`,
    so the progress bar a user watches counts in the same unit. Dividing by
    1024 and printing "GB" therefore names neither unit: `medium` is 1531
    decimal MB = 1.53 GB = 1.43 GiB, and 1531/1024 = 1.50 is a third number.
    Deriving the label first got this wrong in the other direction from the
    defect it fixed -- `megabytes / 1024` with one decimal took the four
    labels that were already exactly right as two-decimal decimal GB down to a
    wrong one-decimal binary value: `cohere-transcribe-03-2026` `~2.13 GB` to
    `~2.1 GB`, both Granite entries `~1.84 GB` to `~1.8 GB`, and
    `canary-1b-v2` `~1.03 GB` to `~1.0 GB`. (Four, not six, and `large-v3` is
    not among them -- it read `~3 GB` by hand, so it was already imprecise
    against the 3091 MB its own download bar counts to, and the divisor only
    made it imprecise differently.)
    """
    megabytes = MODEL_ESTIMATED_SIZE_MB.get(model_name)
    if not megabytes:
        return ""
    if megabytes < 1000:
        return f"~{megabytes} MB"
    return f"~{megabytes / 1000:.2f} GB"


# Single source of truth for how a local model is named in any picker: the
# Settings General/Import tabs and the overlay's retranscribe dialog. Each
# entry is the display name plus the notes that are not the size; the size is
# derived above.
_LOCAL_MODEL_NAMES_AND_NOTES: dict[str, tuple[str, str]] = {
    "tiny": ("tiny", ""),
    "base": ("base", ""),
    "small": ("small", ""),
    "medium": ("medium", ""),
    "large-v3": ("large-v3", "multilingual"),
    "large-v3-turbo": ("large-v3-turbo", "multilingual, fast"),
    "distil-large-v3.5": ("distil-large-v3.5", "English only, improved"),
    "cohere-transcribe-03-2026": ("Cohere Transcribe 03-2026", "ONNX/WebGPU"),
    "granite-4.0-1b-speech": ("IBM Granite 4.0 1B Speech", "ONNX/WebGPU"),
    "granite-speech-4.1-2b": ("IBM Granite Speech 4.1 2B", "ONNX/WebGPU"),
    "nemotron-3.5-asr-streaming-0.6b-int4": (
        "NVIDIA Nemotron 3.5 ASR 0.6B",
        "true 560 ms streaming",
    ),
    # Not "fastest": `tiny` measured 0.033 RTF against Parakeet's 0.043 in
    # the same run, and the supportable claim is the narrower one recorded
    # in docs/agents/benchmark.md -- the fastest local model that transcribed
    # the recording. A picker has no room for that qualifier, so it states
    # what is unambiguous instead.
    "parakeet-tdt-0.6b-v3": ("NVIDIA Parakeet TDT 0.6B v3", "CPU, multilingual"),
    # No "post-trained by Moondream" here: the retranscribe dialog's model
    # combo is 476 px wide at its 560 px minimum, and that note made this
    # label need 572 px, clipped even at the 640 px default (measured
    # 2026-09-27). docs/models.md carries the attribution.
    "parakeet-tdt-0.6b-v3-ultra": (
        "NVIDIA Parakeet TDT 0.6B v3 Ultra",
        "CPU, multilingual",
    ),
    "canary-1b-v2": ("NVIDIA Canary 1B v2", "CPU, pick a language"),
    "granite-speech-5.0-470m-turboctc": (
        "IBM Granite Speech 5.0 470M",
        "CPU, English, no punctuation",
    ),
}

# Retired on 2026-08-26 (see docs/granite-speech-4.1-onnx-variants.md). They
# are not in VALID_MODEL_SIZES, so they appear in no picker; these entries
# exist only so a history row recorded with one still reads as a model name
# instead of a raw identifier.
_RETIRED_MODEL_LABELS: dict[str, str] = {
    "granite-speech-4.1-2b-plus": "IBM Granite Speech 4.1 2B Plus (removed)",
    "granite-speech-4.1-2b-nar": "IBM Granite Speech 4.1 2B NAR (removed)",
}


def _build_local_model_labels() -> dict[str, str]:
    labels: dict[str, str] = {}
    for name, (display, note) in _LOCAL_MODEL_NAMES_AND_NOTES.items():
        details = ", ".join(
            part for part in (_approximate_size_text(name), note) if part
        )
        labels[name] = f"{display} ({details})" if details else display
    labels.update(_RETIRED_MODEL_LABELS)
    return labels


LOCAL_MODEL_LABELS: dict[str, str] = _build_local_model_labels()


def local_model_precision_label(model_name: str) -> str:
    precision = LOCAL_ONNX_MODEL_PRECISION.get(model_name, "")
    if not precision:
        return ""
    return precision.upper()


def local_model_label(model_name: str) -> str:
    label = LOCAL_MODEL_LABELS.get(model_name, model_name)
    precision = local_model_precision_label(model_name)
    if precision:
        label = f"{label} [{precision}]"
    # The row stays in every picker so that a stored selection still shows up
    # as itself; what it cannot do is said on the row, and the reason in the
    # note under the model picker.
    if local_runtime_support.unavailable_reason(model_name):
        label += local_runtime_support.UNAVAILABLE_LABEL_SUFFIX
    return label


def local_model_short_label(model_name: str) -> str:
    """The model's name without the size/runtime/precision parenthetical.

    `local_model_label` builds the whole combo entry -- "Cohere Transcribe
    03-2026 (~2.1 GB, ONNX/WebGPU) [Q4]" -- which is right in a list and
    wrong inside a sentence. Prose that has to name the selected model wants
    the part the user recognises, not the raw settings id (which does not
    match anything on screen) and not the full entry.
    """
    label = LOCAL_MODEL_LABELS.get(model_name, model_name)
    return label.split(" (", 1)[0].strip() or label


def model_choices_for_engine(
    engine: str, custom_models: tuple[str, ...] = ()
) -> tuple[tuple[str, str], ...]:
    """``(value, label)`` pairs of the models ``engine`` can transcribe with.

    The custom endpoint and OpenRouter have no roster: the service decides what
    it offers, so their choices are ``custom_models`` -- the stored model and
    whatever the last Refresh fetched -- in order, blanks and repeats dropped.
    """
    normalized = str(engine or "").strip().lower()
    if normalized == DEFAULT_ENGINE:
        return tuple((value, local_model_label(value)) for value in VALID_MODEL_SIZES)
    if normalized in LISTED_MODEL_ENGINES:
        values = dict.fromkeys(str(model or "").strip() for model in custom_models)
        return tuple((value, value) for value in values if value)
    return _REMOTE_MODEL_CHOICES.get(normalized, ())


_DEFAULT_SETTINGS_DIALOG_SIZE = QtCore.QSize(860, 1010)


_DIALOG_SCREEN_MARGIN = 48


_COMPACT_LIST_ITEM_STYLESHEET = "QListWidget::item { padding: 0px 4px; }"


_COMPACT_LIST_ROW_EXTRA_PX = 4


_COMPACT_TABLE_ROW_EXTRA_PX = 4


def compact_table_row_height(widget: QtWidgets.QTableWidget) -> int:
    """Row height of the dialog's compact tables.

    A free function rather than a `SettingsDialog` method: the benchmark
    results panel uses it too and is a plain widget, embedded both in the
    dialog and in a window of its own.
    """
    return max(widget.fontMetrics().height() + _COMPACT_TABLE_ROW_EXTRA_PX, 18)


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


_FIELD_HINT_MIN_WIDTH_PX = 460


# Hint and note text is drawn a little smaller than the dialog's own font. It
# was a stylesheet `font-size: 11px`, a pixel size, which stays 11 px when
# Windows' "Text size" raises the application font: at 13.5 pt every control
# grew by half and the hints did not grow at all. 0.92 is the ratio that size
# had at the default 9 pt (11 px against 12 px), so the default rendering is
# unchanged: both measure a 15 px line.
_HINT_FONT_SCALE = 0.92


def hint_font() -> QtGui.QFont:
    """The application font scaled down for hints and notes."""
    font = QtGui.QFont(QtWidgets.QApplication.font())
    point_size = font.pointSizeF()
    if point_size > 0:
        font.setPointSizeF(point_size * _HINT_FONT_SCALE)
    elif font.pixelSize() > 0:
        font.setPixelSize(max(1, round(font.pixelSize() * _HINT_FONT_SCALE)))
    return font


def unlabelled_row_label() -> QtWidgets.QLabel:
    """An empty label widget for a form row that has no label text.

    `QFormLayout.addRow("", field)` creates no label widget at all, and a row
    without one does not honour height-for-width: the row keeps the height its
    word-wrapped hint needs at the hint's minimum width. Measured on the
    Transcription and Audio tabs, that was a 15 px blank line under four
    checkbox hints at every dialog width. An empty `QLabel` in the label column
    makes the row wrap like every labelled one (verified with a probe: 30 px
    against 15 px for the same one-line hint).
    """
    return QtWidgets.QLabel("")


_ACTION_ROW_SPACING_PX = 8


def configure_button_row(
    layout: QtWidgets.QHBoxLayout,
    *,
    spacing: int = _ACTION_ROW_SPACING_PX,
) -> None:
    """Give an action row explicit spacing instead of the platform default.

    A free function for the same reason as `compact_table_row_height` above:
    the benchmark results window builds an action row and is not a
    `SettingsDialog`.
    """
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)


_INLINE_FIELD_BUTTON_SPACING_PX = 6


_REMOTE_MODEL_DEFAULTS: dict[str, str] = {
    "groq": DEFAULT_GROQ_MODEL,
    "openai": DEFAULT_OPENAI_MODEL,
    "deepgram": DEFAULT_DEEPGRAM_MODEL,
    "assemblyai": DEFAULT_ASSEMBLYAI_MODEL,
    "elevenlabs": DEFAULT_ELEVENLABS_MODEL,
    "azure": DEFAULT_AZURE_SPEECH_MODEL,
    "funasr": DEFAULT_FUNASR_MODEL,
    "speechmatics": DEFAULT_SPEECHMATICS_MODEL,
    "mistral": DEFAULT_MISTRAL_MODEL,
    "meta": DEFAULT_META_MODEL,
    # Free text, but with a default so that the engine works before the first
    # Refresh.
    "openrouter": DEFAULT_OPENROUTER_MODEL,
    # Free text: there is no default model for an endpoint the app does not
    # know.
    "custom": "",
}


def remote_model_values(settings: object) -> dict[str, str]:
    """Each remote engine's stored model, keyed by engine.

    Read through `settings_store._REMOTE_MODEL_FIELDS`, the one map of which
    field holds which engine's model, so a new engine needs no entry here.
    """
    return {
        engine: str(
            getattr(settings, field, "") or _REMOTE_MODEL_DEFAULTS.get(engine, "")
        )
        for engine, field in _REMOTE_MODEL_FIELDS.items()
    }


@dataclass(frozen=True)
class _RemoteProviderInfo:
    """One row in the canonical remote-provider table.

    ``title`` is the short name of the provider's row on the Providers tab;
    ``label`` is the longer display label used in status/import text (falls
    back to ``name`` elsewhere via :func:`_remote_provider_label`).
    """

    name: str
    title: str
    label: str


# Single source of truth for the remote providers and their UI order.
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
    _RemoteProviderInfo("speechmatics", "Speechmatics", "Speechmatics"),
    _RemoteProviderInfo("mistral", "Mistral", "Mistral (Voxtral)"),
    _RemoteProviderInfo("meta", "Meta", "Meta (Muse Voice)"),
    _RemoteProviderInfo("openrouter", "OpenRouter", "OpenRouter"),
    _RemoteProviderInfo("custom", "Custom", "Custom endpoint"),
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


BENCHMARK_STATUS_LABELS = {
    "running": "Running",
    "completed": "Completed",
    "completed_with_errors": "Completed with errors",
    "canceled": "Canceled",
    "failed": "Failed",
}


def _benchmark_status_text(status: str) -> str:
    return BENCHMARK_STATUS_LABELS.get(
        str(status or "").strip().lower(), str(status or "")
    )


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
        token.strip().upper() for token in str(value or "").split("+") if token.strip()
    }
