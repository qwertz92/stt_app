from pathlib import Path

import pytest

from stt_app.model_download_progress import (
    DOWNLOAD_PROGRESS_UNKNOWN,
    HUB_SNAPSHOT_PROGRESS_BAR_NAME,
    ModelDownloadSpeedTracker,
    completed_download_bytes,
    format_download_queue_line,
    format_eta,
    format_model_download_progress,
    hub_progress_tqdm_class,
    measure_model_download_progress,
    offset_progress_hook,
    report_unknown_download_progress,
)

# `small` is 486 MB in MODEL_ESTIMATED_SIZE_MB; the tests use it as the table
# figure a reported total has to beat.
_SMALL_TABLE_BYTES = 486_000_000


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


def test_a_reported_total_replaces_the_table_and_drops_approx():
    """The downloader's own figure is not an estimate, and the text says so."""
    reported = measure_model_download_progress(
        "small",
        100_000_000,
        reported_total_bytes=500_000_000,
    )
    estimated = measure_model_download_progress("small", 100_000_000)

    assert reported.estimated_total_bytes == 500_000_000
    assert reported.total_is_reported is True
    assert "approx." not in format_model_download_progress(reported)
    assert estimated.estimated_total_bytes == _SMALL_TABLE_BYTES
    assert estimated.total_is_reported is False
    assert "approx." in format_model_download_progress(estimated)


def test_the_table_is_the_floor_while_the_reported_total_is_still_growing():
    """huggingface_hub sums its total per file as each file's metadata lands.

    For the first moment of a multi-file repo that total is the few small
    files -- and with the same small files already fetched it reads as 99%,
    then collapses when the weight file's metadata arrives.
    """
    early = measure_model_download_progress(
        "small",
        1_100_000,
        reported_total_bytes=1_148_000,
    )

    assert early.estimated_total_bytes == _SMALL_TABLE_BYTES
    assert early.percent == 0
    assert early.total_is_reported is False


def test_the_percentage_never_goes_backwards_within_one_download():
    tracker = ModelDownloadSpeedTracker()
    tracker.reset("small", 0, now=10.0)

    high = tracker.measure("small", 240_000_000, now=11.0)
    dipped = tracker.measure("small", 120_000_000, now=12.0)

    assert high.percent == 49
    assert dipped.percent == 49
    assert dipped.downloaded_bytes == 240_000_000


def test_switching_to_the_worker_keeps_what_the_directory_already_showed():
    """A resume used to read nearly 100% and then 0% at the first event.

    The two sources describe the same bytes now -- the reported ones carry
    the baseline of files already complete -- so the high-water mark carries
    across the switch instead of being dropped with the source. Measured
    before the fix with `granite-speech-5.0-470m-turboctc`, its
    551,294,349-byte weight file complete and only the 1,148-byte config
    missing: directory growth said 100%, the worker's first event said 0%.
    """
    tracker = ModelDownloadSpeedTracker()
    tracker.reset("small", 480_000_000, now=10.0)
    tracker.measure("small", 480_000_000, now=11.0)

    reported = tracker.measure("small", 480_000_000, from_downloader=True, now=12.0)
    dipped = tracker.measure("small", 1_000, from_downloader=True, now=13.0)

    assert reported.percent == 99
    assert dipped.percent == 99


def test_the_rate_is_measured_inside_one_source_and_never_across_the_switch():
    """The peak carries over; the sample history does not.

    The two sources are read at different moments by different code, so the
    first pair spanning the switch would time a difference neither of them
    measured.
    """
    tracker = ModelDownloadSpeedTracker(window_seconds=5.0)
    tracker.reset("small", 0, now=0.0)
    tracker.measure("small", 10_000_000, now=1.0)
    before = tracker.measure("small", 20_000_000, now=2.0)
    assert before.speed_bytes_per_second == 10_000_000

    tracker.measure("small", 20_000_000, from_downloader=True, now=2.5)
    after = tracker.measure("small", 100_000_000, from_downloader=True, now=3.0)

    # Half a second of the new source is less than `_MIN_RATE_SPAN_SECONDS`,
    # so the last honest rate stands. Reading across the switch would have
    # divided 100 MB by the three seconds since the download began: 33 MB/s.
    assert after.speed_bytes_per_second == 10_000_000


