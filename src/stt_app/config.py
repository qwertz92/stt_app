from __future__ import annotations

import re

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

SCHEMA_VERSION = 23

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

DEFAULT_MODEL_SIZE = "small"
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
# behaviour (GPU first, CPU fallback, with the per-model CPU preference in
# LOCAL_ONNX_AUTO_CPU_MODELS applied); the rest let the user pin a device when a
# benchmark shows one is better on their hardware.
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
# Then use the download script: python scripts/download_model.py --model small
# It handles the correct directory structure automatically.
DEFAULT_MODEL_DIR = ""

FASTER_WHISPER_MODEL_SIZES = (
    "tiny",
    "base",
    "small",
    "medium",
    "large-v3",
    "large-v3-turbo",  # Multilingual, ~809 MB, pruned large-v3 (4 decoder layers)
    "distil-large-v3.5",  # English-only, ~756 MB, improved v3 (98k h training data)
)

LOCAL_WEBGPU_MODEL_SIZES = (
    "cohere-transcribe-03-2026",
    "granite-4.0-1b-speech",
    "granite-speech-4.1-2b",
    "granite-speech-4.1-2b-plus",
    "granite-speech-4.1-2b-nar",
)

NEMOTRON_MODEL_SIZE = "nemotron-3.5-asr-streaming-0.6b-int4"
LOCAL_NEMOTRON_MODEL_SIZES = (NEMOTRON_MODEL_SIZE,)

# NVIDIA NeMo models served by the pure-Python `onnx-asr` runtime. They need no
# Node.js and no new ONNX Runtime: onnx-asr resolves the same `onnxruntime`
# distribution the app already carries for Nemotron.
PARAKEET_MODEL_SIZE = "parakeet-tdt-0.6b-v3"
CANARY_MODEL_SIZE = "canary-1b-v2"
LOCAL_ONNX_ASR_MODEL_SIZES = (PARAKEET_MODEL_SIZE, CANARY_MODEL_SIZE)

LOCAL_ONNX_MODEL_SIZES = (
    LOCAL_WEBGPU_MODEL_SIZES + LOCAL_NEMOTRON_MODEL_SIZES + LOCAL_ONNX_ASR_MODEL_SIZES
)

# Models whose upstream repo has no ModelScope counterpart (verified against the
# ModelScope API on 2026-08-18). On a network that blocks Hugging Face wholesale
# -- a proxy denying the whole "Generative AI and ML Applications" category is
# the common case -- these cannot be fetched at all. Naming them up front beats
# a download that ends in "check your internet connection", which is exactly the
# one thing that is not wrong.
MODELS_WITHOUT_MODELSCOPE_MIRROR = frozenset(
    {
        "distil-large-v3.5",
        PARAKEET_MODEL_SIZE,
        CANARY_MODEL_SIZE,
        "granite-speech-4.1-2b-plus",
        "granite-speech-4.1-2b-nar",
    }
)

GRANITE_4_1_MODEL_SIZES = (
    "granite-speech-4.1-2b",
    "granite-speech-4.1-2b-plus",
    "granite-speech-4.1-2b-nar",
)

LOCAL_ONNX_MODEL_PRECISION: dict[str, str] = {
    "cohere-transcribe-03-2026": "q4",
    "granite-4.0-1b-speech": "q4",
    "granite-speech-4.1-2b": "q4",
    "granite-speech-4.1-2b-plus": "int8",
    "granite-speech-4.1-2b-nar": "int8",
    NEMOTRON_MODEL_SIZE: "int4",
    PARAKEET_MODEL_SIZE: "int8",
    CANARY_MODEL_SIZE: "int8",
}

LOCAL_ONNX_MODEL_RUNTIME_LABELS: dict[str, str] = {
    "cohere-transcribe-03-2026": "ONNX/WebGPU q4",
    "granite-4.0-1b-speech": "ONNX/WebGPU q4",
    "granite-speech-4.1-2b": "ONNX/WebGPU q4",
    "granite-speech-4.1-2b-plus": "ONNX INT8 AR",
    "granite-speech-4.1-2b-nar": "ONNX INT8 NAR",
    NEMOTRON_MODEL_SIZE: "ORT GenAI INT4, 560 ms streaming",
    PARAKEET_MODEL_SIZE: "onnx-asr INT8 TDT, CPU",
    CANARY_MODEL_SIZE: "onnx-asr INT8 AED, CPU",
}

GRANITE_4_1_REPO_MAP: dict[str, str] = {
    "granite-speech-4.1-2b": "onnx-community/granite-speech-4.1-2b-ONNX",
    "granite-speech-4.1-2b-plus": "smcleod/ibm-granite-speech-4.1-2b-plus-onnx",
    "granite-speech-4.1-2b-nar": "smcleod/ibm-granite-speech-4.1-2b-nar-onnx",
}

