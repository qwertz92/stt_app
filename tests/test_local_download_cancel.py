"""A canceled model download is a cancel in every local engine.

A transcriber that finds its model missing downloads it from its own load path,
and that download waits for the single machine-wide slot. Pressing Cancel there
raised ``ModelDownloadCanceled``, which each engine then either wrapped into
"Failed to download ..." or let escape as a bare ``RuntimeError`` -- both of
which the controller reports as a *failed* transcription. The user is shown an
error dialog for the thing they just asked to stop.
"""

from __future__ import annotations

import pytest

from stt_app.config import (
    CANARY_MODEL_SIZE,
    LOCAL_WEBGPU_MODEL_SIZES,
    NEMOTRON_MODEL_SIZE,
)
from stt_app.model_download_coordinator import ModelDownloadCanceled
from stt_app.transcriber.base import TranscriptionCanceled, TranscriptionError
from stt_app.transcriber.local_faster_whisper import LocalFasterWhisperTranscriber
from stt_app.transcriber.local_nemotron import LocalNemotronTranscriber
from stt_app.transcriber.local_onnx_asr import LocalOnnxAsrTranscriber
from stt_app.transcriber.local_webgpu_asr import LocalOnnxWebGpuTranscriber


def _raise_canceled(*_args, **_kwargs):
    raise ModelDownloadCanceled("Model download canceled.")


def _raise_failure(*_args, **_kwargs):
    raise OSError("the mirror is unreachable")


def _faster_whisper(monkeypatch, download):
    monkeypatch.setattr(
        "stt_app.model_download_coordinator.run_coordinated_download", download
    )
    monkeypatch.setattr(
        "stt_app.transcriber.local_faster_whisper.download_destination_dir",
        lambda *_a, **_k: None,
    )
    transcriber = LocalFasterWhisperTranscriber(model_size="small")
    return transcriber._coordinated_download_if_missing


def _onnx_asr(monkeypatch, download):
    monkeypatch.setattr(
        "stt_app.model_download_coordinator.run_coordinated_download", download
    )
    monkeypatch.setattr(
        "stt_app.transcriber.local_webgpu_asr.resolve_cached_webgpu_model_path",
        lambda *_a, **_k: None,
    )
    transcriber = LocalOnnxAsrTranscriber(model_size=CANARY_MODEL_SIZE)
    return transcriber._resolve_model_path


def _nemotron(monkeypatch, download):
    monkeypatch.setattr(
        "stt_app.transcriber.local_nemotron.run_coordinated_download", download
    )
    monkeypatch.setattr(
        "stt_app.transcriber.local_nemotron.resolve_cached_webgpu_model_path",
        lambda *_a, **_k: None,
    )
    transcriber = LocalNemotronTranscriber(model_size=NEMOTRON_MODEL_SIZE)
    return transcriber._ensure_snapshot


def _webgpu(monkeypatch, download):
    monkeypatch.setattr(
        "stt_app.transcriber.local_webgpu_asr.run_coordinated_download", download
    )
    monkeypatch.setattr(
        "stt_app.transcriber.local_webgpu_asr.resolve_cached_webgpu_model_path",
        lambda *_a, **_k: None,
    )
    transcriber = LocalOnnxWebGpuTranscriber(model_size=LOCAL_WEBGPU_MODEL_SIZES[0])
    return transcriber._ensure_snapshot


_ENGINES = [
    pytest.param(_faster_whisper, id="faster-whisper"),
    pytest.param(_onnx_asr, id="onnx-asr"),
    pytest.param(_nemotron, id="nemotron"),
    pytest.param(_webgpu, id="onnx-webgpu"),
]


@pytest.mark.parametrize("make_loader", _ENGINES)
def test_a_canceled_download_is_reported_as_a_cancel(monkeypatch, make_loader):
    load = make_loader(monkeypatch, _raise_canceled)

    with pytest.raises(TranscriptionCanceled):
        load()


@pytest.mark.parametrize("make_loader", _ENGINES)
def test_a_real_download_failure_is_still_an_error(monkeypatch, make_loader):
    """The other half: a broken mirror must not be silently swallowed as a
    cancel, which would leave the user with no transcript and no reason."""
    load = make_loader(monkeypatch, _raise_failure)

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
