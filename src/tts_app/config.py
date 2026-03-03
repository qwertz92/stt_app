from __future__ import annotations

# Global configuration values. Keep defaults and tunables centralized here.

APP_NAME = "tts_app"
APP_DISPLAY_NAME = "Voice Dictation App"
APP_LOGGER_NAME = "tts_app"

SCHEMA_VERSION = 11

# Hotkeys: RegisterHotKey requires at least one non-modifier key.
# Original default that worked reliably in this project.
DEFAULT_HOTKEY = "Ctrl+Alt+Space"
FALLBACK_HOTKEY = "Ctrl+Win+LShift"
DEFAULT_HOTKEY_ID = 1
DEFAULT_CANCEL_HOTKEY = "Ctrl+Alt+F12"
DEFAULT_CANCEL_HOTKEY_ID = 2

DEFAULT_MODEL_SIZE = "small"
DEFAULT_LANGUAGE_MODE = "auto"
DEFAULT_ENGINE = "local"
DEFAULT_MODE = "batch"
DEFAULT_VAD_ENABLED = False
DEFAULT_SAVE_LAST_WAV = False
DEFAULT_SAVE_ALL_RECORDINGS = False
DEFAULT_RECORDINGS_DIR = ""
DEFAULT_RECORDINGS_MAX_COUNT = 10
DEFAULT_HISTORY_MAX_ITEMS = 20
HISTORY_MAX_ITEMS_MAX = 5_000
DEFAULT_PASTE_MODE = "auto"
DEFAULT_KEEP_TRANSCRIPT_IN_CLIPBOARD = False
DEFAULT_ALLOW_INSECURE_KEY_STORAGE = False
DEFAULT_OFFLINE_MODE = False
DEFAULT_START_BEEP_ENABLED = False
DEFAULT_START_BEEP_TONE = "soft"
VALID_START_BEEP_TONES = ("soft", "high", "chime", "system")

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

VALID_MODEL_SIZES = (
    "tiny",
    "base",
    "small",
    "medium",
    "large-v3",
    "large-v3-turbo",  # Multilingual, ~809 MB, pruned large-v3 (4 decoder layers)
    "distil-large-v3.5",  # English-only, ~756 MB, improved v3 (98k h training data)
)

# Short model name → HuggingFace repo ID.
# Single source of truth used by local transcriber, download script, and settings.
MODEL_REPO_MAP: dict[str, str] = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "distil-large-v3.5": "distil-whisper/distil-large-v3.5-ct2",
}

# Approximate model sizes for UI progress estimation.
# Values are decimal megabytes (MB), not MiB.
MODEL_ESTIMATED_SIZE_MB: dict[str, int] = {
    "tiny": 75,
    "base": 141,
    "small": 484,
    "medium": 1_400,
    "large-v3": 3_000,
    "large-v3-turbo": 809,
    "distil-large-v3.5": 756,
}

VALID_LANGUAGE_MODES = ("auto", "de", "en")
LANGUAGE_MODE_LABELS: dict[str, str] = {
    "auto": "Auto",
    "de": "German",
    "en": "English",
}
# Only providers with implemented runtime paths should be user-selectable.
VALID_ENGINES = ("local", "assemblyai", "groq", "openai", "deepgram")
ENGINE_LANGUAGE_MODES: dict[str, tuple[str, ...]] = {
    "local": VALID_LANGUAGE_MODES,
    "assemblyai": VALID_LANGUAGE_MODES,
    "groq": VALID_LANGUAGE_MODES,
    "openai": VALID_LANGUAGE_MODES,
    "deepgram": VALID_LANGUAGE_MODES,
}
LOCAL_ENGLISH_ONLY_MODELS = ("distil-large-v3.5",)
STREAMING_ENGINES = ("local", "assemblyai", "deepgram")  # engines that support streaming mode
VALID_MODES = ("batch", "streaming")
VALID_PASTE_MODES = ("auto", "wm_paste", "send_input")

GROQ_MODELS = ("whisper-large-v3", "whisper-large-v3-turbo")
DEFAULT_GROQ_MODEL = "whisper-large-v3-turbo"

OPENAI_MODELS = (
    "gpt-4o-mini-transcribe",
    "gpt-4o-transcribe",
    "whisper-1",
)
DEFAULT_OPENAI_MODEL = "gpt-4o-mini-transcribe"

AUDIO_SAMPLE_RATE = 16_000
AUDIO_CHANNELS = 1
AUDIO_BLOCK_DURATION_MS = 100
STREAMING_PARTIAL_INTERVAL_S = 0.35
STREAMING_PARTIAL_MIN_AUDIO_S = 0.25
STREAMING_PARTIAL_WINDOW_S = 8.0
STREAMING_STABLE_WORD_GUARD = 1
STREAMING_OVERLAY_MAX_CHARS = 180
STREAMING_LIVE_INSERT_ENABLED = True
STREAMING_ABORT_ON_FOCUS_CHANGE = True
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

OVERLAY_WIDTH = 320
OVERLAY_HEIGHT = 92
OVERLAY_MAX_HEIGHT = OVERLAY_HEIGHT * 4
OVERLAY_MARGIN_X = 24
OVERLAY_MARGIN_Y = 24
OVERLAY_DETAIL_MIN_HEIGHT = 42
OVERLAY_INITIAL_DETAIL = "Press hotkey to start dictation"
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
DIAGNOSTICS_MAX_LINES = 300
DOC_MODELS_PATH = "docs/models.md"
DOC_SSL_PROXY_PATH = "docs/advanced-setup.md#ssl--proxy-issues"

KEYRING_SERVICE_NAME = "tts-app"

SENDINPUT_RETRY_ATTEMPTS = 3
SENDINPUT_RETRY_SLEEP_S = 0.02
CLIPBOARD_SETTLE_S = 0.02
SENDINPUT_RESTORE_DELAY_S = 0.16
WM_PASTE_TIMEOUT_MS = 250