LOCAL_WEBGPU_DEVICE_POLICIES = ("auto", "gpu", "cpu", "dml", "webgpu")

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


def nemotron_provider_order(device_policy: str) -> tuple[str, ...]:
    """Provider order for a device policy, defaulting to the auto behaviour."""
    return NEMOTRON_DEVICE_PROVIDER_ORDER.get(
        str(device_policy or "").strip().lower(), ("dml", "cpu")
    )

# Models whose known-fastest compatible path is CPU. Explicit non-auto benchmark
# targets still bypass this policy so future runtime fixes can be re-evaluated.
# Both raw-graph Granite 4.1 models share the conformer encoder whose block-local
# attention no GPU execution provider here can run (WebGPU fails the `Einsum`
# shader, DirectML cannot execute the 5-D attention MatMuls). Their WebGPU
# sessions still *create* successfully and only fail at inference, so the
# load-time probe cannot detect it: without this policy every dictation paid a
# full WebGPU load plus a doomed attempt before falling back, measured at 75-110 s
# versus 13.6 s on CPU for the same clip.
LOCAL_ONNX_AUTO_CPU_MODELS = (
    "granite-speech-4.1-2b-nar",
    "granite-speech-4.1-2b-plus",
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
    NEMOTRON_MODEL_SIZE: (
        "onnx-community/nemotron-3.5-asr-streaming-0.6b-onnx-int4"
    ),
    PARAKEET_MODEL_SIZE: "istupakov/parakeet-tdt-0.6b-v3-onnx",
    CANARY_MODEL_SIZE: "istupakov/canary-1b-v2-onnx",
    **GRANITE_4_1_REPO_MAP,
}

LOCAL_MODEL_RUNTIME: dict[str, str] = {
    **{name: "faster-whisper" for name in FASTER_WHISPER_MODEL_SIZES},
    **{name: "onnx-webgpu" for name in LOCAL_WEBGPU_MODEL_SIZES},
    **{name: "onnxruntime-genai" for name in LOCAL_NEMOTRON_MODEL_SIZES},
    **{name: "onnx-asr" for name in LOCAL_ONNX_ASR_MODEL_SIZES},
}

