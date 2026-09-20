"""Model download progress: where the bytes come from, and how they read.

Two sources feed this module.

**Reported bytes.** Where the app owns the download process
(`local_model_download_worker`, used by the Local tab's queue and by the
controller's preload) the worker installs a `tqdm_class` into
`snapshot_download` and streams huggingface_hub's own byte counters back to
the parent. Those are the real numbers: bytes fetched and bytes expected.

**Directory growth**, the fallback, for every path with no worker to ask: a
transcriber downloading from its own load path, a worker from an older build
that reports nothing, and the window between a worker starting and its first
event. `estimate_cached_model_bytes` sizes the download destination.

The fallback used to be the only source, and it is a faithful proxy only
while the downloader writes sequentially. hf_xet does not: it reconstructs a
file from chunk ranges fetched in parallel (measured on the user's own
2026-09-20 download of `granite-speech-5.0-470m-turboctc`, 552 MB in 43 s,
with the concurrency controller ramping to 60 parallel range requests), and
for a writer that seeks, `st_size` is the highest offset written, not the
bytes present. Measured with 64 MB blocks completing out of order: 36.4%
claimed with 12.1% present, then flat while the gaps behind it filled. That
is the "0%, then a jump to about 30%" the field report describes, and the
"measuring speed, briefly a wrong speed, measuring speed again" with it --
a jump divided by the window gives a rate nobody transferred, and the flat
stretch after it gave no rate at all.

Windows is *not* part of that story: a `stat()` of a file another process is
appending to tracks it exactly, measured at 1 MiB every 50 ms, buffered and
flushed alike, through `Path.stat`, `os.scandir` and the app's own
`rglob`-based sizer.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from .config import MODEL_ESTIMATED_SIZE_MB

_DEFAULT_SPEED_WINDOW_SECONDS = 5.0
# A rate is only published once the samples it is computed from span at least
# this long. Without it one 64 MB burst arriving 0.2 s after the previous
# sample reads as 320 MB/s.
_MIN_RATE_SPAN_SECONDS = 1.0
# ETA steps, so a number the user is watching does not twitch every poll.
_ETA_MIN_SECONDS = 5.0
_ETA_STEP_SECONDS = 5

# What a progress hook is handed when the reporter stops being authoritative:
# the Hugging Face download failed and the ModelScope mirror took over, which
# has no hook of its own. Reporting nothing would have frozen the last number
# on screen for the whole mirror transfer; the parent drops the sample and
# falls back to directory growth, which for the mirror's sequential writes is
# exact.
DOWNLOAD_PROGRESS_UNKNOWN = -1

# Frames a worker progress line on the download child's stdout. It lives in
# this module rather than in the worker because the parent side needs it too,
# and importing the worker would pull the whole faster-whisper transcriber
# into the GUI process at startup.
DOWNLOAD_EVENT_PREFIX = "@@STTDL@@"

# The name huggingface_hub gives the one aggregate byte bar of a
# `snapshot_download`. Matching on the name rather than on `unit="B"` is
# load-bearing: 1.32.0 creates a *second* byte bar beside it, the transfer bar
# (`huggingface_hub.snapshot_download.transfer`, bytes actually pulled over
# the network, which Xet deduplication makes smaller than the file), and
# summing both would count most bytes twice. The reconstruct bar is the one
# that reaches the file's size, which is what a progress bar means. The name
# is identical in 1.8.0 (one byte bar) and 1.32.0 (two), and the match is
# exact so the transfer bar's dotted child name does not pass it.
HUB_SNAPSHOT_PROGRESS_BAR_NAME = "huggingface_hub.snapshot_download"

ProgressHook = Callable[[int, int], None]


@dataclass(frozen=True, slots=True)
class ModelDownloadProgress:
    model_name: str
    downloaded_bytes: int
    estimated_total_bytes: int
    speed_bytes_per_second: float | None
    display_name: str = ""
    # True when `estimated_total_bytes` is the downloader's own figure rather
    # than this app's table, which is what lets the text drop "approx.".
    total_is_reported: bool = False
    eta_seconds: float | None = None

    @property
    def shown_name(self) -> str:
        return self.display_name or self.model_name

    @property
    def fraction(self) -> float | None:
        if self.estimated_total_bytes <= 0:
            return None
        return max(
            0.0,
            min(1.0, self.downloaded_bytes / float(self.estimated_total_bytes)),
        )

    @property
    def percent(self) -> int | None:
        fraction = self.fraction
        return None if fraction is None else round(fraction * 100)


class ModelDownloadSpeedTracker:
    """Turn a series of byte readings into a percentage, a rate and an ETA.

    Three properties the field report asked for, each one a rule here:

    * **The percentage never goes backwards.** A reading below the highest one
      seen for this model is clamped to it. Reported bytes only grow, but the
      fallback can dip -- a partial file renamed into place, a cleanup running
      beside the poll -- and a bar that jumps back is read as a failure.
    * **"measuring speed" is a start-up state, not a recurring one.** A rate is
      published from the first sample pair that spans `_MIN_RATE_SPAN_SECONDS`
      *after* growth has been seen, and from then on the windowed rate is
      always published -- including 0.0 B/s for a genuine stall, which is the
      honest answer and not the same sentence as "no measurement yet".
    * **No rate is computed from one burst over a tiny interval.** The rate
      spans the whole window, oldest kept sample to newest.
    """

    def __init__(self, *, window_seconds: float = _DEFAULT_SPEED_WINDOW_SECONDS):
        self._window_seconds = max(0.1, float(window_seconds))
        self._model_name = ""
        self._samples: deque[tuple[float, int]] = deque()
        self._peak_bytes = 0
        self._rate: float | None = None
        self._saw_growth = False
        self._from_downloader = False

    def reset(
        self,
        model_name: str = "",
        downloaded_bytes: int = 0,
        *,
        from_downloader: bool = False,
        now: float | None = None,
    ) -> None:
        self._model_name = str(model_name or "")
        self._samples.clear()
        self._peak_bytes = max(0, int(downloaded_bytes)) if self._model_name else 0
        self._rate = None
        self._saw_growth = False
        self._from_downloader = bool(from_downloader)
        if self._model_name:
            measured_at = time.monotonic() if now is None else float(now)
            self._samples.append((measured_at, self._peak_bytes))

    def measure(
        self,
        model_name: str,
        downloaded_bytes: int,
        *,
        reported_total_bytes: int = 0,
        display_name: str = "",
        from_downloader: bool = False,
        now: float | None = None,
    ) -> ModelDownloadProgress:
        measured_at = time.monotonic() if now is None else float(now)
        reading = max(0, int(downloaded_bytes))

        # The two sources count different things -- the downloader counts this
        # transfer, the directory counts everything in the destination -- so
        # the high-water mark may not carry across a switch between them. It
        # happens in both directions: the worker's first event replaces the
        # directory reading, and the ModelScope fallback retires the
        # worker's. Carried across, whichever source read higher would freeze
        # the display until the other caught up.
        if model_name != self._model_name or bool(from_downloader) != (
            self._from_downloader
        ):
            self.reset(
                model_name,
                reading,
                from_downloader=from_downloader,
                now=measured_at,
            )
            return self._build(reported_total_bytes, display_name)

        if reading > self._peak_bytes:
            self._saw_growth = True
            self._peak_bytes = reading
        value = self._peak_bytes

        if not self._samples:
            self._samples.append((measured_at, value))
            return self._build(reported_total_bytes, display_name)

        self._samples.append((measured_at, value))
        cutoff = measured_at - self._window_seconds
        # Keep the newest sample that is already older than the cutoff, so the
        # pair always spans at least the window while there is history for it.
        while len(self._samples) > 2 and self._samples[1][0] < cutoff:
            self._samples.popleft()

        oldest_at, oldest_bytes = self._samples[0]
        span = measured_at - oldest_at
        if self._saw_growth and span >= _MIN_RATE_SPAN_SECONDS:
            self._rate = max(0, value - oldest_bytes) / span
        return self._build(reported_total_bytes, display_name)

    def _build(
        self,
        reported_total_bytes: int,
        display_name: str,
    ) -> ModelDownloadProgress:
        return measure_model_download_progress(
            self._model_name,
            self._peak_bytes,
            reported_total_bytes=reported_total_bytes,
            display_name=display_name,
            speed_bytes_per_second=self._rate,
        )


def measure_model_download_progress(
    model_name: str,
    downloaded_bytes: int,
    *,
    previous_bytes: int = 0,
    previous_at: float = 0.0,
    now: float | None = None,
    reported_total_bytes: int = 0,
    display_name: str = "",
    speed_bytes_per_second: float | None = None,
) -> ModelDownloadProgress:
    """Build one progress reading.

    The total is the larger of what the downloader reported and what
    `MODEL_ESTIMATED_SIZE_MB` says. The table is the floor because the
    downloader's total is not complete at once: huggingface_hub sums it per
    file as each file's metadata arrives, so for the first moment of a
    9-file repo it is the few small files' 1.1 MB -- and with the same 1.1 MB
    already fetched that reads as 99%, which then collapses to 0.2% when the
    551 MB weight file's metadata lands. The table's figure is measured
    against the real repo (552 MB against the Hub's 552,442,697 bytes for
    `granite-speech-5.0-470m-turboctc`), so the reported total overtakes it
    without the percentage ever moving backwards.
    """
    measured_at = time.monotonic() if now is None else float(now)
    speed = speed_bytes_per_second
    if speed is None and previous_at > 0.0 and measured_at > previous_at:
        speed = max(0, int(downloaded_bytes) - int(previous_bytes)) / (
            measured_at - previous_at
        )

    estimated_mb = MODEL_ESTIMATED_SIZE_MB.get(model_name, 0)
    table_total = max(0, int(estimated_mb * 1_000_000))
    reported_total = max(0, int(reported_total_bytes))
    total = max(reported_total, table_total)
    done = max(0, int(downloaded_bytes))

    eta: float | None = None
    remaining = total - done
    if total > 0 and remaining > 0 and speed is not None and speed > 0:
        eta = remaining / speed

    return ModelDownloadProgress(
        model_name=model_name,
        downloaded_bytes=done,
        estimated_total_bytes=total,
        speed_bytes_per_second=speed,
        display_name=str(display_name or ""),
        total_is_reported=reported_total > 0 and reported_total >= table_total,
        eta_seconds=eta,
    )


def report_unknown_download_progress(hook: ProgressHook | None) -> None:
    """Tell a progress hook that no further reports are coming.

    Used where a download changes hands to a path with no hook of its own.
    Never raises: it sits on a download's error road.
    """
    if hook is None:
        return
    try:
        hook(DOWNLOAD_PROGRESS_UNKNOWN, DOWNLOAD_PROGRESS_UNKNOWN)
    except Exception:
        pass


def format_transfer_rate(bytes_per_second: float) -> str:
    """Both units, because the user reads one of them off Task Manager."""
    return (
        f"{bytes_per_second / 1_000_000.0:.1f} MB/s "
        f"({bytes_per_second * 8 / 1_000_000.0:.1f} Mbit/s)"
    )


def format_eta(seconds: float | None) -> str:
    """Coarse remaining time, or "" when no honest one can be given.

    Rounded to five-second steps below a minute and to whole minutes above
    it: the underlying number moves with every poll, and a digit that
    twitches reads as an app that does not know.
    """
    if seconds is None or seconds < _ETA_MIN_SECONDS:
        return ""
    if seconds < 60:
        steps = int(seconds // _ETA_STEP_SECONDS) * _ETA_STEP_SECONDS
        return f"about {max(_ETA_STEP_SECONDS, steps)} s left"
    if seconds < 3600:
        minutes = int(seconds // 60) or 1
        return f"about {minutes} min left"
    return "over an hour left"


def format_model_download_progress(
    progress: ModelDownloadProgress,
    *,
    queued_count: int = 0,
    include_progress_bar: bool = False,
) -> str:
    downloaded_mb = progress.downloaded_bytes / 1_000_000.0
    if progress.estimated_total_bytes > 0:
        total_mb = progress.estimated_total_bytes / 1_000_000.0
        progress_bar = ""
        if include_progress_bar and progress.fraction is not None:
            width = 18
            filled = round(progress.fraction * width)
            progress_bar = f" [{'#' * filled}{'.' * (width - filled)}]"
        # "approx." only while the total is this app's own table figure. With
        # the downloader's number there is nothing approximate about it.
        qualifier = "" if progress.total_is_reported else "approx. "
        detail = (
            f"Downloading {progress.shown_name}:{progress_bar} "
            f"{downloaded_mb:.0f} of {total_mb:.0f} MB "
            f"({qualifier}{progress.percent}%)"
        )
    else:
        detail = f"Downloading {progress.shown_name}: {downloaded_mb:.0f} MB cached"

    if progress.speed_bytes_per_second is None:
        detail = f"{detail}, measuring speed"
    else:
        detail = f"{detail}, {format_transfer_rate(progress.speed_bytes_per_second)}"

    eta = format_eta(progress.eta_seconds)
    if eta:
        detail = f"{detail}, {eta}"

    if queued_count > 0:
        suffix = "model" if queued_count == 1 else "models"
        detail = f"{detail}. {queued_count} {suffix} queued"
    return f"{detail}."


def format_download_queue_line(queued_labels: list[str]) -> str:
    """What the download queue will start next, by on-screen name.

    A row reading "Queued, 2 of 3" says where a model is in the line; it does
    not say what the line is while the user is watching the progress area.
    """
    if not queued_labels:
        return ""
    head = queued_labels[0]
    extra = len(queued_labels) - 1
    if extra <= 0:
        return f"Next: {head}"
    return f"Next: {head} (+{extra} more)"


def hub_progress_tqdm_class(report: ProgressHook | None):
    """A `tqdm_class` for `snapshot_download` that reports real byte counts.

    `tqdm_class` is the only public seam huggingface_hub offers into its own
    accounting, and it is stable across the versions this app runs on: the
    class is instantiated for every progress bar the download creates, and the
    one named `HUB_SNAPSHOT_PROGRESS_BAR_NAME` is the aggregate over every file
    of the snapshot. Bars this function does not recognise -- the files-count
    bar of the thread pool, 1.32.0's separate network-transfer bar, anything a
    later version adds -- are constructed and then ignored, which leaves the
    caller with no samples and the parent on directory growth rather than on a
    wrong number.

    Three details that are not obvious:

    * The subclass is of `huggingface_hub.utils.tqdm`, not of `tqdm` itself.
      That base absorbs the `name=` keyword real tqdm has no idea about, and
      carries the `__delattr__` and lock class methods the thread-pool helper
      needs.
    * The app runs the worker with `HF_HUB_DISABLE_PROGRESS_BARS=1`, so tqdm
      itself is disabled and `tqdm.update` returns before touching `self.n`.
      The count kept here is therefore our own, never read back off tqdm.
    * `update` is called from hf_xet's own threads as well as from the
      download pool, so the count is taken under a lock, and nothing raised
      here may reach the downloader.
    """
    if report is None:
        return None

    # Lazy: this module is imported by the GUI process, which must not pull in
    # huggingface_hub and tqdm to render a percentage.
    from huggingface_hub.utils import tqdm as hf_tqdm  # type: ignore

    class _ReportingTqdm(hf_tqdm):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            self._stt_reports = kwargs.get("name") == HUB_SNAPSHOT_PROGRESS_BAR_NAME
            self._stt_lock = threading.Lock()
            self._stt_done = float(kwargs.get("initial") or 0)
            super().__init__(*args, **kwargs)
            self._stt_emit()

        def update(self, n=1):
            with self._stt_lock:
                self._stt_done += float(n or 0)
            self._stt_emit()
            return super().update(n)

        def refresh(self, *args, **kwargs):
            # The per-file bars add their expected size to this bar's `total`
            # and then refresh it, which is how the total becomes known.
            self._stt_emit()
            return super().refresh(*args, **kwargs)

        def _stt_emit(self) -> None:
            if not getattr(self, "_stt_reports", False):
                return
            try:
                report(int(self._stt_done), int(getattr(self, "total", 0) or 0))
            except Exception:
                # A progress report may not break the download it describes.
                pass

    return _ReportingTqdm
