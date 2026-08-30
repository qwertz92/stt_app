"""Tests for scripts/import_model.py — LFS pointer detection and model validation."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


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


def _create_small_import_source(path: Path, *, model_bytes: bytes = b"weights") -> Path:
    path.mkdir(parents=True)
    (path / "config.json").write_text("{}", encoding="utf-8")
    (path / "model.bin").write_bytes(model_bytes)
    (path / "tokenizer.json").write_text("{}", encoding="utf-8")
    (path / "vocabulary.txt").write_text("hello", encoding="utf-8")
    return path


def test_snapshot_hash_distinguishes_same_sized_weight_content(tmp_path):
    module = _load_import_module()
    first = _create_small_import_source(tmp_path / "first", model_bytes=b"aaaa")
    second = _create_small_import_source(tmp_path / "second", model_bytes=b"bbbb")

    assert module.compute_fake_hash(first) != module.compute_fake_hash(second)


def test_import_publishes_complete_snapshot_and_ref(tmp_path):
    module = _load_import_module()
    source = _create_small_import_source(tmp_path / "source")
    cache = tmp_path / "cache"

    snapshot = module.import_model(source, "small", target_dir=cache)

    assert {path.name for path in snapshot.iterdir()} == {
        "config.json",
        "model.bin",
        "tokenizer.json",
        "vocabulary.txt",
    }
    assert (snapshot.parents[1] / "refs" / "main").read_text(encoding="utf-8") == (
        snapshot.name
    )
    assert not list(snapshot.parent.glob(".import-incomplete-*"))


def test_import_repairs_stale_snapshot_at_matching_content_hash(tmp_path):
    module = _load_import_module()
    source = _create_small_import_source(tmp_path / "source")
    cache = tmp_path / "cache"
    snapshot_hash = module.compute_fake_hash(source)
    snapshots = cache / "models--Systran--faster-whisper-small" / "snapshots"
    stale_snapshot = snapshots / snapshot_hash
    stale_snapshot.mkdir(parents=True)
    (stale_snapshot / "config.json").write_text("{}", encoding="utf-8")

    snapshot = module.import_model(source, "small", target_dir=cache)

    assert snapshot == stale_snapshot
    assert (snapshot / "model.bin").read_bytes() == b"weights"
    assert module.compute_fake_hash(snapshot) == snapshot_hash
    assert not list(snapshots.glob(".*.displaced-*"))


def test_import_survives_another_importer_publishing_the_same_model(
    tmp_path,
    monkeypatch,
):
    """The handler for this was unreachable on Windows.

    `os.replace` cannot overwrite a directory: Windows raises PermissionError
    (WinError 5, measured for an empty and a non-empty destination alike) and
    POSIX raises ENOTEMPTY -- neither is the `FileExistsError` the clause named.
    So a second import of the same model, started while the first was still
    copying, died with an unhandled PermissionError after copying gigabytes and
    never wrote `refs/main`.
    """
    module = _load_import_module()
    source = _create_small_import_source(tmp_path / "source")
    cache = tmp_path / "cache"
    snapshot_hash = module.compute_fake_hash(source)
    repo = cache / "models--Systran--faster-whisper-small"
    snapshots = repo / "snapshots"
    rival = snapshots / snapshot_hash

    real_exists = Path.exists
    raced = []

    def the_other_importer_publishes_between_our_check_and_our_rename(
        self,
        *args,
        **kwargs,
    ):
        if self == rival and not raced:
            raced.append(True)
            _create_small_import_source(rival)
            return False
        return real_exists(self, *args, **kwargs)

    monkeypatch.setattr(
        Path,
        "exists",
        the_other_importer_publishes_between_our_check_and_our_rename,
    )

    snapshot = module.import_model(source, "small", target_dir=cache)

    assert raced, "the race window was never entered"
    assert snapshot == rival
    assert (snapshot / "model.bin").read_bytes() == b"weights"
    assert (repo / "refs" / "main").read_text(encoding="utf-8") == snapshot_hash
    assert not list(snapshots.glob(".import-incomplete-*"))


def test_a_locked_leftover_does_not_stop_the_reference_from_being_written(
    tmp_path,
    monkeypatch,
):
    """The displaced directory is unreferenced; failing to delete it is not.

    The cleanup ran between the successful rename and the `refs/main` write, so
    a Windows delete refused because a running app still had the old
    `model.bin` mapped aborted the import with the new snapshot already in
    place and nothing pointing at it -- the one state this function exists to
    avoid.
    """
    module = _load_import_module()
    source = _create_small_import_source(tmp_path / "source")
    cache = tmp_path / "cache"
    snapshot_hash = module.compute_fake_hash(source)
    repo = cache / "models--Systran--faster-whisper-small"
    stale = repo / "snapshots" / snapshot_hash
    stale.mkdir(parents=True)
    (stale / "config.json").write_text("{}", encoding="utf-8")

    real_unlink = os.unlink
    refused = []

    def the_old_weights_are_still_mapped(path, *args, **kwargs):
        if ".displaced-" in str(path):
            refused.append(str(path))
            raise PermissionError(13, "Access is denied")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", the_old_weights_are_still_mapped)

    snapshot = module.import_model(source, "small", target_dir=cache)

    assert refused, "the displaced directory was never cleaned up"
    assert (repo / "refs" / "main").read_text(encoding="utf-8") == snapshot_hash
    assert (snapshot / "model.bin").read_bytes() == b"weights"


def test_a_rename_that_fails_for_another_reason_is_not_mistaken_for_a_race(
    tmp_path,
    monkeypatch,
):
    """Widening the clause to `OSError` must not swallow a real failure.

    Only a destination that exists means another importer got there first;
    anything else has to keep propagating, or `refs/main` is written pointing
    at a snapshot that was never published.
    """
    module = _load_import_module()
    source = _create_small_import_source(tmp_path / "source")
    cache = tmp_path / "cache"
    repo = cache / "models--Systran--faster-whisper-small"

    real_replace = Path.replace

    def the_snapshots_directory_refuses_the_rename(self, target, *args, **kwargs):
        if ".import-incomplete-" in str(self):
            raise OSError(13, "Access is denied")
        return real_replace(self, target, *args, **kwargs)

    monkeypatch.setattr(Path, "replace", the_snapshots_directory_refuses_the_rename)

    with pytest.raises(OSError):
        module.import_model(source, "small", target_dir=cache)

    assert not (repo / "refs" / "main").exists()
    assert not list((repo / "snapshots").glob("*"))


def test_import_copy_failure_leaves_no_published_snapshot_or_ref(
    tmp_path,
    monkeypatch,
):
    module = _load_import_module()
    source = _create_small_import_source(tmp_path / "source")
    cache = tmp_path / "cache"
    original_copy = module.shutil.copy2
    calls = 0

    def fail_second_copy(src, dst):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("copy failed")
        return original_copy(src, dst)

    monkeypatch.setattr(module.shutil, "copy2", fail_second_copy)

    with pytest.raises(OSError, match="copy failed"):
        module.import_model(source, "small", target_dir=cache)

    model_root = cache / "models--Systran--faster-whisper-small"
    assert not list((model_root / "snapshots").iterdir())
    assert not (model_root / "refs" / "main").exists()


@pytest.mark.parametrize(
    ("folder", "explicit_model"),
    [
        ("parakeet-tdt-0.6b-v3", "parakeet-tdt-0.6b-v3"),
        ("parakeet-tdt-0.6b-v3", None),
    ],
    ids=["named explicitly", "auto-detected"],
)
def test_a_model_this_script_cannot_import_says_so_before_listing_files(
    tmp_path, monkeypatch, capsys, folder, explicit_model
):
    """The file validation used to run first and give impossible advice.

    `validate_model_files` looks for the CTranslate2 layout, so any other
    runtime's folder -- the app's default model included -- was reported as
    "MISSING FILES: model.bin, tokenizer.json, vocabulary.txt" with the advice
    to download them from the model's HuggingFace page. Those files do not
    exist in that repository, and the accurate message was unreachable.
    """
    module = _load_import_module()
    source = tmp_path / folder
    source.mkdir()
    (source / "config.json").write_text("{}", encoding="utf-8")
    (source / "encoder-model.int8.onnx").write_bytes(b"x")

    argv = ["import_model.py", str(source), "--validate-only"]
    if explicit_model is not None:
        argv += ["--model", explicit_model]
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as excinfo:
        module.main()

    assert excinfo.value.code == 1
    output = capsys.readouterr()
    combined = output.out + output.err
    assert "download_model.py" in combined
    assert "'parakeet-tdt-0.6b-v3' is not a CTranslate2/faster-whisper model" in combined, (
        "the folder name must resolve to the model it names, so the message "
        "says the model is out of scope instead of asking for a --model that "
        f"is then rejected: {combined}"
    )
    assert "Unknown model" not in combined, (
        "a model the app itself offers is not 'unknown' -- that wording "
        "contradicted the very next line, which explained it was out of "
        f"scope for this script: {combined}"
    )
    assert "MISSING FILES" not in combined, (
        "the CTranslate2 file list was printed for a model this script cannot "
        "import at all"
    )
    assert "HuggingFace page" not in combined


def test_a_whisper_folder_still_reaches_the_file_validation(
    tmp_path, monkeypatch, capsys
):
    """The reordering must not skip the check the script exists for."""
    module = _load_import_module()
    source = tmp_path / "faster-whisper-small"
    source.mkdir()
    (source / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        sys, "argv", ["import_model.py", str(source), "--validate-only"]
    )

    with pytest.raises(SystemExit):
        module.main()

    combined = "".join(capsys.readouterr())
    assert "Detected model: small" in combined
    assert "MISSING FILES" in combined


def test_a_faster_whisper_folder_under_an_odd_name_is_not_sent_to_the_other_script(
    tmp_path, monkeypatch, capsys
):
    """It only needs `--model`; telling it to go use `download_model.py` is wrong.

    That advice belongs to a folder holding a model this script cannot import.
    A complete CTranslate2 model whose folder is merely named something
    unrecognised is importable -- with `--model small` -- and the ONNX
    sentence contradicts the line printed directly above it.
    """
    module = _load_import_module()
    source = tmp_path / "my-model-folder"
    source.mkdir()
    for name in ("config.json", "tokenizer.json", "vocabulary.txt"):
        (source / name).write_text("{}", encoding="utf-8")
    (source / "model.bin").write_bytes(b"x" * 64)

    monkeypatch.setattr(sys, "argv", ["import_model.py", str(source), "--validate-only"])
    with pytest.raises(SystemExit) as excinfo:
        module.main()

    assert excinfo.value.code == 1
    combined = "".join(capsys.readouterr())
    assert "Could not auto-detect" in combined
    assert "--model" in combined
    # Not the whole script name: the git-lfs advice for a pointer-sized
    # `model.bin` legitimately names it. The wrong-runtime sentence is the
    # one that must not appear.
    assert "imports CTranslate2/faster-whisper models only" not in combined, (
        f"a CTranslate2 folder was told it holds the wrong runtime: {combined}"
    )


def test_validate_only_still_reports_the_files_when_the_name_is_unresolved(
    tmp_path, monkeypatch, capsys
):
    """`--validate-only` exists to report the file state; it must still do so.

    Moving the name decision ahead of the validation made an unresolved name
    exit before `Source:`, `Found files:` and the missing list were printed,
    so the flag's whole output disappeared for the folders most likely to need
    it.
    """
    module = _load_import_module()
    source = tmp_path / "my-model-folder-incomplete"
    source.mkdir()
    (source / "config.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["import_model.py", str(source), "--validate-only"])
    with pytest.raises(SystemExit) as excinfo:
        module.main()

    assert excinfo.value.code == 1
    combined = "".join(capsys.readouterr())
    assert "Source:" in combined
    assert "Found files: config.json" in combined
    assert "MISSING FILES" in combined, (
        f"the file diagnostic the flag exists for was not printed: {combined}"
    )


def test_an_empty_folder_under_a_known_name_is_still_told_what_is_missing(
    tmp_path, monkeypatch, capsys
):
    """An incomplete download is the case the advice exists for.

    The advice is withheld only for a folder holding another runtime's model,
    which is recognised by *nothing of the CTranslate2 layout being present
    and the name not resolving*. Gating on `found_files` alone collapses those
    two conditions into one and silences the empty-but-named folder as well,
    so `--model small` on an empty directory printed a bare `FAILED` and no
    diagnostics at all.
    """
    module = _load_import_module()
    source = tmp_path / "whatever"
    source.mkdir()

    monkeypatch.setattr(
        sys,
        "argv",
        ["import_model.py", str(source), "--model", "small", "--validate-only"],
    )
    with pytest.raises(SystemExit) as excinfo:
        module.main()

    assert excinfo.value.code == 1
    combined = "".join(capsys.readouterr())
    assert "MISSING FILES" in combined, (
        f"an incomplete download of a known model got no advice: {combined}"
    )
    assert "model.bin" in combined


def test_an_empty_model_argument_does_not_print_a_nameless_model_line(
    tmp_path, monkeypatch, capsys
):
    """`--model ""` is reachable, and it used to print `Model:` with nothing.

    The line is guarded on truthiness rather than `is not None` for exactly
    this: `None` means "detect it", but an empty string is a name the user
    typed, and reporting it as the model contradicts the "Unknown model ''"
    error printed two lines later.
    """
    module = _load_import_module()
    source = tmp_path / "whatever"
    source.mkdir()

    monkeypatch.setattr(
        sys,
        "argv",
        ["import_model.py", str(source), "--model", "", "--validate-only"],
    )
    with pytest.raises(SystemExit) as excinfo:
        module.main()

    assert excinfo.value.code == 1
    combined = "".join(capsys.readouterr())
    lines = [line.strip() for line in combined.splitlines()]
    assert "Model:" not in lines, f"an empty model line was printed: {combined}"
    assert any("Unknown model" in line for line in lines), combined


@pytest.mark.parametrize(
    ("label", "make_source", "expected"),
    [
        (
            "a complete model",
            lambda source: [
                (source / "config.json").write_text("{}", encoding="utf-8"),
                (source / "tokenizer.json").write_text("{}", encoding="utf-8"),
                (source / "vocabulary.txt").write_text("a\n", encoding="utf-8"),
                # Above `_MODEL_BIN_MIN_BYTES` (10 MB): a smaller file is
                # rejected as an incomplete download, which is a different
                # verdict than the one under test.
                (source / "model.bin").write_bytes(b"x" * 10_000_001),
            ],
            "OK: all required files are present",
        ),
        (
            "an incomplete one",
            lambda source: [(source / "config.json").write_text("{}", encoding="utf-8")],
            "FAILED: required files are missing",
        ),
    ],
)
def test_validate_only_prints_the_verdict_it_was_asked_for(
    tmp_path, monkeypatch, capsys, label, make_source, expected
):
    """The one answer the flag exists to produce, and nothing pinned it.

    Deleting the verdict left the whole module green: the surrounding tests
    assert on `Source:`, `Found files:` and `MISSING FILES`, all of which are
    printed with or without `--validate-only`. This is also the line whose own
    docstring records a previous defect in it -- it printed U+2713 and U+2717
    until redirected output on Windows made `print` raise.
    """
    module = _load_import_module()
    source = tmp_path / "small"
    source.mkdir()
    make_source(source)

    monkeypatch.setattr(
        sys, "argv", ["import_model.py", str(source), "--validate-only"]
    )
    if expected.startswith("OK"):
        module.main()
    else:
        with pytest.raises(SystemExit) as excinfo:
            module.main()
        assert excinfo.value.code == 1

    combined = "".join(capsys.readouterr())
    assert expected in combined, f"{label}: no verdict was printed\n{combined}"


def test_a_folder_holding_another_runtimes_model_is_not_told_what_to_download(
    tmp_path, monkeypatch, capsys
):
    """The withheld half of the advice gate, which had no test.

    Nothing of the CTranslate2 layout is present *and* the name did not
    resolve, so this is another runtime's model. Listing `config.json`,
    `model.bin`, `tokenizer.json` and `vocabulary.txt` for it is advice about
    files the repository does not contain, for a model this script cannot
    import at all -- which is exactly what the gate's own comment says it
    exists to prevent. Only the "must print" direction was covered, so
    removing the second half of the condition left the module green.
    """
    module = _load_import_module()
    source = tmp_path / "some-other-runtime-export"
    source.mkdir()
    (source / "model.onnx").write_bytes(b"onnx")

    monkeypatch.setattr(
        sys, "argv", ["import_model.py", str(source), "--validate-only"]
    )
    with pytest.raises(SystemExit) as excinfo:
        module.main()

    assert excinfo.value.code == 1
    combined = "".join(capsys.readouterr())
    assert "MISSING FILES" not in combined, (
        "the script listed files to download for a model it cannot import:\n"
        f"{combined}"
    )
    assert "Could not auto-detect" in combined, combined


def test_an_explicit_model_name_is_not_reported_as_detected(
    tmp_path, monkeypatch, capsys
):
    """`Detected model:` claims the script worked the name out of the folder.

    With `--model small` the user supplied it, and saying otherwise hides
    where the name came from when the folder is named something else entirely.
    """
    module = _load_import_module()
    source = tmp_path / "an-unrelated-folder-name"
    source.mkdir()

    monkeypatch.setattr(
        sys,
        "argv",
        ["import_model.py", str(source), "--model", "small", "--validate-only"],
    )
    with pytest.raises(SystemExit):
        module.main()

    combined = "".join(capsys.readouterr())
    assert "Detected model:" not in combined, (
        f"a name the user typed was reported as detected:\n{combined}"
    )
    assert "Model: small" in combined, combined


def test_an_unresolved_name_with_no_ctranslate2_files_gets_the_runtime_hint(
    tmp_path, monkeypatch, capsys
):
    """Nothing of the layout is present, so "wrong runtime" beats "odd name".

    The hint disappeared under a mutation of its guard with every test still
    passing; the sibling case (some CTranslate2 files present, so the folder
    name really is just unrecognised) is what must *not* get it.
    """
    module = _load_import_module()
    source = tmp_path / "some-other-runtime-export"
    source.mkdir()
    (source / "model.onnx").write_bytes(b"onnx")

    monkeypatch.setattr(sys, "argv", ["import_model.py", str(source)])
    with pytest.raises(SystemExit):
        module.main()
    hinted = "".join(capsys.readouterr())

    named = tmp_path / "another-odd-name"
    named.mkdir()
    (named / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["import_model.py", str(named)])
    with pytest.raises(SystemExit):
        module.main()
    plain = "".join(capsys.readouterr())

    assert "CTranslate2/faster-whisper models only" in hinted, hinted
    assert "CTranslate2/faster-whisper models only" not in plain, plain
