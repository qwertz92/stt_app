from __future__ import annotations

import re
from typing import NamedTuple

# Global configuration values. Keep defaults and tunables centralized here.

APP_NAME = "stt_app"
LEGACY_APP_NAME = "tts_app"
APP_DISPLAY_NAME = "Voice Dictation App"
APP_LOGGER_NAME = "stt_app"
# Explicit Windows AppUserModelID. Without one, Windows groups our windows
# under the host process (python.exe / pythonw.exe) and shows its generic icon
# on the taskbar (e.g. for the Settings dialog). Setting an explicit, stable ID
# makes the taskbar button use the app/window icon instead.
APP_USER_MODEL_ID = "Farfeleder.VoiceDictationApp"
# How long a quit may take before the process is ended anyway, with every
# thread's stack written to the log. A normal quit takes about 0.4 s with a
# loaded WebGPU runtime (measured 2026-10-03), and the slowest bounded step,
# killing a Node runtime that ignores its shutdown command, about 4 s.
QUIT_WATCHDOG_TIMEOUT_S = 15.0

SCHEMA_VERSION = 25

# Hotkeys: RegisterHotKey requires at least one non-modifier key.
# Original default that worked reliably in this project.
DEFAULT_HOTKEY = "Ctrl+Alt+Space"
# Tried in order when the preferred hotkey is already owned by another process.
# Every entry must end in a NON-modifier key: RegisterHotKey matches the
# modifier state exactly, so a combination whose key is itself a modifier (the
# old "Ctrl+Win+LShift") registers successfully and can then never fire.
# Ctrl+Win+Space is deliberately absent — Windows owns it for input-language
# switching.
#
# RegisterHotKey takes a combination *globally*: the foreground app never
# sees it again. An automatic fallback the user never chose must therefore
# not be something another program needs. Ruled out for that reason:
#   Ctrl+Shift+Space  parameter hints in VS Code / Visual Studio,
#                     smart-complete in the JetBrains IDEs
#   Ctrl+Alt+D        a bound command in Visual Studio
#   Ctrl+Alt+F8       "Quick Evaluate Expression" in the JetBrains IDEs
#   Ctrl+Alt+F9       "Calculate all worksheets in all open workbooks" in
#                     Microsoft Excel. In-app rather than a global grab --
#                     which is exactly why taking it globally is worse: Excel
#                     would never receive the key again on that machine.
#   Ctrl+Alt+F1/F6/F8/F11/F12  the legacy Intel Graphics Control Panel could
#                     register these system-wide, when it is installed and
#                     its hotkeys are enabled -- often neither is true, and
#                     the newer Command Center dropped the feature. Note
#                     DEFAULT_CANCEL_HOTKEY and DEFAULT_SHOW_OVERLAY_HOTKEY
#                     are in that range: they are user-visible defaults the
#                     user can change and sees fail loudly, which is a very
#                     different thing from a silent automatic substitution.
#   Ctrl+Win+Space    Windows owns it for input-language switching
# That rules out the whole Ctrl+Alt+F-key row. Ctrl+Win with a function key
# is the space that is actually free: editors bind Ctrl+Alt and Ctrl+Shift
# heavily and the Win modifier hardly at all, Windows itself uses only
# Win+Ctrl+F4 (close virtual desktop), and a survey of PowerToys, Windows
# Terminal, Teams, Zoom, Discord, the GPU vendor tools and the major
# peripheral suites found no default on Win+Ctrl+F6..F9. All four were also
# verified free on a normal desktop.
#
# F10/F11/F12 are left out as a precaution only: the app's own defaults sit
# on Ctrl+Alt+F11 and Ctrl+Alt+F12, and keeping the recording fallback out
# of that number range avoids confusing a user who reads both lists. It is
# not a technical collision -- Ctrl+Win+F11 and Ctrl+Alt+F11 are different
# hotkeys, and all of Ctrl+Win+F10/F11/F12 measured free. The real guard is
# in `_register_hotkey_with_fallback`, which skips any fallback equal to the
# user's configured cancel, overlay or re-paste hotkey.
FALLBACK_HOTKEYS = (
    "Ctrl+Win+F9",
    "Ctrl+Win+F8",
    "Ctrl+Win+F7",
    "Ctrl+Win+F6",
)
FALLBACK_HOTKEY = FALLBACK_HOTKEYS[0]
# How often to try to reclaim the preferred hotkey while running on a fallback.
# The usual cause is another app that grabbed it first (a terminal, an IDE);
# when that app closes the preferred combination should come back on its own.
HOTKEY_RECLAIM_INTERVAL_MS = 30_000
DEFAULT_HOTKEY_ID = 1
DEFAULT_CANCEL_HOTKEY = "Ctrl+Alt+F12"
DEFAULT_CANCEL_HOTKEY_ID = 2
# Hotkey that only brings the overlay to the front (same action as the tray's
# "Show overlay"). Preset for an out-of-the-box experience but optional:
# clearing the field stores an empty string, which disables the hotkey.
DEFAULT_SHOW_OVERLAY_HOTKEY = "Ctrl+Alt+F11"
DEFAULT_SHOW_OVERLAY_HOTKEY_ID = 3
# Optional hotkey that pastes the last transcript again into the currently
# focused window. Empty string = disabled; no default combo is preset because
# an accidental global paste shortcut is riskier than an overlay reveal.
DEFAULT_REPASTE_HOTKEY = ""
DEFAULT_REPASTE_HOTKEY_ID = 4

# NVIDIA NeMo models served by the pure-Python `onnx-asr` runtime; the rest of
# their wiring is further down. Declared here because the default names one.
PARAKEET_MODEL_SIZE = "parakeet-tdt-0.6b-v3"
# Moondream's post-trained Parakeet TDT 0.6B v3 ("Parakeet Ultra"): the same
# architecture and the same onnx-asr model type, other weights. See
# `docs/models.md` for its source, licence and the measurements.
PARAKEET_ULTRA_MODEL_SIZE = "parakeet-tdt-0.6b-v3-ultra"
# Every Parakeet TDT variant: they share the language handling (Auto only, no
# `language=` sent) and the onnx-asr model type.
PARAKEET_MODEL_SIZES = (PARAKEET_MODEL_SIZE, PARAKEET_ULTRA_MODEL_SIZE)
CANARY_MODEL_SIZE = "canary-1b-v2"

# What a fresh install transcribes with. Parakeet, not faster-whisper `small`:
# it is 3.6x faster (measured on a Ryzen 5 7600X, CPU only: mean RTF 0.043
# against 0.154 for `small` on the same 24.3 s recording and the same device,
# from this machine's own benchmark history; the means of two runs each), needs
# neither a GPU nor Node.js, detects its language itself across the 25
# European locales its model card lists, and is a 670 MB download
# against `small`'s 486 MB. Someone trying the app without opening Settings
# gets fast, accurate dictation out of the box instead of the slowest sensible
# Whisper size.
#
# The one thing it costs is streaming: Parakeet is batch-only, and `DEFAULT_MODE`
# is `batch`, so the out-of-the-box combination is consistent -- picking
# streaming in Settings names the model in the disabled entry's tooltip.
# Changing this does not touch an existing install: `SettingsStore.load` only
# falls back to the default when the key is absent.
DEFAULT_MODEL_SIZE = PARAKEET_MODEL_SIZE
# What `LocalFasterWhisperTranscriber` loads when its caller names no model.
# Separate from `DEFAULT_MODEL_SIZE` since that one is no longer a
# faster-whisper size, and passing it would build a model that cannot load.
DEFAULT_FASTER_WHISPER_MODEL_SIZE = "small"
DEFAULT_LANGUAGE_MODE = "auto"
DEFAULT_ENGINE = "local"
DEFAULT_MODE = "batch"
DEFAULT_STREAMING_FULL_FINAL_TRANSCRIPT = False
# What happens to an in-flight transcription when a new recording starts while it
# is still running. A finished transcription is never discarded:
#   "insert"  -> keep running; insert its result into the window that was focused
#               when it was recorded, and save it to history (default).
#   "history" -> keep running; save its result to history only (do not insert).
#   "cancel"  -> request a real stop (local compute is aborted, a not-yet-started
#               remote upload never starts); if it still finishes, save to history.
CONCURRENT_TRANSCRIPTION_MODE_INSERT = "insert"
CONCURRENT_TRANSCRIPTION_MODE_HISTORY = "history"
CONCURRENT_TRANSCRIPTION_MODE_CANCEL = "cancel"
VALID_CONCURRENT_TRANSCRIPTION_MODES = (
    CONCURRENT_TRANSCRIPTION_MODE_INSERT,
    CONCURRENT_TRANSCRIPTION_MODE_HISTORY,
    CONCURRENT_TRANSCRIPTION_MODE_CANCEL,
)
DEFAULT_CONCURRENT_TRANSCRIPTION_MODE = CONCURRENT_TRANSCRIPTION_MODE_INSERT
# When True, a finished queued transcription is inserted into its captured
# window as soon as it completes, even while another transcription is still
# running. An active recording (or an in-progress start/stop) always blocks
# insertion. When False, queued results are inserted only once no
# transcription is running (the pre-existing behavior).
DEFAULT_IMMEDIATE_BACKGROUND_INSERT = False
# Where a finished transcript is inserted:
#   "recording_window" -> the window/control that was focused when its
#                         recording started (default; a queued result follows
#                         its own recording even after the user moved on).
#   "current_window"   -> whatever window/control is focused at the moment the
#                         transcript is ready to insert.
# The caret position inside the target control is always the position at
# insert time; Windows offers no way to paste at a remembered caret offset.
INSERT_TARGET_RECORDING_WINDOW = "recording_window"
INSERT_TARGET_CURRENT_WINDOW = "current_window"
VALID_INSERT_TARGETS = (
    INSERT_TARGET_RECORDING_WINDOW,
    INSERT_TARGET_CURRENT_WINDOW,
)
DEFAULT_INSERT_TARGET = INSERT_TARGET_RECORDING_WINDOW
DEFAULT_VAD_ENABLED = False
# Keep one PortAudio input stream open so a recording starts instantly even on
# machines where opening the microphone takes seconds (EDR/GPO-hooked audio
# stacks). Opt-in because the microphone then stays open all the time and
# Windows shows the microphone-in-use indicator permanently.
DEFAULT_KEEP_MICROPHONE_WARM = False
DEFAULT_SAVE_LAST_WAV = False
DEFAULT_SAVE_ALL_RECORDINGS = False
DEFAULT_RECORDINGS_DIR = ""
DEFAULT_RECORDINGS_MAX_COUNT = 10
# 0 turns pruning off entirely: every archived recording is kept.
RECORDINGS_MAX_COUNT_UNLIMITED = 0
# Upper bound of the retention spin box; past this "keep unlimited" is the
# honest setting rather than a number nobody counts to.
RECORDINGS_MAX_COUNT_CEILING = 100_000
DEFAULT_HISTORY_MAX_ITEMS = 500
HISTORY_MAX_ITEMS_MAX = 5_000
DISPLAY_TIMEZONE_LOCAL = "local"
DISPLAY_TIMEZONE_UTC = "utc"
VALID_DISPLAY_TIMEZONES = (DISPLAY_TIMEZONE_LOCAL, DISPLAY_TIMEZONE_UTC)
DEFAULT_DISPLAY_TIMEZONE = DISPLAY_TIMEZONE_LOCAL
DEFAULT_PASTE_MODE = "auto"
DEFAULT_KEEP_TRANSCRIPT_IN_CLIPBOARD = False
DEFAULT_ALLOW_INSECURE_KEY_STORAGE = False
DEFAULT_OFFLINE_MODE = False
# On by default: the setting only takes effect once a Cohere/Granite model is
# selected, and a user who selects one wants to dictate with it. Without it
# every single dictation pays the full Node + ONNX model load, while
# faster-whisper and Nemotron stay warm. Users who need the RAM/VRAM back can
# turn it off; existing settings files keep whatever they stored.
DEFAULT_KEEP_ONNX_MODEL_LOADED = True
# Execution-device policy for the local ONNX engines. "auto" keeps the existing
# behaviour (GPU first, CPU fallback); the rest let the user pin a device when
# a benchmark shows one is better on their hardware. The per-model CPU
# preference this used to mention went away with Granite 4.1 Plus/NAR, the only
# two models that needed it.
DEFAULT_LOCAL_ONNX_DEVICE = "auto"
DEFAULT_START_BEEP_ENABLED = False
DEFAULT_START_BEEP_TONE = "soft"
# Completion tone after a successful transcript insertion (batch, queued
# background, and re-paste inserts; streaming appends stay silent). Shares the
# start-tone choices; a different default tone keeps start/end distinguishable.
DEFAULT_COMPLETION_BEEP_ENABLED = False
DEFAULT_COMPLETION_BEEP_TONE = "chime"
# Middle-clicking the tray icon toggles dictation (same as the hotkey).
DEFAULT_TRAY_MIDDLE_CLICK_TOGGLE = True
DEFAULT_OVERLAY_ALWAYS_ON_TOP = True
VALID_START_BEEP_TONES = ("soft", "high", "chime", "system")
# User-defined technical terms/names to bias transcription toward. Applies to
# local faster-whisper, OpenAI, Groq, AssemblyAI, and Deepgram; see
# parse_custom_vocabulary() for the raw-text parsing rules.
DEFAULT_CUSTOM_VOCABULARY = ""
CUSTOM_VOCABULARY_MAX_TERMS = 100

