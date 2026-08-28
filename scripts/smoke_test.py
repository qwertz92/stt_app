from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


if TYPE_CHECKING:  # pragma: no cover - import-time typing only
    from stt_app.settings_store import AppSettings


def _read_settings_without_touching_them() -> tuple[AppSettings | None, str | None]:
    """Load the saved settings from a throwaway copy.

    Returns `(settings, problem)`. `settings` is `None` only when there is
    nothing to read; `problem` describes a configuration this install cannot
    use, and both can be set at once when a file exists but does not parse.

    Two writes have to be kept off the real install, and the second is easy
    to miss because it happens one level above the file:

    - `SettingsStore.load` is not read-only. It writes a fresh file when none
      exists, rewrites the file whenever the stored payload differs from the
      normalized one, and renames both the file and its `.bak` to
      `*.corrupt.<timestamp>` when the JSON will not parse. Reading a copy
      keeps all three off the real file.
    - `app_paths.settings_path` is not a lookup. It goes through
      `appdata_root`, which creates the data folder and, when only the legacy
      one exists, renames the user's entire data directory onto the current
      name. `existing_settings_path` answers the same question and touches
      nothing.

    Reporting the corrupt case matters as much as not causing it. Loading a
    copy makes a broken settings file *invisible*: the copy is repaired, the
    defaults come back, and the script would print a clean bill of health --
    and then describe the default model as "the configured local model" and
    offer to download it. The old code was equally silent but at least
    quarantined the real file, so the user found out.
    """
    from stt_app.app_paths import existing_settings_path
    from stt_app.settings_store import SettingsStore

    real_path = existing_settings_path()
    if real_path is None:
        return None, None

    try:
        json.loads(real_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"the saved settings file cannot be read ({exc})"

    with tempfile.TemporaryDirectory() as temp_dir:
        copy_path = Path(temp_dir) / "settings.json"
        shutil.copy2(real_path, copy_path)
        return SettingsStore(copy_path).load(), None


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
        help="Load the configured local model (may download it; skipped for remote engines).",
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
        # The configured model, not a hardcoded `small`. Hardcoding it meant
        # the smoke test could pass while the runtime this install actually
        # transcribes with was never exercised -- and on a clean machine it
        # pulled 486 MB of a model the default configuration does not use.
        # `DEFAULT_ENGINE` happens to be "local" today, but the branch below
        # asks "is this the engine with a model to load", which is a different
        # question. Reading the default would silently invert this branch
        # the day the default becomes a provider.
        local_engine = "local"

        problem: str | None = None
        try:
            settings, problem = _read_settings_without_touching_them()
        except Exception as exc:
            settings = None
            problem = f"the saved settings could not be read ({exc})"
        if problem:
            optional_failures.append(f"Could not read the saved settings: {problem}")

        if settings is None:
            # Print a step line here too. Without one this branch is the only
            # path through the check that produces no `[4/5]` output at all,
            # so a reader of the log sees the step vanish rather than fail.
            print("[4/5] Skipping model load: no readable saved settings")
        elif settings.engine != local_engine:
            # Only the local engine has a model to load. Every remote provider
            # builds from an API key this script does not read, and none of
            # them implements `preload_model` at all, so running the step
            # against one reports a failure that says nothing about the
            # install.
            print(
                f"[4/5] Skipping model load: the configured engine is "
                f"'{settings.engine}', which transcribes remotely"
            )
        else:
            from stt_app.transcriber.factory import create_transcriber

            print(f"[4/5] Checking local model load ({settings.model_size})")
            try:
                transcriber = create_transcriber(settings)
                transcriber.preload_model()
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
