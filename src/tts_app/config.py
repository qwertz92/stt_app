from __future__ import annotations

# Global configuration values. Keep defaults and tunables centralized here.

APP_NAME = "tts_app"
APP_DISPLAY_NAME = "TTS Dictation App"
APP_LOGGER_NAME = "tts_app"

SCHEMA_VERSION = 6

# Hotkeys: RegisterHotKey requires at least one non-modifier key.
# Original default that worked reliably in this project.
DEFAULT_HOTKEY = "Ctrl+Alt+Space"
FALLBACK_HOTKEY = "Ctrl+Win+LShift"
DEFAULT_HOTKEY_ID = 1
LEGACY_DEFAULT_HOTKEY = "Ctrl+Win+LShift"
PREVIOUS_DEFAULT_HOTKEY = "Ctrl+Shift+Alt+Space"

DEFAULT_MODEL_SIZE = "small"
DEFAULT_LANGUAGE_MODE = "auto"
DEFAULT_ENGINE = "local"
DEFAULT_MODE = "batch"
DEFAULT_VAD_ENABLED = True
DEFAULT_SAVE_LAST_WAV = False

VALID_MODEL_SIZES = ("tiny", "base", "small", "medium", "large-v3")
VALID_LANGUAGE_MODES = ("auto", "de", "en")
VALID_ENGINES = ("local", "openai", "azure", "deepgram")
VALID_MODES = ("batch", "streaming")

AUDIO_SAMPLE_RATE = 16_000
AUDIO_CHANNELS = 1
AUDIO_BLOCK_DURATION_MS = 100

VAD_ENERGY_THRESHOLD = 0.02
VAD_MIN_SPEECH_MS = 120
VAD_MAX_SILENCE_MS = 700

OVERLAY_WIDTH = 320
OVERLAY_HEIGHT = 92
OVERLAY_MARGIN_X = 24
OVERLAY_MARGIN_Y = 24
OVERLAY_INITIAL_DETAIL = "Press hotkey to start dictation"
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

KEYRING_SERVICE_NAME = "tts-app"

SENDINPUT_RETRY_ATTEMPTS = 3
SENDINPUT_RETRY_SLEEP_S = 0.02
CLIPBOARD_SETTLE_S = 0.02
SENDINPUT_RESTORE_DELAY_S = 0.16
WM_PASTE_TIMEOUT_MS = 250