# Approximate model sizes for UI progress estimation.
# Values are decimal megabytes (MB), not MiB.
MODEL_ESTIMATED_SIZE_MB: dict[str, int] = {
    "tiny": 75,
    "base": 141,
    "small": 484,
    "medium": 1_400,
    "large-v3": 3_000,
    "large-v3-turbo": 1_622,
    "distil-large-v3.5": 756,
    # Selectable local ONNX downloads. Cohere, Granite 4.0, and Granite 4.1 2B
    # are q4 Transformers.js packages; Granite 4.1 Plus/NAR use the smallest
    # currently published INT8 tier.
    "cohere-transcribe-03-2026": 2_128,
    "granite-4.0-1b-speech": 1_843,
    "granite-speech-4.1-2b": 1_843,
    "granite-speech-4.1-2b-plus": 4_065,
    "granite-speech-4.1-2b-nar": 2_490,
    NEMOTRON_MODEL_SIZE: 793,
    # Measured from the int8 downloads: 670.48 MB and 1029.33 MB.
    PARAKEET_MODEL_SIZE: 670,
    CANARY_MODEL_SIZE: 1_029,
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
# byte-identical output), so Auto is the only honest choice.
PARAKEET_LANGUAGE_MODES = ("auto",)
# Canary must NEVER offer Auto. onnx-asr hardcodes the <|en|> source/target
# token, so without an explicit language it silently *translates* German into
# English rather than transcribing it. The 25 trained locales only; the vocab
# carries ~180 ISO codes and an untrained one raises KeyError.
CANARY_LANGUAGE_MODES = (
    "de", "en", "bg", "cs", "da", "el", "es", "et", "fi", "fr", "hr", "hu",
    "it", "lt", "lv", "mt", "nl", "pl", "pt", "ro", "ru", "sk", "sl", "sv", "uk",
)
GRANITE_LANGUAGE_MODES = ("auto", "de", "en", "fr", "es", "pt", "ja")
GRANITE_NO_JAPANESE_LANGUAGE_MODES = ("auto", "de", "en", "fr", "es", "pt")
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
# MAI-Transcribe-1.5 covers 42 languages; MAI-Transcribe-1 a smaller subset.
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
AZURE_LANGUAGE_MODES = AZURE_MAI_TRANSCRIBE_1_5_LANGUAGE_MODES
# App language code -> Azure locale code, where they differ.
AZURE_LOCALE_OVERRIDES: dict[str, str] = {"no": "nb"}
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
}
LOCAL_ENGLISH_ONLY_MODELS = ("distil-large-v3.5",)
LOCAL_BATCH_ONLY_MODELS = LOCAL_WEBGPU_MODEL_SIZES + LOCAL_ONNX_ASR_MODEL_SIZES
# Models that must never expose Auto. Cohere needs an explicit language; Canary
# would otherwise translate to English instead of transcribing.
LOCAL_EXPLICIT_LANGUAGE_MODELS = LOCAL_WEBGPU_MODEL_SIZES + (CANARY_MODEL_SIZE,)
MODEL_LANGUAGE_MODES: dict[tuple[str, str], tuple[str, ...]] = {
    ("local", "cohere-transcribe-03-2026"): COHERE_LANGUAGE_MODES,
    ("local", "granite-4.0-1b-speech"): GRANITE_LANGUAGE_MODES,
    ("local", "granite-speech-4.1-2b"): GRANITE_LANGUAGE_MODES,
    ("local", "granite-speech-4.1-2b-plus"): GRANITE_NO_JAPANESE_LANGUAGE_MODES,
    ("local", "granite-speech-4.1-2b-nar"): GRANITE_NO_JAPANESE_LANGUAGE_MODES,
    ("local", NEMOTRON_MODEL_SIZE): NEMOTRON_LANGUAGE_MODES,
    ("local", PARAKEET_MODEL_SIZE): PARAKEET_LANGUAGE_MODES,
    ("local", CANARY_MODEL_SIZE): CANARY_LANGUAGE_MODES,
    (
        "assemblyai",
        "universal-3-5-pro",
    ): ASSEMBLYAI_UNIVERSAL_3_5_LANGUAGE_MODES,
    ("assemblyai", "universal-2"): WHISPER_LANGUAGE_MODES,
    ("deepgram", "nova-3"): DEEPGRAM_NOVA_3_LANGUAGE_MODES,
    ("deepgram", "nova-2"): DEEPGRAM_NOVA_2_LANGUAGE_MODES,
    ("azure", "mai-transcribe-1.5"): AZURE_MAI_TRANSCRIBE_1_5_LANGUAGE_MODES,
    ("azure", "mai-transcribe-1"): AZURE_MAI_TRANSCRIBE_1_LANGUAGE_MODES,
    ("funasr", "fun-asr-realtime"): FUNASR_LANGUAGE_MODES,
}
STREAMING_ENGINES = ("local", "assemblyai", "deepgram")  # engines that support streaming mode
VALID_MODES = ("batch", "streaming")
VALID_PASTE_MODES = ("auto", "wm_paste", "send_input")


def supports_streaming(engine: str, model_size: str = "") -> bool:
    normalized_engine = str(engine or "").strip().lower()
    normalized_model = str(model_size or "").strip()
    if normalized_engine not in STREAMING_ENGINES:
        return False
    if normalized_engine == DEFAULT_ENGINE and normalized_model in LOCAL_BATCH_ONLY_MODELS:
        return False
    return True


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

OPENAI_MODELS = (
    "gpt-4o-mini-transcribe",
    "gpt-4o-transcribe",
    "whisper-1",
)
DEFAULT_OPENAI_MODEL = "gpt-4o-mini-transcribe"

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

ELEVENLABS_MODELS = (
    "scribe_v2",
)
DEFAULT_ELEVENLABS_MODEL = "scribe_v2"

# Azure LLM Speech (Microsoft Foundry) enhanced-mode models.
# These are remote, cloud-only models from the Microsoft AI (MAI) team.
AZURE_SPEECH_MODELS = (
    "mai-transcribe-1.5",
    "mai-transcribe-1",
)
DEFAULT_AZURE_SPEECH_MODEL = "mai-transcribe-1.5"
# REST API version for the fast-transcription `:transcribe` endpoint.
AZURE_SPEECH_API_VERSION = "2025-10-15"
# Per-resource endpoint, e.g. "https://<resource>.cognitiveservices.azure.com".
# Empty until the user configures it in Settings.
DEFAULT_AZURE_ENDPOINT = ""

# Alibaba Fun-ASR (DashScope Model Studio). Remote, cloud-only; driven over the
# real-time WebSocket API in a batch fashion. Needs only a DashScope API key.
FUNASR_MODELS = (
    "fun-asr-realtime",
)
DEFAULT_FUNASR_MODEL = "fun-asr-realtime"
# International (Singapore) DashScope inference WebSocket endpoint.
FUNASR_WS_URL_INTL = "wss://dashscope-intl.aliyuncs.com/api-ws/v1/inference/"

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
# of those four as measured. Sustained human
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
SENDINPUT_RESTORE_DELAY_S = 0.16
WM_PASTE_TIMEOUT_MS = 250
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
