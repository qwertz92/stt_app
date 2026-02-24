from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


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
