"""Call the real AssemblyAI and Groq services through the app's transcribers.

WHAT IT CHECKS
For every clip given on the command line:

  * AssemblyAI batch: upload, poll, transcript,
  * Groq batch: the same through a different SDK,
  * AssemblyAI streaming: the clip is pushed at real-time pace (100 ms of
    audio every 100 ms, exactly as the microphone delivers it), partials are
    counted, and `stop_stream()` has to return the final text.

and then, once and always last:

  * a quit during the AssemblyAI batch poll. `request_transcription_shutdown`
    sets a process-wide flag that is never cleared, so any case after it
    would measure the flag rather than the service -- which is why it is the
    last case and why the script ends after it.

A provider with no key in the Windows credential manager is SKIPPED, not
failed.

WHY A FAKE CANNOT REPLACE IT
The repository's provider tests run against fake SDK objects. They prove the
app's own logic -- the poll bound, the shutdown flag, the turn-order
bookkeeping -- against a service that behaves the way the test author
believed it does. They cannot notice a parameter the real service started
rejecting, a model identifier that was retired, an SDK upgrade that moved a
field, or a realtime handshake that now needs something else. Those are the
failures that only show up on the user's machine, with the user's key, after
a release.

WHAT IT TOUCHES
* The network, and the user's paid quota at both providers. Roughly one
  request per clip per provider plus one realtime session per clip.
* The API keys: read by the app's own `KeyringSecretStore` from the Windows
  credential manager and handed straight to `create_transcriber`. They are
  never printed, never logged and never written to the report.
* Nothing else. `APPDATA` and `LOCALAPPDATA` point at a throwaway folder
  under %TEMP% before the first `stt_app` import, so the real settings,
  history and recordings are unreachable.

CLIPS AND LANGUAGES
`--clip` is required and repeatable, because the repository's own
`samples/benchmark_sample.wav` is synthetic sine tones and no speech service
can return anything useful for it. Use real recordings; the streaming case
additionally needs 16 kHz mono PCM16, which is what the app itself records.

A clip may carry its own language as `PATH:LANG`, so one run can cover a
German and an English recording. The suffix after the last colon counts as a
language only when it is `auto` or a two-letter code (optionally with a
region, `de-AT`) AND the part before it is longer than one character -- so a
Windows drive letter is never mistaken for one. Everything else uses
`--language`, which defaults to `auto`.

HOW LONG IT TAKES
Roughly 30 to 60 seconds per clip, plus about the clip's own length again for
the streaming case, which runs at real-time pace on purpose.

COMMAND LINE
    .venv\\Scripts\\python.exe scripts\\release_check_providers.py \\
        --clip C:\\clips\\de_sample.wav:de \\
        --clip C:\\clips\\en_sample.wav:en \\
        --report out.json
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import threading
import time
import wave
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _release_check_common as common

SANDBOX = common.make_sandbox("stt_release_providers_")
common.redirect_this_process_to(SANDBOX)
common.add_src_to_sys_path()

from stt_app.secret_store import KeyringSecretStore  # noqa: E402
from stt_app.settings_store import AppSettings  # noqa: E402
from stt_app.transcriber import base as transcriber_base  # noqa: E402
from stt_app.transcriber.factory import create_transcriber  # noqa: E402

PROVIDERS = ("assemblyai", "groq")
LANGUAGE_TOKEN = re.compile(r"(?:auto|[A-Za-z]{2}(?:-[A-Za-z]{2})?)\Z")
PREVIEW_CHARS = 60

LOG_LINES: list[str] = []


class _Collector(logging.Handler):
    """Keep the app's own log so a failure can be read afterwards."""

    def emit(self, record: logging.LogRecord) -> None:
        LOG_LINES.append(
            f"{record.levelname} {record.name}: {record.getMessage()[:300]}"
        )


logging.getLogger().addHandler(_Collector())
logging.getLogger("stt_app").setLevel(logging.INFO)


def parse_clip(raw: str, default_language: str) -> tuple[Path, str]:
    """Split `PATH` or `PATH:LANG`, leaving Windows drive letters alone."""
    head, separator, tail = raw.rpartition(":")
    if separator and len(head) > 1 and LANGUAGE_TOKEN.match(tail):
        return Path(head).expanduser(), tail.lower()
    return Path(raw).expanduser(), default_language


def wav_info(path: Path) -> dict[str, object]:
    with wave.open(str(path), "rb") as handle:
        return {
            "seconds": round(handle.getnframes() / float(handle.getframerate()), 2),
            "rate": handle.getframerate(),
            "channels": handle.getnchannels(),
            "width": handle.getsampwidth(),
        }


