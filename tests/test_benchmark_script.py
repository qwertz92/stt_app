from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from stt_app import local_benchmark


def _load_benchmark_module():
    root = Path(__file__).resolve().parents[1]
    script_path = root / "scripts" / "benchmark_local.py"
    spec = importlib.util.spec_from_file_location("benchmark_local", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["benchmark_local"] = module
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


def test_benchmark_csv_writer_creates_run_and_summary_rows(tmp_path):
    module = _load_benchmark_module()
    run = module.BenchmarkRun(
        run_index=1,
        seconds=1.2,
        audio_duration_seconds=2.0,
        real_time_factor=0.6,
        transcript_chars=12,
        transcript_words=2,
        detected_language="en",
        language_probability=0.98,
    )
    case = module.BenchmarkCase(
        model="small",
        device="cpu",
        compute_type="int8",
        download_seconds=0.0,
        load_seconds=0.5,
        runs=[run],
    )
    out_path = tmp_path / "bench.csv"

    module._write_csv(out_path, [case])

    text = out_path.read_text(encoding="utf-8")
    assert "row_type,model,device,compute_type" in text
    assert "run,small,cpu,int8,1" in text
    assert "summary,small,cpu,int8" in text


def test_successful_cases_filters_errors():
    module = _load_benchmark_module()
    ok_case = module.BenchmarkCase(
        model="small",
        device="cpu",
        compute_type="int8",
        download_seconds=0.0,
        load_seconds=0.5,
        runs=[
            module.BenchmarkRun(
                run_index=1,
                seconds=1.0,
                audio_duration_seconds=2.0,
                real_time_factor=0.5,
                transcript_chars=10,
                transcript_words=2,
                detected_language="en",
                language_probability=0.9,
            )
        ],
    )
    bad_case = module.BenchmarkCase(
        model="medium",
        device="cpu",
        compute_type="int8",
        download_seconds=0.0,
        load_seconds=0.6,
        runs=[],
        error="failed",
    )

    successful = module._successful_cases([ok_case, bad_case])
    assert successful == [ok_case]


def test_normalize_webgpu_benchmark_devices_expands_groups():
    module = _load_benchmark_module()

    assert module.normalize_webgpu_benchmark_devices("gpu,cpu") == ["gpu", "cpu"]
    assert module.normalize_webgpu_benchmark_devices("all") == [
        "webgpu",
        "dml",
        "cpu",
    ]


def test_run_benchmark_cases_expands_webgpu_device_targets(monkeypatch, tmp_path):
    audio_path = tmp_path / "sample.wav"
    audio_path.write_bytes(b"RIFF")

    def fake_webgpu_case(**kwargs):
        return local_benchmark.BenchmarkCase(
            model=kwargs["model_name"],
            device=kwargs["device"],
            compute_type="onnx-q4",
            download_seconds=0.0,
            load_seconds=0.1,
            runs=[],
        )

    monkeypatch.setattr(local_benchmark, "_run_webgpu_case", fake_webgpu_case)

    cases = local_benchmark.run_benchmark_cases(
        audio_path=audio_path,
        model_names=["cohere-transcribe-03-2026"],
        webgpu_devices="gpu,cpu",
    )

    assert [case.device for case in cases] == ["gpu", "cpu"]


def test_run_benchmark_cases_can_cancel_between_cases(monkeypatch, tmp_path):
    audio_path = tmp_path / "sample.wav"
    audio_path.write_bytes(b"RIFF")
    completed = []

    def fake_case(**kwargs):
        return local_benchmark.BenchmarkCase(
            model=kwargs["model_name"],
            device=kwargs["device"],
            compute_type=kwargs["compute_type"],
            download_seconds=0.0,
            load_seconds=0.1,
            runs=[],
        )

    monkeypatch.setattr(local_benchmark, "_run_case", fake_case)

    with pytest.raises(local_benchmark.BenchmarkCancelled):
        local_benchmark.run_benchmark_cases(
            audio_path=audio_path,
            model_names=["tiny", "base"],
            case_callback=completed.append,
            cancel_check=lambda: bool(completed),
        )

    assert [case.model for case in completed] == ["tiny"]


def test_webgpu_benchmark_case_closes_transcriber_when_preload_fails(
    monkeypatch,
    tmp_path,
):
    audio_path = tmp_path / "sample.wav"
    audio_path.write_bytes(b"RIFF")
    instances = []

    class FakeWebGpuTranscriber:
        def __init__(self, **kwargs):
            self.closed = False
            instances.append(self)

        def preload_model(self):
            raise RuntimeError("load failed")

        def close(self):
            self.closed = True

    monkeypatch.setattr(
        "stt_app.transcriber.local_webgpu_asr.LocalOnnxWebGpuTranscriber",
        FakeWebGpuTranscriber,
    )

    with pytest.raises(RuntimeError, match="load failed"):
        local_benchmark._run_webgpu_case(
            audio_path=audio_path,
            model_name="cohere-transcribe-03-2026",
            runs=1,
            language="en",
            warmup=False,
        )

    assert instances
    assert instances[0].closed is True


# --- BenchmarkCase download_seconds tests ---


class TestBenchmarkDownloadSeconds:
    def test_benchmark_case_has_download_seconds(self):
        module = _load_benchmark_module()
        case = module.BenchmarkCase(
            model="tiny",
            device="cpu",
            compute_type="int8",
            download_seconds=3.14,
            load_seconds=1.0,
            runs=[],
        )
        assert case.download_seconds == pytest.approx(3.14)

    def test_case_from_dict_parses_download_seconds(self):
        module = _load_benchmark_module()
        data = {
            "model": "small",
            "device": "cpu",
            "compute_type": "int8",
            "download_seconds": 5.5,
            "load_seconds": 2.0,
            "runs": [],
        }
        case = module._case_from_dict(data)
        assert case.download_seconds == pytest.approx(5.5)

    def test_case_from_dict_defaults_download_to_zero(self):
        module = _load_benchmark_module()
        data = {
            "model": "small",
            "device": "cpu",
            "compute_type": "int8",
            "load_seconds": 2.0,
            "runs": [],
        }
        case = module._case_from_dict(data)
        assert case.download_seconds == pytest.approx(0.0)

    def test_csv_includes_download_seconds_column(self, tmp_path):
        module = _load_benchmark_module()
        run = module.BenchmarkRun(
            run_index=1,
            seconds=1.0,
            audio_duration_seconds=2.0,
            real_time_factor=0.5,
            transcript_chars=10,
            transcript_words=2,
            detected_language="en",
            language_probability=0.9,
        )
        case = module.BenchmarkCase(
            model="tiny",
            device="cpu",
            compute_type="int8",
            download_seconds=4.2,
            load_seconds=0.5,
            runs=[run],
        )
        out_path = tmp_path / "bench.csv"
        module._write_csv(out_path, [case])

        text = out_path.read_text(encoding="utf-8")
        assert "download_seconds" in text
        assert "4.2" in text
