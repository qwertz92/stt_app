from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from .config import MODEL_ESTIMATED_SIZE_MB

_DEFAULT_SPEED_WINDOW_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class ModelDownloadProgress:
    model_name: str
    downloaded_bytes: int
    estimated_total_bytes: int
    speed_bytes_per_second: float | None

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
        return None if fraction is None else int(round(fraction * 100))


class ModelDownloadSpeedTracker:
    """Estimate transfer speed across bursty on-disk cache updates."""

    def __init__(self, *, window_seconds: float = _DEFAULT_SPEED_WINDOW_SECONDS):
        self._window_seconds = max(0.1, float(window_seconds))
        self._model_name = ""
        self._samples: deque[tuple[float, int]] = deque()

    def reset(
        self,
        model_name: str = "",
        downloaded_bytes: int = 0,
        *,
        now: float | None = None,
    ) -> None:
        self._model_name = str(model_name or "")
        self._samples.clear()
        if self._model_name:
            measured_at = time.monotonic() if now is None else float(now)
            self._samples.append((measured_at, max(0, int(downloaded_bytes))))

    def measure(
        self,
        model_name: str,
        downloaded_bytes: int,
        *,
        now: float | None = None,
    ) -> ModelDownloadProgress:
        measured_at = time.monotonic() if now is None else float(now)
        normalized_bytes = max(0, int(downloaded_bytes))
        if (
            model_name != self._model_name
            or (
                self._samples
                and normalized_bytes < self._samples[-1][1]
            )
        ):
            self.reset(model_name, normalized_bytes, now=measured_at)
            return measure_model_download_progress(model_name, normalized_bytes)

        if not self._samples:
            self.reset(model_name, normalized_bytes, now=measured_at)
            return measure_model_download_progress(model_name, normalized_bytes)

        self._samples.append((measured_at, normalized_bytes))
        cutoff = measured_at - self._window_seconds
        while len(self._samples) > 1 and self._samples[0][0] < cutoff:
            self._samples.popleft()

        previous = next(
            (
                sample
                for sample in self._samples
                if sample[0] < measured_at and sample[1] < normalized_bytes
            ),
            None,
        )
        if previous is None:
            return measure_model_download_progress(model_name, normalized_bytes)
        return measure_model_download_progress(
            model_name,
            normalized_bytes,
            previous_bytes=previous[1],
            previous_at=previous[0],
            now=measured_at,
        )


def measure_model_download_progress(
    model_name: str,
    downloaded_bytes: int,
    *,
    previous_bytes: int = 0,
    previous_at: float = 0.0,
    now: float | None = None,
) -> ModelDownloadProgress:
    measured_at = time.monotonic() if now is None else float(now)
    speed: float | None = None
    if previous_at > 0.0 and measured_at > previous_at:
        speed = max(0, int(downloaded_bytes) - int(previous_bytes)) / (
            measured_at - previous_at
        )

    estimated_mb = MODEL_ESTIMATED_SIZE_MB.get(model_name, 0)
    return ModelDownloadProgress(
        model_name=model_name,
        downloaded_bytes=max(0, int(downloaded_bytes)),
        estimated_total_bytes=max(0, int(estimated_mb * 1_000_000)),
        speed_bytes_per_second=speed,
    )


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
            filled = int(round(progress.fraction * width))
            progress_bar = f" [{'#' * filled}{'.' * (width - filled)}]"
        detail = (
            f"Downloading '{progress.model_name}': approx. {progress.percent}%"
            f"{progress_bar} ({downloaded_mb:.0f}/{total_mb:.0f} MB)"
        )
    else:
        detail = f"Downloading '{progress.model_name}': {downloaded_mb:.0f} MB cached"

    if progress.speed_bytes_per_second is None:
        detail = f"{detail}, measuring speed"
    else:
        speed_mb_s = progress.speed_bytes_per_second / 1_000_000.0
        speed_mbit_s = progress.speed_bytes_per_second * 8 / 1_000_000.0
        detail = f"{detail}, {speed_mb_s:.1f} MB/s ({speed_mbit_s:.1f} Mbit/s)"

    if queued_count > 0:
        suffix = "model" if queued_count == 1 else "models"
        detail = f"{detail}. {queued_count} {suffix} queued"
    return f"{detail}."
