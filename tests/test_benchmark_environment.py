import json
from importlib import metadata

import pytest

import stt_app.benchmark_environment as benchmark_environment
from stt_app import __version__
from stt_app.benchmark_environment import BenchmarkEnvironment


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


# The payload one real machine returned for the collector's PowerShell call
# (AMD Ryzen 5 7600X, 2 x 16 GB DDR5 running at its JEDEC 4800 MT/s because
# EXPO is off). Kept verbatim so the parser is exercised against a shape that
# was observed rather than imagined.
_REAL_PAYLOAD: dict = {
    "cpu": [
        {
            "Name": "AMD Ryzen 5 7600X 6-Core Processor             ",
            "MaxClockSpeed": 4701,
            "NumberOfCores": 6,
            "NumberOfLogicalProcessors": 12,
            "L2CacheSize": 6144,
            "L3CacheSize": 32768,
        }
    ],
    "memory": [
        {
            "Capacity": 17179869184,
            "Speed": 4800,
            "ConfiguredClockSpeed": 4800,
            "SMBIOSMemoryType": 34,
        },
        {
            "Capacity": 17179869184,
            "Speed": 4800,
            "ConfiguredClockSpeed": 4800,
            "SMBIOSMemoryType": 34,
        },
    ],
}

_REAL_MEMORY_LABEL = (
    "2 x 16 GB DDR5-4800, running at 4800 MT/s (38.4 GB/s per channel, theoretical)"
)


def test_hardware_facts_read_a_list_shaped_payload():
    facts = benchmark_environment._hardware_facts_from_payload(_REAL_PAYLOAD)

    assert facts.cpu == "AMD Ryzen 5 7600X 6-Core Processor"
    assert facts.physical_cores == 6
    assert facts.cpu_clock == "4.70 GHz nominal"
    assert facts.cpu_cache == "L2 6 MB, L3 32 MB"
    assert facts.memory_modules == _REAL_MEMORY_LABEL


def test_hardware_facts_read_a_bare_object_payload():
    """ConvertTo-Json of older PowerShell unwraps a one-element array."""
    payload = {
        "cpu": _REAL_PAYLOAD["cpu"][0],
        "memory": _REAL_PAYLOAD["memory"][0],
    }

    facts = benchmark_environment._hardware_facts_from_payload(payload)

    assert facts.cpu == "AMD Ryzen 5 7600X 6-Core Processor"
    assert facts.physical_cores == 6
    assert facts.cpu_clock == "4.70 GHz nominal"
    assert facts.cpu_cache == "L2 6 MB, L3 32 MB"
    assert facts.memory_modules == (
        "1 x 16 GB DDR5-4800, running at 4800 MT/s "
        "(38.4 GB/s per channel, theoretical)"
    )


def test_hardware_facts_sum_the_cores_of_every_socket():
    """Two sockets: the cores add up, the other facts describe one processor."""
    processor = {
        "Name": "Intel Xeon Gold 6338",
        "MaxClockSpeed": 2000,
        "NumberOfCores": 32,
        "L2CacheSize": 40960,
        "L3CacheSize": 49152,
    }
    payload = {"cpu": [dict(processor), dict(processor)], "memory": []}

    facts = benchmark_environment._hardware_facts_from_payload(payload)

    assert facts.physical_cores == 64
    assert facts.cpu == "Intel Xeon Gold 6338"
    assert facts.cpu_clock == "2.00 GHz nominal"
    assert facts.cpu_cache == "L2 40 MB, L3 48 MB"
    assert facts.memory_modules == ""


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("None", None),
        ("a list", [1, 2, 3]),
        ("a string", "Get-CimInstance : Access is denied."),
        ("wrong value types", {"cpu": 7, "memory": 7}),
        ("empty arrays", {"cpu": [], "memory": []}),
        ("entries that are not objects", {"cpu": ["x"], "memory": ["y"]}),
    ],
)
def test_hardware_facts_from_an_unusable_payload_are_all_empty(label, payload):
    facts = benchmark_environment._hardware_facts_from_payload(payload)

    assert facts.cpu == "", label
    assert facts.physical_cores == 0, label
    assert facts.cpu_clock == "", label
    assert facts.cpu_cache == "", label
    assert facts.memory_modules == "", label


@pytest.mark.parametrize(
    ("max_clock_mhz", "expected"),
    [
        (0, ""),
        (-1, ""),
        (4701, "4.70 GHz nominal"),
        (3000, "3.00 GHz nominal"),
        (2496, "2.50 GHz nominal"),
    ],
)
def test_cpu_clock_label(max_clock_mhz, expected):
    assert benchmark_environment._cpu_clock_label(max_clock_mhz) == expected


@pytest.mark.parametrize(
    ("l2_kb", "l3_kb", "expected"),
    [
        (0, 0, ""),
        (6144, 32768, "L2 6 MB, L3 32 MB"),
        (6144, 0, "L2 6 MB"),
        (0, 32768, "L3 32 MB"),
        (2560, 0, "L2 2.5 MB"),
        (-4, -4, ""),
    ],
)
def test_cpu_cache_label(l2_kb, l3_kb, expected):
    assert benchmark_environment._cpu_cache_label(l2_kb, l3_kb) == expected


