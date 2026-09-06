from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class BenchmarkEnvironment:
    os: str = ""
    python: str = ""
    cpu: str = ""
    logical_cpus: int = 0
    physical_cores: int = 0
    cpu_clock: str = ""
    cpu_cache: str = ""
    memory: str = ""
    memory_modules: str = ""
    gpus: list[str] = field(default_factory=list)
    frameworks: dict[str, str] = field(default_factory=dict)
    node: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> BenchmarkEnvironment:
        if not isinstance(raw, dict):
            return cls()
        gpus = raw.get("gpus", [])
        frameworks = raw.get("frameworks", {})
        return cls(
            os=str(raw.get("os", "")),
            python=str(raw.get("python", "")),
            cpu=str(raw.get("cpu", "")),
            logical_cpus=_safe_int(raw.get("logical_cpus"), default=0),
            physical_cores=_safe_int(raw.get("physical_cores"), default=0),
            cpu_clock=str(raw.get("cpu_clock", "")),
            cpu_cache=str(raw.get("cpu_cache", "")),
            memory=str(raw.get("memory", "")),
            memory_modules=str(raw.get("memory_modules", "")),
            gpus=[str(item) for item in gpus if str(item).strip()]
            if isinstance(gpus, list)
            else [],
            frameworks={
                str(key): str(value)
                for key, value in frameworks.items()
                if str(key).strip() and str(value).strip()
            }
            if isinstance(frameworks, dict)
            else {},
            node=str(raw.get("node", "")),
        )

    def summary_details(self) -> dict[str, Any]:
        # `or ""` on the two core counts is load-bearing: two of the four
        # consumers of this mapping drop a value only when it is "" (or a
        # falsy container), and an int 0 passes both filters and renders as a
        # bare "0" for a machine whose core count could not be read.
        return {
            "OS": self.os,
            "Python": self.python,
            "CPU": self.cpu,
            "Logical CPU cores": self.logical_cpus or "",
            "Physical CPU cores": self.physical_cores or "",
            "CPU clock": self.cpu_clock,
            "CPU cache": self.cpu_cache,
            "Memory": self.memory,
            "Memory modules": self.memory_modules,
            "GPU": self.gpus,
            "Frameworks": [
                f"{name} {version}" for name, version in self.frameworks.items()
            ],
            "Node.js": self.node,
        }


@dataclass(slots=True, frozen=True)
class _HardwareFacts:
    """What one PowerShell query can say about this machine's CPU and RAM.

    Every field is best-effort: a query that fails, times out or returns
    something unparsable leaves all of them at these defaults, and the CPU
    name then falls back to `platform.processor()` exactly as it did before
    the query existed.
    """

    cpu: str = ""
    physical_cores: int = 0
    cpu_clock: str = ""
    cpu_cache: str = ""
    memory_modules: str = ""


def collect_benchmark_environment() -> BenchmarkEnvironment:
    hardware = _windows_hardware_facts()
    return BenchmarkEnvironment(
        os=_os_label(),
        python=_python_label(),
        cpu=_cpu_label(hardware.cpu),
        logical_cpus=os.cpu_count() or 0,
        physical_cores=hardware.physical_cores,
        cpu_clock=hardware.cpu_clock,
        cpu_cache=hardware.cpu_cache,
        memory=_memory_label(),
        memory_modules=hardware.memory_modules,
        gpus=_gpu_labels(),
        frameworks=_framework_versions(),
        node=_node_version(),
    )


def _os_label() -> str:
    release = platform.release()
    version = platform.version()
    machine = platform.machine()
    parts = [platform.system()]
    if release:
        parts.append(release)
    if version:
        parts.append(f"({version})")
    if machine:
        parts.append(machine)
    return " ".join(part for part in parts if part).strip()


def _python_label() -> str:
    implementation = platform.python_implementation()
    version = platform.python_version()
    bitness = platform.architecture()[0]
    return f"{implementation} {version} {bitness}".strip()


def _cpu_label(detected: str = "") -> str:
    if detected:
        return detected

    cpu = platform.processor().strip()
    if cpu:
        return cpu

    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        try:
            content = cpuinfo.read_text(encoding="utf-8", errors="ignore")
            for line in content.splitlines():
                if line.lower().startswith("model name"):
                    _, _, value = line.partition(":")
                    value = value.strip()
                    if value:
                        return value
        except OSError:
            pass
    return "Unknown CPU"


