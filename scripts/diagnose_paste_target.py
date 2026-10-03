"""Show what the paste target check reads in the window you click into.

After a paste the app asks whether the focused element shows a caret and,
when it finds none in a Chromium or Electron window, reports the paste as
"not in a text field" (`stt_app.paste_target_check`). This script runs that
same check -- the production worker thread, reader and rechecks -- against
whatever has the keyboard focus, without pasting anything, so a wrong
verdict in a real application can be seen and reported.

Usage (Windows, from the repository root):

    .venv\\Scripts\\python.exe scripts\\diagnose_paste_target.py

Then, within the countdown (5 seconds by default), click into the place you
want measured -- a chat prompt, an editor, a terminal, a browser text field
-- and leave the mouse and keyboard alone until the readings are done. Each
of the 10 readings prints the verdict and its evidence:

    text_field       the focused element shows a caret
    not_text_field   a Chromium window whose focus shows no caret; a paste
                     there is reported as doubtful
    unknown          the check cannot tell; a paste there is reported as
                     before the check existed

The evidence holds window class names and caret answers only, never window
titles or text, so the output can be pasted into a bug report as it is.
Nothing is pasted, typed or clicked, the clipboard is not touched, and the
app's settings folder is not read: APPDATA points at a throwaway folder that
is deleted on exit.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"

DEFAULT_DELAY_SECONDS = 5
DEFAULT_COUNT = 10
DEFAULT_INTERVAL_SECONDS = 0.5
# Longer than one check with every recheck (about 0.4 s); a reading that has
# not arrived by then is printed as missing rather than waited for.
READING_TIMEOUT_SECONDS = 5.0


def _non_negative(kind):
    """An argparse type that refuses a negative number with a clear message."""

    def parse(text: str):
        try:
            value = kind(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
        if value < 0:
            raise argparse.ArgumentTypeError("must not be negative")
        return value

    return parse


def _parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--delay",
        type=_non_negative(int),
        default=DEFAULT_DELAY_SECONDS,
        help="seconds to click into the target before the first reading",
    )
    parser.add_argument(
        "--count",
        type=_non_negative(int),
        default=DEFAULT_COUNT,
        help="number of readings",
    )
    parser.add_argument(
        "--interval",
        type=_non_negative(float),
        default=DEFAULT_INTERVAL_SECONDS,
        help="seconds between readings",
    )
    return parser.parse_args(argv)


def _sandbox_environment(sandbox: Path) -> None:
    """Point this process away from the real install before importing it."""
    os.environ["APPDATA"] = str(sandbox / "appdata")
    os.environ["LOCALAPPDATA"] = str(sandbox / "localappdata")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["STT_APP_DISABLE_MODELSCOPE"] = "1"
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))


def _read_once(check) -> tuple[object | None, float, str]:
    """One check through the production worker.

    Returns (reading, milliseconds, why it is missing); the reason is ""
    when a reading arrived.
    """
    done = threading.Event()
    readings = []
    started = time.perf_counter()
    if not check.request(lambda reading: (readings.append(reading), done.set())):
        return None, 0.0, "no answer (the previous check is still running)"
    done.wait(READING_TIMEOUT_SECONDS)
    elapsed_ms = (time.perf_counter() - started) * 1000
    if not readings:
        return None, elapsed_ms, f"no answer within {READING_TIMEOUT_SECONDS:g} s"
    return readings[0], elapsed_ms, ""


def _run(arguments: argparse.Namespace) -> int:
    from stt_app.paste_target_check import PasteTargetCheck

    for remaining in range(arguments.delay, 0, -1):
        print(f"Click into the target now: first reading in {remaining} s", flush=True)
        time.sleep(1)
    verdicts: Counter[str] = Counter()
    check = PasteTargetCheck()
    try:
        for index in range(1, arguments.count + 1):
            reading, elapsed_ms, missing = _read_once(check)
            if reading is None:
                verdicts["no answer"] += 1
                print(f"{index:2d}  {missing}", flush=True)
            else:
                verdicts[reading.verdict] += 1
                print(
                    f"{index:2d}  verdict={reading.verdict} {reading.evidence()} "
                    f"ms={elapsed_ms:.1f}",
                    flush=True,
                )
            if index < arguments.count:
                time.sleep(arguments.interval)
    finally:
        check.close()
    summary = ", ".join(
        f"{verdict} {count}" for verdict, count in sorted(verdicts.items())
    )
    print(f"SUMMARY {summary}")
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = _parse_arguments(argv)
    if sys.platform != "win32":
        print("This diagnostic reads the Windows desktop and runs on Windows only.")
        return 2
    with tempfile.TemporaryDirectory(prefix="stt_app_paste_target_") as sandbox:
        _sandbox_environment(Path(sandbox))
        return _run(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
