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
import hashlib
import os
import platform
import re
import shutil
import ssl
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

DEFAULT_VERSION = "24.18.0"
_VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")

# Download roots, tried in order. Each release directory publishes both the
# archive and the authoritative SHASUMS256.txt file used to verify it.
_DOWNLOAD_ROOT_TEMPLATES = (
    "https://nodejs.org/dist/v{ver}",
    "https://npmmirror.com/mirrors/node/v{ver}",
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


def _validated_version(value: str) -> str:
    version = str(value or "").strip()
    if not _VERSION_RE.fullmatch(version):
        raise ValueError(
            f"Invalid Node.js version {value!r}; expected a numeric version like "
            f"{DEFAULT_VERSION}."
        )
    return version


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _expected_archive_sha256(checksums: str, archive_name: str) -> str:
    for line in checksums.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2:
            continue
        checksum, filename = parts
        if filename.lstrip("*") == archive_name and re.fullmatch(
            r"[0-9a-fA-F]{64}", checksum
        ):
            return checksum.lower()
    raise RuntimeError(f"No SHA-256 checksum found for {archive_name}.")


def _download(version: str, dest_zip: Path) -> None:
    version = _validated_version(version)
    archive_name = f"node-v{version}-win-x64.zip"
    errors: list[str] = []
    for template in _DOWNLOAD_ROOT_TEMPLATES:
        root_url = template.format(ver=version)
        archive_url = f"{root_url}/{archive_name}"
        checksums_url = f"{root_url}/SHASUMS256.txt"
        print(f"Downloading {archive_url} ...")
        try:
            with urllib.request.urlopen(archive_url, timeout=120) as response, open(
                dest_zip, "wb"
            ) as handle:
                shutil.copyfileobj(response, handle)
            if dest_zip.stat().st_size <= 0:
                raise RuntimeError("downloaded archive is empty")
            with urllib.request.urlopen(checksums_url, timeout=30) as response:
                checksums = response.read().decode("ascii")
            expected = _expected_archive_sha256(checksums, archive_name)
            actual = _sha256_file(dest_zip)
            if actual != expected:
                raise RuntimeError(
                    f"SHA-256 mismatch for {archive_name}: expected {expected}, "
                    f"received {actual}"
                )
            print(f"Verified SHA-256: {actual}")
            return
        except Exception as exc:  # noqa: BLE001 (report and try the next mirror)
            dest_zip.unlink(missing_ok=True)
            errors.append(f"{archive_url}: {exc}")
    raise RuntimeError(
        "Could not download Node.js from any mirror:\n  " + "\n  ".join(errors)
    )


def _extract_zip_safely(archive: zipfile.ZipFile, target_dir: Path) -> None:
    """Extract an archive only when every member remains below target_dir."""
    target_root = target_dir.resolve()
    for member in archive.infolist():
        member_path = (target_root / member.filename).resolve()
        try:
            member_path.relative_to(target_root)
        except ValueError as exc:
            raise RuntimeError(
                f"Unsafe path in Node.js archive: {member.filename!r}."
            ) from exc
    archive.extractall(target_root)


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


def configure_corporate_ca(target_dir: Path) -> None:
    """Make npm trust the corporate proxy CA (Zscaler etc.).

    Node/npm ship their own CA list and ignore the Windows certificate store, so
    behind a TLS-intercepting proxy `npm install` fails with
    `UNABLE_TO_GET_ISSUER_CERT_LOCALLY`. We export the Windows ROOT+CA stores to
    a PEM bundle and point npm/Node at it via NODE_EXTRA_CA_CERTS. Python (and
    therefore pip) already trusts these via the OS store, which is why pip works
    but npm does not.

    No-op if NODE_EXTRA_CA_CERTS is already set, or off Windows.
    """
    if platform.system() != "Windows":
        return
    if os.environ.get("NODE_EXTRA_CA_CERTS", "").strip():
        print(f"NODE_EXTRA_CA_CERTS already set: {os.environ['NODE_EXTRA_CA_CERTS']}")
        return
    if not hasattr(ssl, "enum_certificates"):
        return
    pems: list[str] = []
    for store in ("ROOT", "CA"):
        try:
            for cert, encoding, _trust in ssl.enum_certificates(store):
                if encoding == "x509_asn":
                    pems.append(ssl.DER_cert_to_PEM_cert(cert))
        except Exception:  # noqa: BLE001
            continue
    if not pems:
        return
    bundle = target_dir / "corporate-ca-bundle.pem"
    bundle.write_text("".join(pems), encoding="ascii")
    print(f"Exported {len(pems)} CA certificates to {bundle}")
    try:
        subprocess.run(
            ["setx", "NODE_EXTRA_CA_CERTS", str(bundle)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        print("Set NODE_EXTRA_CA_CERTS (takes effect for newly started programs).")
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: could not set NODE_EXTRA_CA_CERTS: {exc}")
        print(f'    setx NODE_EXTRA_CA_CERTS "{bundle}"')


def install(version: str, target_dir: Path, force: bool, skip_ca: bool = False) -> int:
    version = _validated_version(version)
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
                _extract_zip_safely(archive, target_dir)

    if not node_exe.is_file():
        print(f"ERROR: node.exe not found at {node_exe} after extraction.")
        return 1

    print(f"Node.js is ready: {node_exe}")
    if _set_node_path_env(node_exe):
        print("Set STT_APP_NODE_PATH (takes effect for newly started programs).")
    else:
        print("Set this environment variable manually and restart the app:")
        print(f'    setx STT_APP_NODE_PATH "{node_exe}"')

    if not skip_ca:
        configure_corporate_ca(target_dir)

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
        "--skip-ca",
        action="store_true",
        help="Do not export the corporate CA / set NODE_EXTRA_CA_CERTS.",
    )
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
    try:
        return install(args.version, target_dir, args.force, skip_ca=args.skip_ca)
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        parser.exit(1, f"ERROR: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
