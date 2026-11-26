"""Measure how long other Python threads stand still while the app works.

WHY
The paste owner window (`clipboard_owner`) hands a pasted transcript to the
program that asks for it from a Python callback on its own thread. That
callback needs the interpreter lock, so while another thread of this process
sits in one native call that keeps the lock, the asking program waits: its
paste arrives late. This script measures that wait for the work the app does
in its own process.

HOW
A ticker thread written in pure Python sleeps 1 ms in a loop and remembers
the longest time between two wake-ups; the work runs on the main thread
meanwhile. The figure includes the sleep itself and the timer's granularity,
which the `idle` case shows (the ticker with nothing else running).

CASES
  idle               nothing but the ticker, for two seconds
  native             one call that keeps the lock (`sum(range(N))`), for scale
  silero             build the Silero speech-check session (once per start)
  settings           build the Settings dialog, show it without activating it
                     and close it; opens a window for a moment
  model:<size>       load that local model, then transcribe 30 s of English
                     speech from `tests/data` twice; the model must be on
                     this machine already (`HF_HUB_OFFLINE=1`), and it runs on
                     the CPU so that a dictation on the GPU is left alone

`APPDATA` and `LOCALAPPDATA` point at a throwaway folder and the Settings
dialog gets a key store without keys. Nothing is downloaded, no keystroke
is sent and the clipboard is not touched.

COMMAND LINE
    .venv\\Scripts\\python.exe scripts\\diagnose_interpreter_stall.py idle native
    .venv\\Scripts\\python.exe scripts\\diagnose_interpreter_stall.py settings
    .venv\\Scripts\\python.exe scripts\\diagnose_interpreter_stall.py \\
        model:tiny model:parakeet-tdt-0.6b-v3
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _release_check_common as common

SANDBOX = common.make_sandbox("stt_interpreter_stall_")
common.redirect_this_process_to(SANDBOX)
os.environ["STT_APP_DISABLE_MODELSCOPE"] = "1"
PROJECT_ROOT = common.add_src_to_sys_path()

SPEECH_SECONDS = 30
SAMPLE_RATE = 16_000


class StallTicker:
    """The longest time a pure-Python thread waited for its next turn."""

    def __init__(self) -> None:
        self.longest_ms = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        last = time.perf_counter()
        while not self._stop.is_set():
            time.sleep(0.001)
            now = time.perf_counter()
            self.longest_ms = max(self.longest_ms, (now - last) * 1000.0)
            last = now

    def __enter__(self) -> StallTicker:
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        self._thread.join(5)


def measure(label: str, work) -> None:
    started = time.perf_counter()
    with StallTicker() as ticker:
        work()
    took_ms = (time.perf_counter() - started) * 1000.0
    sys.stdout.write(
        f"{label:<46} took {took_ms:9.0f} ms   longest stall "
        f"{ticker.longest_ms:8.1f} ms\n"
    )
    sys.stdout.flush()


def _speech_wav() -> Path:
    import numpy as np

    from stt_app.transcriber._pcm_audio import pcm16_wav_bytes

    sys.path.insert(0, str(PROJECT_ROOT / "tests"))
    import speech_fixtures

    excerpts = list(speech_fixtures.speech_excerpts().values())
    samples = np.concatenate(excerpts)
    while len(samples) < SPEECH_SECONDS * SAMPLE_RATE:
        samples = np.concatenate([samples, *excerpts])
    samples = samples[: SPEECH_SECONDS * SAMPLE_RATE].astype(np.int16)
    path = SANDBOX / "speech.wav"
    path.write_bytes(pcm16_wav_bytes(samples.tobytes(), SAMPLE_RATE))
    return path


def model_case(model_size: str) -> None:
    from stt_app.settings_store import AppSettings
    from stt_app.transcriber.factory import create_transcriber

    wav = _speech_wav()
    settings = AppSettings(
        engine="local",
        model_size=model_size,
        language_mode="en",
        offline_mode=True,
        local_onnx_device="cpu",
    )
    transcriber = create_transcriber(settings)
    try:
        measure(f"{model_size}: load", transcriber.preload_model)
        measure(f"{model_size}: transcribe", lambda: transcriber.transcribe_batch(wav))
        measure(
            f"{model_size}: transcribe again",
            lambda: transcriber.transcribe_batch(wav),
        )
    finally:
        close = getattr(transcriber, "close", None)
        if callable(close):
            close()


class _NoKeys:
    """A secret store without keys: the real one would read the keyring."""

    def get_api_key(self, _provider: str) -> None:
        return None

    def get_api_key_source(self, _provider: str) -> str:
        return ""

    def has_api_key(self, _provider: str) -> bool:
        return False


def settings_case() -> None:
    from PySide6 import QtCore, QtWidgets

    from stt_app.logger import AppLogger
    from stt_app.settings_dialog import SettingsDialog
    from stt_app.settings_store import SettingsStore

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    made = []

    def build_and_show() -> None:
        dialog = SettingsDialog(SettingsStore(), _NoKeys(), AppLogger())
        dialog.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
        dialog.show()
        app.processEvents()
        made.append(dialog)

    measure("settings dialog: build and show", build_and_show)
    measure("settings dialog: close", lambda: (made[0].close(), app.processEvents()))


def silero_case() -> None:
    from stt_app import silero_vad

    measure("silero speech check: build the session", silero_vad._build_session)


def idle_case() -> None:
    measure("idle (the ticker alone)", lambda: time.sleep(2.0))


def native_case() -> None:
    measure("one native call that keeps the lock", lambda: sum(range(60_000_000)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "cases", nargs="+", help="idle, native, silero, settings, model:<size>"
    )
    args = parser.parse_args()
    sys.stdout.write(f"sandbox: {SANDBOX}\n")
    for case in args.cases:
        if case == "idle":
            idle_case()
        elif case == "native":
            native_case()
        elif case == "silero":
            silero_case()
        elif case == "settings":
            settings_case()
        elif case.startswith("model:"):
            model_case(case.partition(":")[2])
        else:
            parser.error(f"unknown case: {case}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