def pcm16_mono_16k(path: Path) -> bytes:
    with wave.open(str(path), "rb") as handle:
        layout = (
            handle.getframerate(),
            handle.getnchannels(),
            handle.getsampwidth(),
        )
        if layout != (16000, 1, 2):
            raise RuntimeError(
                f"{path.name} is not 16 kHz mono PCM16: {wav_info(path)}"
            )
        return handle.readframes(handle.getnframes())


def settings_for(engine: str, language: str) -> AppSettings:
    return replace(AppSettings(), engine=engine, language_mode=language, mode="batch")


def close_quietly(transcriber, case: dict[str, object]) -> None:
    """Not every provider class has a `close`; AssemblyAI's has none."""
    if not hasattr(transcriber, "close"):
        return
    try:
        transcriber.close()
    except Exception as exc:
        case["close_error"] = f"{type(exc).__name__}: {exc}"[:200]


def preview(text: str) -> str:
    """A short, ASCII-safe look at the transcript; the report keeps it whole."""
    collapsed = " ".join(text.split())
    shortened = collapsed[:PREVIEW_CHARS]
    suffix = "..." if len(collapsed) > PREVIEW_CHARS else ""
    return common.ascii_safe(shortened + suffix)


def batch_case(
    checks: common.Checks, engine: str, language: str, clip: Path, store
) -> dict[str, object]:
    name = f"{engine}.batch.{clip.name}.{language}"
    case: dict[str, object] = {
        "engine": engine,
        "mode": "batch",
        "language": language,
        "clip": clip.name,
        **wav_info(clip),
    }
    transcriber = create_transcriber(settings_for(engine, language), secret_store=store)
    started = time.perf_counter()
    text = ""
    try:
        text = transcriber.transcribe_batch(clip.read_bytes())
        case["text"] = text
        case["ok"] = bool(text.strip())
    except Exception as exc:
        case["ok"] = False
        case["error"] = f"{type(exc).__name__}: {exc}"[:800]
    case["seconds_taken"] = round(time.perf_counter() - started, 2)
    close_quietly(transcriber, case)
    checks.verdict(
        name,
        bool(case["ok"]),
        f"{case['seconds_taken']}s chars={len(text)} "
        + (
            f'text="{preview(text)}"'
            if text
            else f"error={case.get('error', '')[:200]}"
        ),
    )
    return case


def streaming_case(
    checks: common.Checks, language: str, clip: Path, store
) -> dict[str, object]:
    name = f"assemblyai.streaming.{clip.name}.{language}"
    case: dict[str, object] = {
        "engine": "assemblyai",
        "mode": "streaming",
        "language": language,
        "clip": clip.name,
        **wav_info(clip),
    }
    settings = replace(settings_for("assemblyai", language), mode="streaming")
    transcriber = create_transcriber(settings, secret_store=store)
    partials: list[tuple[float, str]] = []
    errors: list[str] = []
    started = time.perf_counter()

    def on_partial(text: str) -> None:
        partials.append((round(time.perf_counter() - started, 2), text))

    def on_error(text: str) -> None:
        errors.append(text[:400])

    final = ""
    try:
        transcriber.start_stream(on_partial=on_partial, on_error=on_error)
        case["connect_seconds"] = round(time.perf_counter() - started, 2)
        pcm = pcm16_mono_16k(clip)
        chunk_bytes = 16000 * 2 // 10  # 100 ms of 16 kHz mono PCM16
        next_at = time.perf_counter()
        for offset in range(0, len(pcm), chunk_bytes):
            transcriber.push_audio_chunk(pcm[offset : offset + chunk_bytes])
            next_at += 0.1
            delay = next_at - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
        stop_started = time.perf_counter()
        final = transcriber.stop_stream()
        case["stop_seconds"] = round(time.perf_counter() - stop_started, 2)
        case["final_text"] = final
        case["ok"] = bool(final.strip()) and not errors
    except Exception as exc:
        case["ok"] = False
        case["error"] = f"{type(exc).__name__}: {exc}"[:800]
        try:
            transcriber.abort_stream()
        except Exception as abort_exc:
            case["abort_error"] = f"{type(abort_exc).__name__}: {abort_exc}"[:200]
    case["partial_count"] = len(partials)
    case["first_partial_after_s"] = partials[0][0] if partials else None
    case["last_partial"] = partials[-1][1] if partials else ""
    case["stream_errors"] = errors
    close_quietly(transcriber, case)
    checks.verdict(
        name,
        bool(case["ok"]),
        f"partials={len(partials)} first_after={case['first_partial_after_s']}s "
        f"stop={case.get('stop_seconds')}s chars={len(final)} "
        f"stream_errors={[common.ascii_safe(item)[:80] for item in errors]} "
        + (
            f'text="{preview(final)}"'
            if final
            else f"error={common.ascii_safe(case.get('error', ''))[:200]}"
        ),
    )
    return case