# One PowerShell process answers for both WMI classes. A launch is the
# expensive part of this collection -- measured on one Windows 11 machine, this
# whole query takes 1.35-1.41 s while the shorter video-controller query takes
# 0.35 s -- so the CPU name, which used to be a query of its own, now comes out
# of this payload and the added facts cost no extra process start.
# `@(...)` keeps a single-socket / single-module machine from collapsing to a
# bare object, which the parser nevertheless still accepts.
_HARDWARE_QUERY = (
    "@{ cpu = @(Get-CimInstance Win32_Processor | Select-Object Name, "
    "MaxClockSpeed, NumberOfCores, NumberOfLogicalProcessors, L2CacheSize, "
    "L3CacheSize); memory = @(Get-CimInstance Win32_PhysicalMemory | "
    "Select-Object Capacity, Speed, ConfiguredClockSpeed, SMBIOSMemoryType) }"
    " | ConvertTo-Json -Depth 3 -Compress"
)

# SMBIOS "Memory Device -- Type" (structure type 17, offset 12h), which is what
# `Win32_PhysicalMemory.SMBIOSMemoryType` reports verbatim. Transcribed from the
# DMTF table as implemented by dmidecode's `dmi_memory_device_type` and by
# smbios-lib: 0x12 DDR, 0x13 DDR2, 0x14 DDR2 FB-DIMM, 0x15-0x17 reserved,
# 0x18 DDR3, 0x19 FBD2, 0x1A DDR4, 0x1B-0x1E LPDDR..LPDDR4, 0x1F logical
# non-volatile device, 0x20 HBM, 0x21 HBM2, 0x22 DDR5, 0x23 LPDDR5, 0x24 HBM3.
# This is NOT the `Win32_PhysicalMemory.MemoryType` enumeration, where DDR is 20
# and DDR2 is 21; the two tables agree only from DDR3 (24) upwards, so reading
# one table with the other's numbers mislabels exactly the pre-DDR3 machines.
_SMBIOS_MEMORY_TYPES = {
    18: "DDR",
    19: "DDR2",
    20: "DDR2 FB-DIMM",
    24: "DDR3",
    25: "FBD2",
    26: "DDR4",
    27: "LPDDR",
    28: "LPDDR2",
    29: "LPDDR3",
    30: "LPDDR4",
    32: "HBM",
    33: "HBM2",
    34: "DDR5",
    35: "LPDDR5",
    36: "HBM3",
}

# Anything the table does not name is reported without a number, because the
# raw SMBIOS code says nothing to a reader and guessing a generation is worse
# than admitting the type is unknown.
_UNKNOWN_MEMORY_TYPE = "RAM"


def _windows_hardware_facts() -> _HardwareFacts:
    if platform.system().lower() != "windows":
        # Reading the same facts out of /proc/cpuinfo is a separate job and
        # nothing here needs it: the app only runs on Windows.
        return _HardwareFacts()
    lines = _command_lines(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            _HARDWARE_QUERY,
        ],
        timeout=6.0,
    )
    if not lines:
        return _HardwareFacts()
    try:
        # `-Compress` yields one line; joining is only for the case where it
        # does not, and JSON carries no raw newline inside a string anyway.
        payload = json.loads("".join(lines))
    except ValueError:
        return _HardwareFacts()
    return _hardware_facts_from_payload(payload)


def _hardware_facts_from_payload(payload: object) -> _HardwareFacts:
    if not isinstance(payload, dict):
        return _HardwareFacts()
    processors = _payload_entries(payload.get("cpu"))
    modules = _payload_entries(payload.get("memory"))
    first = processors[0] if processors else {}
    name = first.get("Name")
    return _HardwareFacts(
        # A `Name` WMI could not read is JSON null, and `str()` of that is
        # the word 'None'; no name is the honest value and the caller falls
        # back to `platform.processor()` for it.
        cpu=" ".join(name.split()) if isinstance(name, str) else "",
        physical_cores=sum(
            max(_safe_int(entry.get("NumberOfCores"), default=0), 0)
            for entry in processors
        ),
        cpu_clock=_cpu_clock_label(_safe_int(first.get("MaxClockSpeed"), default=0)),
        # Clock and cache describe one processor package. Two identical
        # sockets each have this much cache, and summing them would claim a
        # single shared cache that does not exist; only the core count adds up.
        cpu_cache=_cpu_cache_label(
            _safe_int(first.get("L2CacheSize"), default=0),
            _safe_int(first.get("L3CacheSize"), default=0),
        ),
        memory_modules=_memory_modules_label(modules),
    )


def _payload_entries(value: object) -> list[dict[str, Any]]:
    """`ConvertTo-Json` in older PowerShell unwraps a one-element array."""
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [entry for entry in value if isinstance(entry, dict)]
    return []


def _cpu_clock_label(max_clock_mhz: int) -> str:
    """`MaxClockSpeed` is the nominal (base) frequency Windows reports.

    Turbo is not in it and cannot be read from WMI, which is why the label
    says "nominal" rather than "max": a 4.70 GHz part that boosts to 5.30 GHz
    would otherwise be recorded as if 4.70 were its ceiling.
    """
    if max_clock_mhz <= 0:
        return ""
    return f"{max_clock_mhz / 1000:.2f} GHz nominal"