# --- Model directory configuration ---
# How faster-whisper resolves models (WhisperModel constructor):
#
#   1. If model_size_or_path is an EXISTING DIRECTORY on disk:
#      -> Uses it directly as the model (must contain: config.json, model.bin,
#         tokenizer.json, and vocabulary.txt or vocabulary.json).
#
#   2. Otherwise, maps the short name (e.g. "small") to a HuggingFace repo ID
#      (e.g. "Systran/faster-whisper-small") and calls
#      huggingface_hub.snapshot_download(repo_id, cache_dir=download_root).
#      The default cache directory is:
#        Windows: %USERPROFILE%\.cache\huggingface\hub\
#        Linux:   ~/.cache/huggingface/hub/
#      Inside that, models are stored in HF's internal structure:
#        models--Systran--faster-whisper-small/
#          refs/main          (text file with commit hash)
#          snapshots/<hash>/  (actual model files)
#          blobs/             (SHA256-named raw files)
#
# DEFAULT_MODEL_DIR controls the 'download_root' parameter of WhisperModel.
# When empty (""), the standard HuggingFace cache is used.
# When set to a path (e.g. "C:\whisper-models"), ALL models are cached there
# in the same HF structure above — each model in its own subfolder.
# This avoids duplicate model copies when running multiple instances.
#
# For fully offline / manual setup, point DEFAULT_MODEL_DIR to a folder
# containing flat model subdirectories:
#   C:\whisper-models\faster-whisper-small\
#     config.json
#     model.bin
#     tokenizer.json
#     vocabulary.txt
# Then use the download script, which handles the layout itself:
#   python scripts/download_model.py            # the default model
#   python scripts/download_model.py --model small
#
# The two layouts are not interchangeable. The faster-whisper models use the
# HuggingFace models--<repo>/snapshots/<id> structure above; the ONNX models,
# the default included, are written into a flat folder named after the
# repository instead, which is why --output-dir is not simply a cache_dir for
# them.
DEFAULT_MODEL_DIR = ""

FASTER_WHISPER_MODEL_SIZES = (
    "tiny",
    "base",
    "small",
    "medium",
    "large-v3",
    "large-v3-turbo",  # Multilingual, ~1.6 GB, pruned large-v3 (4 decoder layers)
    "distil-large-v3.5",  # English-only, ~1.5 GB, improved v3 (98k h training data)
)

LOCAL_WEBGPU_MODEL_SIZES = (
    "cohere-transcribe-03-2026",
    "granite-4.0-1b-speech",
    "granite-speech-4.1-2b",
)

NEMOTRON_MODEL_SIZE = "nemotron-3.5-asr-streaming-0.6b-int4"
LOCAL_NEMOTRON_MODEL_SIZES = (NEMOTRON_MODEL_SIZE,)

# NVIDIA NeMo models served by the pure-Python `onnx-asr` runtime. They need no
# Node.js and no new ONNX Runtime: onnx-asr resolves the same `onnxruntime`
# distribution the app already carries for Nemotron. The ids are declared
# further up, next to `DEFAULT_MODEL_SIZE`, which names one of them.
LOCAL_ONNX_ASR_MODEL_SIZES = (
    PARAKEET_MODEL_SIZE,
    PARAKEET_ULTRA_MODEL_SIZE,
    CANARY_MODEL_SIZE,
)

# IBM Granite Speech 5.0 470M TurboCTC, a community INT8 ONNX export of IBM's
# CTC encoder. It runs on the `onnxruntime` CPU provider the app already ships,
# with numpy for the log-mel features and `tokenizers` (a faster-whisper
# dependency) for the byte-level BPE decode -- no Node.js, no onnx-asr, no new
# dependency. English only, batch only, and it writes lower-case text without
# punctuation, which is what the model was trained to produce.
GRANITE_CTC_MODEL_SIZE = "granite-speech-5.0-470m-turboctc"
LOCAL_GRANITE_CTC_MODEL_SIZES = (GRANITE_CTC_MODEL_SIZE,)

LOCAL_ONNX_MODEL_SIZES = (
    LOCAL_WEBGPU_MODEL_SIZES
    + LOCAL_NEMOTRON_MODEL_SIZES
    + LOCAL_ONNX_ASR_MODEL_SIZES
    + LOCAL_GRANITE_CTC_MODEL_SIZES
)

# The local models whose runtime takes an execution device. The onnx-asr models
# (Parakeet/Canary) and Granite Speech 5.0 TurboCTC are CPU-only and ignore the
# policy, so the Settings picker must not claim to control them and a measured
# preference for one would be meaningless. One definition, because the picker,
# the stored preference map and the benchmark all have to answer this question
# the same way.
DEVICE_AWARE_LOCAL_MODELS = LOCAL_WEBGPU_MODEL_SIZES + LOCAL_NEMOTRON_MODEL_SIZES

# Models whose upstream repo has no ModelScope counterpart (the first three
# verified against the ModelScope API on 2026-08-18, Granite Speech 5.0
# TurboCTC on 2026-09-19). On a network that blocks Hugging Face wholesale
# -- a proxy denying the whole "Generative AI and ML Applications" category is
# the common case -- these cannot be fetched at all. Naming them up front beats
# a download that ends in "check your internet connection", which is exactly the
# one thing that is not wrong. Parakeet Ultra is here by design and was not
# probed: it is fetched at one pinned commit (its layout's `revision`), and a
# mirror serves a repository by name at whatever it holds now, so the download
# never falls back to one.
MODELS_WITHOUT_MODELSCOPE_MIRROR = frozenset(
    {
        "distil-large-v3.5",
        PARAKEET_MODEL_SIZE,
        PARAKEET_ULTRA_MODEL_SIZE,
        CANARY_MODEL_SIZE,
        GRANITE_CTC_MODEL_SIZE,
    }
)

LOCAL_ONNX_MODEL_PRECISION: dict[str, str] = {
    "cohere-transcribe-03-2026": "q4",
    "granite-4.0-1b-speech": "q4",
    "granite-speech-4.1-2b": "q4",
    NEMOTRON_MODEL_SIZE: "int4",
    PARAKEET_MODEL_SIZE: "int8",
    PARAKEET_ULTRA_MODEL_SIZE: "int8",
    CANARY_MODEL_SIZE: "int8",
    GRANITE_CTC_MODEL_SIZE: "int8",
}

LOCAL_ONNX_MODEL_RUNTIME_LABELS: dict[str, str] = {
    "cohere-transcribe-03-2026": "ONNX/WebGPU q4",
    "granite-4.0-1b-speech": "ONNX/WebGPU q4",
    "granite-speech-4.1-2b": "ONNX/WebGPU q4",
    NEMOTRON_MODEL_SIZE: "ORT GenAI INT4, 560 ms streaming",
    PARAKEET_MODEL_SIZE: "onnx-asr INT8 TDT, CPU",
    PARAKEET_ULTRA_MODEL_SIZE: "onnx-asr INT8 TDT, CPU",
    CANARY_MODEL_SIZE: "onnx-asr INT8 AED, CPU",
    GRANITE_CTC_MODEL_SIZE: "ONNX Runtime INT8 CTC, CPU",
}

GRANITE_4_1_REPO_MAP: dict[str, str] = {
    "granite-speech-4.1-2b": "onnx-community/granite-speech-4.1-2b-ONNX",
}

LOCAL_WEBGPU_DEVICE_POLICIES = ("auto", "gpu", "cpu", "dml", "webgpu")

# Devices a finished benchmark case can report as its resolved runtime device,
# and therefore the only values a measured preference may hold.
ONNX_MEASURABLE_DEVICES = ("webgpu", "dml", "cpu")

# What `auto` means for the Cohere/Granite Node runtime on Windows. The runner
# computes its own list per platform; this is the Python side's description of
# it, used to decide whether a measured device says anything new.
LOCAL_WEBGPU_AUTO_DEVICE_ORDER = ("webgpu", "dml", "cpu")

# A measured device replaces the default first device only when it was at least
# this much faster. In the six benchmark runs recorded on the development
# machine up to 2026-09-18, the runs of one case differed by a median of 4%
# (31 cases; up to 36% where the first run carried the warm-up), so a gap of a
# few percent says nothing about which device is actually quicker, while a
# reorder costs a model reload. 10% is a design value above that noise, not a
# measured optimum.
MEASURED_DEVICE_MIN_GAIN = 0.10

# Nemotron runs on ONNX Runtime GenAI, which has DirectML and CPU but no WebGPU
# provider, so every GPU-flavoured policy maps onto DirectML for it. Shared by
# the factory and the benchmark so the two cannot disagree about what a policy
# means for this engine.
NEMOTRON_DEVICE_PROVIDER_ORDER: dict[str, tuple[str, ...]] = {
    "auto": ("dml", "cpu"),
    "gpu": ("dml",),
    "dml": ("dml",),
    "webgpu": ("dml",),
    "cpu": ("cpu",),
}


def onnx_auto_device_order(model_size: str) -> tuple[str, ...]:
    """What the `auto` policy tries for ``model_size``, before any preference.

    Empty for a model whose runtime takes no device at all. The two
    device-aware runtimes have different chains -- ORT GenAI has no WebGPU
    provider -- and the factory, the benchmark's winner rule and the note under
    the picker all have to read the same one.
    """
    if model_size in LOCAL_NEMOTRON_MODEL_SIZES:
        return NEMOTRON_DEVICE_PROVIDER_ORDER[DEFAULT_LOCAL_ONNX_DEVICE]
    if model_size in DEVICE_AWARE_LOCAL_MODELS:
        return LOCAL_WEBGPU_AUTO_DEVICE_ORDER
    return ()


def order_with_preferred_device(
    default_order: tuple[str, ...] | list[str],
    preferred: str | None,
) -> tuple[str, ...]:
    """``default_order`` with ``preferred`` moved to the front.

    Unchanged when the preference is empty or names a device this order does
    not contain. Every other device keeps its relative place, so the fallback
    chain after the preferred one is still the normal one.
    """
    order = tuple(default_order)
    device = str(preferred or "").strip().lower()
    if device not in order:
        return order
    return (device, *(item for item in order if item != device))


def effective_preferred_device(
    policy: str,
    preferred: str | None,
    default_order: tuple[str, ...] | list[str],
) -> str:
    """The device ``auto`` should try first, or ``""`` when it changes nothing.

    Empty for a pinned policy (an explicit choice always wins over a
    measurement), for a device this order cannot reach, and for one that
    already leads the order -- there is then nothing to reorder and nothing to
    tell the user, so the command line and the note stay as they were.
    """
    if str(policy or "").strip().lower() != DEFAULT_LOCAL_ONNX_DEVICE:
        return ""
    order = tuple(default_order)
    device = str(preferred or "").strip().lower()
    if device not in order or order[0] == device:
        return ""
    return device


def nemotron_provider_order(
    device_policy: str,
    preferred_device: str = "",
) -> tuple[str, ...]:
    """Provider order for a device policy, defaulting to the auto behaviour.

    ``preferred_device`` is a device a benchmark measured as fastest for this
    model; it only ever reorders the `auto` chain, because
    ``effective_preferred_device`` answers "" for every pinned policy.
    """
    order = NEMOTRON_DEVICE_PROVIDER_ORDER.get(
        str(device_policy or "").strip().lower(), ("dml", "cpu")
    )
    return order_with_preferred_device(
        order,
        effective_preferred_device(device_policy, preferred_device, order),
    )


LOCAL_WEBGPU_BENCHMARK_DEVICE_GROUPS: dict[str, tuple[str, ...]] = {
    "auto": ("auto",),
    "gpu": ("gpu",),
    "cpu": ("cpu",),
    "gpu,cpu": ("gpu", "cpu"),
    "dml": ("dml",),
    "webgpu": ("webgpu",),
    "all": ("webgpu", "dml", "cpu"),
}

VALID_MODEL_SIZES = FASTER_WHISPER_MODEL_SIZES + LOCAL_ONNX_MODEL_SIZES

# Short model name → HuggingFace repo ID.
# Single source of truth used by local transcribers, download script, and settings.
MODEL_REPO_MAP: dict[str, str] = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "distil-large-v3.5": "distil-whisper/distil-large-v3.5-ct2",
    "cohere-transcribe-03-2026": "onnx-community/cohere-transcribe-03-2026-ONNX",
    "granite-4.0-1b-speech": "onnx-community/granite-4.0-1b-speech-ONNX",
    NEMOTRON_MODEL_SIZE: ("onnx-community/nemotron-3.5-asr-streaming-0.6b-onnx-int4"),
    PARAKEET_MODEL_SIZE: "istupakov/parakeet-tdt-0.6b-v3-onnx",
    # An ONNX export of Moondream's `moondream/parakeet-ultra` by Olicorne,
    # fetched at one pinned revision (see its layout in `local_webgpu_asr`).
    PARAKEET_ULTRA_MODEL_SIZE: "Olicorne/parakeet-tdt-0.6b-v3-ultra-onnx",
    CANARY_MODEL_SIZE: "istupakov/canary-1b-v2-onnx",
    GRANITE_CTC_MODEL_SIZE: "qwertz92/granite-speech-5.0-470m-turboctc-onnx",
    **GRANITE_4_1_REPO_MAP,
}

