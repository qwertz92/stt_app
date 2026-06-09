from stt_app.model_download_progress import (
    ModelDownloadSpeedTracker,
    format_model_download_progress,
    measure_model_download_progress,
)


def test_measure_model_download_progress_calculates_percent_and_speed():
    progress = measure_model_download_progress(
        "small",
        242_000_000,
        previous_bytes=142_000_000,
        previous_at=10.0,
        now=12.0,
    )

    assert progress.percent == 50
    assert progress.speed_bytes_per_second == 50_000_000


def test_measure_model_download_progress_clamps_estimated_percent():
    progress = measure_model_download_progress(
        "small",
        700_000_000,
    )

    assert progress.percent == 100


def test_speed_tracker_keeps_rate_between_bursty_cache_updates():
    tracker = ModelDownloadSpeedTracker(window_seconds=5.0)
    tracker.reset("small", 100_000_000, now=10.0)

    first_growth = tracker.measure("small", 120_000_000, now=11.0)
    between_writes = tracker.measure("small", 120_000_000, now=12.0)

    assert first_growth.speed_bytes_per_second == 20_000_000
    assert between_writes.speed_bytes_per_second == 10_000_000


def test_speed_tracker_stops_reporting_stale_rate():
    tracker = ModelDownloadSpeedTracker(window_seconds=3.0)
    tracker.reset("small", 100_000_000, now=10.0)
    tracker.measure("small", 120_000_000, now=11.0)

    stale = tracker.measure("small", 120_000_000, now=15.0)

    assert stale.speed_bytes_per_second is None


def test_speed_tracker_resets_when_cache_size_decreases():
    tracker = ModelDownloadSpeedTracker()
    tracker.reset("small", 100_000_000, now=10.0)

    progress = tracker.measure("small", 50_000_000, now=11.0)

    assert progress.speed_bytes_per_second is None


def test_format_model_download_progress_includes_rate_and_queue():
    progress = measure_model_download_progress(
        "small",
        242_000_000,
        previous_bytes=142_000_000,
        previous_at=10.0,
        now=12.0,
    )

    text = format_model_download_progress(progress, queued_count=2)

    assert "approx. 50%" in text
    assert "50.0 MB/s" in text
    assert "400.0 Mbit/s" in text
    assert "2 models queued" in text


def test_format_model_download_progress_can_include_text_bar():
    progress = measure_model_download_progress("small", 242_000_000)

    text = format_model_download_progress(progress, include_progress_bar=True)

    assert "[#########.........]" in text