def _cpu_cache_label(l2_kb: int, l3_kb: int) -> str:
    """WMI reports cache sizes in KiB.

    They are rendered as "MB" the way every OS tool writes a cache size, so
    the number is really MiB: 6144 KiB reads as "6 MB". This is deliberately
    neither `_format_bytes` (whose 1024 ladder is for byte totals and always
    keeps one decimal) nor the decimal megabytes of `MODEL_ESTIMATED_SIZE_MB`.
    """
    parts = [
        f"{name} {_mebibytes(size_kb)} MB"
        for name, size_kb in (("L2", l2_kb), ("L3", l3_kb))
        if size_kb > 0
    ]
    return ", ".join(parts)


def _memory_modules_label(modules: list[dict[str, Any]]) -> str:
    """Describe the installed modules, grouped by everything that matters.

    The rated speed beside the configured one is what this label exists for:
    a kit sold as DDR5-6000 that runs at 4800 because XMP/EXPO was never
    enabled is invisible in the CPU name and in the RAM total, and it is a
    plausible explanation for a machine benchmarking below a comparable one.
    """
    groups: dict[tuple[int, int, int, int], int] = {}
    for module in modules:
        if not isinstance(module, dict):
            continue
        key = (
            max(_safe_int(module.get("Capacity"), default=0), 0),
            _safe_int(module.get("SMBIOSMemoryType"), default=0),
            max(_safe_int(module.get("Speed"), default=0), 0),
            max(_safe_int(module.get("ConfiguredClockSpeed"), default=0), 0),
        )
        capacity, type_code, rated, configured = key
        if (
            capacity <= 0
            and type_code not in _SMBIOS_MEMORY_TYPES
            and rated <= 0
            and configured <= 0
        ):
            # An entry that says nothing at all; "1 x RAM" would be noise.
            continue
        groups[key] = groups.get(key, 0) + 1
    return " + ".join(_memory_group_label(key, count) for key, count in groups.items())


def _memory_group_label(key: tuple[int, int, int, int], count: int) -> str:
    capacity, type_code, rated, configured = key
    # With only one of the two speeds known it has to serve as both: naming a
    # rated speed the module may not be running at is the misleading half.
    effective_rated = rated or configured
    effective_configured = configured or rated

    descriptor = _SMBIOS_MEMORY_TYPES.get(type_code, _UNKNOWN_MEMORY_TYPE)
    if effective_rated > 0:
        descriptor = f"{descriptor}-{effective_rated}"
    if capacity > 0:
        descriptor = f"{_drop_trailing_zero(_format_bytes(capacity))} {descriptor}"

    label = f"{count} x {descriptor}"
    if rated > 0 and configured > 0 and rated != configured:
        label += f", rated {rated} MT/s, running at {configured} MT/s"
    elif effective_configured > 0:
        label += f", running at {effective_configured} MT/s"
    if effective_configured > 0:
        # One 64-bit channel transfers 8 bytes per transfer. WMI does not say
        # how many channels are populated, so this is never multiplied and the
        # label says "per channel" instead of pretending to a system total.
        label += (
            f" ({effective_configured * 8 / 1000:.1f} GB/s per channel, theoretical)"
        )
    return label


def _mebibytes(size_kb: int) -> str:
    return _drop_trailing_zero(f"{size_kb / 1024:.1f}")


def _drop_trailing_zero(text: str) -> str:
    """Turn "16.0" into "16" and "16.0 GB" into "16 GB"."""
    number, separator, rest = text.partition(" ")
    if number.endswith(".0"):
        number = number[:-2]
    return number + separator + rest


def _memory_label() -> str:
    if platform.system().lower() == "windows":
        value = _windows_total_memory_bytes()
        if value > 0:
            return _format_bytes(value)

    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        try:
            content = meminfo.read_text(encoding="utf-8", errors="ignore")
            for line in content.splitlines():
                if line.startswith("MemTotal:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return _format_bytes(int(parts[1]) * 1024)
        except (OSError, ValueError):
            pass
    return ""


def _windows_total_memory_bytes() -> int:
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        state = MEMORYSTATUSEX()
        state.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(state)):
            return int(state.ullTotalPhys)
    except Exception:
        return 0
    return 0


def _gpu_labels() -> list[str]:
    if platform.system().lower() != "windows":
        return []
    lines = _command_lines(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            'Get-CimInstance Win32_VideoController | ForEach-Object { '
            '"$($_.Name) (driver $($_.DriverVersion))" }',
        ],
        timeout=4.0,
    )
    return _unique_nonempty(lines)