LOCAL_MODEL_RUNTIME: dict[str, str] = {
    **dict.fromkeys(FASTER_WHISPER_MODEL_SIZES, "faster-whisper"),
    **dict.fromkeys(LOCAL_WEBGPU_MODEL_SIZES, "onnx-webgpu"),
    **dict.fromkeys(LOCAL_NEMOTRON_MODEL_SIZES, "onnxruntime-genai"),
    **dict.fromkeys(LOCAL_ONNX_ASR_MODEL_SIZES, "onnx-asr"),
    **dict.fromkeys(LOCAL_GRANITE_CTC_MODEL_SIZES, "granite-ctc"),
}

# Approximate model sizes for UI progress estimation.
# Values are decimal megabytes (MB), not MiB.
# Measured against the actual repositories with the download allow-patterns,
# not copied from a model card: `distil-large-v3.5` was listed at 756 MB while
# its CTranslate2 `model.bin` is 1513 MB, so the download bar reached "approx.
# 100%" at half the transfer and then kept counting.
MODEL_ESTIMATED_SIZE_MB: dict[str, int] = {
    "tiny": 78,
    "base": 148,
    "small": 486,
    "medium": 1_531,
    "large-v3": 3_091,
    "large-v3-turbo": 1_622,
    "distil-large-v3.5": 1_516,
    # Selectable local ONNX downloads. Cohere, Granite 4.0, and Granite 4.1 2B
    # are q4 Transformers.js packages.
    "cohere-transcribe-03-2026": 2_128,
    "granite-4.0-1b-speech": 1_843,
    "granite-speech-4.1-2b": 1_843,
    NEMOTRON_MODEL_SIZE: 793,
    # Measured from the int8 downloads: 670.48 MB and 1029.33 MB.
    PARAKEET_MODEL_SIZE: 670,
    # The four files the pinned revision's layout fetches: 667,821,528 bytes
    # (encoder 649,524,002, decoder/joint 18,203,490, vocab 93,939, config 97).
    PARAKEET_ULTRA_MODEL_SIZE: 668,
    CANARY_MODEL_SIZE: 1_029,
    # Measured against the repository with this model's allow-patterns applied:
    # 552,442,697 bytes, of which `onnx/model_int8.onnx` is 551,294,349. The
    # fp32 and fp16 graphs beside it (1.76 GiB and 0.88 GiB) are never fetched.
    GRANITE_CTC_MODEL_SIZE: 552,
}

LANGUAGE_MODE_LABELS: dict[str, str] = {
    "auto": "Auto",
    "de": "German",
    "en": "English",
    "af": "Afrikaans",
    "am": "Amharic",
    "ar": "Arabic",
    "as": "Assamese",
    "ast": "Asturian",
    "hy": "Armenian",
    "az": "Azerbaijani",
    "ba": "Bashkir",
    "be": "Belarusian",
    "bn": "Bengali",
    "bo": "Tibetan",
    "br": "Breton",
    "bs": "Bosnian",
    "bg": "Bulgarian",
    "ca": "Catalan",
    "yue": "Cantonese",
    "ceb": "Cebuano",
    "ny": "Chichewa",
    "zh": "Chinese",
    "hr": "Croatian",
    "cs": "Czech",
    "da": "Danish",
    "nl": "Dutch",
    "et": "Estonian",
    "eu": "Basque",
    "fi": "Finnish",
    "fo": "Faroese",
    "fr": "French",
    "ff": "Fulah",
    "lg": "Ganda",
    "gl": "Galician",
    "gu": "Gujarati",
    "el": "Greek",
    "he": "Hebrew",
    "ha": "Hausa",
    "haw": "Hawaiian",
    "hi": "Hindi",
    "ht": "Haitian Creole",
    "hu": "Hungarian",
    "is": "Icelandic",
    "id": "Indonesian",
    "ig": "Igbo",
    "ga": "Irish",
    "it": "Italian",
    "ja": "Japanese",
    "jw": "Javanese",
    "ka": "Georgian",
    "kn": "Kannada",
    "kk": "Kazakh",
    "kea": "Kabuverdianu",
    "km": "Khmer",
    "ko": "Korean",
    "ku": "Kurdish",
    "ky": "Kyrgyz",
    "la": "Latin",
    "lb": "Luxembourgish",
    "ln": "Lingala",
    "lo": "Lao",
    "luo": "Luo",
    "lv": "Latvian",
    "lt": "Lithuanian",
    "mk": "Macedonian",
    "ms": "Malay",
    "mg": "Malagasy",
    "ml": "Malayalam",
    "mn": "Mongolian",
    "mr": "Marathi",
    "mi": "Maori",
    "mt": "Maltese",
    "my": "Myanmar",
    "ne": "Nepali",
    "nso": "Northern Sotho",
    "nn": "Nynorsk",
    "no": "Norwegian",
    "oc": "Occitan",
    "or": "Odia",
    "pa": "Punjabi",
    "fa": "Persian",
    "pl": "Polish",
    "ps": "Pashto",
    "pt": "Portuguese",
    "ro": "Romanian",
    "ru": "Russian",
    "sa": "Sanskrit",
    "sd": "Sindhi",
    "sr": "Serbian",
    "si": "Sinhala",
    "sk": "Slovak",
    "sl": "Slovenian",
    "sn": "Shona",
    "so": "Somali",
    "sq": "Albanian",
    "es": "Spanish",
    "su": "Sundanese",
    "sw": "Swahili",
    "sv": "Swedish",
    "tl": "Tagalog",
    "ta": "Tamil",
    "te": "Telugu",
    "tg": "Tajik",
    "th": "Thai",
    "tk": "Turkmen",
    "tr": "Turkish",
    "tt": "Tatar",
    "uk": "Ukrainian",
    "umb": "Umbundu",
    "ur": "Urdu",
    "uz": "Uzbek",
    "vi": "Vietnamese",
    "cy": "Welsh",
    "wo": "Wolof",
    "xh": "Xhosa",
    "yi": "Yiddish",
    "yo": "Yoruba",
    "zu": "Zulu",
}
VALID_LANGUAGE_MODES = tuple(LANGUAGE_MODE_LABELS)
_NON_WHISPER_LANGUAGE_MODES = frozenset(
    {
        "ast",
        "yue",
        "ceb",
        "ny",
        "ff",
        "lg",
        "ig",
        "ga",
        "kea",
        "ku",
        "ky",
        "luo",
        "nso",
        "or",
        "umb",
        "wo",
        "xh",
        "zu",
    }
)
WHISPER_LANGUAGE_MODES = tuple(
    value for value in VALID_LANGUAGE_MODES if value not in _NON_WHISPER_LANGUAGE_MODES
)
# Shared by all four OpenAI models, `gpt-transcribe` included. Its own guide
# enumerates no list for it -- only the code *formats* it accepts ("ISO 639-1
# codes, such as en, es, and fr", selected ISO 639-3 codes, and regional zh
# locales) plus "The API rejects unsupported or incorrectly formatted language
# codes" -- and points at the Whisper language list for `whisper-1`, which is
# where this one comes from. Narrowing it for one model would need a source
# that does not exist; a code the newer model refuses surfaces as the API's
# own error rather than as a wrong transcript.
OPENAI_LANGUAGE_MODES = (
    "auto",
    "de",
    "en",
    "af",
    "ar",
    "hy",
    "az",
    "be",
    "bs",
    "bg",
    "ca",
    "zh",
    "hr",
    "cs",
    "da",
    "nl",
    "et",
    "fi",
    "fr",
    "gl",
    "el",
    "he",
    "hi",
    "hu",
    "is",
    "id",
    "it",
    "ja",
    "kn",
    "kk",
    "ko",
    "lv",
    "lt",
    "mk",
    "ms",
    "mr",
    "mi",
    "ne",
    "no",
    "fa",
    "pl",
    "pt",
    "ro",
    "ru",
    "sr",
    "sk",
    "sl",
    "es",
    "sw",
    "sv",
    "tl",
    "ta",
    "th",
    "tr",
    "uk",
    "ur",
    "vi",
    "cy",
)
ELEVENLABS_LANGUAGE_MODES = (
    "auto",
    "de",
    "en",
    "af",
    "am",
    "ar",
    "hy",
    "as",
    "ast",
    "az",
    "be",
    "bn",
    "bs",
    "bg",
    "my",
    "yue",
    "ca",
    "ceb",
    "ny",
    "hr",
    "cs",
    "da",
    "nl",
    "et",
    "tl",
    "fi",
    "fr",
    "ff",
    "gl",
    "lg",
    "ka",
    "el",
    "gu",
    "ha",
    "he",
    "hi",
    "hu",
    "is",
    "ig",
    "id",
    "ga",
    "it",
    "ja",
    "jw",
    "kea",
    "kn",
    "kk",
    "km",
    "ko",
    "ku",
    "ky",
    "lo",
    "lv",
    "ln",
    "lt",
    "luo",
    "lb",
    "mk",
    "ms",
    "ml",
    "mt",
    "zh",
    "mi",
    "mr",
    "mn",
    "ne",
    "nso",
    "no",
    "oc",
    "or",
    "ps",
    "fa",
    "pl",
    "pt",
    "pa",
    "ro",
    "ru",
    "sr",
    "sn",
    "sd",
    "sk",
    "sl",
    "so",
    "es",
    "sw",
    "sv",
    "ta",
    "tg",
    "te",
    "th",
    "tr",
    "uk",
    "umb",
    "ur",
    "uz",
    "vi",
    "cy",
    "wo",
    "xh",
    "yo",
    "zu",
)

