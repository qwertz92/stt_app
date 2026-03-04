from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def main() -> int:
    parser = argparse.ArgumentParser(description="Windows smoke test for stt_app")
    parser.add_argument(
        "--check-mic",
        action="store_true",
        help="Probe default input device via sounddevice.",
    )
    parser.add_argument(
        "--check-model",
        action="store_true",
        help="Load faster-whisper model metadata (may download model).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Return non-zero if optional checks fail.",
    )
    args = parser.parse_args()

    print("[1/5] Import core modules")
    from stt_app.config import DEFAULT_HOTKEY
    from stt_app.hotkey import parse_hotkey
    from stt_app.secret_store import KeyringSecretStore
    from stt_app.settings_store import SettingsStore
    from stt_app.text_inserter import TextInserter
    from stt_app.transcriber.local_faster_whisper import LocalFasterWhisperTranscriber

    print("[2/5] Basic initialization")
    with tempfile.TemporaryDirectory() as temp_dir:
        settings = SettingsStore(Path(temp_dir) / "settings.json").load()
        _ = settings.hotkey
    _ = KeyringSecretStore
    _ = TextInserter
    parse_hotkey(DEFAULT_HOTKEY)

    optional_failures: list[str] = []

    if args.check_mic:
        print("[3/5] Checking microphone devices")
        try:
            import sounddevice as sd

            input_devices = [d for d in sd.query_devices() if d.get("max_input_channels", 0) > 0]
            print(f"Found {len(input_devices)} input device(s)")
            if not input_devices:
                optional_failures.append("No input devices detected.")
        except Exception as exc:
            optional_failures.append(f"Microphone probe failed: {exc}")

    if args.check_model:
        print("[4/5] Checking faster-whisper model load")
        try:
            transcriber = LocalFasterWhisperTranscriber(model_size="small")
            transcriber._ensure_model()
            print("Model load succeeded")
        except Exception as exc:
            optional_failures.append(f"Model load failed: {exc}")

    print("[5/5] Smoke test complete")

    if optional_failures:
        for failure in optional_failures:
            print(f"WARN: {failure}")
        if args.strict:
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
