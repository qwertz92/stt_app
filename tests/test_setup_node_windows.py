from __future__ import annotations

import hashlib
import importlib.util
import io
import sys
import zipfile
from pathlib import Path

import pytest


def _load_setup_node_module():
    root = Path(__file__).resolve().parents[1]
    script_path = root / "scripts" / "setup_node_windows.py"
    spec = importlib.util.spec_from_file_location("setup_node_windows", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["setup_node_windows"] = module
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return output.getvalue()


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_validated_version_rejects_url_and_path_injection():
    module = _load_setup_node_module()

    with pytest.raises(ValueError, match="Invalid Node.js version"):
        module._validated_version("24.18.0/../../payload")


def test_download_verifies_published_checksum(monkeypatch, tmp_path):
    module = _load_setup_node_module()
    archive = _zip_bytes({"node-v24.18.0-win-x64/node.exe": b"node"})
    checksum = hashlib.sha256(archive).hexdigest()

    def fake_urlopen(url, timeout):
        assert timeout in {30, 120}
        if str(url).endswith("SHASUMS256.txt"):
            return _Response(
                f"{checksum}  node-v24.18.0-win-x64.zip\n".encode("ascii")
            )
        return _Response(archive)

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    destination = tmp_path / "node.zip"

    module._download("24.18.0", destination)

    assert destination.read_bytes() == archive


def test_download_rejects_mismatched_checksums_from_all_mirrors(
    monkeypatch, tmp_path
):
    module = _load_setup_node_module()
    archive = b"not-the-published-archive"

    def fake_urlopen(url, timeout):
        assert timeout in {30, 120}
        if str(url).endswith("SHASUMS256.txt"):
            return _Response(
                ("0" * 64 + "  node-v24.18.0-win-x64.zip\n").encode("ascii")
            )
        return _Response(archive)

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    destination = tmp_path / "node.zip"

    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        module._download("24.18.0", destination)

    assert not destination.exists()


def test_safe_extract_rejects_parent_traversal(tmp_path):
    module = _load_setup_node_module()
    archive_path = tmp_path / "malicious.zip"
    archive_path.write_bytes(_zip_bytes({"../outside.txt": b"escaped"}))
    target = tmp_path / "target"

    with zipfile.ZipFile(archive_path) as archive:
        with pytest.raises(RuntimeError, match="Unsafe path"):
            module._extract_zip_safely(archive, target)

    assert not (tmp_path / "outside.txt").exists()


def test_safe_extract_allows_expected_node_layout(tmp_path):
    module = _load_setup_node_module()
    archive_path = tmp_path / "node.zip"
    archive_path.write_bytes(
        _zip_bytes({"node-v24.18.0-win-x64/node.exe": b"node"})
    )
    target = tmp_path / "target"

    with zipfile.ZipFile(archive_path) as archive:
        module._extract_zip_safely(archive, target)

    assert (target / "node-v24.18.0-win-x64" / "node.exe").read_bytes() == b"node"
