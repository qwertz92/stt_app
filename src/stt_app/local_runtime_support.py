"""Which local runtimes this Python environment can actually run.

The app ships one wheel set per platform, and on Windows ARM64 one runtime is
missing from it: CTranslate2, the engine under every faster-whisper model, has
no win_arm64 build (`pyproject.toml`'s `[tool.uv]` table says how the install
still succeeds there). `faster_whisper` stays installed -- the Silero speech
check reads its bundled graph -- but importing it fails. So every place that
would hand a Whisper model to that import asks here first: a picker marks the
model, the download queue skips it, and a transcription refuses with the
reason before it fetches hundreds of megabytes it could not run.

The answer comes from `importlib.util.find_spec`, which locates a package
without importing it: importing CTranslate2 loads a large native library, and
this is asked on the Qt thread for every picker row.
"""

from __future__ import annotations

import functools
import importlib.util
import platform

from .config import LOCAL_MODEL_RUNTIME

# The packages a faster-whisper model needs to load, most informative first.
_FASTER_WHISPER_MODULES = ("ctranslate2", "faster_whisper")

# Appended to a picker row of a model this environment cannot run. Short on
# purpose: the retranscribe dialog's model combo has no spare width.
UNAVAILABLE_LABEL_SUFFIX = " [unavailable]"


def _module_missing(name: str) -> bool:
    """True only when the lookup positively finds no such package.

    A lookup that itself fails (`find_spec` raises `ValueError` for a module
    whose `__spec__` is None) says nothing about the package, and answering
    "missing" would lock the user out of a runtime that may work. The import
    that follows reports the real error instead.
    """
    try:
        return importlib.util.find_spec(name) is None
    except (ImportError, ValueError):
        return False


def _is_windows_arm64() -> bool:
    return platform.system() == "Windows" and platform.machine().upper() == "ARM64"


@functools.cache
def faster_whisper_unavailable_reason() -> str | None:
    """Why faster-whisper models cannot run here, or None when they can.

    Cached for the process: a package does not appear while the app runs, and
    a picker asks once per row. Tests that change the answer call
    `faster_whisper_unavailable_reason.cache_clear()`.
    """
    missing = next(
        (name for name in _FASTER_WHISPER_MODULES if _module_missing(name)), None
    )
    if missing is None:
        return None
    if missing == "ctranslate2" and _is_windows_arm64():
        return (
            "The Whisper models need CTranslate2, which has no Windows ARM64 "
            "build. Pick another local model or a cloud engine."
        )
    return (
        f"The '{missing}' package is not installed in this Python environment "
        "(run `uv sync`), so the Whisper models cannot run."
    )


def unavailable_reason(model_name: str) -> str | None:
    """Why the local model ``model_name`` cannot run here, or None."""
    if LOCAL_MODEL_RUNTIME.get(model_name) != "faster-whisper":
        return None
    return faster_whisper_unavailable_reason()
