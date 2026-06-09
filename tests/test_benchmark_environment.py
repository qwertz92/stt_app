from importlib import metadata

from stt_app import __version__
import stt_app.benchmark_environment as benchmark_environment


def test_framework_versions_include_python_node_and_source_runtimes(monkeypatch):
    python_versions = {
        "stt-app": __version__,
        "onnxruntime": "1.26.0",
        "onnxruntime-genai": "0.14.1",
    }
    node_versions = {
        "@huggingface/transformers": "4.1.0",
        "@huggingface/tokenizers": "0.1.3",
        "onnxruntime-node": "1.24.3",
        "onnxruntime-web": "1.26.0-dev",
    }

    def _version(package_name: str) -> str:
        if package_name in python_versions:
            return python_versions[package_name]
        raise metadata.PackageNotFoundError(package_name)

    monkeypatch.setattr(benchmark_environment.metadata, "version", _version)
    monkeypatch.setattr(
        benchmark_environment,
        "_node_package_version",
        lambda package_name: node_versions.get(package_name, ""),
    )
    monkeypatch.setattr(benchmark_environment, "_source_revision", lambda: "abc123")
    monkeypatch.setattr(
        benchmark_environment,
        "_cuda_versions",
        lambda: {"CUDA driver API": "13.0"},
    )

    versions = benchmark_environment._framework_versions()

    assert versions["stt_app"] == __version__
    assert "stt_app installed metadata" not in versions
    assert versions["stt_app source"] == "abc123"
    assert versions["ONNX Runtime"] == "1.26.0"
    assert versions["ORT GenAI"] == "0.14.1"
    assert versions["Nemotron providers"] == "CPU only"
    assert versions["ONNX Runtime Node"] == "1.24.3"
    assert versions["ONNX Runtime Web"] == "1.26.0-dev"
    assert versions["CUDA driver API"] == "13.0"


def test_node_package_version_prefers_installed_package(monkeypatch, tmp_path):
    package_dir = tmp_path / "node_modules" / "onnxruntime-node"
    package_dir.mkdir(parents=True)
    (package_dir / "package.json").write_text(
        '{"version": "1.24.3"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(benchmark_environment, "_project_root", lambda: tmp_path)

    assert benchmark_environment._node_package_version("onnxruntime-node") == "1.24.3"


def test_cuda_versions_reports_when_cuda_is_not_detected(monkeypatch):
    monkeypatch.setattr(benchmark_environment, "_command_lines", lambda *_args: [])

    assert benchmark_environment._cuda_versions() == {"CUDA": "not detected"}