COHERE_LANGUAGE_MODES = (
    "de",
    "en",
    "fr",
    "it",
    "es",
    "pt",
    "el",
    "nl",
    "pl",
    "ar",
    "vi",
    "zh",
    "ja",
    "ko",
)
# Parakeet TDT v3 is implicitly multilingual: onnx-asr accepts a `language`
# argument but the model ignores it (verified: "de" and a bogus code produce
# byte-identical output), so Auto is the only honest choice. Parakeet Ultra is
# the same architecture behind the same onnx-asr model type and gets the same.
PARAKEET_LANGUAGE_MODES = ("auto",)
# Canary must NEVER offer Auto. onnx-asr hardcodes the <|en|> source/target
# token, so without an explicit language it silently *translates* German into
# English rather than transcribing it. The 25 trained locales only; the vocab
# carries ~180 ISO codes and an untrained one raises KeyError.
CANARY_LANGUAGE_MODES = (
    "de",
    "en",
    "bg",
    "cs",
    "da",
    "el",
    "es",
    "et",
    "fi",
    "fr",
    "hr",
    "hu",
    "it",
    "lt",
    "lv",
    "mt",
    "nl",
    "pl",
    "pt",
    "ro",
    "ru",
    "sk",
    "sl",
    "sv",
    "uk",
)
GRANITE_LANGUAGE_MODES = ("auto", "de", "en", "fr", "es", "pt", "ja")
# Bare app language codes for Nemotron's transcription-ready and broad-coverage
# locales. "no" maps to the official Norwegian Bokmal prompt ID.
NEMOTRON_LANGUAGE_IDS: dict[str, int] = {
    "auto": 101,
    "de": 9,
    "en": 0,
    "es": 3,
    "fr": 8,
    "it": 15,
    "pt": 13,
    "nl": 16,
    "tr": 18,
    "ru": 11,
    "ar": 7,
    "hi": 6,
    "ja": 10,
    "ko": 14,
    "uk": 19,
    "pl": 17,
    "sv": 24,
    "cs": 22,
    "no": 103,
    "da": 25,
    "bg": 30,
    "fi": 26,
    "hr": 29,
    "sk": 28,
    "zh": 4,
    "hu": 23,
    "ro": 20,
    "vi": 33,
    "et": 60,
}
NEMOTRON_LANGUAGE_MODES = tuple(NEMOTRON_LANGUAGE_IDS)
ASSEMBLYAI_UNIVERSAL_3_5_LANGUAGE_MODES = (
    "auto",
    "de",
    "en",
    "ar",
    "da",
    "nl",
    "fi",
    "fr",
    "he",
    "hi",
    "it",
    "ja",
    "no",
    "pt",
    "es",
    "sv",
    "tr",
    "vi",
    "zh",
)
DEEPGRAM_NOVA_3_LANGUAGE_MODES = (
    "auto",
    "de",
    "en",
    "ar",
    "be",
    "bn",
    "bs",
    "bg",
    "ca",
    "zh",
    "hr",
    "cs",
    "da",
    "nl",
    "et",
    "fi",
    "fr",
    "el",
    "gu",
    "he",
    "hi",
    "hu",
    "id",
    "it",
    "ja",
    "kn",
    "ko",
    "lv",
    "lt",
    "mk",
    "ms",
    "mr",
    "no",
    "fa",
    "pl",
    "pt",
    "ro",
    "ru",
    "sr",
    "sk",
    "sl",
    "es",
    "sv",
    "tl",
    "ta",
    "te",
    "th",
    "tr",
    "uk",
    "ur",
    "vi",
)
DEEPGRAM_NOVA_2_LANGUAGE_MODES = (
    "auto",
    "de",
    "en",
    "bg",
    "ca",
    "zh",
    "cs",
    "da",
    "nl",
    "et",
    "fi",
    "fr",
    "el",
    "hi",
    "hu",
    "id",
    "it",
    "ja",
    "ko",
    "lv",
    "lt",
    "ms",
    "no",
    "pl",
    "pt",
    "ro",
    "ru",
    "sk",
    "es",
    "sv",
    "th",
    "tr",
    "uk",
    "vi",
)
# Azure LLM Speech (MAI-Transcribe). "auto" uses the model's default
# multilingual mode; selecting a language sends a `locales` hint.
# Copied from the language table on Microsoft's MAI-Transcribe page (read
# 2026-09-19, page updated 2026-09-10): 60 languages for MAI-Transcribe-2, 43
# for MAI-Transcribe-1.5. The page no longer lists MAI-Transcribe-1, which it
# marks as deprecated, so that list is the one this app shipped with. The
# codes are the app's: Azure's `nb` and `fil` are `no` and `tl` here
# (`AZURE_LOCALE_OVERRIDES`).
AZURE_MAI_TRANSCRIBE_2_LANGUAGE_MODES = (
    "auto",
    "de",
    "en",
    "af",
    "ar",
    "as",
    "az",
    "bg",
    "bn",
    "bs",
    "ca",
    "cs",
    "da",
    "el",
    "es",
    "et",
    "fa",
    "fi",
    "fr",
    "gl",
    "gu",
    "he",
    "hi",
    "hu",
    "hy",
    "id",
    "is",
    "it",
    "ja",
    "kk",
    "kn",
    "ko",
    "lt",
    "lv",
    "mk",
    "ml",
    "mr",
    "ms",
    "ne",
    "nl",
    "no",
    "or",
    "pa",
    "pl",
    "pt",
    "ro",
    "ru",
    "sk",
    "sl",
    "sv",
    "sw",
    "ta",
    "te",
    "th",
    "tl",
    "tr",
    "uk",
    "ur",
    "vi",
    "yue",
    "zh",
)
AZURE_MAI_TRANSCRIBE_1_5_LANGUAGE_MODES = (
    "auto",
    "de",
    "en",
    "ar",
    "as",
    "bg",
    "bn",
    "ca",
    "cs",
    "da",
    "el",
    "es",
    "et",
    "fi",
    "fr",
    "gu",
    "hi",
    "hu",
    "id",
    "it",
    "ja",
    "kn",
    "ko",
    "lt",
    "ml",
    "mr",
    "nl",
    "no",
    "or",
    "pa",
    "pl",
    "pt",
    "ro",
    "ru",
    "sk",
    "sl",
    "sv",
    "ta",
    "te",
    "th",
    "tr",
    "uk",
    "vi",
    "zh",
)
AZURE_MAI_TRANSCRIBE_1_LANGUAGE_MODES = (
    "auto",
    "de",
    "en",
    "ar",
    "cs",
    "da",
    "es",
    "fi",
    "fr",
    "hi",
    "hu",
    "id",
    "it",
    "ja",
    "ko",
    "nl",
    "no",
    "pl",
    "pt",
    "ro",
    "ru",
    "sv",
    "th",
    "tr",
    "vi",
)
# The engine-level list is the default model's.
AZURE_LANGUAGE_MODES = AZURE_MAI_TRANSCRIBE_2_LANGUAGE_MODES
# App language code -> Azure locale code, where they differ.
AZURE_LOCALE_OVERRIDES: dict[str, str] = {"no": "nb", "tl": "fil"}
# Alibaba Fun-ASR (DashScope Model Studio) covers 31 languages. Notably it does
# NOT document German support; its strength is Chinese (incl. dialects) and
# East/Southeast-Asian languages. "auto" uses multilingual mode; a specific
# language is sent as a language_hints entry.
FUNASR_LANGUAGE_MODES = (
    "auto",
    "en",
    "zh",
    "yue",
    "ja",
    "ko",
    "vi",
    "id",
    "th",
    "ms",
    "tl",
    "ar",
    "hi",
    "bg",
    "hr",
    "cs",
    "da",
    "nl",
    "et",
    "fi",
    "el",
    "hu",
    "ga",
    "lv",
    "lt",
    "mt",
    "pl",
    "pt",
    "ro",
    "sk",
    "sl",
    "sv",
)
# App language code -> Fun-ASR language_hints code, where they differ.
# Most are identical bare codes; this maps only the exceptions.
FUNASR_LANGUAGE_HINTS: dict[str, str] = {}
# Speechmatics batch transcription. The languages page (read 2026-09-27,
# https://docs.speechmatics.com/speech-to-text/languages) lists 62 codes; this
# is every one of them the app has a code for, after `cmn` (Mandarin) is
# mapped to the app's `zh` (`SPEECHMATICS_LANGUAGE_CODES`). Left out: `auto`
# (below), the bilingual and multi-language packs (`ar_en`, `en_ms`, `cmn_en`,
# `cmn_en_ms_ta`, `en_ta`, and `tl`, which is "Tagalog (Filipino) & English
# bilingual"), and `eo`, `ia` and `ug`, which the app has no code for. In
# the order of `VALID_LANGUAGE_MODES`.
SPEECHMATICS_LANGUAGE_MODES = (
    "de",
    "en",
    "ar",
    "ba",
    "be",
    "bn",
    "bg",
    "ca",
    "yue",
    "zh",
    "hr",
    "cs",
    "da",
    "nl",
    "et",
    "eu",
    "fi",
    "fr",
    "gl",
    "el",
    "he",
    "hi",
    "hu",
    "id",
    "ga",
    "it",
    "ja",
    "ko",
    "lv",
    "lt",
    "ms",
    "mn",
    "mr",
    "mt",
    "no",
    "fa",
    "pl",
    "pt",
    "ro",
    "ru",
    "sk",
    "sl",
    "es",
    "sw",
    "sv",
    "ta",
    "th",
    "tr",
    "uk",
    "ur",
    "vi",
    "cy",
)
# Melia 1 "transcribes the individual languages listed here and switches
# between them automatically, without language selection", and "does not
# support the `auto` option" (the same page): the app's Auto is sent as
# `"language": "multi"`, and a chosen language as a `language_hints` entry.
# Enhanced and Standard do take `auto`, but automatic identification needs "at
# least 60 seconds of speech" and rejects the job by default otherwise
# (https://docs.speechmatics.com/speech-to-text/batch/language-identification,
# read 2026-09-27) -- longer than most dictations -- so those two take a chosen
# language only.
SPEECHMATICS_MELIA_LANGUAGE_MODES = ("auto", *SPEECHMATICS_LANGUAGE_MODES)
# App language code -> Speechmatics language code, where they differ.
SPEECHMATICS_LANGUAGE_CODES: dict[str, str] = {"zh": "cmn"}
# Mistral Voxtral Mini Transcribe 2: "English, Chinese, Hindi, Spanish,
# Arabic, French, Portuguese, Russian, German, Japanese, Korean, Italian, and
# Dutch" (https://mistral.ai/news/voxtral-transcribe-2/, 2026-02-04, read
# 2026-09-27). Without `language` the model detects it.
MISTRAL_LANGUAGE_MODES = (
    "auto",
    "en",
    "zh",
    "hi",
    "es",
    "ar",
    "fr",
    "pt",
    "ru",
    "de",
    "ja",
    "ko",
    "it",
    "nl",
)
# Only providers with implemented runtime paths should be user-selectable.
VALID_ENGINES = (
    "local",
    "assemblyai",
    "groq",
    "openai",
    "deepgram",
    "elevenlabs",
    "azure",
    "funasr",
    "custom",
    "speechmatics",
    "mistral",
)
ENGINE_LANGUAGE_MODES: dict[str, tuple[str, ...]] = {
    "local": WHISPER_LANGUAGE_MODES,
    "assemblyai": WHISPER_LANGUAGE_MODES,
    "groq": WHISPER_LANGUAGE_MODES,
    "openai": OPENAI_LANGUAGE_MODES,
    "deepgram": VALID_LANGUAGE_MODES,
    "elevenlabs": ELEVENLABS_LANGUAGE_MODES,
    "azure": AZURE_LANGUAGE_MODES,
    "funasr": FUNASR_LANGUAGE_MODES,
    # A bring-your-own endpoint: which languages work is the served model's
    # business, so every code the app knows is offered and sent as a hint.
    "custom": VALID_LANGUAGE_MODES,
    # The default model's list; Enhanced and Standard have their own.
    "speechmatics": SPEECHMATICS_MELIA_LANGUAGE_MODES,
    "mistral": MISTRAL_LANGUAGE_MODES,
}
LOCAL_ENGLISH_ONLY_MODELS = ("distil-large-v3.5", GRANITE_CTC_MODEL_SIZE)
LOCAL_BATCH_ONLY_MODELS = (
    LOCAL_WEBGPU_MODEL_SIZES
    + LOCAL_ONNX_ASR_MODEL_SIZES
    + LOCAL_GRANITE_CTC_MODEL_SIZES
)
# Models that must never expose Auto. Cohere needs an explicit language; Canary
# would otherwise translate to English instead of transcribing.
LOCAL_EXPLICIT_LANGUAGE_MODELS = (*LOCAL_WEBGPU_MODEL_SIZES, CANARY_MODEL_SIZE)
MODEL_LANGUAGE_MODES: dict[tuple[str, str], tuple[str, ...]] = {
    ("local", "cohere-transcribe-03-2026"): COHERE_LANGUAGE_MODES,
    ("local", "granite-4.0-1b-speech"): GRANITE_LANGUAGE_MODES,
    ("local", "granite-speech-4.1-2b"): GRANITE_LANGUAGE_MODES,
    ("local", NEMOTRON_MODEL_SIZE): NEMOTRON_LANGUAGE_MODES,
    ("local", PARAKEET_MODEL_SIZE): PARAKEET_LANGUAGE_MODES,
    ("local", PARAKEET_ULTRA_MODEL_SIZE): PARAKEET_LANGUAGE_MODES,
    ("local", CANARY_MODEL_SIZE): CANARY_LANGUAGE_MODES,
    (
        "assemblyai",
        "universal-3-5-pro",
    ): ASSEMBLYAI_UNIVERSAL_3_5_LANGUAGE_MODES,
    ("assemblyai", "universal-2"): WHISPER_LANGUAGE_MODES,
    ("deepgram", "nova-3"): DEEPGRAM_NOVA_3_LANGUAGE_MODES,
    ("deepgram", "nova-2"): DEEPGRAM_NOVA_2_LANGUAGE_MODES,
    ("azure", "mai-transcribe-2"): AZURE_MAI_TRANSCRIBE_2_LANGUAGE_MODES,
    ("azure", "mai-transcribe-1.5"): AZURE_MAI_TRANSCRIBE_1_5_LANGUAGE_MODES,
    ("azure", "mai-transcribe-1"): AZURE_MAI_TRANSCRIBE_1_LANGUAGE_MODES,
    ("funasr", "fun-asr-realtime"): FUNASR_LANGUAGE_MODES,
    ("speechmatics", "melia-1"): SPEECHMATICS_MELIA_LANGUAGE_MODES,
    ("speechmatics", "enhanced"): SPEECHMATICS_LANGUAGE_MODES,
    ("speechmatics", "standard"): SPEECHMATICS_LANGUAGE_MODES,
}
STREAMING_ENGINES = (
    "local",
    "assemblyai",
    "deepgram",
)  # engines that support streaming mode
VALID_MODES = ("batch", "streaming")
VALID_PASTE_MODES = ("auto", "wm_paste", "send_input")


def supports_streaming(engine: str, model_size: str = "") -> bool:
    normalized_engine = str(engine or "").strip().lower()
    normalized_model = str(model_size or "").strip()
    if normalized_engine not in STREAMING_ENGINES:
        return False
    return not (
        normalized_engine == DEFAULT_ENGINE
        and normalized_model in LOCAL_BATCH_ONLY_MODELS
    )


def language_modes_for_selection(
    engine: str,
    model: str = "",
    mode: str = "batch",
) -> tuple[str, ...]:
    normalized_engine = str(engine or "").strip().lower()
    normalized_model = str(model or "").strip()
    normalized_mode = str(mode or "").strip().lower()

    if normalized_engine == "assemblyai" and normalized_mode == "streaming":
        return ("auto",)
    if (
        normalized_engine == DEFAULT_ENGINE
        and normalized_model in LOCAL_ENGLISH_ONLY_MODELS
    ):
        return ("auto", "en")
    model_key = (normalized_engine, normalized_model)
    if model_key in MODEL_LANGUAGE_MODES:
        return MODEL_LANGUAGE_MODES[model_key]
    return ENGINE_LANGUAGE_MODES.get(normalized_engine, VALID_LANGUAGE_MODES)


# --- Who actually receives the custom vocabulary --------------------------
#
# The single source of truth for that question, and the one the settings
# dialog asks. It has to be derived rather than listed, because the answer is
# per *runtime*, not per model: adding a model to LOCAL_MODEL_RUNTIME is what
# decides it, and a hand-written list of model names silently went stale every
# time one was added. `tests/test_custom_vocabulary_support.py` pins these
# against `transcriber/factory.py`, which is where the terms are handed over
# (or not) -- so the two cannot drift.
#
# Remote engines whose request carries the terms: AssemblyAI as
# `keyterms_prompt`, OpenAI as repeated `keywords[]` form fields
# (`gpt-transcribe`) or as `prompt` (the three older models), Groq as
# `prompt`, Deepgram as its repeated `keyterm` (nova-3) / `keywords` (nova-2)
# query parameters, a custom endpoint as `prompt` (transcription API) or as a
# sentence of its chat instruction, Speechmatics as `additional_vocab`
# entries (Enhanced and Standard), Mistral as repeated `context_bias`
# fields. ElevenLabs, Azure LLM Speech and Fun-ASR expose no biasing input
# at all.
CUSTOM_VOCABULARY_ENGINES: tuple[str, ...] = (
    "assemblyai",
    "groq",
    "openai",
    "deepgram",
    "custom",
    "speechmatics",
    "mistral",
)
# A model of one of those engines that has no biasing input. Melia 1:
# "Custom Dictionary: ... Not yet."
# (https://docs.speechmatics.com/speech-to-text/models, read 2026-09-27).
CUSTOM_VOCABULARY_EXCLUDED_MODELS: tuple[tuple[str, str], ...] = (
    ("speechmatics", "melia-1"),
)
# Local runtimes with a biasing input: faster-whisper takes the terms as its
# `initial_prompt`. The onnx-asr (Parakeet, Canary), ONNX Runtime GenAI
# (Nemotron), Transformers.js (Cohere, Granite ONNX) and Granite CTC runtimes
# have none.
CUSTOM_VOCABULARY_LOCAL_RUNTIMES: tuple[str, ...] = ("faster-whisper",)
# How the supported set is named in a sentence to the user. Written out rather
# than generated: "Whisper models" is what the picker calls the seven
# faster-whisper entries, and a generated list would have to name all seven.
# A test pins that every engine above appears in it.
CUSTOM_VOCABULARY_SUPPORTED_SUMMARY = (
    "Whisper models, OpenAI, Groq, AssemblyAI, Deepgram, Speechmatics "
    "Enhanced/Standard, Mistral, and a custom endpoint"
)


