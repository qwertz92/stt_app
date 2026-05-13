from __future__ import annotations

import json
import os
import platform
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
    memory: str = ""
    gpus: list[str] = field(default_factory=list)
    frameworks: dict[str, str] = field(default_factory=dict)
    node: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "BenchmarkEnvironment":
        if not isinstance(raw, dict):
            return cls()
        gpus = raw.get("gpus", [])
        frameworks = raw.get("frameworks", {})
        return cls(
            os=str(raw.get("os", "")),
            python=str(raw.get("python", "")),
            cpu=str(raw.get("cpu", "")),
            logical_cpus=_safe_int(raw.get("logical_cpus"), default=0),
            memory=str(raw.get("memory", "")),
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
        return {
            "OS": self.os,
            "Python": self.python,
            "CPU": self.cpu,
            "Logical CPU cores": self.logical_cpus or "",
            "Memory": self.memory,
            "GPU": self.gpus,
            "Frameworks": [
                f"{name} {version}" for name, version in self.frameworks.items()
            ],
            "Node.js": self.node,
        }


def collect_benchmark_environment() -> BenchmarkEnvironment:
    return BenchmarkEnvironment(
        os=_os_label(),
        python=_python_label(),
        cpu=_cpu_label(),
        logical_cpus=os.cpu_count() or 0,
        memory=_memory_label(),
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


def _cpu_label() -> str:
    if platform.system().lower() == "windows":
        cpu = _first_command_line(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Processor | "
                "Select-Object -First 1 -ExpandProperty Name",
            ]
        )
        if cpu:
            return cpu

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
            "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }",
        ],
        timeout=4.0,
    )
    return _unique_nonempty(lines)


def _framework_versions() -> dict[str, str]:
    names = {
        "faster-whisper": "faster-whisper",
        "CTranslate2": "ctranslate2",
        "PySide6": "PySide6",
        "NumPy": "numpy",
    }
    versions: dict[str, str] = {}
    for label, package_name in names.items():
        try:
            versions[label] = metadata.version(package_name)
        except metadata.PackageNotFoundError:
            continue

    transformers_js = _transformers_js_version()
    if transformers_js:
        versions["Transformers.js"] = transformers_js
    return versions


def _transformers_js_version() -> str:
    root = Path(__file__).resolve().parents[2]
    lock_path = root / "package-lock.json"
    if lock_path.exists():
        try:
            payload = json.loads(lock_path.read_text(encoding="utf-8"))
            packages = payload.get("packages", {})
            if isinstance(packages, dict):
                package = packages.get("node_modules/@huggingface/transformers", {})
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
                version = str(dependencies.get("@huggingface/transformers", "")).strip()
                if version:
                    return version
        except (OSError, ValueError):
            pass
    return ""


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
