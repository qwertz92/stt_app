"""Tests for scripts/import_model.py — LFS pointer detection and model validation."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_import_module():
    root = Path(__file__).resolve().parents[1]
    script_path = root / "scripts" / "import_model.py"
    spec = importlib.util.spec_from_file_location("import_model", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["import_model"] = module
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


# --- is_lfs_pointer tests ---


class TestIsLfsPointer:
    def test_detects_lfs_pointer_file(self, tmp_path):
        module = _load_import_module()
        lfs_content = (
            "version https://git-lfs.github.com/spec/v1\n"
            "oid sha256:abc123def456\n"
            "size 3000000000\n"
        )
        f = tmp_path / "model.bin"
        f.write_text(lfs_content, encoding="utf-8")
        assert module.is_lfs_pointer(f) is True

    def test_real_binary_is_not_lfs_pointer(self, tmp_path):
        module = _load_import_module()
        f = tmp_path / "model.bin"
        f.write_bytes(b"\x00" * 10_000_000)  # 10 MB binary blob
        assert module.is_lfs_pointer(f) is False

    def test_empty_file_is_not_lfs_pointer(self, tmp_path):
        module = _load_import_module()
        f = tmp_path / "model.bin"
        f.write_bytes(b"")
        assert module.is_lfs_pointer(f) is False

    def test_nonexistent_file_returns_false(self, tmp_path):
        module = _load_import_module()
        f = tmp_path / "no_such_file"
        assert module.is_lfs_pointer(f) is False

    def test_small_non_lfs_text_is_not_pointer(self, tmp_path):
        module = _load_import_module()
        f = tmp_path / "model.bin"
        f.write_text("this is just some text", encoding="utf-8")
        assert module.is_lfs_pointer(f) is False


# --- validate_model_files tests ---


class TestValidateModelFiles:
    def _create_valid_model_dir(self, path: Path, *, model_bin_size: int = 50_000_000):
        """Create a directory with valid model files."""
        path.mkdir(parents=True, exist_ok=True)
        (path / "config.json").write_text("{}")
        (path / "model.bin").write_bytes(b"\x00" * model_bin_size)
        (path / "tokenizer.json").write_text("{}")
        (path / "vocabulary.txt").write_text("hello\nworld")
        return path

    def test_valid_model_dir_passes(self, tmp_path):
        module = _load_import_module()
        model_dir = self._create_valid_model_dir(tmp_path / "model")
        is_valid, found, missing = module.validate_model_files(model_dir)
        assert is_valid is True
        assert len(missing) == 0
        assert "model.bin" in found
        assert "config.json" in found

    def test_missing_model_bin_fails(self, tmp_path):
        module = _load_import_module()
        model_dir = tmp_path / "model"
        model_dir.mkdir()
        (model_dir / "config.json").write_text("{}")
        (model_dir / "tokenizer.json").write_text("{}")
        (model_dir / "vocabulary.txt").write_text("hello")
        is_valid, _found, missing = module.validate_model_files(model_dir)
        assert is_valid is False
        assert "model.bin" in missing

    def test_lfs_pointer_model_bin_is_rejected(self, tmp_path):
        module = _load_import_module()
        model_dir = tmp_path / "model"
        model_dir.mkdir()
        (model_dir / "config.json").write_text("{}")
        (model_dir / "tokenizer.json").write_text("{}")
        (model_dir / "vocabulary.txt").write_text("hello")
        # Write an LFS pointer instead of real model weights
        lfs_content = (
            "version https://git-lfs.github.com/spec/v1\n"
            "oid sha256:abc123def456\n"
            "size 3000000000\n"
        )
        (model_dir / "model.bin").write_text(lfs_content, encoding="utf-8")

        is_valid, found, missing = module.validate_model_files(model_dir)
        assert is_valid is False
        assert any("LFS pointer" in m for m in missing)
        assert "model.bin" not in found

    def test_suspiciously_small_model_bin_is_rejected(self, tmp_path):
        module = _load_import_module()
        model_dir = tmp_path / "model"
        model_dir.mkdir()
        (model_dir / "config.json").write_text("{}")
        (model_dir / "tokenizer.json").write_text("{}")
        (model_dir / "vocabulary.txt").write_text("hello")
        # 100 KB — way too small for a real model (minimum 10 MB)
        (model_dir / "model.bin").write_bytes(b"\x00" * 100_000)

        is_valid, found, missing = module.validate_model_files(model_dir)
        assert is_valid is False
        assert any("too small" in m for m in missing)
        assert "model.bin" not in found

    def test_model_bin_above_threshold_passes(self, tmp_path):
        module = _load_import_module()
        model_dir = self._create_valid_model_dir(
            tmp_path / "model", model_bin_size=15_000_000
        )
        is_valid, found, _missing = module.validate_model_files(model_dir)
        assert is_valid is True
        assert "model.bin" in found
