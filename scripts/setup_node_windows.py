#!/usr/bin/env python3
r"""Deterministic, no-admin Node.js bootstrap for the GPU/ONNX (WebGPU) models.

The Cohere and IBM Granite Speech models run through a small Node.js helper
(`@huggingface/transformers` + `onnxruntime-node`). They therefore need a
Node.js runtime on the machine that runs the app (native Windows, not WSL).

On locked-down corporate machines the usual installers fail:

* the machine-wide Node.js MSI is blocked by organization policy
  (`winget install OpenJS.NodeJS.LTS` -> exit code 1625), and
* PowerShell may run in *ConstrainedLanguage* mode, which blocks the .NET calls
  normally used to set environment variables.

This script sidesteps both by installing the **portable Node.js ZIP** (no admin
required) into the user's profile and pointing the app at it through the
`STT_APP_NODE_PATH` environment variable, set with the native `setx` command
(which works even under ConstrainedLanguage). No LLM/agent is involved, so it is
safe for environments where those are disallowed.

Run it with the *Windows* Python interpreter (it writes Windows paths and the
Windows user registry), not inside WSL:

    python scripts\setup_node_windows.py                 # auto: download + configure
    python scripts\setup_node_windows.py --version 24.18.0
    python scripts\setup_node_windows.py --target-dir "D:\tools\node"
    python scripts\setup_node_windows.py --check         # only report current state

If Node.js is already reachable, the script reports it and does nothing unless
`--force` is given. The download uses nodejs.org with an automatic fallback to
the npmmirror.com mirror (useful when nodejs.org is also blocked).
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

DEFAULT_VERSION = "24.18.0"

# Download hosts, tried in order. Both expose the same
# node-v<ver>-win-x64.zip layout under /v<ver>/ (nodejs.org) or /<ver>/.
_DOWNLOAD_TEMPLATES = (
    "https://nodejs.org/dist/v{ver}/node-v{ver}-win-x64.zip",
    "https://npmmirror.com/mirrors/node/v{ver}/node-v{ver}-win-x64.zip",
)


def _existing_node() -> str | None:
    configured = os.environ.get("STT_APP_NODE_PATH", "").strip()
    if configured and Path(configured).is_file():
        return configured
    found = shutil.which("node") or shutil.which("node.exe")
    if found:
        return found
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    candidate = Path(program_files) / "nodejs" / "node.exe"
    if candidate.is_file():
        return str(candidate)
    return None


def _download(version: str, dest_zip: Path) -> None:
    errors: list[str] = []
    for template in _DOWNLOAD_TEMPLATES:
        url = template.format(ver=version)
        print(f"Downloading {url} ...")
        try:
            with urllib.request.urlopen(url, timeout=120) as response, open(
                dest_zip, "wb"
            ) as handle:
                shutil.copyfileobj(response, handle)
            if dest_zip.stat().st_size > 0:
                return
        except Exception as exc:  # noqa: BLE001 (report and try the next mirror)
            errors.append(f"{url}: {exc}")
    raise RuntimeError(
        "Could not download Node.js from any mirror:\n  " + "\n  ".join(errors)
    )


def _set_node_path_env(node_exe: Path) -> bool:
    """Persist STT_APP_NODE_PATH for the user via setx (no admin needed)."""
    try:
        subprocess.run(
            ["setx", "STT_APP_NODE_PATH", str(node_exe)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: could not set STT_APP_NODE_PATH automatically: {exc}")
        return False


def install(version: str, target_dir: Path, force: bool) -> int:
    existing = _existing_node()
    if existing and not force:
        print(f"Node.js already available: {existing}")
        print("Nothing to do (use --force to install the portable ZIP anyway).")
        return 0

    target_dir.mkdir(parents=True, exist_ok=True)
    node_root = target_dir / f"node-v{version}-win-x64"
    node_exe = node_root / "node.exe"

    if not node_exe.is_file():
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "node.zip"
            _download(version, zip_path)
            print(f"Extracting to {target_dir} ...")
            with zipfile.ZipFile(zip_path) as archive:
                archive.extractall(target_dir)

    if not node_exe.is_file():
        print(f"ERROR: node.exe not found at {node_exe} after extraction.")
        return 1

    print(f"Node.js is ready: {node_exe}")
    if _set_node_path_env(node_exe):
        print("Set STT_APP_NODE_PATH (takes effect for newly started programs).")
    else:
        print("Set this environment variable manually and restart the app:")
        print(f'    setx STT_APP_NODE_PATH "{node_exe}"')

    print("\nRestart the app; the GPU/ONNX models (Cohere, Granite) will use it.")
    print("The app auto-runs 'npm install' on first ONNX use; npm ships in the")
    print("same folder as node.exe and is located automatically.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install a portable Node.js runtime for the GPU/ONNX models.",
    )
    parser.add_argument("--version", default=DEFAULT_VERSION, help="Node.js version.")
    parser.add_argument(
        "--target-dir",
        default=None,
        help=r"Install directory (default: %USERPROFILE%\programs).",
    )
    parser.add_argument("--force", action="store_true", help="Install even if Node exists.")
    parser.add_argument(
        "--check", action="store_true", help="Only report the current Node.js state."
    )
    args = parser.parse_args()

    if platform.system() != "Windows":
        print(
            "This helper configures Windows. Run it with the Windows Python "
            "interpreter (the app runs on native Windows, not WSL)."
        )
        # Still allow --check to run for diagnostics.
        if not args.check:
            return 2

    if args.check:
        existing = _existing_node()
        print(f"STT_APP_NODE_PATH={os.environ.get('STT_APP_NODE_PATH', '') or '(unset)'}")
        print(f"Detected Node.js: {existing or '(none)'}")
        return 0 if existing else 1

    default_root = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "programs"
    target_dir = Path(args.target_dir) if args.target_dir else default_root
    return install(args.version, target_dir, args.force)


if __name__ == "__main__":
    raise SystemExit(main())