def supports_custom_vocabulary(engine: str, model: str = "") -> bool:
    """Whether this engine/model combination is sent the custom vocabulary."""
    normalized_engine = str(engine or "").strip().lower()
    if normalized_engine in CUSTOM_VOCABULARY_ENGINES:
        return (
            normalized_engine,
            str(model or "").strip(),
        ) not in CUSTOM_VOCABULARY_EXCLUDED_MODELS
    if normalized_engine in VALID_ENGINES and normalized_engine != DEFAULT_ENGINE:
        return False
    # `local`, and any unknown engine: `create_transcriber` falls back to the
    # local runtimes for those, and an unknown *model* falls through to
    # faster-whisper there, so both answers mirror the factory.
    runtime = LOCAL_MODEL_RUNTIME.get(
        str(model or "").strip(),
        CUSTOM_VOCABULARY_LOCAL_RUNTIMES[0],
    )
    return runtime in CUSTOM_VOCABULARY_LOCAL_RUNTIMES


def parse_custom_vocabulary(raw: str) -> list[str]:
    """Parse the raw custom-vocabulary setting into a list of terms.

    Terms are split on newlines, commas, and semicolons, stripped of
    surrounding whitespace, and empties are dropped. Duplicates are removed
    case-insensitively while preserving the first-seen order and casing.
    The result is capped at ``CUSTOM_VOCABULARY_MAX_TERMS`` terms (silently).
    """
    text = str(raw or "")
    candidates = re.split(r"[\n,;]+", text)

    terms: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        term = candidate.strip()
        if not term:
            continue
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        terms.append(term)
        if len(terms) >= CUSTOM_VOCABULARY_MAX_TERMS:
            break
    return terms


GROQ_MODELS = ("whisper-large-v3", "whisper-large-v3-turbo")
DEFAULT_GROQ_MODEL = "whisper-large-v3-turbo"

# `POST /v1/audio/transcriptions` accepts five model ids. Four are offered
# here; `gpt-4o-transcribe-diarize` is deliberately not, because the app has
# no speaker UI and OpenAI's guide calls it a specialized model that "isn't
# the recommended model for ordinary file transcription".
#
# `gpt-transcribe` is the current one -- "Start with gpt-transcribe. This is
# the recommended model for transcribing recorded speech in its original
# language." The other three were notified of deprecation on 2026-08-26 and
# OpenAI's deprecations page gives 2027-02-26 as their removal date from the
# API, so they stay selectable and their labels carry the date. (Both pages
# read 2026-09-21; the replacement the deprecation notice names for realtime
# use, `gpt-live-transcribe`, is a realtime-session model and does not answer
# on this endpoint.)
OPENAI_MODELS = (
    "gpt-transcribe",
    "gpt-4o-mini-transcribe",
    "gpt-4o-transcribe",
    "whisper-1",
)
DEFAULT_OPENAI_MODEL = "gpt-transcribe"
# Which models take the repeated array fields `languages[]` and `keywords[]`
# instead of the singular `language` and `prompt`: "For gpt-transcribe,
# languages replaces the singular language field. Don't send both fields."
# Named as a set rather than compared against `DEFAULT_OPENAI_MODEL` so the
# request shape does not silently follow a change of default.
OPENAI_ARRAY_FIELD_MODELS = ("gpt-transcribe",)
# Characters OpenAI refuses inside a `keywords[]` term: "Keep each keyword on
# one line and don't include <, >, a carriage return, or a line feed. The API
# rejects the entire request when it encounters one of these characters."
# `parse_custom_vocabulary` splits on newlines but not on carriage returns, so
# a lone CR inside a line survives its `strip()` and would reach the request.
OPENAI_KEYWORD_FORBIDDEN_CHARACTERS = ("<", ">", "\r", "\n")

DEEPGRAM_MODELS = (
    "nova-3",
    "nova-2",
)
DEFAULT_DEEPGRAM_MODEL = "nova-3"

ASSEMBLYAI_MODELS = (
    "universal-3-5-pro",
    "universal-2",
)
DEFAULT_ASSEMBLYAI_MODEL = "universal-3-5-pro"
# Realtime model. Universal-3.6 Pro (launched 2026-09-29) is streaming-only:
# the batch `speech_models` list has no 3.6 id, so batch stays on 3.5 Pro.
# Same price as 3.5 Pro realtime ($0.45/h), same parameters, 32 languages;
# its code-switching needs no language parameter, so the UI keeps Auto only.
ASSEMBLYAI_STREAMING_MODEL = "universal-3-6-pro"
ASSEMBLYAI_STREAMING_MODEL_LABEL = "Universal-3.6 Pro"

# Data-residency regions (`assemblyai_region`, `deepgram_region`). Each
# provider's default is its default endpoint, i.e. what every build before
# these settings sent. What each choice guarantees, in the vendors' words
# (pages read 2026-10-01):
# - AssemblyAI batch
#   (https://www.assemblyai.com/docs/pre-recorded-audio/select-the-region):
#   "The default endpoint (`api.assemblyai.com`) processes your pre-recorded
#   audio transcription requests in the US region"; "The EU endpoint
#   (`api.eu.assemblyai.com`) guarantees your data never leaves the European
#   Union". There is no separate US batch host, so "auto" and "us" share it.
# - AssemblyAI streaming
#   (https://www.assemblyai.com/docs/streaming/endpoints-and-data-zones): the
#   default `streaming.assemblyai.com` "automatically routes requests to the
#   nearest available region" and "Your data may be processed in any of the
#   US or EU locations"; the data-zone hosts `streaming.us.assemblyai.com`
#   and `streaming.eu.assemblyai.com` "guarantee your data never leaves the
#   specified region". So "auto" streams to the routed host, "us" and "eu"
#   to the data zones (review of 2026-10-01: "us" used to stream to the
#   routed host while the picker said US).
# - Deepgram (https://developers.deepgram.com/reference/custom-endpoints):
#   `api.deepgram.com` is "the default global endpoint", with no residency
#   stated for it, and `api.eu.deepgram.com` routes "traffic through the EU";
#   regional endpoints "use the same API keys". Hence "global", not "us".
#   That page names the REST host; the WebSocket host is the same name with
#   `wss://`, which it does not spell out.
ASSEMBLYAI_REGION_AUTO = "auto"
ASSEMBLYAI_REGION_US = "us"
ASSEMBLYAI_REGION_EU = "eu"
ASSEMBLYAI_REGIONS = (
    ASSEMBLYAI_REGION_AUTO,
    ASSEMBLYAI_REGION_US,
    ASSEMBLYAI_REGION_EU,
)
DEFAULT_ASSEMBLYAI_REGION = ASSEMBLYAI_REGION_AUTO
ASSEMBLYAI_API_BASE_URLS = {
    ASSEMBLYAI_REGION_AUTO: "https://api.assemblyai.com",
    ASSEMBLYAI_REGION_US: "https://api.assemblyai.com",
    ASSEMBLYAI_REGION_EU: "https://api.eu.assemblyai.com",
}
ASSEMBLYAI_STREAMING_HOSTS = {
    ASSEMBLYAI_REGION_AUTO: "streaming.assemblyai.com",
    ASSEMBLYAI_REGION_US: "streaming.us.assemblyai.com",
    ASSEMBLYAI_REGION_EU: "streaming.eu.assemblyai.com",
}
DEEPGRAM_REGION_GLOBAL = "global"
DEEPGRAM_REGION_EU = "eu"
DEEPGRAM_REGIONS = (DEEPGRAM_REGION_GLOBAL, DEEPGRAM_REGION_EU)
DEFAULT_DEEPGRAM_REGION = DEEPGRAM_REGION_GLOBAL
DEEPGRAM_API_HOSTS = {
    DEEPGRAM_REGION_GLOBAL: "api.deepgram.com",
    DEEPGRAM_REGION_EU: "api.eu.deepgram.com",
}


def _known_region(value: object, known: tuple[str, ...], default: str) -> str:
    """A region this build knows; anything else is the provider's default.

    A value this build does not know -- a typo in a hand-edited file, a
    region a newer build added -- must not reach a provider as a host name.
    """
    normalized = str(value or "").strip().lower()
    return normalized if normalized in known else default


def normalize_assemblyai_region(value: object) -> str:
    return _known_region(value, ASSEMBLYAI_REGIONS, DEFAULT_ASSEMBLYAI_REGION)


def normalize_deepgram_region(value: object) -> str:
    return _known_region(value, DEEPGRAM_REGIONS, DEFAULT_DEEPGRAM_REGION)


# Total time one batch job may stay queued/processing before the app gives
# up on it. The SDK's own `wait_for_completion` is `while True:` with no
# bound, so a job AssemblyAI never finishes holds the single transcription
# worker for the rest of the session -- and, because ThreadPoolExecutor's
# exit handler joins its workers, stops the app from exiting at all
# (measured: `shutdown(wait=False, cancel_futures=True)` does not release
# it), leaving a process that still holds the single-instance lock.
# 30 minutes is 10-60x the normal turnaround and covers roughly 85 minutes
# of audio at AssemblyAI's advertised async speed, so no dictation and few
# imports can reach it; one that does fails with its transcript id, which
# can be retrieved from the AssemblyAI dashboard.
ASSEMBLYAI_BATCH_MAX_WAIT_S = 1800.0

ELEVENLABS_MODELS = ("scribe_v2",)
DEFAULT_ELEVENLABS_MODEL = "scribe_v2"

# Azure LLM Speech (Microsoft Foundry) enhanced-mode models.
# These are remote, cloud-only models from the Microsoft AI (MAI) team.
# `mai-transcribe-1` stays selectable although Microsoft marked it
# "Deprecated on Aug 20, 2026": deprecated is not removed, and whether the
# service still answers for it is not documented. Its label says so.
AZURE_SPEECH_MODELS = (
    "mai-transcribe-2",
    "mai-transcribe-1.5",
    "mai-transcribe-1",
)
DEFAULT_AZURE_SPEECH_MODEL = "mai-transcribe-2"
# What `enhancedMode.model` is sent as. Settings store the lower-case id;
# every example on Microsoft's page writes the name this way, and whether the
# service compares case-insensitively is not documented, so the documented
# spelling is the one that is sent.
AZURE_API_MODEL_NAMES: dict[str, str] = {
    "mai-transcribe-2": "MAI-Transcribe-2",
    "mai-transcribe-1.5": "MAI-Transcribe-1.5",
    "mai-transcribe-1": "MAI-Transcribe-1",
}
# REST API version for the fast-transcription `:transcribe` endpoint.
AZURE_SPEECH_API_VERSION = "2025-10-15"
# Per-resource endpoint, e.g. "https://<resource>.cognitiveservices.azure.com".
# Empty until the user configures it in Settings.
DEFAULT_AZURE_ENDPOINT = ""

# Alibaba Fun-ASR (DashScope Model Studio). Remote, cloud-only; driven over the
# real-time WebSocket API in a batch fashion. Needs only a DashScope API key.
FUNASR_MODELS = ("fun-asr-realtime",)
DEFAULT_FUNASR_MODEL = "fun-asr-realtime"
# International (Singapore) DashScope inference WebSocket endpoint.
FUNASR_WS_URL_INTL = "wss://dashscope-intl.aliyuncs.com/api-ws/v1/inference/"

# Total budget for one Fun-ASR request, matching ASSEMBLYAI_BATCH_MAX_WAIT_S.
# The receive loop had none: `websocket-client` answers a server PING inside
# `recv_data_frame` without returning, and every arriving frame restarts the
# socket timeout, so a service that pings but never sends `task-finished`
# parked the app's single transcription worker forever.
FUNASR_BATCH_MAX_WAIT_S = 1800.0

