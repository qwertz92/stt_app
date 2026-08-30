#!/usr/bin/env python3
r"""Deterministic, no-admin Node.js bootstrap for the GPU/ONNX (WebGPU) models.

The Cohere and IBM Granite Speech models run through a small Node.js helper
(`@huggingface/transformers`, which brings its own pinned `onnxruntime-node`).
They therefore need a Node.js runtime on the machine that runs the app (native
Windows, not WSL).

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
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

DEFAULT_VERSION = "24.18.0"
_VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")

# Download roots, tried in order. Only the first is nodejs.org, and only
# nodejs.org publishes the authoritative SHASUMS256.txt: taking the checksum
# from whichever root served the archive means the mirror vouches for its own
# bytes, which cannot detect a substituted archive from that mirror.
_UPSTREAM_ROOT_TEMPLATE = "https://nodejs.org/dist/v{ver}"
_DOWNLOAD_ROOT_TEMPLATES = (
    _UPSTREAM_ROOT_TEMPLATE,
    "https://npmmirror.com/mirrors/node/v{ver}",
)

# `urlopen` follows redirects with no scheme or host restriction -- CPython's
# HTTPRedirectHandler allows http, https, ftp and "" -- so an https request can
# be redirected to plaintext http and followed silently. These are the only
# hosts (or subdomains of them) a Node download may end up on.
_ALLOWED_DOWNLOAD_DOMAINS = ("nodejs.org", "npmmirror.com")


def _is_allowed_download_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return any(
        host == domain or host.endswith(f".{domain}")
        for domain in _ALLOWED_DOWNLOAD_DOMAINS
    )


def _open_download(url: str, timeout: float):
    """Open `url`, refusing a redirect that left https or the allowed hosts."""
    if not _is_allowed_download_url(url):
        raise RuntimeError(f"Refusing to download from {url}")
    response = urllib.request.urlopen(url, timeout=timeout)
    final_url = response.geturl()
    if not _is_allowed_download_url(final_url):
        response.close()
        raise RuntimeError(f"Download was redirected to an untrusted URL: {final_url}")
    return response


def _existing_node() -> str | None:
    configured = os.environ.get("STT_APP_NODE_PATH", "").strip()
    if configured and Path(configured).is_file():
        return configured
    found = shutil.which("node") or shutil.which("node.exe")
    if found:
        return found
    program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
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


def _fetch_checksums(version: str, archive_root: str) -> str:
    """Read SHASUMS256.txt, preferring nodejs.org over whoever served the zip.

    A mirror that supplies both the archive and the checksum it is verified
    against cannot fail that verification, so the checksum is fetched from
    nodejs.org whenever it is reachable -- including for a mirror download,
    where it is the only thing that makes the verification mean anything. The
    mirror exists for networks where nodejs.org is blocked, so falling back to
    its own copy keeps it usable; that case says out loud what it is worth.
    """
    upstream_url = f"{_UPSTREAM_ROOT_TEMPLATE.format(ver=version)}/SHASUMS256.txt"
    try:
        with _open_download(upstream_url, timeout=30) as response:
            return response.read().decode("ascii")
    except Exception as exc:
        fallback_url = f"{archive_root}/SHASUMS256.txt"
        if fallback_url == upstream_url:
            raise
        print(f"WARNING: could not reach {upstream_url}: {exc}")
        print(
            "WARNING: verifying the archive against the same mirror that served "
            "it, which cannot detect a substituted archive from that mirror."
        )
        with _open_download(fallback_url, timeout=30) as response:
            return response.read().decode("ascii")


def _download(version: str, dest_zip: Path) -> None:
    version = _validated_version(version)
    archive_name = f"node-v{version}-win-x64.zip"
    errors: list[str] = []
    for template in _DOWNLOAD_ROOT_TEMPLATES:
        root_url = template.format(ver=version)
        archive_url = f"{root_url}/{archive_name}"
        print(f"Downloading {archive_url} ...")
        try:
            with _open_download(archive_url, timeout=120) as response, open(
                dest_zip, "wb"
            ) as handle:
                shutil.copyfileobj(response, handle)
            if dest_zip.stat().st_size <= 0:
                raise RuntimeError("downloaded archive is empty")
            checksums = _fetch_checksums(version, root_url)
            expected = _expected_archive_sha256(checksums, archive_name)
            actual = _sha256_file(dest_zip)
            if actual != expected:
                raise RuntimeError(
                    f"SHA-256 mismatch for {archive_name}: expected {expected}, "
                    f"received {actual}"
                )
            print(f"Verified SHA-256: {actual}")
            return
        except Exception as exc:
            dest_zip.unlink(missing_ok=True)
            errors.append(f"{archive_url}: {exc}")
    raise RuntimeError(
        "Could not download Node.js from any mirror:\n  " + "\n  ".join(errors)
    )


# node.exe alone does not make a usable install: the app runs `npm install`
# on first ONNX use. An extraction cut short by Ctrl+C, a full disk or an AV
# quarantine can leave node.exe in place with npm missing.
_REQUIRED_NODE_FILES = (
    "node.exe",
    "npm.cmd",
    "npx.cmd",
    "node_modules/npm/bin/npm-cli.js",
)


def _missing_node_files(node_root: Path) -> list[str]:
    return [name for name in _REQUIRED_NODE_FILES if not (node_root / name).is_file()]


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
            capture_output=True,
            text=True,
        )
        return True
    except Exception as exc:
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
        except Exception:
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
            capture_output=True,
            text=True,
        )
        print("Set NODE_EXTRA_CA_CERTS (takes effect for newly started programs).")
    except Exception as exc:
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

    # Checking node.exe alone reported a half-extracted install as ready, and
    # --force could not repair it either: --force only skipped the
    # already-available early return above, while this guard still saw node.exe
    # and downloaded nothing.
    missing = _missing_node_files(node_root)
    if missing or force:
        if node_root.exists() and (missing or force):
            if missing:
                print(
                    f"Incomplete Node.js install at {node_root} (missing: "
                    + ", ".join(missing)
                    + "); reinstalling."
                )
            shutil.rmtree(node_root, ignore_errors=True)
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "node.zip"
            _download(version, zip_path)
            print(f"Extracting to {target_dir} ...")
            with zipfile.ZipFile(zip_path) as archive:
                _extract_zip_safely(archive, target_dir)

    missing = _missing_node_files(node_root)
    if missing:
        print(
            f"ERROR: Node.js install at {node_root} is incomplete after "
            "extraction; missing: " + ", ".join(missing)
        )
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