def _framework_versions() -> dict[str, str]:
    names = {
        "faster-whisper": "faster-whisper",
        "CTranslate2": "ctranslate2",
        "ONNX Runtime": "onnxruntime",
        "ONNX Runtime DirectML": "onnxruntime-directml",
        "ONNX Runtime GPU": "onnxruntime-gpu",
        "ORT GenAI": "onnxruntime-genai",
        "ORT GenAI DirectML": "onnxruntime-genai-directml",
        "ORT GenAI CUDA": "onnxruntime-genai-cuda",
        "PySide6": "PySide6",
        "NumPy": "numpy",
    }
    versions: dict[str, str] = {}
    for label, package_name in names.items():
        try:
            versions[label] = metadata.version(package_name)
        except metadata.PackageNotFoundError:
            continue

    if "ORT GenAI DirectML" in versions:
        versions["Nemotron providers"] = "DirectML"
    elif "ORT GenAI CUDA" in versions:
        versions["Nemotron providers"] = "CUDA"
    elif "ORT GenAI" in versions:
        versions["Nemotron providers"] = "CPU only"

    try:
        from . import __version__

        versions["stt_app"] = __version__
        installed_version = metadata.version("stt-app")
        if installed_version != __version__:
            versions["stt_app installed metadata"] = installed_version
    except metadata.PackageNotFoundError:
        pass
    except Exception:
        pass

    source_revision = _source_revision()
    if source_revision:
        versions["stt_app source"] = source_revision

    node_packages = {
        "Transformers.js": "@huggingface/transformers",
        "Tokenizers.js": "@huggingface/tokenizers",
        "ONNX Runtime Node": "onnxruntime-node",
        "ONNX Runtime Web": "onnxruntime-web",
    }
    for label, package_name in node_packages.items():
        version = _node_package_version(package_name)
        if version:
            versions[label] = version

    versions.update(_cuda_versions())
    return versions


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _node_package_version(package_name: str) -> str:
    root = _project_root()
    installed_path = root / "node_modules" / Path(package_name) / "package.json"
    if installed_path.exists():
        try:
            payload = json.loads(installed_path.read_text(encoding="utf-8"))
            version = str(payload.get("version", "")).strip()
            if version:
                return version
        except (OSError, ValueError):
            pass

    lock_path = root / "package-lock.json"
    if lock_path.exists():
        try:
            payload = json.loads(lock_path.read_text(encoding="utf-8"))
            packages = payload.get("packages", {})
            if isinstance(packages, dict):
                package = packages.get(f"node_modules/{package_name}", {})
                if isinstance(package, dict):
                    version = str(package.get("version", "")).strip()
                    if version:
                        return version
        except (OSError, ValueError):
            pass

    package_path = root / "package.json"
    if package_path.exists():
        try:
            payload = json.loads(package_path.read_text(encoding="utf-8"))
            dependencies = payload.get("dependencies", {})
            if isinstance(dependencies, dict):
                version = str(dependencies.get(package_name, "")).strip()
                if version:
                    return version
        except (OSError, ValueError):
            pass
    return ""


def _source_revision() -> str:
    return _first_command_line(
        [
            "git",
            "-C",
            str(_project_root()),
            "rev-parse",
            "--short=12",
            "HEAD",
        ]
    )


def _cuda_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    nvidia_smi = "\n".join(_command_lines(["nvidia-smi"]))
    match = re.search(r"CUDA Version:\s*([0-9.]+)", nvidia_smi)
    if match:
        versions["CUDA driver API"] = match.group(1)

    nvcc_lines = _command_lines(["nvcc", "--version"])
    for line in nvcc_lines:
        match = re.search(r"release\s+([0-9.]+)", line)
        if match:
            versions["CUDA Toolkit"] = match.group(1)
            break
    if not versions:
        versions["CUDA"] = "not detected"
    return versions


def _node_version() -> str:
    if shutil.which("node") is None:
        return ""
    return _first_command_line(["node", "--version"])


def _first_command_line(args: list[str]) -> str:
    lines = _command_lines(args)
    return lines[0] if lines else ""


def _command_lines(args: list[str], *, timeout: float = 3.0) -> list[str]:
    if shutil.which(args[0]) is None:
        return []
    kwargs: dict[str, Any] = {}
    if platform.system().lower() == "windows":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if creationflags:
            kwargs["creationflags"] = creationflags
    try:
        completed = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            **kwargs,
        )
    except Exception:
        return []
    if completed.returncode != 0:
        return []
    return [
        line.strip()
        for line in completed.stdout.splitlines()
        if line.strip() and not line.strip().startswith("---")
    ]


def _unique_nonempty(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = " ".join(str(value).split())
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _format_bytes(value: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    index = 0
    while size >= 1024.0 and index < len(units) - 1:
        size /= 1024.0
        index += 1
    return f"{size:.1f} {units[index]}"


def _safe_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "BenchmarkEnvironment",
    "collect_benchmark_environment",
]