# Speechmatics batch (SaaS) models, selected by `transcription_config.model`
# (https://docs.speechmatics.com/speech-to-text/models, read 2026-09-27):
# Enhanced "the highest accuracy"; Standard and Melia 1 "High", Melia 1
# multilingual and the cheapest ($0.129/h, read 2026-09-27 on a pricing
# summary; the vendor's pricing page states $0.129 without naming the
# model). Melia 1 is the default because it is the one that transcribes
# without a chosen language, which the app's default Auto needs.
SPEECHMATICS_MELIA_MODEL = "melia-1"
SPEECHMATICS_MODELS = (SPEECHMATICS_MELIA_MODEL, "enhanced", "standard")
DEFAULT_SPEECHMATICS_MODEL = SPEECHMATICS_MELIA_MODEL
# Batch hosts open to every customer
# (https://docs.speechmatics.com/get-started/authentication, read
# 2026-09-27); "EU2 and US2 ... are provided for enterprise customer high
# availability and failover purposes only" and are not offered. "Melia 1
# is available for Batch transcription in the EU and US regions only."
SPEECHMATICS_API_HOSTS = {
    "eu1": "eu1.asr.api.speechmatics.com",
    "us1": "us1.asr.api.speechmatics.com",
    "au1": "au1.asr.api.speechmatics.com",
}
SPEECHMATICS_REGIONS = tuple(SPEECHMATICS_API_HOSTS)
DEFAULT_SPEECHMATICS_REGION = "eu1"
SPEECHMATICS_MELIA_REGIONS = ("eu1", "us1")
# A job's total wait, as for AssemblyAI (`ASSEMBLYAI_BATCH_MAX_WAIT_S`),
# and the pause between status requests: the GET limit is 50 per second,
# so one per second is far inside it.
SPEECHMATICS_BATCH_MAX_WAIT_S = 1800.0
SPEECHMATICS_POLL_INTERVAL_S = 1.0


def normalize_speechmatics_region(value: object) -> str:
    return _known_region(value, SPEECHMATICS_REGIONS, DEFAULT_SPEECHMATICS_REGION)


# Mistral Voxtral Mini Transcribe 2, released 2026-02-04, $0.003 per minute
# (https://docs.mistral.ai/models/voxtral-mini-transcribe-26-02, read
# 2026-09-27). The dated id rather than `voxtral-mini-latest`, so the model
# does not change under the user.
MISTRAL_MODELS = ("voxtral-mini-2602",)
DEFAULT_MISTRAL_MODEL = "voxtral-mini-2602"

# A bring-your-own OpenAI-compatible endpoint: a LiteLLM or vLLM gateway, a
# local speech server, or any host that speaks the OpenAI REST shapes. The
# base URL, the model and the API style are the user's; nothing is validated
# against a list, because the endpoint decides what it offers.
DEFAULT_CUSTOM_ENDPOINT = ""
DEFAULT_CUSTOM_MODEL = ""
# `transcriptions`: `POST {base}/audio/transcriptions`, the OpenAI speech API.
# `chat`: `POST {base}/chat/completions` with an `input_audio` content part,
# for gateways that route audio only to a multimodal LLM.
CUSTOM_API_MODE_TRANSCRIPTIONS = "transcriptions"
CUSTOM_API_MODE_CHAT = "chat"
CUSTOM_API_MODES = (CUSTOM_API_MODE_TRANSCRIPTIONS, CUSTOM_API_MODE_CHAT)
DEFAULT_CUSTOM_API_MODE = CUSTOM_API_MODE_TRANSCRIPTIONS
DEFAULT_CUSTOM_KEY_COMMAND = ""
# How long a token printed by the key command is reused. Short-lived gateway
# tokens typically live an hour or two; five minutes keeps the command (which
# can take a couple of seconds) off most dictations without holding a token
# near its expiry. A 401 re-runs the command once regardless.
CUSTOM_KEY_COMMAND_TTL_S = 300.0
CUSTOM_KEY_COMMAND_TIMEOUT_S = 30.0

# --- How much audio one remote batch request carries ----------------------
#
# The app records 16 kHz mono 16-bit WAV, 1.92 MB per minute, and sends the
# recording as it is. A recording longer than its engine's bound, or larger
# than its byte cap, goes out in consecutive parts cut at quiet points
# (`transcriber/_audio_parts.py`), because past these limits the provider
# refuses the request -- or, for the two token-capped OpenAI models, returns a
# transcript cut short that reads like a complete one. The parts are split,
# not compressed: compressing would need an encoder dependency, and OpenAI
# does not even accept FLAC. The vendors' own pages were read on 2026-09-27.
#
# Each engine has both a seconds bound and a byte cap. The seconds are chosen
# for the app's 16 kHz WAV, which reaches them well before the cap; an
# imported WAV at 44.1 or 48 kHz is encoded at its own rate and carries up to
# three times the bytes per second, so the cap is what bounds its parts.
#
# An engine without an entry is sent whole: Deepgram takes 2 GB, ElevenLabs
# 3 GB / 10 h and AssemblyAI 2.2 GB / 10 h, past any dictation, and Fun-ASR
# streams the recording over a WebSocket.
#
# `gpt-4o-transcribe` and `gpt-4o-mini-transcribe` state "2,000 max output
# tokens" on their model pages, and a forum report shows the transcript cut
# off silently after about 8-9 minutes of English at 2,048 output tokens, i.e.
# about 230-255 tokens per minute. Fast speech (200 against 150 words per
# minute) in a language that tokenizes denser than English can need 1.7 times
# that, which reaches 2,000 tokens inside five minutes; three minutes leave a
# margin of about 2.8 over the English rate. A part costs no more than the
# minutes it holds, so the shorter bound only adds seams.
OPENAI_TOKEN_CAPPED_MAX_PART_SECONDS = 180.0
# OpenAI takes 25 MB per file, about 13 minutes of this WAV. `gpt-transcribe`
# and `whisper-1` state no token cap, and ten minutes also stays below the
# 1,400 s duration cap that is reported second-hand only.
OPENAI_MAX_PART_SECONDS = 600.0
# The speech-to-text guide: "Files can be up to 25 MB." Read as decimal
# megabytes, the smaller of the two readings.
OPENAI_MAX_REQUEST_BYTES = 25_000_000
# Groq's speech-to-text page lists "Max File Size: 25 MB (free tier), 100MB
# (dev tier)" and, separately, "Max Attachment File Size: 25 MB. If you need to
# process larger files, use the `url` parameter" (read 2026-09-27): an upload,
# which is what the app sends, is held to 25 MB on every tier.
GROQ_MAX_PART_SECONDS = 600.0
# As decimal megabytes, the smaller of the two readings.
GROQ_MAX_REQUEST_BYTES = 25_000_000
# The fast-transcription REST reference: "shorter than 2 hours ... smaller
# than 250 MB" (the quotas page says 5 h / 500 MB; the smaller is taken).
# 250 MB is about 130 minutes of this WAV, so an hour stays under both.
AZURE_MAX_PART_SECONDS = 3600.0
# The same REST reference (2025-10-15): "shorter than 2 hours in audio
# duration and smaller than 250 MB in size."
AZURE_MAX_REQUEST_BYTES = 250_000_000
# A custom endpoint in its transcription-API style is held to OpenAI's
# limits, the API it imitates; a server that accepts more loses nothing but a
# seam every ten minutes.
CUSTOM_MAX_PART_SECONDS = OPENAI_MAX_PART_SECONDS
CUSTOM_MAX_REQUEST_BYTES = OPENAI_MAX_REQUEST_BYTES
# Its chat style sends the WAV base64-encoded inside a JSON body, which grows
# it by a third, and a multimodal LLM writes the transcript as output tokens:
# five minutes and 15 MB of raw WAV (about 20 MB of request) stay inside what
# gateways and model output limits commonly allow.
CUSTOM_CHAT_MAX_PART_SECONDS = 300.0
CUSTOM_CHAT_MAX_REQUEST_BYTES = 15_000_000
# Speechmatics: a file "less than 1 GB in size or the job will be rejected"
# (https://docs.speechmatics.com/speech-to-text/batch/limits, read
# 2026-09-27), and no duration limit is stated. Half an hour per job keeps
# one job's turnaround well inside `SPEECHMATICS_BATCH_MAX_WAIT_S`.
SPEECHMATICS_MAX_PART_SECONDS = 1800.0
SPEECHMATICS_MAX_REQUEST_BYTES = 999_000_000
# Mistral: "Maximum audio duration: 60 minutes", "Maximum file size: 500
# MB" (https://docs.mistral.ai/resources/known-limitations, read
# 2026-09-27). The request is synchronous, so half an hour per part keeps
# one request well inside its socket timeout.
MISTRAL_MAX_PART_SECONDS = 1800.0
MISTRAL_MAX_REQUEST_BYTES = 500_000_000
REMOTE_BATCH_MAX_PART_SECONDS: dict[str, float] = {
    "openai": OPENAI_MAX_PART_SECONDS,
    "groq": GROQ_MAX_PART_SECONDS,
    "azure": AZURE_MAX_PART_SECONDS,
    "custom": CUSTOM_MAX_PART_SECONDS,
    "speechmatics": SPEECHMATICS_MAX_PART_SECONDS,
    "mistral": MISTRAL_MAX_PART_SECONDS,
}
REMOTE_BATCH_MAX_REQUEST_BYTES: dict[str, int] = {
    "openai": OPENAI_MAX_REQUEST_BYTES,
    "groq": GROQ_MAX_REQUEST_BYTES,
    "azure": AZURE_MAX_REQUEST_BYTES,
    "custom": CUSTOM_MAX_REQUEST_BYTES,
    "speechmatics": SPEECHMATICS_MAX_REQUEST_BYTES,
    "mistral": MISTRAL_MAX_REQUEST_BYTES,
}
# An API style whose limit differs from its engine's: (engine, api_mode) ->
# (seconds, bytes). Only the custom endpoint has more than one style.
REMOTE_BATCH_API_MODE_LIMITS: dict[tuple[str, str], tuple[float, int]] = {
    ("custom", CUSTOM_API_MODE_CHAT): (
        CUSTOM_CHAT_MAX_PART_SECONDS,
        CUSTOM_CHAT_MAX_REQUEST_BYTES,
    ),
}
# A model whose own limit is tighter than its engine's.
REMOTE_BATCH_MODEL_MAX_PART_SECONDS: dict[tuple[str, str], float] = {
    ("openai", "gpt-4o-transcribe"): OPENAI_TOKEN_CAPPED_MAX_PART_SECONDS,
    ("openai", "gpt-4o-mini-transcribe"): OPENAI_TOKEN_CAPPED_MAX_PART_SECONDS,
}


class RemotePartLimit(NamedTuple):
    """How much audio one batch request of an engine carries: at most
    `seconds` of it, in a file of at most `max_bytes`."""

    seconds: float
    max_bytes: int


def remote_batch_part_limit(
    engine: str, model: str = "", api_mode: str = ""
) -> RemotePartLimit | None:
    """The limit one batch request of this engine and model is held to, or
    None when the recording is always sent whole.

    The single answer every provider asks: a new one adds its entries above
    and passes this to `transcribe_in_parts` (`transcriber/_audio_parts.py`).
    The byte cap is read strictly, so an engine given a seconds bound without
    a cap fails loudly instead of being sent with no cap at all.
    """
    normalized_engine = str(engine or "").strip().lower()
    api_mode_limit = REMOTE_BATCH_API_MODE_LIMITS.get(
        (normalized_engine, str(api_mode or "").strip().lower())
    )
    if api_mode_limit is not None:
        return RemotePartLimit(seconds=api_mode_limit[0], max_bytes=api_mode_limit[1])
    model_key = (normalized_engine, str(model or "").strip())
    seconds = REMOTE_BATCH_MODEL_MAX_PART_SECONDS.get(
        model_key, REMOTE_BATCH_MAX_PART_SECONDS.get(normalized_engine)
    )
    if seconds is None:
        return None
    return RemotePartLimit(
        seconds=seconds,
        max_bytes=REMOTE_BATCH_MAX_REQUEST_BYTES[normalized_engine],
    )


AUDIO_SAMPLE_RATE = 16_000
AUDIO_CHANNELS = 1
AUDIO_BLOCK_DURATION_MS = 100
# A successfully started PortAudio input stream should deliver a callback well
# before this. A longer delay means the device stream is stalled, not silent.
AUDIO_CAPTURE_FIRST_CALLBACK_TIMEOUT_MS = 2_000
# Windows raises several MMDevice notifications for one physical event (per
# role, per endpoint); coalesce them before re-enumerating devices and
# restarting the warm microphone stream.
AUDIO_DEVICE_CHANGE_SETTLE_MS = 600
# Empty string = follow the Windows default input device at every stream open.
DEFAULT_INPUT_DEVICE_NAME = ""
STREAMING_PARTIAL_INTERVAL_S = 0.35
STREAMING_PARTIAL_MIN_AUDIO_S = 0.25
STREAMING_PARTIAL_WINDOW_S = 8.0
# Consecutive failed live inserts before a streaming session gives up. A single
# failure is usually transient — the user is holding a modifier key, so the
# injected Ctrl+V would arrive as Ctrl+Alt+V — and ending the whole dictation
# for that is far worse than skipping one update and retrying on the next
# partial. A genuinely dead target still ends the session, just not instantly.
#
# Kept small on purpose: each attempt runs on the Qt thread and a held modifier
# costs the full PASTE_MODIFIER_RELEASE_TIMEOUT_S (1.5 s) before it fails, so a
# large limit turns a stuck target into seconds of unresponsive UI — the very
# thing the off-thread handshake work was fixing. Three attempts absorb the
# transient case at a bounded ~4.5 s worst case.
# How long the finalizer waits for an in-flight provider handshake before
# stopping the stream anyway. Deepgram's own connect wait is 8 s, so this has to
# exceed it or the common case would time out; the bound exists only so a
# provider that never returns cannot hang the finalize forever.
STREAMING_CONNECT_JOIN_TIMEOUT_S = 15.0