def _module(
    capacity: object = 17179869184,
    speed: int | None = 6000,
    configured: int | None = 6000,
    type_code: int = 34,
) -> dict:
    module: dict = {"Capacity": capacity, "SMBIOSMemoryType": type_code}
    if speed is not None:
        module["Speed"] = speed
    if configured is not None:
        module["ConfiguredClockSpeed"] = configured
    return module


@pytest.mark.parametrize(
    ("label", "modules", "expected"),
    [
        ("no modules", [], ""),
        (
            "two identical modules at their rated speed",
            [_module(), _module()],
            "2 x 16 GB DDR5-6000, running at 6000 MT/s "
            "(48.0 GB/s per channel, theoretical)",
        ),
        (
            "XMP/EXPO not enabled",
            [_module(configured=4800)],
            "1 x 16 GB DDR5-6000, rated 6000 MT/s, running at 4800 MT/s "
            "(38.4 GB/s per channel, theoretical)",
        ),
        (
            "only the rated speed is known",
            [_module(configured=None)],
            "1 x 16 GB DDR5-6000, running at 6000 MT/s "
            "(48.0 GB/s per channel, theoretical)",
        ),
        (
            "only the configured speed is known",
            [_module(speed=None, configured=4800)],
            "1 x 16 GB DDR5-4800, running at 4800 MT/s "
            "(38.4 GB/s per channel, theoretical)",
        ),
        (
            "no speed at all: no rate suffix and no bandwidth clause",
            [_module(speed=None, configured=None)],
            "1 x 16 GB DDR5",
        ),
        (
            "a speed of zero counts as unknown",
            [_module(speed=0, configured=0)],
            "1 x 16 GB DDR5",
        ),
        (
            "an unknown type code",
            [_module(type_code=99)],
            "1 x 16 GB RAM-6000, running at 6000 MT/s "
            "(48.0 GB/s per channel, theoretical)",
        ),
        (
            "SMBIOS 26 is DDR4",
            [_module(type_code=26, speed=3200, configured=3200)],
            "1 x 16 GB DDR4-3200, running at 3200 MT/s "
            "(25.6 GB/s per channel, theoretical)",
        ),
        (
            "SMBIOS 18 is DDR, and 20 is DDR2 FB-DIMM rather than DDR",
            [_module(type_code=18, speed=400, configured=400)],
            "1 x 16 GB DDR-400, running at 400 MT/s "
            "(3.2 GB/s per channel, theoretical)",
        ),
        (
            "SMBIOS 19 is DDR2, and 21 is reserved rather than DDR2",
            [_module(type_code=19, speed=800, configured=800)],
            "1 x 16 GB DDR2-800, running at 800 MT/s "
            "(6.4 GB/s per channel, theoretical)",
        ),
        (
            "SMBIOS 20 is DDR2 FB-DIMM",
            [_module(type_code=20, speed=667, configured=667)],
            "1 x 16 GB DDR2 FB-DIMM-667, running at 667 MT/s "
            "(5.3 GB/s per channel, theoretical)",
        ),
        (
            "SMBIOS 21 is reserved, so it reads as plain RAM",
            [_module(type_code=21, speed=None, configured=None)],
            "1 x 16 GB RAM",
        ),
        (
            "a 32 GB module",
            [_module(capacity=34359738368)],
            "1 x 32 GB DDR5-6000, running at 6000 MT/s "
            "(48.0 GB/s per channel, theoretical)",
        ),
        (
            "a module that says nothing at all is skipped",
            [{}],
            "",
        ),
        (
            "a capacity handed over as a string still counts",
            [{"Capacity": "17179869184", "SMBIOSMemoryType": 34}],
            "1 x 16 GB DDR5",
        ),
        (
            "an unknown capacity leaves the size out",
            [_module(capacity=0, speed=None, configured=None)],
            "1 x DDR5",
        ),
    ],
)
def test_memory_modules_label(label, modules, expected):
    assert benchmark_environment._memory_modules_label(modules) == expected, label


def test_memory_modules_label_joins_two_different_groups():
    modules = [
        _module(),
        _module(),
        _module(capacity=8589934592, speed=5600, configured=4800),
    ]

    assert benchmark_environment._memory_modules_label(modules) == (
        "2 x 16 GB DDR5-6000, running at 6000 MT/s "
        "(48.0 GB/s per channel, theoretical)"
        " + "
        "1 x 8 GB DDR5-5600, rated 5600 MT/s, running at 4800 MT/s "
        "(38.4 GB/s per channel, theoretical)"
    )


