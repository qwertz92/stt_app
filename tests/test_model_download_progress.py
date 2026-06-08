from stt_app.model_download_progress import (
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
