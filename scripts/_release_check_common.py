"""Shared plumbing for the four `release_check_*` scripts.

Those four scripts each exercise something on the real machine that no test
double can stand in for: the real Windows clipboard, real exclusive file
locks, real provider APIs, a real PyInstaller bundle. Only the boring part is
common to all of them, and it lives here so that it exists exactly once:

* a throwaway `APPDATA` / `LOCALAPPDATA` sandbox, so a check can neither read
  nor write the user's own settings, history and recordings,
* the one-line-per-check printer and the exit code made out of its counts,
* the JSON report writer.

Nothing here imports `stt_app` or `huggingface_hub`, so a script may import
this module before it sets its sandbox up -- which it must, because both of
those read the environment at import time.

Exit codes, shared by all four scripts:

* 0 -- every check that ran is OK (skipped checks do not fail a run),
* 1 -- at least one check failed,
* 2 -- the machine cannot run the check at all (wrong operating system, a
  missing package, a file that is not there), or the command line was wrong.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_PREREQUISITE = 2

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"


class MissingPrerequisite(RuntimeError):
    """This machine cannot run the check at all.

    Raised for a wrong operating system, a package that is not installed or a
    file the caller named that does not exist -- never for a check that ran
    and came out negative. `run_main` turns it into exit code 2, which is what
    tells a release run "this was not measured" apart from "this is broken".
    """


def add_src_to_sys_path() -> Path:
    """Make `import stt_app` work from a source checkout, and return its root.

    The scripts locate the repository relative to their own file, so none of
    them carries a path from the machine they were written on.
    """
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    return PROJECT_ROOT


def make_sandbox(prefix: str) -> Path:
    """Create a throwaway folder with an `appdata` and a `localappdata` in it.

    The folder is deliberately left behind: a failed check is read afterwards,
    and every script prints the path it used. It lives under `%TEMP%`.
    """
    root = Path(tempfile.mkdtemp(prefix=prefix))
    (root / "appdata").mkdir()
    (root / "localappdata").mkdir()
    return root


def redirect_this_process_to(sandbox: Path) -> None:
    """Point this process at the sandbox, before the first `stt_app` import.

    `app_paths.appdata_root()` reads `APPDATA` when it is first asked, and
    `huggingface_hub` freezes `HF_HUB_CACHE` and `HF_HUB_OFFLINE` into module
    constants at *import* time. Setting either of them after the import has
    happened changes nothing, which is why every script calls this above its
    own `stt_app` imports and marks those imports `# noqa: E402`.

    `HF_HUB_OFFLINE=1` is what keeps a check from starting a multi-gigabyte
    model download on a machine where the model is not cached.
    """
    os.environ["APPDATA"] = str(sandbox / "appdata")
    os.environ["LOCALAPPDATA"] = str(sandbox / "localappdata")
    os.environ["HF_HUB_OFFLINE"] = "1"


def child_environment(sandbox: Path) -> dict[str, str]:
    """The environment for a child process that must not see the real install.

    Same sandbox as `redirect_this_process_to`, plus the ModelScope mirror
    switched off, so a child that cannot find a model fails instead of
    fetching it from the fallback host. `PYTHONPATH`, `PYTHONHOME` and
    `VIRTUAL_ENV` are dropped because a frozen executable must run on its own
    bundled interpreter, not on the one this script happens to sit in.
    """
    env = dict(os.environ)
    env["APPDATA"] = str(sandbox / "appdata")
    env["LOCALAPPDATA"] = str(sandbox / "localappdata")
    env["HF_HUB_OFFLINE"] = "1"
    env["STT_APP_DISABLE_MODELSCOPE"] = "1"
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        env.pop(name, None)
    return env


def ascii_safe(value: object) -> str:
    """Render anything as a string a redirected Windows log can hold.

    `sys.stdout` becomes cp1252 the moment the output is redirected, so a
    German transcript printed as it stands raises `UnicodeEncodeError` and
    ends the run. `backslashreplace` keeps the character visible as an escape
    instead of losing it.
    """
    return str(value).encode("ascii", "backslashreplace").decode("ascii")


def add_report_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Write the full evidence as JSON to PATH. Without it the run "
            "prints its check lines and nothing else is kept."
        ),
    )


def existing_file(raw: str) -> Path:
    """An `argparse` type for a file that has to be there already."""
    path = Path(raw).expanduser()
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"no such file: {path}")
    return path


class Checks:
    """One printed line per check, and the exit code made out of the counts.

    `ok=True` prints `OK`, `ok=False` prints `FAIL`, and `ok=None` prints
    `SKIP` -- the third one is for a case that could not be measured (no API
    key stored, Windows refused the foreground) and it must never turn into a
    failure, or a release run reports a problem the code does not have.
    """

    def __init__(self, title: str) -> None:
        self.title = title
        self.items: list[dict[str, object]] = []
        self.details: dict[str, object] = {}
        self.started_at = datetime.now(UTC).isoformat(timespec="seconds")

    def record(self, name: str, ok: bool | None, detail: object = "") -> bool:
        text = ascii_safe(detail)
        tag = "SKIP" if ok is None else ("OK  " if ok else "FAIL")
        self.items.append({"name": name, "ok": ok, "detail": text})
        sys.stdout.write(f"{tag} {name}: {text}\n")
        sys.stdout.flush()
        return ok is True

    def passed(self, name: str, detail: object = "") -> bool:
        return self.record(name, True, detail)

    def failed(self, name: str, detail: object = "") -> bool:
        return self.record(name, False, detail)

    def skipped(self, name: str, reason: object) -> bool:
        return self.record(name, None, reason)

    def verdict(self, name: str, ok: bool, detail: object = "") -> bool:
        return self.record(name, bool(ok), detail)

    def crashed(self, name: str, exc: BaseException) -> bool:
        return self.record(name, False, f"{type(exc).__name__}: {exc}")

    def counts(self) -> tuple[int, int, int]:
        ok = sum(1 for item in self.items if item["ok"] is True)
        failed = sum(1 for item in self.items if item["ok"] is False)
        skipped = sum(1 for item in self.items if item["ok"] is None)
        return ok, failed, skipped

    def finish(self, report_path: Path | None = None) -> int:
        ok, failed, skipped = self.counts()
        verdict = "FAILED" if failed else "PASSED"
        sys.stdout.write(
            f"SUMMARY {self.title}: {len(self.items)} checks, {ok} OK, "
            f"{failed} FAIL, {skipped} SKIP -- {verdict}\n"
        )
        if report_path is not None:
            payload = {
                "title": self.title,
                "started_at": self.started_at,
                "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "counts": {"ok": ok, "fail": failed, "skip": skipped},
                "checks": self.items,
                "details": self.details,
            }
            write_json_report(report_path, payload)
            sys.stdout.write(f"report written to {report_path}\n")
        sys.stdout.flush()
        return EXIT_FAILED if failed else EXIT_OK


def write_json_report(path: Path, payload: dict[str, object]) -> None:
    """Write the evidence as ASCII JSON.

    `ensure_ascii=True` so the file survives every editor and every terminal
    that opens it later; `default=str` so one value the writer did not expect
    cannot cost the whole report.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=True, indent=2, default=str)
    path.write_text(text + "\n", encoding="ascii", newline="\n")


def run_main(entry: Callable[[], int]) -> None:
    """Call `entry` and exit, turning a missing prerequisite into code 2."""
    try:
        code = entry()
    except MissingPrerequisite as exc:
        sys.stdout.write(f"PREREQUISITE {ascii_safe(exc)}\n")
        raise SystemExit(EXIT_PREREQUISITE) from None
    raise SystemExit(code)
