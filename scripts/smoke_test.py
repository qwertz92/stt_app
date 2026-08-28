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

    The check has to match what `SettingsStore` accepts, not merely "is this
    JSON". It requires a top-level **object**, so `[]`, `null` and `5` all
    parse here and are rejected there -- which reinstated the silent-defaults
    path for exactly those payloads. And it falls back to `settings.json.bak`,
    so a primary file that will not parse is not a broken install; reporting
    one was a false alarm that also skipped the check the user asked for. Both
    files are therefore copied, and the verdict is whichever the store reaches.
    """
    from stt_app.app_paths import existing_settings_path
    from stt_app.settings_store import SettingsStore

    real_path = existing_settings_path()
    if real_path is None:
        return None, None

    backup_path = real_path.with_suffix(real_path.suffix + ".bak")
    problems = []
    usable_path: Path | None = None
    for path, label in ((real_path, "settings file"), (backup_path, "backup")):
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"the saved {label} cannot be read ({exc})")
            continue
        if not isinstance(payload, dict):
            problems.append(
                f"the saved {label} is a JSON {type(payload).__name__}, not an "
                "object, so the app will discard it"
            )
            continue
        usable_path = path
        break

    if usable_path is None:
        return None, "; ".join(problems) or "the saved settings file is unreadable"

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_dir_path = Path(temp_dir)
        copy_path = temp_dir_path / "settings.json"
        # Whichever file parsed above, copied under the primary's name. Not
        # `real_path`: the loop reaches the backup precisely when the primary
        # raised `OSError`, and copying a file that just refused to be read
        # raises it again -- turning the one case the backup exists to rescue
        # into "reading the saved settings raised PermissionError(...)" and
        # `--strict` 1, for an install the app starts fine on. The file that
        # parsed cannot fail for that reason, and using it reproduces what the
        # app ends up running on, since the store's own recovery reads the
        # `.bak` and rewrites the primary from it.
        shutil.copy2(usable_path, copy_path)
        # No `.bak` is copied alongside. The store would only reach one if it
        # rejected the file that parsed here, and it cannot: both do
        # `json.loads(read_text("utf-8"))` and require a top-level object, and
        # this reader is the stricter of the two (it also demands `is_file()`
        # and catches every `ValueError`). A copy was made for a while under a
        # comment claiming the opposite; instrumenting `load_json_with_backup`
        # across eight payload shapes showed the source was always `primary`.
        settings = SettingsStore(copy_path).load()

    # A problem that the backup rescued is still worth naming: the app is
    # running on recovered settings and the primary file is damaged. It is
    # returned *alongside* the settings, so the model check the user asked for
    # still runs -- that is the half that used to be missing. `--strict` then
    # exits 1, which is what a flag named strict is for; without it the run
    # reports the damage as a WARN and returns 0.
    return settings, "; ".join(problems) or None


def _legacy_data_folder_would_be_moved() -> bool:
    """Is this install still in the pre-rename data folder?

    `appdata_root()` renames it onto the current name the first time
    anything asks for it, so this answers "would asking cause that move".
    Everything the model check touches -- the download coordinator's lock
    directory most of all -- goes through that call.
    """
    from stt_app.app_paths import existing_appdata_root
    from stt_app.config import LEGACY_APP_NAME

    root = existing_appdata_root()
    return root is not None and root.name == LEGACY_APP_NAME


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
        help=(
            "Load the configured local model (may download it; skipped for "
            "remote engines). Unlike the other checks this one runs the real "
            "load path, so it initializes the app data directory the same way "
            "starting the app would -- which is why it is declined outright "
            "while this install's data is still in the legacy folder."
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Return non-zero if optional checks fail.",
    )
    args = parser.parse_args()

    def step(message: str) -> None:
        """Print a step and flush it.

        Redirected stdout is block-buffered on Windows, so nothing reaches the
        log until the buffer fills or the process exits. The model check can
        wait indefinitely on the machine-wide download lock (another process
        downloading a model is exactly when a user runs a diagnostic), and a
        run killed during that wait produced a zero-byte log -- no indication
        of which step it died in.
        """
        print(message)
        sys.stdout.flush()

    step("[1/5] Import core modules")
    from stt_app.config import DEFAULT_HOTKEY
    from stt_app.hotkey import parse_hotkey
    from stt_app.secret_store import KeyringSecretStore
    from stt_app.settings_store import SettingsStore
    from stt_app.text_inserter import TextInserter

    step("[2/5] Basic initialization")
    with tempfile.TemporaryDirectory() as temp_dir:
        settings = SettingsStore(Path(temp_dir) / "settings.json").load()
        _ = settings.hotkey
    _ = KeyringSecretStore
    _ = TextInserter
    parse_hotkey(DEFAULT_HOTKEY)

    optional_failures: list[str] = []

    if args.check_mic:
        step("[3/5] Checking microphone devices")
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
        # `DEFAULT_ENGINE` is the engine with a model to load, and it is also
        # what `factory.create_transcriber` branches on, so reading it here is
        # not a coincidence to be avoided -- writing "local" beside it was a
        # second copy of one constant, and on the hypothetical day the default
        # moves it is the copy that would be wrong.
        from stt_app.config import (
            DEFAULT_ENGINE,
            DEFAULT_MODEL_SIZE,
            LEGACY_APP_NAME,
        )

        problem: str | None = None
        try:
            settings, problem = _read_settings_without_touching_them()
        except Exception as exc:
            settings = None
            problem = f"reading the saved settings raised {exc!r}"
        if problem:
            optional_failures.append(f"Settings problem: {problem}")

        # Read before the announcement below, not after: the legacy guard
        # declines the check outright, and printing "checking the default
        # model (X)" in front of "Skipping model load" is a straight
        # self-contradiction. It reads the same answer the guard reads.
        legacy_data_move_pending = _legacy_data_folder_would_be_moved()

        if settings is None:
            # Two ways to get here, and the app does the same thing in both:
            # it runs on defaults. No settings file at all is a fresh install;
            # an unusable one is quarantined by `SettingsStore.load`, which
            # then writes defaults. So the default model is what this machine
            # would load either way, and skipping the check silently returned
            # 0 from the one check the user invoked. Reporting the unusable
            # file and *then* declining to check anything was the same gap in
            # a second place -- that message already says "the app will
            # discard it", which names exactly why the defaults are the right
            # thing to check.
            from stt_app.settings_store import AppSettings

            settings = AppSettings()
            if not legacy_data_move_pending:
                if problem:
                    print(
                        f"The saved settings cannot be used, so the app would "
                        f"discard them and run on defaults; checking the default "
                        f"model ({DEFAULT_MODEL_SIZE})."
                    )
                else:
                    print(
                        f"No saved settings yet; checking the default model "
                        f"({DEFAULT_MODEL_SIZE}), which is what this machine "
                        f"would use."
                    )

        if legacy_data_move_pending:
            # Loading a model reaches the download coordinator, whose lock
            # directory is `appdata_root()` -- and that is a *setup* call: with
            # only the legacy folder present it renames the user's entire data
            # directory onto the current name. A diagnostic that migrates
            # settings, history and recordings is exactly the side effect
            # `_read_settings_without_touching_them` exists to avoid, one
            # level further out, and the model check reintroduced it through a
            # call chain no grep of this script reveals.
            step(
                "[4/5] Skipping model load: this install's data is still in "
                f"the legacy '{LEGACY_APP_NAME}' folder"
            )
            print(
                "Loading a model would move it. Start the app once to migrate "
                "it, then re-run this check."
            )
        elif settings.engine != DEFAULT_ENGINE:
            # Only the local engine has a model to load. Every remote provider
            # builds from an API key this script does not read, and none of
            # them implements `preload_model` at all, so running the step
            # against one reports a failure that says nothing about the
            # install.
            step(
                f"[4/5] Skipping model load: the configured engine is "
                f"'{settings.engine}', which transcribes remotely"
            )
        else:
            from stt_app.transcriber.factory import create_transcriber

            step(f"[4/5] Checking local model load ({settings.model_size})")
            try:
                transcriber = create_transcriber(settings)
                transcriber.preload_model()
                print("Model load succeeded")
            except Exception as exc:
                optional_failures.append(f"Model load failed: {exc}")

    step("[5/5] Smoke test complete")

    if optional_failures:
        for failure in optional_failures:
            print(f"WARN: {failure}")
        if args.strict:
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