STREAMING_LIVE_INSERT_RETRY_LIMIT = 3

# Bucket size for that measurement, deliberately much finer than the batch
# gate's SILENCE_GATE_WINDOW_MS. At 100 ms two keystrokes 100-150 ms apart
# land in ADJACENT buckets, so the run never breaks and typing is
# indistinguishable from a spoken word -- measured, typing at 120 wpm
# reported a 1.5 s "speech" run. At 20 ms the silence between keystrokes
# falls in its own bucket and breaks the run: typing at 80-120 wpm measures
# 0.02 s, a mouse double-click 0.04 s, a single 50 ms click 0.06 s.
#
# The finer bucket also splits real words at their internal stop closures,
# which is why STREAMING_NEW_SEGMENT_MIN_SPEECH_S below had to be rederived
# rather than carried over.
STREAMING_SPEECH_RUN_WINDOW_MS = 20
# How much measured speech a rolling window must contain before it may be
# APPENDED after a pause instead of aligned. A pause longer than the window
# means the overlap search has nothing to anchor on, so such a window is
# taken on trust -- and the model reliably invents words when the audio is
# mostly quiet.
#
# THE TWO CLASSES OVERLAP AND NO THRESHOLD SEPARATES THEM. Measured the way
# production measures it (longest unbroken run at
# STREAMING_SPEECH_RUN_WINDOW_MS, candidate embedded in 7 s of room tone):
#
# All of these are SYNTHETIC constructions -- the repository has no recorded
# audio (samples/benchmark_sample.wav is sine tones from
# scripts/generate_sample_audio.py). Treat them as shapes, not as ground
# truth about German speech.
#
#   "Bitte." -- 2x80 ms voiced around an 85 ms tt-closure    0.085 s
#   "Stopp." -- 2x90 ms voiced around a 50 ms closure        0.100 s
#   250 ms word with a 40 ms internal closure                0.140 s
#   "Ja." 180 ms continuous                                  0.180 s
#
#   digital silence and room tone up to -50 dBFS             0.000 s
#   mechanical key clack (18 ms decay)                       0.080 s
#   knuckle knock / trackpad click                           0.100 s
#   door latch / lip smack                                   0.140 s
#   heavy low-frequency thump                                0.200 s
#   typing at 80-120 wpm                                     0.020 s
#
# Rows with a construction in tests/test_vad.py: the four words, the key
# clack, the knuckle knock and the room-tone levels. The typing row, the
# door latch, the lip
# smack and the heavy thump were measured during review but have no fixture
# and the sustained-noise figures below are an estimate -- do not quote any
# of those as measured. Sustained human
# noises (a breath, a sigh, paper rustle) run 0.35-0.70 s and pass easily,
# but that figure is an estimate -- there is no fixture for it, so do not
# quote it as measured.
#
# A voiceless closure is genuinely silent for 40-100 ms and breaks the run,
# so what this meter reports for a short word is the longest *voiced piece*
# of it -- 80-90 ms -- which is exactly what an isolated knock reports. Three
# earlier values (0.35, 0.15, 0.18) were set as if a clean cut existed and
# each DELETED real words; a fourth (0.08 derived from 300 ms excerpts) was
# right by accident, for the wrong reason.
#
# So this gate does NOT recognise speech, and it does not filter keyboard
# noise either -- a single key clack measures exactly the cut, and above
# ~130 wpm the decay tails bridge the gaps and typing reports seconds of
# "speech".
#
# Stated precisely, because the loose version ("it blocks silence") invites
# swapping this for a cheap peak meter: it blocks audio whose longest
# CONTIGUOUS run above `silence_gate_threshold` is shorter than the cut. That
# is a duration test, not a loudness test -- a 5 ms click at -13 dBFS, 35 dB
# louder than room tone, is blocked, while room tone above -48 dBFS is not
# blocked at all. What it therefore covers reliably is silence, which is the
# case that once grew the transcript to 896 junk words with an open
# microphone. Anything with a long enough run passes, deliberately:
#
#   deleting a word   -- silent, invisible, unrecoverable
#   admitting a knock -- visible junk, bounded to the current segment by
#                        `protected_prefix`, and the user can see and fix it
#
# Separating a knock from "Bitte." needs spectral features (a real VAD), not
# an energy threshold. Until then, bounding the damage is the protection --
# do not "fix" this by raising the number again.
STREAMING_NEW_SEGMENT_MIN_SPEECH_S = 0.08

# How much audio may pile up while a remote streaming provider is still
# completing its network handshake. Deepgram waits up to 8 s for its socket and
# the AssemblyAI SDK connects synchronously, so the microphone is opened first
# and the first seconds of speech are buffered until the stream is ready.
# 16 kHz mono 16-bit is 32 kB/s, so this ceiling is about 60 s of audio; it only
# exists so a connection that never completes cannot grow without bound.
STREAMING_PRECONNECT_BUFFER_MAX_BYTES = 2_000_000
# How long the preconnect flush waits for room in a provider's own send queue,
# per chunk. Deepgram's queue holds 32 chunks (3.2 s of audio) against the
# 62.5 s this buffer may hold, and the flush used to burst through it: measured,
# 33 `put_nowait` calls complete in about 45 us, roughly a hundredth of
# CPython's 5 ms thread switch interval, so the sender thread was never
# scheduled during the burst and chunk 33 was rejected -- failing the dictation
# on a socket that had just connected. The flush runs on the connect worker
# thread, not on the PortAudio callback, so it is allowed to wait; the
# microphone callback keeps its nonblocking path. Five seconds is well past the
# 3.2 s the queue itself represents, so only a sender that has genuinely
# stopped draining reaches it.
STREAMING_PRECONNECT_FLUSH_PUT_TIMEOUT_S = 5.0
STREAMING_STABLE_WORD_GUARD = 1
STREAMING_REVISION_WORD_WINDOW = 1
STREAMING_OVERLAY_MAX_CHARS = 180
STREAMING_LIVE_INSERT_ENABLED = True
# Whether losing focus ENDS a live stream, or only suspends insertion.
#
# It used to end it. Live insertion writes at the caret, so once another
# window is in front the words would land in the wrong document -- but
# throwing the whole session away for that is far more disruptive than the
# problem: people switch windows mid-thought, and the rest of the dictation
# was simply gone. With this False the session keeps recording, nothing is
# pasted while the target is not in front, and everything is delivered when
# the recording stops. Set it back to True for the old hard abort.
STREAMING_ABORT_ON_FOCUS_CHANGE = False
STREAMING_FOCUS_POLL_MS = 25
STREAMING_BEEP_ON_ABORT = True
STREAMING_ABORT_BEEP_HZ = 900
STREAMING_ABORT_BEEP_DURATION_MS = 120
STREAMING_ABORT_JOIN_TIMEOUT_S = 0.2

VAD_ENERGY_THRESHOLD = 0.02
DEFAULT_VAD_ENERGY_THRESHOLD = VAD_ENERGY_THRESHOLD
VAD_ENERGY_THRESHOLD_MIN = 0.003
VAD_ENERGY_THRESHOLD_MAX = 0.1
VAD_MIN_SPEECH_MS = 120
VAD_MAX_SILENCE_MS = 700

# Silence gate: skip transcription entirely when the recording's loudest
# 100 ms window stays below the threshold, so speech models cannot
# hallucinate words from silence. Opt-in and deliberately tuned well below
# the VAD default so whispering into a good microphone still passes; the
# measured peak level is logged on every batch stop to make tuning easy.
# On by default: it is the only hallucination guard that covers every engine.
# The Cohere/Granite runtime has no no-speech probability and no VAD at all, so
# a silent recording is decoded into fluent invented text. The gate measures
# the *loudest* 100 ms window against a very low threshold (~-48 dBFS), which
# whispering clears comfortably, and a gated recording stays recoverable.
DEFAULT_SILENCE_GATE_ENABLED = True
DEFAULT_SILENCE_GATE_THRESHOLD = 0.004
SILENCE_GATE_THRESHOLD_MIN = 0.0005
SILENCE_GATE_THRESHOLD_MAX = 0.1
SILENCE_GATE_WINDOW_MS = 100

# A speech check beside the two energy gates: the Silero VAD v6 graph that
# faster-whisper ships (`silero_vad.py` says where and why). It scores every
# 32 ms window for speech, which loudness cannot do -- a knuckle knock and a
# short word measure the same energy run (STREAMING_NEW_SEGMENT_MIN_SPEECH_S
# above). It can only take a decision away from an energy gate, never make
# one: audio it cannot measure is left to the energy gate alone.
#
# Every figure below is the highest window probability in the audio, measured
# on 2026-09-27..10-01 with that graph (faster-whisper 1.2.1, ONNX Runtime
# 1.30.0, CPU, one thread). Three properties of the graph decide the design:
#   - The score of non-speech is not a fixed number. It moves with where the
#     audio starts on the 32 ms grid, with the noise seed and with where the
#     event falls: a knuckle knock scored 0.064-0.227 over 50 seeds, white
#     room tone at -42 dBFS 0.043-0.159. Speech at a normal level holds still
#     (the owner's recordings move by at most 0.018 across eight grid
#     offsets). So every non-speech figure here is a range or a count of
#     seeds/offsets, never one value.
#   - The score depends on level. Speech whose loudest 100 ms is 0.0005 --
#     the quietest level the Audio tab lets the silence gate admit,
#     SILENCE_GATE_THRESHOLD_MIN -- scores as low as 0.027 as recorded, and
#     the lowest scores come from a high crest: a transient far louder than
#     the speech sets the recording's level while the speech stays quiet
#     (crest up to 51.6 in the dictation clips, at most 3.8 in LibriSpeech).
#   - A copy of the audio amplified by 8 and clipped to full scale restores
#     that speech (0.25 or more) and lifts non-speech far less.
#
# Real speech, batch (the whole recording from its start, worst of 8 grid
# offsets unless stated):
#   - 493 of the owner's recordings that pass the level gate: lowest 0.938,
#     1st percentile 0.981, median 0.995. (Aggregates only; the recordings
#     were read, never copied.)
#   - 20 LibriSpeech dev-clean clips and 5 German/English dictation clips,
#     attenuated by up to 42 dB and kept while the level gate admits them:
#     at -30 dB 18 pass, lowest 0.992; at -34 dB 8, lowest 0.984; at -38 dB
#     only a 192 s clip passes, at 0.688 (0.140 at its worst offset), 0.996
#     amplified.
#   - A quiet microphone: 37 excerpts (the 20 LibriSpeech clips and 17 of the
#     dictation clips, 15 s each) scaled so their loudest 100 ms is L, plus
#     no transient: as recorded the lowest is 0.027 / 0.045 / 0.096 / 0.160
#     at L = 0.0005 / 0.00075 / 0.001 / 0.0015 (3 / 2 / 1 / 0 below the batch
#     cut), amplified by 8 0.246 / 0.253 / 0.271 / 0.381 (none below).
#   - The six LibriSpeech words of tests/data scaled to L = 0.0005 and ended
#     by a key clack that lifts them over the default level gate (grid offset
#     0): five of six score 0.060-0.143 as recorded, every one 0.808 or more
#     amplified.
#   - Speech whose loudest 100 ms is 0.0003, below every settable threshold,
#     lifted over the level gate by a key clack (50 cases, worst of 8
#     offsets): as recorded 18-19 of 50 below the batch cut, lowest 0.025;
#     amplified none, lowest 0.980. faster-whisper medium transcribed 13 of
#     those 18 within 80 % word agreement of its own transcript of the clip
#     at full level, Parakeet TDT 1 of 18. This is the case that rules out a
#     check on the recording as recorded alone.
# Real speech, streaming (the trailing window after a pause, as recorded):
#   - The six LibriSpeech words and phrases after 7.5 s of room tone, 20
#     seeds: lowest 0.930; attenuated by up to 22 dB, lowest 0.864; at a
#     loudest 100 ms of 0.0005, lowest 0.510.
#   - 2868 utterances of the owner's recordings that follow a pause of at
#     least 0.7 s and that the energy run admits, each scored as the
#     streaming window sees it (7 s of room tone, the utterance up to the
#     next such pause, 0.3 s after) at 8 grid offsets. Every utterance whose
#     worst offset scored below 0.35 was played to Parakeet TDT and
#     faster-whisper medium; "heard" means one of them transcribed words.
#       cut   below at offset 0   below at some offset   heard   both heard
#       0.05          1                    3                1         0
#       0.08          4                   11                3         0
#       0.10          7                   17                6         0
#       0.15         18                   25               12         0
#     Averaged over the offsets, 0.08 refuses 4.5 utterances, 1.1 of them
#     heard by a hearer.
# Non-speech, SYNTHETIC (seeded signals built for the calibration, not
# recordings). Batch, offsets of 8 below the cut, as recorded / with the
# amplified copy as well: white room tone -48 dBFS 8/4, -42 dBFS 8/3, pink
# -36 dBFS 8/6, fan-like low-pass noise 3/0 and 3/1, mains hum 6/6, 5 ms
# click 8/7, key clack 8/5, knuckle knock 6/4, door latch 8/4, heavy thump
# 8/2, mouse double click 8/8, typing 80 / 120 / 160 wpm 8/7, 8/3, 8/0, chair
# creak 5/1, cough-like burst 8/4, 318 Hz tone 2/0, 1 kHz beep 7/5. At grid
# offset 0 over 50 seeds (the tests' fixtures): thump after a pause skipped
# 50 as recorded / 32 with both, knock 45 / 1, room tone -42 dBFS 49 / 40,
# -48 dBFS 50 / 38. Streaming, 50 seeds, refused at 0.08: thump 45
# (0.038-0.098), typing 160 wpm 49 (0.035-0.086), knock 3 (0.064-0.227),
# room tone -42 dBFS 26 (0.052-0.159).
#
# A window counts as speech at Silero's own default. Used for the logged
# speech seconds and the batch scan's early stop, not for either decision.
SILERO_SPEECH_PROBABILITY = 0.5
# Batch: a recording that passed the level gate is skipped only when a
# COMPLETE scan stays below this as recorded AND a complete scan of the copy
# amplified by SILERO_BATCH_QUIET_SPEECH_GAIN stays below it too. 0.15 sits
# under every real-speech case above once the amplified scan is included
# (lowest 0.246) and over most of the transients. The cut and the second scan
# are placed for the speech side, not to catch every noise: a dictation
# wrongly skipped is lost until the user retries it by hand, a missed noise
# costs what it cost before this check existed. The price is visible above:
# with both scans a knock is skipped 1 time in 50 instead of 45, typing at
# 160 wpm never, a thump 32 times in 50.
SILERO_BATCH_MIN_PROBABILITY = 0.15
# The amplification of the second batch scan. It is the ratio of the default
# silence-gate threshold to the lowest one the Audio tab allows
# (DEFAULT_SILENCE_GATE_THRESHOLD / SILENCE_GATE_THRESHOLD_MIN = 8): speech
# at the quietest admissible level is scored as if it had reached the default
# gate's level. Measured gains 2 and 4 restore less of the quiet speech
# (gain 4: lowest 0.230 at L = 0.0005), 8 is the smallest that restored every
# case above.
SILERO_BATCH_QUIET_SPEECH_GAIN = 8.0
# The batch scan runs on the Qt thread at every stop the level gate admits
# while the gate is on, so it is bounded.
# Measured on a Ryzen 5 7600X, warm, from the WAV bytes: audio with no speech
# costs about 1.5-1.7 ms per second scanned per scan, while speech settles
# the natural scan after about 3 s whatever the length (4-11 ms for 10 s,
# 60 s and ten minutes, over two sessions) and no second scan runs. The scan
# stops once this much speech is found and never scans past the budget. A
# speechless recording within the budget pays both scans: 31 ms for 10 s,
# 92 ms for 30 s (2026-10-01). A scan that stopped for either reason is
# incomplete, and an incomplete scan never skips: the unscanned rest may hold
# the speech, so a speechless recording longer than the budget is transcribed
# as before (46 ms for 60 s, one scan). Building the session costs 29 ms
# when ONNX Runtime is already loaded (onnx-asr, Nemotron and Granite CTC
# load it) and 123-275 ms when it still has to be imported (four fresh
# interpreters); it happens once, on a daemon thread (`start_loading`) or on
# the streaming worker that asks first, never inside a stop.
SILERO_BATCH_STOP_AFTER_SPEECH_S = 1.0
SILERO_BATCH_MAX_SCAN_S = 30.0
# A session load that failed is retried after this long, not never: the file
# held for a moment by a scanner or a backup tool would otherwise switch the
# check off until the app restarts, and every post-pause streaming partial
# (about every 350 ms) retrying it would pay and log the failure each time.
SILERO_LOAD_RETRY_S = 60.0
# Streaming: the trailing window after a pause longer than the window must
# also reach this, as recorded, before it is decoded and appended on trust.
# The check runs only where a refusal means "skip the window" or "drop the
# finalizer's tail", never where it would turn an append into a replace. A
# refusal is not a delay but can be a loss: the window is re-checked on every
# partial, so a word followed by more speech is admitted with it, but a word
# that ends the dictation after a long pause is dropped by the finalizer. So
# the cut sits for the speech side: the table above, 4.5 expected refusals of
# 2868 owner utterances, 1.1 of them heard as words by one hearer and none by
# both, and every LibriSpeech word at least 0.51. On the noise side it
# refuses most thumps and fast typing and about half of the near-gate room
# tone, and still admits most knocks, whose damage stays bounded by the
# segment floor as before. No amplified copy here: the quietest words scored
# 0.510 as recorded, and amplification would admit more of the noise.
SILERO_STREAM_MIN_PROBABILITY = 0.08