def test_a_burst_after_a_flat_stretch_is_not_reported_as_its_own_rate():
    """The pattern the field report produced, at the app's 500 ms poll.

    Directory growth under a chunk-reconstructing downloader is flat and then
    jumps; the old tracker divided the jump by the gap to the oldest smaller
    sample and printed a rate nobody transferred, then went back to
    "measuring speed" for the flat stretch after it.
    """
    tracker = ModelDownloadSpeedTracker(window_seconds=5.0)
    tracker.reset("small", 0, now=0.0)
    at = 0.0
    seen: list[float | None] = []
    for step in range(1, 41):
        at = step * 0.5
        # 64 MB lands every 4 s, nothing in between.
        landed = (step // 8) * 64_000_000
        seen.append(
            tracker.measure("small", landed, now=at).speed_bytes_per_second
        )

    measured = [rate for rate in seen if rate is not None]
    assert measured, "no rate was ever published"
    # The true average is 16 MB/s. A 5 s window straddles one or two of the
    # 4 s bursts, so it oscillates between 12.8 and 25.6 MB/s -- which is
    # what "the average over the last five seconds" means for input this
    # lumpy, and honest. What it can never be again is the instantaneous
    # 128 MB/s of one 64 MB burst divided by the 0.5 s poll it landed in.
    assert max(measured) <= 2 * 64_000_000 / 5.0, max(measured)
    # And once a rate exists it is never withdrawn again.
    first_rate = seen.index(measured[0])
    assert all(rate is not None for rate in seen[first_rate:])


def test_measuring_speed_only_ever_precedes_the_first_growth():
    tracker = ModelDownloadSpeedTracker(window_seconds=5.0)
    tracker.reset("small", 0, now=0.0)

    # Eight seconds of metadata and reconstruction set-up: nothing arrives.
    quiet = [
        tracker.measure("small", 0, now=step * 0.5).speed_bytes_per_second
        for step in range(1, 17)
    ]
    assert quiet == [None] * 16, "invented a 0.0 B/s rate before anything arrived"

    tracker.measure("small", 10_000_000, now=8.5)
    running = tracker.measure("small", 20_000_000, now=9.5)
    assert running.speed_bytes_per_second is not None

    # A stall after that is 0.0 B/s, which is the honest answer and not the
    # same sentence as "no measurement yet".
    stalled = tracker.measure("small", 20_000_000, now=16.0)
    assert stalled.speed_bytes_per_second == 0.0


def test_the_tracker_publishes_an_eta_from_the_windowed_rate():
    tracker = ModelDownloadSpeedTracker(window_seconds=5.0)
    tracker.reset("small", 0, now=0.0)
    for step in range(1, 11):
        progress = tracker.measure(
            "small",
            step * 10_000_000,
            # Above `small`'s 486 MB table figure, or the table would win as
            # the floor and the remaining bytes would be a different number.
            reported_total_bytes=600_000_000,
            now=step * 1.0,
        )

    assert progress.speed_bytes_per_second == 10_000_000
    assert progress.eta_seconds == 50.0
    assert "about 50 s left" in format_model_download_progress(progress)


def test_format_eta_is_coarse_and_silent_when_it_would_be_noise():
    assert format_eta(None) == ""
    assert format_eta(2.0) == ""
    assert format_eta(32.0) == "about 30 s left"
    assert format_eta(125.0) == "about 2 min left"
    assert format_eta(7200.0) == "over an hour left"


def test_format_model_download_progress_includes_rate_and_queue():
    progress = measure_model_download_progress(
        "small",
        242_000_000,
        previous_bytes=142_000_000,
        previous_at=10.0,
        now=12.0,
    )

    text = format_model_download_progress(progress, queued_count=2)

    assert "242 of 486 MB" in text
    assert "approx. 50%" in text
    assert "50.0 MB/s" in text
    assert "400.0 Mbit/s" in text
    assert "2 models queued" in text


def test_format_model_download_progress_names_the_model_as_the_screen_does():
    progress = measure_model_download_progress(
        "granite-speech-5.0-470m-turboctc",
        212_000_000,
        reported_total_bytes=552_442_697,
        display_name="IBM Granite Speech 5.0 470M",
    )

    text = format_model_download_progress(progress)

    assert text.startswith("Downloading IBM Granite Speech 5.0 470M:")
    assert "granite-speech-5.0-470m-turboctc" not in text
    assert "212 of 552 MB (38%)" in text


def test_format_model_download_progress_can_include_text_bar():
    progress = measure_model_download_progress("small", 242_000_000)

    text = format_model_download_progress(progress, include_progress_bar=True)

    assert "[#########.........]" in text


def test_format_download_queue_line_names_what_comes_next():
    assert format_download_queue_line([]) == ""
    assert format_download_queue_line(["Parakeet"]) == "Next: Parakeet"
    assert (
        format_download_queue_line(["Parakeet", "Canary", "small"])
        == "Next: Parakeet (+2 more)"
    )


# --- the bytes a resume does not fetch again -----------------------------


def _write(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    return path


def test_the_baseline_counts_the_flat_layout_without_its_partials(tmp_path):
    """The ONNX models download into a flat `local_dir`.

    Its partials live under `.cache/huggingface/download` and reach the
    percentage through the per-file bar's `initial=`, so counting them here
    would count them twice.
    """
    destination = tmp_path / "granite-speech-5.0-470m-turboctc-onnx"
    _write(destination / "config.json", 1_148)
    _write(destination / "onnx" / "model_int8.onnx", 4_000_000)
    _write(
        destination / ".cache" / "huggingface" / "download" / "tok.json.incomplete",
        900_000,
    )
    _write(destination / ".cache" / "huggingface" / "download" / "tok.json.metadata", 80)

    assert completed_download_bytes("granite-speech-5.0-470m-turboctc", destination) == (
        4_001_148
    )


def test_the_baseline_skips_partial_blobs(tmp_path):
    """faster-whisper downloads into `models--<repo>`.

    There a partial carries an `.incomplete` suffix beside the finished blob,
    and huggingface_hub resumes it and reports its bytes through the per-file
    bar's `initial=`.
    """
    destination = tmp_path / "models--Systran--faster-whisper-small"
    _write(destination / "blobs" / "aaa", 3_000_000)
    _write(destination / "blobs" / "bbb.incomplete", 500_000)
    _write(destination / "refs" / "main", 40)

    assert completed_download_bytes("small", destination) == 3_000_040


def test_the_baseline_does_not_follow_a_snapshot_symlink_into_its_blob(tmp_path):
    """Every snapshot entry points at a blob in the same tree, and `stat()`
    follows it -- counting the model twice.

    Skipped where Windows refuses the symlink (no Developer Mode and not an
    administrator, which is this developer's machine): huggingface_hub then
    *moves* a newly downloaded blob to the snapshot path instead of linking
    it, so there is one real file and nothing to skip.
    """
    destination = tmp_path / "models--Systran--faster-whisper-small"
    blob = _write(destination / "blobs" / "aaa", 3_000_000)
    snapshot = destination / "snapshots" / "rev"
    snapshot.mkdir(parents=True)
    try:
        (snapshot / "model.bin").symlink_to(blob)
    except OSError as exc:
        pytest.skip(f"this machine cannot create symlinks: {exc}")

    assert completed_download_bytes("small", destination) == 3_000_000


def test_the_baseline_never_exceeds_the_models_own_size(tmp_path):
    """A blob cache keeps the blobs of every revision it ever fetched.

    A repo re-uploaded upstream therefore leaves a full model's worth of
    bytes that the next download neither fetches again nor uses. Uncapped
    that is a download starting above 100%.
    """
    destination = tmp_path / "models--Systran--faster-whisper-small"
    _write(destination / "blobs" / "old-revision", _SMALL_TABLE_BYTES)
    _write(destination / "blobs" / "older-revision", _SMALL_TABLE_BYTES)

    assert completed_download_bytes("small", destination) == _SMALL_TABLE_BYTES


def test_the_baseline_of_a_destination_that_is_not_there_yet_is_zero(tmp_path):
    assert completed_download_bytes("small", None) == 0
    assert completed_download_bytes("small", tmp_path / "nothing") == 0


def test_the_offset_hook_adds_the_baseline_to_both_numbers():
    report = _Recorder()
    hook = offset_progress_hook(report, 551_294_349)

    hook(0, 1_148)
    hook(1_148, 1_148)

    assert report.seen == [
        (551_294_349, 551_295_497),
        (551_295_497, 551_295_497),
    ]


def test_the_offset_hook_passes_the_unknown_sentinel_through_untouched():
    """It means "stop believing me", not a byte count to shift."""
    report = _Recorder()

    offset_progress_hook(report, 551_294_349)(
        DOWNLOAD_PROGRESS_UNKNOWN, DOWNLOAD_PROGRESS_UNKNOWN
    )

    assert report.seen == [(DOWNLOAD_PROGRESS_UNKNOWN, DOWNLOAD_PROGRESS_UNKNOWN)]


def test_the_offset_hook_is_the_hook_itself_when_there_is_nothing_to_add():
    report = _Recorder()

    assert offset_progress_hook(report, 0) is report
    assert offset_progress_hook(None, 42) is None


# --- the huggingface_hub seam -------------------------------------------


class _Recorder:
    """Collect what a progress hook is handed."""

    def __init__(self) -> None:
        self.seen: list[tuple[int, int]] = []

    def __call__(self, done: int, total: int) -> None:
        self.seen.append((done, total))


class _HubDriver:
    """Drive a `tqdm_class` the way `snapshot_download` drives it.

    Mirrors both installed versions: the aggregate reconstruct bar (named
    `huggingface_hub.snapshot_download`, `unit="B"`), the files-count bar the
    thread pool makes, and -- in 1.32.0 -- a second byte bar for the network
    transfer. Per-file progress reaches the aggregate through hub's own
    `_AggregatedTqdm`, which adds the file's size to `total`, refreshes, and
    forwards `initial` and every chunk as `update`.
    """

    def __init__(self, tqdm_class, *, with_transfer_bar: bool = False):
        self.aggregate = tqdm_class(
            desc="Reconstructing (incomplete total...)",
            disable=True,
            total=0,
            initial=0,
            unit="B",
            unit_scale=True,
            name=HUB_SNAPSHOT_PROGRESS_BAR_NAME,
        )
        self.transfer = None
        if with_transfer_bar:
            self.transfer = tqdm_class(
                desc="Downloading bytes",
                disable=True,
                total=0,
                initial=0,
                unit="B",
                unit_scale=True,
                name=f"{HUB_SNAPSHOT_PROGRESS_BAR_NAME}.transfer",
            )
        self.files = tqdm_class(
            [], desc="Fetching 2 files", total=2, disable=True
        )

    def start_file(self, total, initial=0):
        self.aggregate.total = (self.aggregate.total or 0) + total
        if self.transfer is not None:
            self.transfer.total = (self.transfer.total or 0) + total
        self.aggregate.refresh()
        if initial:
            self.aggregate.update(initial)

    def chunk(self, size):
        self.aggregate.update(size)
        if self.transfer is not None:
            self.transfer.update(size)
        self.files.update(0)


def test_the_hub_tqdm_class_reports_only_the_aggregate_byte_bar():
    report = _Recorder()
    driver = _HubDriver(hub_progress_tqdm_class(report))

    driver.start_file(1_000)
    driver.chunk(400)
    driver.start_file(551_294_349)
    driver.chunk(600)

    seen = report.seen
    assert seen[-1] == (1_000, 551_295_349)
    # Only the aggregate ever reports; the files-count bar's own updates and
    # its total of 2 must never be read as bytes.
    assert all(total in (0, 1_000, 551_295_349) for _done, total in seen)


def test_the_transfer_bar_of_the_newer_hub_is_not_counted_twice():
    """1.32.0 adds a second `unit="B"` bar beside the reconstruct bar.

    It counts what came over the network, which Xet deduplication makes
    smaller than the file; summing both would count most bytes twice, so the
    class matches the reconstruct bar's exact name.
    """
    report = _Recorder()
    driver = _HubDriver(hub_progress_tqdm_class(report), with_transfer_bar=True)

    driver.start_file(1_000_000)
    driver.chunk(250_000)
    driver.chunk(250_000)

    assert report.seen[-1] == (500_000, 1_000_000)


def test_a_resumed_file_starts_at_the_bytes_already_on_disk():
    report = _Recorder()
    driver = _HubDriver(hub_progress_tqdm_class(report))

    driver.start_file(1_000_000, initial=400_000)
    driver.chunk(100_000)

    assert report.seen[-1] == (500_000, 1_000_000)


def test_a_resume_of_a_nearly_finished_download_reads_as_nearly_finished():
    """The whole chain for the case the reviewer reproduced.

    huggingface_hub returns a file that is already complete before it builds
    any bar, so the weight file reaches the `tqdm_class` neither as `done` nor
    as `total`: the only bar of this download is the 1,148-byte config. With
    the baseline added, the first event says 551 of 552 MB instead of 0.
    """
    baseline = 551_294_349
    report = _Recorder()
    driver = _HubDriver(
        hub_progress_tqdm_class(offset_progress_hook(report, baseline))
    )

    driver.start_file(1_148)
    driver.chunk(1_148)

    first = measure_model_download_progress(
        "granite-speech-5.0-470m-turboctc",
        report.seen[0][0],
        reported_total_bytes=report.seen[0][1],
    )
    last = measure_model_download_progress(
        "granite-speech-5.0-470m-turboctc",
        report.seen[-1][0],
        reported_total_bytes=report.seen[-1][1],
    )

    assert first.downloaded_bytes == baseline
    assert first.percent >= 99
    assert last.downloaded_bytes == baseline + 1_148


def _snapshot_download_driving_one_file(total: int, initial: int = 0):
    """Stand in for `snapshot_download`, fetching exactly one file."""

    def fake(_repo_id, **kwargs):
        tqdm_class = kwargs.get("tqdm_class")
        assert tqdm_class is not None, "no tqdm_class was installed"
        driver = _HubDriver(tqdm_class)
        driver.start_file(total, initial=initial)
        driver.chunk(total - initial)
        return "/snapshot"

    return fake


def test_download_model_snapshot_counts_the_files_already_on_disk(
    monkeypatch, tmp_path
):
    """The blob layout, through the real `download_model_snapshot`."""
    import huggingface_hub

    from stt_app.transcriber import local_faster_whisper

    destination = tmp_path / "models--Systran--faster-whisper-small"
    _write(destination / "blobs" / "weights", 40_000_000)
    _write(destination / "blobs" / "tokenizer.incomplete", 7_000)
    monkeypatch.setattr(
        huggingface_hub,
        "snapshot_download",
        _snapshot_download_driving_one_file(10_000, initial=7_000),
    )
    report = _Recorder()

    local_faster_whisper.download_model_snapshot(
        "small", str(tmp_path), progress_hook=report
    )

    # The complete blob is the baseline; the partial one is hub's `initial=`
    # and must not be counted twice.
    assert report.seen[-1] == (40_010_000, 40_010_000)


def test_download_webgpu_model_snapshot_counts_the_files_already_on_disk(
    monkeypatch, tmp_path
):
    """The flat `local_dir` layout, through the real ONNX download."""
    import huggingface_hub

    from stt_app.transcriber import local_webgpu_asr

    destination = tmp_path / "granite-speech-5.0-470m-turboctc-onnx"
    _write(destination / "onnx" / "model_int8.onnx", 4_000_000)
    monkeypatch.setattr(
        huggingface_hub,
        "snapshot_download",
        _snapshot_download_driving_one_file(1_148),
    )
    monkeypatch.setattr(
        local_webgpu_asr, "_verify_downloaded_layout", lambda *_args: None
    )
    report = _Recorder()

    local_webgpu_asr.download_webgpu_model_snapshot(
        "granite-speech-5.0-470m-turboctc", str(tmp_path), progress_hook=report
    )

    assert report.seen[-1] == (4_001_148, 4_001_148)


def test_a_download_with_no_hook_installs_nothing_and_scans_nothing(
    monkeypatch, tmp_path
):
    """The transcribers' own load-path downloads and `download_model.py`.

    They pass no hook, so neither the bar nor the baseline scan of the
    destination may run for them.
    """
    import huggingface_hub

    from stt_app.transcriber import local_faster_whisper

    seen: list[dict] = []
    monkeypatch.setattr(
        huggingface_hub,
        "snapshot_download",
        lambda _repo_id, **kwargs: (seen.append(kwargs), "/snapshot")[1],
    )
    monkeypatch.setattr(
        local_faster_whisper,
        "completed_download_bytes",
        lambda *_args: pytest.fail("scanned the destination with no hook"),
    )

    local_faster_whisper.download_model_snapshot("small", str(tmp_path))

    assert "tqdm_class" not in seen[0]


def test_a_reporting_hook_that_raises_cannot_break_the_download():
    def explode(_done, _total):
        raise RuntimeError("no")

    driver = _HubDriver(hub_progress_tqdm_class(explode))
    driver.start_file(1_000)
    driver.chunk(500)


def test_no_hook_means_no_tqdm_class_at_all():
    assert hub_progress_tqdm_class(None) is None


def test_report_unknown_download_progress_is_silent_and_optional():
    report = _Recorder()
    report_unknown_download_progress(None)
    report_unknown_download_progress(report)

    assert report.seen == [
        (DOWNLOAD_PROGRESS_UNKNOWN, DOWNLOAD_PROGRESS_UNKNOWN)
    ]
