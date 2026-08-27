"""A canceled model download is a cancel in every local engine.

A transcriber that finds its model missing downloads it from its own load path,
and that download waits for the single machine-wide slot. Pressing Cancel there
raised ``ModelDownloadCanceled``, which each engine then either wrapped into
"Failed to download ..." or let escape as a bare ``RuntimeError`` -- both of
which the controller reports as a *failed* transcription. The user is shown an
error dialog for the thing they just asked to stop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stt_app.config import (
    CANARY_MODEL_SIZE,
    LOCAL_WEBGPU_MODEL_SIZES,
    NEMOTRON_MODEL_SIZE,
    PARAKEET_MODEL_SIZE,
)
from stt_app.model_download_coordinator import ModelDownloadCanceled
from stt_app.transcriber.base import TranscriptionCanceled, TranscriptionError
from stt_app.transcriber.local_faster_whisper import LocalFasterWhisperTranscriber
from stt_app.transcriber.local_nemotron import LocalNemotronTranscriber
from stt_app.transcriber.local_onnx_asr import LocalOnnxAsrTranscriber
from stt_app.transcriber.local_webgpu_asr import LocalOnnxWebGpuTranscriber


@pytest.fixture(autouse=True)
def _use_the_real_prefetch(real_model_prefetch):
    """This file drives the pre-fetch, which the suite stubs out."""
    return real_model_prefetch


def _raise_canceled(*_args, **_kwargs):
    raise ModelDownloadCanceled("Model download canceled.")


def _raise_failure(*_args, **_kwargs):
    raise OSError("the mirror is unreachable")


# Each builder installs `download` as the coordinated-download call and
# returns `(transcriber, loader)`; the loader is the method that reaches
# that download when the model is missing.
def _faster_whisper(monkeypatch, download):
    monkeypatch.setattr(
        "stt_app.model_download_coordinator.run_coordinated_download", download
    )
    monkeypatch.setattr(
        "stt_app.transcriber.local_faster_whisper.download_destination_dir",
        lambda *_a, **_k: None,
    )
    transcriber = LocalFasterWhisperTranscriber(model_size="small")
    return transcriber, transcriber._coordinated_download_if_missing


def _onnx_asr(monkeypatch, download):
    monkeypatch.setattr(
        "stt_app.model_download_coordinator.run_coordinated_download", download
    )
    monkeypatch.setattr(
        "stt_app.transcriber.local_webgpu_asr.resolve_cached_webgpu_model_path",
        lambda *_a, **_k: None,
    )
    transcriber = LocalOnnxAsrTranscriber(model_size=CANARY_MODEL_SIZE)
    return transcriber, transcriber._resolve_model_path


def _nemotron(monkeypatch, download):
    monkeypatch.setattr(
        "stt_app.transcriber.local_nemotron.run_coordinated_download", download
    )
    monkeypatch.setattr(
        "stt_app.transcriber.local_nemotron.resolve_cached_webgpu_model_path",
        lambda *_a, **_k: None,
    )
    transcriber = LocalNemotronTranscriber(model_size=NEMOTRON_MODEL_SIZE)
    return transcriber, transcriber._ensure_snapshot


def _webgpu(monkeypatch, download):
    monkeypatch.setattr(
        "stt_app.transcriber.local_webgpu_asr.run_coordinated_download", download
    )
    monkeypatch.setattr(
        "stt_app.transcriber.local_webgpu_asr.resolve_cached_webgpu_model_path",
        lambda *_a, **_k: None,
    )
    transcriber = LocalOnnxWebGpuTranscriber(model_size=LOCAL_WEBGPU_MODEL_SIZES[0])
    return transcriber, transcriber._ensure_snapshot


_ENGINES = [
    pytest.param(_faster_whisper, id="faster-whisper"),
    pytest.param(_onnx_asr, id="onnx-asr"),
    pytest.param(_nemotron, id="nemotron"),
    pytest.param(_webgpu, id="onnx-webgpu"),
]


@pytest.mark.parametrize("make_loader", _ENGINES)
def test_a_canceled_download_is_reported_as_a_cancel(monkeypatch, make_loader):
    _transcriber, load = make_loader(monkeypatch, _raise_canceled)

    with pytest.raises(TranscriptionCanceled):
        load()


@pytest.mark.parametrize("make_loader", _ENGINES)
def test_a_real_download_failure_is_still_an_error(monkeypatch, make_loader):
    """The other half: a broken mirror must not be silently swallowed as a
    cancel, which would leave the user with no transcript and no reason."""
    _transcriber, load = make_loader(monkeypatch, _raise_failure)

    with pytest.raises((TranscriptionError, OSError)) as excinfo:
        load()
    assert not isinstance(excinfo.value, TranscriptionCanceled)


def test_a_canceled_download_ends_a_benchmark_instead_of_failing_a_case(
    monkeypatch, tmp_path
):
    """A benchmark model load reaches the same download slot.

    `run_benchmark_cases` turns every non-`BenchmarkCancelled` exception into a
    recorded case with an `error`, and those rows are written to the persistent
    benchmark history. Pressing Cancel while a benchmark was fetching a model
    therefore left a permanent "error" row for something the user stopped.
    """
    from stt_app import local_benchmark

    def _cancel_the_download(**_kwargs):
        raise TranscriptionCanceled("Model download canceled.")

    monkeypatch.setattr(local_benchmark, "_run_case", _cancel_the_download)
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"RIFF")
    recorded: list[object] = []

    with pytest.raises(local_benchmark.BenchmarkCancelled):
        local_benchmark.run_benchmark_cases(
            audio_path=audio,
            model_names=["small"],
            case_callback=recorded.append,
        )

    assert recorded == []


def test_the_benchmark_installs_its_cancel_check_on_the_transcriber(monkeypatch):
    """`_raise_if_canceled` only polls *between* measurable steps.

    The model load is one of those steps, and for an uncached model it
    downloads through the machine-wide slot -- an unbounded wait, with the
    transcriber's own cancel check as the only interrupt. Without installing
    it, a benchmark could not be stopped during the load at all, which is
    where a first run spends most of its time. The app kills the whole worker
    process instead, but `run_benchmark_cases` is also called in-process by
    the CLI, where nothing else can stop it.
    """
    from stt_app import local_benchmark
    from stt_app.transcriber import local_onnx_asr as onnx_asr_module

    installed: list[object] = []

    class RecordingTranscriber:
        runtime_device = "cpu"
        runtime_details_text = ""

        def __init__(self, **_kwargs):
            pass

        def set_cancel_check(self, cancel_check):
            installed.append(cancel_check)

        def preload_model(self):
            assert installed and installed[-1] is not None, (
                "the load runs before the cancel check is installed"
            )

        def transcribe_batch(self, _audio):
            return "hello"

        def close(self):
            pass

    monkeypatch.setattr(
        onnx_asr_module, "LocalOnnxAsrTranscriber", RecordingTranscriber
    )
    check = object.__new__(type("Check", (), {"__call__": lambda self: False}))

    case = local_benchmark._run_onnx_case(
        audio_path=Path("samples/benchmark_sample.wav"),
        model_name=PARAKEET_MODEL_SIZE,
        runs=1,
        language=None,
        warmup=False,
        cancel_check=check,
    )

    assert case.runs
    # Installed for the run, then cleared: the benchmark builds a fresh
    # transcriber per case, but a leaked check would outlive its own run.
    assert len(installed) == 2 and installed[-1] is None


@pytest.mark.parametrize("make_loader", _ENGINES)
def test_the_download_gets_the_base_check_not_the_raw_attribute(
    monkeypatch, make_loader
):
    """`_cancel_check` is the user's callable; `_is_cancel_requested` is not.

    The base method never raises, logs a broken check once and then latches.
    The coordinator re-raises whatever escapes the check it is given, so
    handing it the raw attribute meant a user check that raised failed the
    *download* instead of stopping it -- and did so on every poll, with no
    log line naming the check as the cause.
    """
    seen: dict[str, object] = {}

    def _capture(*_args, **kwargs):
        seen.update(kwargs)
        raise ModelDownloadCanceled("Model download canceled.")

    transcriber, load = make_loader(monkeypatch, _capture)
    transcriber.set_cancel_check(lambda: (_ for _ in ()).throw(ValueError("boom")))

    with pytest.raises(TranscriptionCanceled):
        load()

    check = seen.get("cancel_check")
    assert check is not None, "the download was started without a cancel check"
    assert check == transcriber._is_cancel_requested, (
        "the download was handed the raw `_cancel_check` attribute"
    )
    # The point of the base method: a check that raises is absorbed, not
    # propagated into the download.
    assert check() is False