OVERLAY_WIDTH = 396
OVERLAY_HEIGHT = 98
OVERLAY_MAX_HEIGHT = OVERLAY_HEIGHT * 4
# When the transcription queue is visible the overlay may grow taller than the
# normal transcript cap, but it stays bounded (and scrolls beyond this) instead
# of expanding to full screen height.
OVERLAY_QUEUE_MAX_HEIGHT = OVERLAY_HEIGHT * 6
OVERLAY_MARGIN_X = 24
OVERLAY_MARGIN_Y = 24
OVERLAY_DETAIL_MIN_HEIGHT = 42
# Compact states (Idle/Listening/Processing) used to pin the detail area to the
# minimum, which silently clipped anything longer than two lines — most visibly
# the startup hotkey notice, which explains a fallback binding and is exactly
# the text a user must be able to read. Compact now grows to fit, up to this
# cap, and only scrolls beyond it.
OVERLAY_COMPACT_DETAIL_MAX_HEIGHT = 108
# Minimum visible height of the scrollable queue panel before it scrolls.
OVERLAY_QUEUE_MIN_HEIGHT = 96
# How long the overlay is brought to the foreground (temporary topmost) after a
# result so a floating overlay is actually seen: a brief glance on success, a
# longer window on errors/insert failures so the transcript can be copied.
OVERLAY_RESULT_REVEAL_MS = 2500
# How long a confirmation ("copied to clipboard") stays before the overlay
# returns to its idle hint.
OVERLAY_NOTICE_MS = 2200
OVERLAY_ERROR_REVEAL_MS = 9000
OVERLAY_INITIAL_DETAIL = "Press hotkey to start dictation"
# Overlay error states offer one follow-up action. "insert" replaces Retry
# (which re-transcribes) with Insert when the transcription succeeded and only
# the insertion failed, because there is no failed transcription to retry then.
OVERLAY_ERROR_ACTION_INSERT = "insert"
# The tray menu's cancel entry, named by the preload's progress line when no
# cancel hotkey reaches the download and the overlay's slot holds Insert.
TRAY_CANCEL_ACTION_LABEL = "Cancel current action"
# The tray menu's re-paste entry, named by the reports of transcripts that are
# still waiting to be inserted, which is what that entry inserts first.
TRAY_REPASTE_ACTION_LABEL = "Insert transcript again"
# The two kinds of row the overlay's queue panel shows: a transcription still
# running or queued (its button cancels it), and a finished transcript whose
# paste failed or may have failed (its button dismisses the row; the text
# stays in history).
QUEUE_ROW_KIND_TRANSCRIPTION = "transcription"
QUEUE_ROW_KIND_UNDELIVERED = "undelivered"
# An Error state that must offer NO action at all. `None` cannot express
# this: the action slot treats "not Insert" as Retry, so passing None gave
# the user a Retry button on a transcript that had already been inserted --
# and Retry re-transcribes the last *failed* recording, which may be an
# entirely different one, pasting it on top.
OVERLAY_ERROR_ACTION_NONE = "none"
OVERLAY_OPACITY_MIN_PERCENT = 25
OVERLAY_OPACITY_MAX_PERCENT = 100
DEFAULT_OVERLAY_OPACITY_PERCENT = OVERLAY_OPACITY_MAX_PERCENT
VALID_OVERLAY_CORNERS = (
    "top-right",
    "top-left",
    "bottom-right",
    "bottom-left",
)
DEFAULT_OVERLAY_CORNER = "top-right"
OVERLAY_STATE_COLORS = {
    "Idle": "#2f3a4a",
    "Listening": "#1b5e20",
    "Processing": "#0d47a1",
    "Done": "#4e342e",
    "Error": "#b71c1c",
}

LOG_FILE_NAME = "dictation.log"
LOG_MAX_BYTES = 1_000_000
LOG_BACKUP_COUNT = 3
# "Copy diagnostics" returns the current session: everything since the last
# line carrying this marker. The line budget is only the safety net for a very
# long session (or a log without a marker), because the text goes to the
# clipboard and has to stay pasteable.
SESSION_START_LOG_MARKER = "app_session_started"
DIAGNOSTICS_MAX_LINES = 800
DOC_MODELS_PATH = "docs/models.md"
DOC_SSL_PROXY_PATH = "docs/advanced-setup.md#ssl--proxy-issues"

KEYRING_SERVICE_NAME = "stt-app"
LEGACY_KEYRING_SERVICE_NAMES = ("tts-app",)

SENDINPUT_RETRY_ATTEMPTS = 3
SENDINPUT_RETRY_SLEEP_S = 0.02
CLIPBOARD_SETTLE_S = 0.02
WM_PASTE_TIMEOUT_MS = 250
# A dictation overwrites the clipboard and puts it back afterwards, so the
# capture has to copy every format the clipboard holds -- a screenshot
# (CF_DIB), a file selection (CF_HDROP), formatted text (HTML Format) --
# and not just the text. That copy runs on the Qt main thread and is paid
# once per dictation, so it needs a ceiling: past either of these the
# capture keeps CF_UNICODETEXT alone and the rest of the clipboard is left
# as the transcript replaced it. 128 MiB is far above the few megabytes a
# screenshot costs and far below what would stall the UI; the total is
# twice that because one clipboard legitimately carries the same picture in
# several formats (CF_DIB and CF_DIBV5 side by side).
CLIPBOARD_CAPTURE_MAX_FORMAT_BYTES = 128 * 1024 * 1024
CLIPBOARD_CAPTURE_MAX_TOTAL_BYTES = 256 * 1024 * 1024
# How long the transcript stays on the clipboard after a SendInput paste before
# the previous clipboard is put back. The predecessor was 160 ms *slept on the
# Qt main thread*, and that thread is what bounded it: streaming inserts run
# every ~350 ms, so a longer sleep froze the UI while the user was dictating.
# 160 ms was never enough for the case that loses a transcript. Electron and
# Chromium targets read the clipboard from their renderer process, seconds late
# under CPU load: a 326-character transcript reported as pasted 196 ms after
# the transcription finished delivered the *previous* clipboard content while
# local transcription pinned the CPU, and the user had to insert it again by
# hand 4.5 s later. The wait now runs on a throwaway timer thread, so the Qt
# thread pays nothing for it and the only cost of holding the clipboard longer
# is that the user's own Ctrl+V within this window pastes the transcript
# instead of what they copied -- and copying something themselves cancels the
# restore outright, so the window closes as soon as they need it to.
CLIPBOARD_RESTORE_DELAY_S = 1.5
# And how long that deferred restore keeps waiting for a target that still has
# not answered WM_NULL. Past this the transcript is left on the clipboard for
# good: a target this slow has demonstrably not read it yet, and restoring is
# exactly what turns its eventual paste into the user's old content.
CLIPBOARD_RESTORE_MAX_WAIT_S = 10.0
# A restore that fails -- another program held the clipboard for longer than
# one operation's opens, or closed our open after its `EmptyClipboard` -- is
# written again from the captured state this many times, this far apart, on
# the restore's timer thread; only then is it reported through the tray. A
# restore that found the clipboard emptied by our own write had left the user
# with nothing at all, and nothing ever tried again. Each attempt is one
# reopen-guarded operation (about 0.33 s at most), never a sleep on the Qt
# thread, and every attempt first checks that the clipboard still holds our
# transcript or our own partial restore, so it never writes over a copy the
# user made meanwhile. 3 x 1.0 s covers a clipboard manager or an RDP
# redirection holding the clipboard for a few seconds.
CLIPBOARD_RESTORE_RETRY_ATTEMPTS = 3
CLIPBOARD_RESTORE_RETRY_DELAY_S = 1.0
# Inserts are often triggered straight from a WM_HOTKEY press, so the user's
# physical Ctrl/Alt/Shift/Win keys can still be down when Ctrl+V is injected.
# The target would then see e.g. Ctrl+Alt+V (AltGr+V) instead of a paste, so
# the inserter waits for all physical modifiers to be released first.
PASTE_MODIFIER_RELEASE_TIMEOUT_S = 1.5
PASTE_MODIFIER_POLL_INTERVAL_S = 0.01
# Before restoring the previous clipboard after a SendInput paste, wait until
# the target window's thread answers WM_NULL again: a busy target has not
# processed the injected Ctrl+V yet, and restoring early would make its late
# clipboard read paste the old content instead of the transcript. If the
# target stays unresponsive past this budget, the restore is skipped so the
# eventual paste still reads the transcript.
PASTE_TARGET_RESPONSIVE_TIMEOUT_S = 2.0
PASTE_TARGET_RESPONSIVE_PROBE_MS = 200
# The probe blocks for PASTE_TARGET_RESPONSIVE_PROBE_MS only while the target
# is merely busy. `SMTO_ABORTIFHUNG` makes it return *immediately* for a target
# Windows already considers hung -- which is the case this wait exists for --
# so without a sleep the loop is unthrottled. Measured on the Qt main thread:
# 953,446 probes in 1.994 s at 100% of one core, i.e. every paste into a hung
# target froze the whole app for the full budget. At 10 ms that is 200 probes,
# and the busy case pays at most one extra sleep per 200 ms probe.
PASTE_TARGET_RESPONSIVE_POLL_INTERVAL_S = 0.01
