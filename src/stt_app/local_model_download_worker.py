"""Subprocess entry point that downloads one local model.

It streams the downloader's own byte counters back to the parent as prefixed
JSON lines on stdout -- the same shape `benchmark_worker` uses -- so the
Settings queue and the overlay can show a real percentage and a real rate
instead of guessing them from how fast the destination directory grows. The
parent side is `local_model_download`; what the numbers mean, and why the
directory was not good enough, is in `model_download_progress`.

A worker that reports nothing is a supported case: the parent simply keeps
measuring the directory, which is what every build before this one did.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time

from .model_download_progress import (
    DOWNLOAD_EVENT_PREFIX,
    DOWNLOAD_PROGRESS_UNKNOWN,
)
from .ssl_utils import inject_system_trust_store, sync_ca_bundle_env_vars
from .transcriber.local_faster_whisper import download_model_snapshot

# hf_xet reports every 200 ms and the plain HTTP path once per 10 MiB chunk,
# so this throttle costs nothing on either and bounds a future library that
# reports per socket read.
_MIN_EVENT_INTERVAL_S = 0.2


def _emit(event: dict) -> None:
    stream = sys.stdout
    if stream is None:
        # A windowed frozen build hands a worker no stdout unless the parent
        # gave it one, and the parent may also have closed its read end.
        return
    try:
        stream.write(f"{DOWNLOAD_EVENT_PREFIX}{json.dumps(event)}\n")
        stream.flush()
    except Exception:
        # This runs inside huggingface_hub's (and hf_xet's) progress
        # callbacks. Nothing here may break the download it describes.
        pass


class ProgressReporter:
    """Throttle the hook's calls into stdout events.

    Called from hf_xet's own threads as well as from the download pool, hence
    the lock. The byte count is held monotonic here as well as in the parent:
    a report that went backwards would be the one thing the parent cannot
    tell from a restart.
    """

    def __init__(self, *, min_interval_s: float):
        self._min_interval_s = float(min_interval_s)
        self._lock = threading.Lock()
        self._done = 0
        self._total = 0
        self._emitted_at = 0.0
        self._emitted: tuple[int, int] | None = None

    def report(self, done: int, total: int) -> None:
        now = time.monotonic()
        with self._lock:
            if done == DOWNLOAD_PROGRESS_UNKNOWN:
                self._emitted = None
                self._emitted_at = now
                payload = {
                    "event": "bytes",
                    "done": DOWNLOAD_PROGRESS_UNKNOWN,
                    "total": DOWNLOAD_PROGRESS_UNKNOWN,
                }
                _emit(payload)
                return
            self._done = max(self._done, int(done))
            self._total = max(self._total, int(total))
            sample = (self._done, self._total)
            if sample == self._emitted:
                return
            # A changed total is published at once: it is what the percentage
            # is measured against, and it settles in the first second.
            total_changed = (
                self._emitted is not None and sample[1] != self._emitted[1]
            )
            if (
                self._emitted is not None
                and not total_changed
                and now - self._emitted_at < self._min_interval_s
            ):
                return
            self._emitted = sample
            self._emitted_at = now
        _emit({"event": "bytes", "done": sample[0], "total": sample[1]})

    def flush(self) -> None:
        """Publish the last sample the throttle held back."""
        with self._lock:
            sample = (self._done, self._total)
            if sample == self._emitted or not sample[0]:
                return
            self._emitted = sample
        _emit({"event": "bytes", "done": sample[0], "total": sample[1]})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download one local STT model.")
    parser.add_argument("--model", required=True, help="Local model name.")
    parser.add_argument(
        "--model-dir",
        default="",
        help="Optional custom model cache directory.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    inject_system_trust_store()
    sync_ca_bundle_env_vars()
    reporter = ProgressReporter(min_interval_s=_MIN_EVENT_INTERVAL_S)
    try:
        download_model_snapshot(
            args.model,
            args.model_dir,
            progress_hook=reporter.report,
        )
    except Exception as exc:
        if sys.stderr is not None:
            sys.stderr.write(f"{exc}\n")
        return 1
    reporter.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