def shutdown_case(checks: common.Checks, clip: Path, store) -> dict[str, object]:
    """A quit during the AssemblyAI batch poll must end the wait quickly.

    This has to be the last case of the run: the flag it sets is process-wide
    and is never cleared, so every provider call after it would be measuring
    the flag instead of the service.
    """
    name = "assemblyai.quit_during_the_batch_poll"
    case: dict[str, object] = {"engine": "assemblyai", "scenario": name}
    transcriber = create_transcriber(
        settings_for("assemblyai", "auto"), secret_store=store
    )
    outcome: dict[str, object] = {}

    def run() -> None:
        started = time.perf_counter()
        try:
            outcome["text"] = transcriber.transcribe_batch(clip.read_bytes())
        except Exception as exc:
            outcome["error"] = f"{type(exc).__name__}: {exc}"[:600]
        outcome["seconds"] = round(time.perf_counter() - started, 2)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    time.sleep(2.5)  # the upload is done or under way; the poll has usually begun
    flagged_at = time.perf_counter()
    transcriber_base.request_transcription_shutdown()
    worker.join(40)
    ended = not worker.is_alive()
    case["worker_ended"] = ended
    case["seconds_from_quit_to_end"] = round(time.perf_counter() - flagged_at, 2)
    case.update(outcome)
    case["ok"] = bool(ended and ("error" in outcome or "text" in outcome))
    checks.verdict(
        name,
        bool(case["ok"]),
        f"worker_ended={ended} after={case['seconds_from_quit_to_end']}s "
        f"outcome={'error' if 'error' in outcome else 'text'}: "
        f"{common.ascii_safe(outcome.get('error', ''))[:160]}",
    )
    return case


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--clip",
        action="append",
        required=True,
        metavar="PATH[:LANG]",
        help=(
            "A real speech recording to transcribe; repeat for more. The "
            "optional :LANG suffix overrides --language for that one clip."
        ),
    )
    parser.add_argument(
        "--language",
        default="auto",
        help="Language for clips that name none of their own (default: auto).",
    )
    parser.add_argument(
        "--no-streaming",
        action="store_true",
        help="Leave out the AssemblyAI realtime case, which runs in real time.",
    )
    common.add_report_argument(parser)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    clips = [parse_clip(raw, args.language) for raw in args.clip]
    for path, _language in clips:
        if not path.is_file():
            raise common.MissingPrerequisite(f"no such clip: {path}")

    checks = common.Checks("real provider calls")
    checks.details["sandbox"] = str(SANDBOX)
    checks.details["clips"] = [f"{path} ({language})" for path, language in clips]
    sys.stdout.write(f"sandbox: {SANDBOX}\n")

    store = KeyringSecretStore()
    available = {provider: store.has_api_key(provider) for provider in PROVIDERS}
    checks.details["providers_with_a_key"] = available
    cases: list[dict[str, object]] = []
    checks.details["cases"] = cases

    def missing(provider: str) -> str:
        return (
            f"no {provider} key in the Windows credential manager; "
            "store one in Settings > Remote"
        )

    for clip, language in clips:
        for engine in PROVIDERS:
            name = f"{engine}.batch.{clip.name}.{language}"
            if not available[engine]:
                checks.skipped(name, missing(engine))
                continue
            try:
                cases.append(batch_case(checks, engine, language, clip, store))
            except Exception as exc:
                checks.crashed(name, exc)
        name = f"assemblyai.streaming.{clip.name}.{language}"
        if args.no_streaming:
            checks.skipped(name, "--no-streaming was given")
        elif not available["assemblyai"]:
            checks.skipped(name, missing("assemblyai"))
        else:
            try:
                cases.append(streaming_case(checks, language, clip, store))
            except Exception as exc:
                checks.crashed(name, exc)

    # Always last: the flag this sets is process-wide and never cleared.
    shutdown_name = "assemblyai.quit_during_the_batch_poll"
    if not available["assemblyai"]:
        checks.skipped(shutdown_name, missing("assemblyai"))
    else:
        try:
            cases.append(shutdown_case(checks, clips[0][0], store))
        except Exception as exc:
            checks.crashed(shutdown_name, exc)

    checks.details["log_lines"] = [
        common.ascii_safe(line)
        for line in LOG_LINES
        if "stt_app" in line
        and ("WARNING" in line or "ERROR" in line or "timing" in line)
    ][:60]
    return checks.finish(args.report)


if __name__ == "__main__":
    common.run_main(main)
