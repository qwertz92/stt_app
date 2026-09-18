"""Smoke-test a built PyInstaller bundle without touching the real install.

WHAT IT CHECKS
Against `stt_app.exe` from a `onedir` build, in this order:

  1. the frozen inventory-scan worker lists the cached local models,
  2. the frozen benchmark worker transcribes one clip with one model per
     local runtime that is cached (onnx-asr, faster-whisper, ONNX Runtime
     GenAI, Node/ONNX, the Granite CTC graph), and every case has to produce
     text,
  3. with `--node-models`, the same worker runs all three Node.js models on
     webgpu and on cpu, which is the pass that exercises the bundled Node
     runtime and Transformers.js on both devices,
  4. unless `--no-gui`, the frozen GUI itself is started for 40 seconds and
     its own log is read back.

WHY A FAKE CANNOT REPLACE IT
The whole class of defect here is "works from source, not from the bundle":
a hidden import PyInstaller's module graph could not see, package *data*
loaded through `importlib.resources` that was never collected (onnx-asr's
mel and resampler graphs are exactly that), a worker entry point whose
argument was wired into the source path but not into the frozen one, the
Node runner and its `node_modules` missing from the bundle. None of it is
visible to a test that imports `stt_app` from `src`, because there the files
are simply there.

WHAT IT TOUCHES
* Every child runs with `APPDATA` and `LOCALAPPDATA` pointed at a throwaway
  folder under %TEMP%, `HF_HUB_OFFLINE=1` and the ModelScope mirror switched
  off, so nothing is downloaded and the real settings, history and recordings
  are unreachable. The user's Hugging Face model cache is only read.
* The GUI pass puts a tray icon on screen for 40 seconds and then kills it.
  It registers global hotkeys, which the running copy of the app already
  holds -- those errors are expected and are reported as such.
* No network beyond what a model load would attempt, which offline mode
  refuses.

HOW LONG IT TAKES
About 25 seconds with `--no-gui` and no `--node-models`; about a minute more
with `--node-models`, and 40 seconds more with the GUI pass.

LANGUAGE
`--language` defaults to `auto`, which reaches the worker as "no language" --
faster-whisper then detects it, Parakeet ignores it, and Cohere falls back to
German because it has no auto mode at all. Name the clip's real language when
you know it; the numbers are then comparable with the benchmark history.

COMMAND LINE
    .venv\\Scripts\\python.exe scripts\\release_check_frozen_bundle.py \\
        --exe dist\\stt_app\\stt_app.exe --clip C:\\clips\\de_sample.wav \\
        --language de --report out.json
    ... --no-gui          leave the GUI pass out
    ... --node-models     add the Node.js models on webgpu and cpu
    ... --model-dir PATH  scan and load from this Model Dir instead of the
                          Hugging Face cache (the Granite CTC graph is English
                          only, so pair it with an English clip)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _release_check_common as common

EVENT_PREFIX = "@@STTBENCH@@"

# One model per local runtime, in the order they are preferred. Only the
# first cached faster-whisper size is taken: they are one runtime, and a
# second size measures nothing new here.
PREFERRED = (
    "parakeet-tdt-0.6b-v3",  # onnx-asr, whose graphs are package data files
    "tiny",
    "base",
    "small",  # faster-whisper / CTranslate2
    "nemotron-3.5-asr-streaming-0.6b-int4",  # ONNX Runtime GenAI
    "cohere-transcribe-03-2026",  # Node.js and Transformers.js from the bundle
    # The app's own numpy features, the CPU ONNX Runtime and `tokenizers`.
    "granite-speech-5.0-470m-turboctc",
)
WHISPER_SIZES = ("tiny", "base", "small")
NODE_MODELS = (
    "cohere-transcribe-03-2026",
    "granite-speech-4.1-2b",
    "granite-4.0-1b-speech",
)
NODE_DEVICES = ("webgpu", "cpu")

SCAN_TIMEOUT_S = 180
BENCHMARK_TIMEOUT_S = 1500
GUI_SECONDS = 40


def benchmark_options(
    clip: Path, models: list[str], language: str, model_dir: str = ""
) -> dict[str, object]:
    """The worker's option dict.

    `auto` becomes `None`, exactly as the settings dialog does it: the string
    "auto" is not a language code and faster-whisper would choke on it, while
    `None` means "detect it".
    """
    return {
        "audio_path": str(clip),
        "model_names": models,
        "device": "auto",
        "compute_type": "int8",
        "runs": 1,
        "beam_size": 5,
        "language": None if language == "auto" else language,
        "vad_filter": False,
        "warmup": False,
        "threads": 0,
        "model_dir": model_dir,
    }


def parse_benchmark_events(
    stdout: str,
) -> tuple[list[dict[str, object]], list[str], list[str]]:
    """Pull the case events out of the worker's stdout.

    Anything without the prefix is library noise (faster-whisper and ONNX
    Runtime both print to stdout) and is ignored, which is what the app's own
    parent launcher does too.
    """
    cases: list[dict[str, object]] = []
    kinds: list[str] = []
    errors: list[str] = []
    for line in stdout.splitlines():
        if not line.startswith(EVENT_PREFIX):
            continue
        try:
            event = json.loads(line[len(EVENT_PREFIX) :])
        except ValueError:
            errors.append(f"unparsable event line: {line[:200]}")
            continue
        kinds.append(str(event.get("event")))
        if event.get("event") == "case":
            case = event.get("case") or {}
            runs = case.get("runs") or []
            first_run = runs[0] if runs else {}
            cases.append(
                {
                    "model": case.get("model"),
                    "device": case.get("device"),
                    "error": case.get("error"),
                    "load_seconds": round(case.get("load_seconds") or 0.0, 2),
                    "rtf": [round(run.get("real_time_factor", 0.0), 3) for run in runs],
                    "transcript": str(first_run.get("transcript") or ""),
                    "runtime_details": str(case.get("runtime_details") or "")[:300],
                }
            )
        elif event.get("event") == "error":
            errors.append(str(event.get("message")))
    return cases, kinds, errors


def run_benchmark_pass(
    checks: common.Checks,
    label: str,
    exe: Path,
    sandbox: Path,
    options: dict[str, object],
) -> dict[str, object]:
    options_path = sandbox / f"{label}_options.json"
    options_path.write_text(json.dumps(options), encoding="utf-8")
    started = time.perf_counter()
    try:
        # `run_child`, not `subprocess.run`: the worker starts a Node child
        # for the Cohere and Granite models, and a timeout that ends only the
        # worker leaves that child running with the model loaded.
        completed = common.run_child(
            [str(exe), "--local-benchmark-worker", "--options", str(options_path)],
            env=common.child_environment(sandbox),
            timeout_s=BENCHMARK_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        checks.failed(
            f"frozen.{label}.worker_exits_cleanly",
            f"no exit within {BENCHMARK_TIMEOUT_S}s: {exc}",
        )
        return {"timed_out": True}

    cases, kinds, errors = parse_benchmark_events(completed.stdout)
    seconds = round(time.perf_counter() - started, 2)
    checks.verdict(
        f"frozen.{label}.worker_exits_cleanly",
        completed.returncode == 0 and not errors,
        f"exit={completed.returncode} {seconds}s events={sorted(set(kinds))} "
        f"worker_errors={[common.ascii_safe(item)[:160] for item in errors]}",
    )
    requested = list(options["model_names"])  # type: ignore[arg-type]
    measured = {str(case["model"]) for case in cases}
    checks.verdict(
        f"frozen.{label}.every_requested_model_produced_a_case",
        measured >= set(requested),
        f"requested={requested} measured={sorted(measured)}",
    )
    for case in cases:
        text = str(case["transcript"])
        detail = (
            f"rtf={case['rtf']} load={case['load_seconds']}s chars={len(text)} "
            f'text="{common.ascii_safe(" ".join(text.split()))[:60]}"'
        )
        if case["error"]:
            detail = f"error={common.ascii_safe(case['error'])[:200]}"
        checks.verdict(
            f"frozen.{label}.{case['model']}.{case['device']}",
            not case["error"] and bool(text.strip()),
            detail,
        )
    return {
        "exit": completed.returncode,
        "seconds": seconds,
        "models_requested": requested,
        "event_kinds": sorted(set(kinds)),
        "cases": cases,
        "worker_errors": errors,
        "stderr_tail": completed.stderr[-1200:],
    }


def scan_pass(
    checks: common.Checks, exe: Path, sandbox: Path, model_dir: str = ""
) -> list[str]:
    scan_out = sandbox / "scan.json"
    command = [str(exe), "--local-model-scan-worker", "--output", str(scan_out)]
    if model_dir:
        command += ["--model-dir", model_dir]
    started = time.perf_counter()
    try:
        completed = common.run_child(
            command,
            env=common.child_environment(sandbox),
            timeout_s=SCAN_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        checks.failed("frozen.scan.worker_exits_cleanly", f"no exit: {exc}")
        checks.skipped("frozen.scan.lists_the_cached_models", "the worker did not exit")
        return []
    cached: list[str] = []
    # A worker that crashed or was killed while writing leaves half a file,
    # and that is a finding about the bundle, not a reason to end the run.
    unreadable = ""
    if scan_out.exists():
        try:
            payload = json.loads(scan_out.read_text(encoding="utf-8"))
            cached = [str(name) for name in payload.get("cached_models") or []]
        except (OSError, ValueError, AttributeError, TypeError) as exc:
            unreadable = f" unreadable_output={type(exc).__name__}: {exc}"[:200]
    seconds = round(time.perf_counter() - started, 2)
    checks.verdict(
        "frozen.scan.worker_exits_cleanly",
        completed.returncode == 0,
        f"exit={completed.returncode} {seconds}s "
        f"stderr={common.ascii_safe(completed.stderr[-200:])}",
    )
    checks.verdict(
        "frozen.scan.lists_the_cached_models",
        bool(cached),
        f"{len(cached)} cached: {cached}{unreadable}",
    )
    checks.details["scan"] = {
        "exit": completed.returncode,
        "seconds": seconds,
        "cached_models": cached,
        "stderr_tail": completed.stderr[-600:],
    }
    return cached


def gui_pass(checks: common.Checks, exe: Path, sandbox: Path) -> None:
    settings_dir = sandbox / "appdata" / "stt_app"
    settings_dir.mkdir(parents=True, exist_ok=True)
    (settings_dir / "settings.json").write_text(
        json.dumps({"schema_version": 23, "offline_mode": True}), encoding="utf-8"
    )
    gui = subprocess.Popen([str(exe)], env=common.child_environment(sandbox))
    log_path = settings_dir / "logs" / "dictation.log"
    log_text = ""
    # In a `finally`: the started app has a tray icon and tries to take global
    # hotkeys, so an interrupt or an error during the wait must not leave it
    # running after the script is gone.
    try:
        deadline = time.monotonic() + GUI_SECONDS
        while time.monotonic() < deadline:
            time.sleep(1.0)
            if gui.poll() is not None:
                break
        exit_code = gui.poll()
        if log_path.exists():
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
    finally:
        if gui.poll() is None:
            common.kill_process_tree(gui.pid)
    lines = log_text.splitlines()
    errors = [line for line in lines if "[ERROR]" in line]
    # A second instance cannot take global hotkeys the running app holds, so
    # those errors say nothing about the bundle.
    unexpected = [line for line in errors if "hotkey" not in line.lower()]
    checks.verdict(
        f"frozen.gui.stays_up_for_{GUI_SECONDS}s",
        exit_code is None,
        f"early_exit_code={exit_code}",
    )
    checks.verdict(
        "frozen.gui.writes_its_own_log",
        bool(lines),
        f"{len(lines)} log lines, {len(log_text)} bytes",
    )
    checks.verdict(
        "frozen.gui.no_error_in_the_log_beyond_hotkey_registration",
        not unexpected,
        f"{len(errors)} error lines, {len(unexpected)} of them unexpected: "
        + common.ascii_safe(unexpected[0][:200] if unexpected else "none"),
    )
    checks.details["gui"] = {
        "early_exit_code": exit_code,
        "log_bytes": len(log_text),
        "log_line_count": len(lines),
        "log_lines": [
            common.ascii_safe(line[:260])
            for line in lines
            if any(
                key in line
                for key in (
                    "ERROR",
                    "WARNING",
                    "Traceback",
                    "hotkey",
                    "preload",
                    "tray",
                    "Model",
                    "update",
                    "inventory",
                    "keyring",
                    "startup",
                )
            )
        ][:80],
        "appdata_listing": sorted(
            str(path.relative_to(sandbox)) for path in (sandbox / "appdata").rglob("*")
        )[:40],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--exe",
        required=True,
        type=common.existing_file,
        metavar="PATH",
        help="stt_app.exe from a PyInstaller onedir build.",
    )
    parser.add_argument(
        "--clip",
        required=True,
        type=common.existing_file,
        metavar="PATH",
        help=(
            "A real speech recording. The repository's own "
            "samples/benchmark_sample.wav is synthetic sine tones and every "
            "model would return nothing for it."
        ),
    )
    parser.add_argument(
        "--language",
        default="auto",
        help="Language of the clip (default: auto, which means 'detect it').",
    )
    parser.add_argument(
        "--model-dir",
        default=None,
        type=common.existing_directory,
        metavar="PATH",
        help=(
            "A Model Dir to scan and load from, as the app's setting of that "
            "name. Default: the Hugging Face cache. It is only read."
        ),
    )
    parser.add_argument(
        "--no-gui",
        action="store_true",
        help="Leave out the pass that starts the frozen GUI for 40 seconds.",
    )
    parser.add_argument(
        "--node-models",
        action="store_true",
        help=(
            "Add a pass over all three Node.js models on webgpu and on cpu "
            "(about a minute)."
        ),
    )
    common.add_report_argument(parser)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    sandbox = common.make_sandbox("stt_release_frozen_")
    checks = common.Checks("frozen bundle", report_path=args.report)
    checks.details["exe"] = str(args.exe)
    checks.details["clip"] = str(args.clip)
    checks.details["sandbox"] = str(sandbox)
    sys.stdout.write(f"sandbox: {sandbox}\n")
    model_dir = str(args.model_dir) if args.model_dir is not None else ""
    checks.details["model_dir"] = model_dir

    cached = scan_pass(checks, args.exe, sandbox, model_dir)

    whisper = next((name for name in WHISPER_SIZES if name in cached), None)
    models = [
        name
        for name in PREFERRED
        if name in cached and (name not in WHISPER_SIZES or name == whisper)
    ]
    if not models:
        checks.skipped(
            "frozen.benchmark.worker_exits_cleanly",
            "none of the preferred models is cached on this machine",
        )
    else:
        checks.details["benchmark"] = run_benchmark_pass(
            checks,
            "benchmark",
            args.exe,
            sandbox,
            benchmark_options(args.clip, models, args.language, model_dir),
        )

    if args.node_models:
        node_models = [name for name in NODE_MODELS if name in cached]
        if not node_models:
            checks.skipped(
                "frozen.node.worker_exits_cleanly",
                "no Node.js model is cached on this machine",
            )
        else:
            options = benchmark_options(
                args.clip, node_models, args.language, model_dir
            )
            options["webgpu_devices"] = list(NODE_DEVICES)
            checks.details["node"] = run_benchmark_pass(
                checks, "node", args.exe, sandbox, options
            )

    if args.no_gui:
        for name in (
            f"frozen.gui.stays_up_for_{GUI_SECONDS}s",
            "frozen.gui.writes_its_own_log",
            "frozen.gui.no_error_in_the_log_beyond_hotkey_registration",
        ):
            checks.skipped(name, "--no-gui was given")
    else:
        try:
            gui_pass(checks, args.exe, sandbox)
        except Exception as exc:
            checks.crashed("frozen.gui.pass_crashed", exc)

    return checks.finish()


if __name__ == "__main__":
    common.run_main(main)