def _only_the_hardware_query(payload_text: str):
    """Answer the hardware query only, so the test starts no real process."""

    def _command_lines(args, *, timeout: float = 3.0) -> list[str]:
        if any("Win32_Processor" in str(part) for part in args):
            assert timeout >= 6.0, "the hardware query needs a 6 s budget"
            return payload_text.splitlines()
        return []

    return _command_lines


def test_collect_benchmark_environment_records_the_hardware_facts(monkeypatch):
    monkeypatch.setattr(benchmark_environment.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        benchmark_environment,
        "_command_lines",
        _only_the_hardware_query(json.dumps(_REAL_PAYLOAD)),
    )

    environment = benchmark_environment.collect_benchmark_environment()

    assert environment.cpu == "AMD Ryzen 5 7600X 6-Core Processor"
    assert environment.physical_cores == 6
    assert environment.cpu_clock == "4.70 GHz nominal"
    assert environment.cpu_cache == "L2 6 MB, L3 32 MB"
    assert environment.memory_modules == _REAL_MEMORY_LABEL


@pytest.mark.parametrize(
    ("label", "command_lines"),
    [
        ("the query fails or times out", lambda args, *, timeout=3.0: []),
        (
            "the output is not JSON",
            _only_the_hardware_query("Get-CimInstance : Access is denied."),
        ),
    ],
)
def test_collect_benchmark_environment_survives_a_failed_query(
    monkeypatch, label, command_lines
):
    """Every new fact stays empty and the CPU name falls back as before."""
    monkeypatch.setattr(benchmark_environment.platform, "system", lambda: "Windows")
    monkeypatch.setattr(benchmark_environment, "_command_lines", command_lines)
    monkeypatch.setattr(benchmark_environment.platform, "processor", lambda: "x86_64")

    environment = benchmark_environment.collect_benchmark_environment()

    assert environment.cpu == "x86_64", label
    assert environment.physical_cores == 0, label
    assert environment.cpu_clock == "", label
    assert environment.cpu_cache == "", label
    assert environment.memory_modules == "", label


def test_from_dict_without_the_new_keys_falls_back_to_the_defaults():
    """Every history entry written before this change still loads."""
    legacy = {
        "os": "Windows 11",
        "python": "CPython 3.12 64bit",
        "cpu": "AMD Ryzen",
        "logical_cpus": 12,
        "memory": "32.0 GB",
        "gpus": ["Intel Arc A750"],
        "frameworks": {"faster-whisper": "1.2.1"},
        "node": "v22.0.0",
    }

    environment = BenchmarkEnvironment.from_dict(legacy)

    assert environment.physical_cores == 0
    assert environment.cpu_clock == ""
    assert environment.cpu_cache == ""
    assert environment.memory_modules == ""


def test_from_dict_reads_the_new_keys_with_the_existing_tolerance():
    environment = BenchmarkEnvironment.from_dict(
        {
            "physical_cores": "6",
            "cpu_clock": "4.70 GHz nominal",
            "cpu_cache": "L2 6 MB, L3 32 MB",
            "memory_modules": _REAL_MEMORY_LABEL,
        }
    )

    assert environment.physical_cores == 6
    assert environment.cpu_clock == "4.70 GHz nominal"
    assert environment.cpu_cache == "L2 6 MB, L3 32 MB"
    assert environment.memory_modules == _REAL_MEMORY_LABEL
    assert BenchmarkEnvironment.from_dict({"physical_cores": "many"}).physical_cores == 0


def test_summary_details_lists_the_hardware_facts_beside_their_neighbours():
    environment = BenchmarkEnvironment(
        cpu="AMD Ryzen 5 7600X 6-Core Processor",
        logical_cpus=12,
        physical_cores=6,
        cpu_clock="4.70 GHz nominal",
        cpu_cache="L2 6 MB, L3 32 MB",
        memory="31.1 GB",
        memory_modules=_REAL_MEMORY_LABEL,
    )

    details = environment.summary_details()

    assert list(details) == [
        "OS",
        "Python",
        "CPU",
        "Logical CPU cores",
        "Physical CPU cores",
        "CPU clock",
        "CPU cache",
        "Memory",
        "Memory modules",
        "GPU",
        "Frameworks",
        "Node.js",
    ]
    assert details["Physical CPU cores"] == 6
    assert details["Memory modules"] == _REAL_MEMORY_LABEL


def test_summary_details_hides_an_unknown_physical_core_count():
    """Two of the four consumers render a bare int 0; "" is filtered by all."""
    details = BenchmarkEnvironment().summary_details()

    assert details["Physical CPU cores"] == ""
    assert details["Logical CPU cores"] == ""


def test_hardware_facts_take_a_null_cpu_name_as_no_name():
    """A `Name` WMI could not read arrived as JSON null and was recorded
    as the string 'None'."""
    payload = {
        "cpu": [{**_REAL_PAYLOAD["cpu"][0], "Name": None}],
        "memory": _REAL_PAYLOAD["memory"],
    }

    facts = benchmark_environment._hardware_facts_from_payload(payload)

    assert facts.cpu == ""
    assert facts.physical_cores == 6
