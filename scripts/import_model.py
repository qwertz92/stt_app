#!/usr/bin/env python3
"""Import manually downloaded faster-whisper model files into the HuggingFace cache.

Use this script when you have downloaded model files manually (e.g. from a
browser, git clone, or USB stick) and need to place them into the correct
HuggingFace cache structure so the app can find them automatically.

Usage examples:

    # Import a folder that was downloaded via git clone or browser:
    python scripts/import_model.py C:\\Downloads\\faster-whisper-large-v3-turbo

    # Import and specify which model it is (if auto-detection fails):
    python scripts/import_model.py C:\\Downloads\\my-model-folder --model large-v3-turbo

    # Import into a custom model directory instead of the default HF cache:
    python scripts/import_model.py C:\\Downloads\\faster-whisper-small --target-dir D:\\whisper-models

    # Just validate files without importing:
    python scripts/import_model.py C:\\Downloads\\faster-whisper-small --validate-only

After importing, the model is ready to use. Select the model size in Settings
and it will load from the local cache — no internet required.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Add src/ to path so we can import from the package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from stt_app.config import FASTER_WHISPER_MODEL_SIZES, MODEL_REPO_MAP
from stt_app.persistence import atomic_write_text

IMPORTABLE_MODEL_REPO_MAP = {
    name: MODEL_REPO_MAP[name] for name in FASTER_WHISPER_MODEL_SIZES
}

# Files required by CTranslate2 / faster-whisper.
REQUIRED_FILES = {"config.json", "model.bin", "tokenizer.json"}
# At least one of these vocabulary files must be present.
VOCABULARY_FILES = {"vocabulary.txt", "vocabulary.json"}
# Additional optional files that should be copied if present.
OPTIONAL_FILES = {"preprocessor_config.json"}

# Git LFS pointer files are small text files starting with this header.
_LFS_POINTER_HEADER = "version https://git-lfs.github.com/spec/v1"
# Minimum expected size for model.bin (real model weights are at least ~30 MB).
_MODEL_BIN_MIN_BYTES = 10_000_000  # 10 MB

# Build reverse map: common folder name patterns → short model name
_NON_IMPORTABLE_MODEL_NAMES = frozenset(MODEL_REPO_MAP) - frozenset(IMPORTABLE_MODEL_REPO_MAP)

_FOLDER_HINTS: dict[str, str] = {}
for _short, _repo in IMPORTABLE_MODEL_REPO_MAP.items():
    # "Systran/faster-whisper-small" → "faster-whisper-small"
    _repo_name = _repo.split("/")[-1]
    _FOLDER_HINTS[_repo_name.lower()] = _short
    _FOLDER_HINTS[_short.lower()] = _short

# Folders named after a model this script cannot import are recognised too, so
# they get the "wrong runtime" message instead of "could not auto-detect the
# model name" followed by advice to pass a `--model` that is then rejected.
for _other in MODEL_REPO_MAP:
    if _other in IMPORTABLE_MODEL_REPO_MAP:
        continue
    _FOLDER_HINTS.setdefault(_other.lower(), _other)
    _FOLDER_HINTS.setdefault(MODEL_REPO_MAP[_other].split("/")[-1].lower(), _other)


def _warn(*lines: str) -> None:
    """Write to stderr with stdout flushed first.

    Redirecting output merges the two streams, and stdout is block-buffered
    once it is a pipe or a file while stderr is not. Every stdout line written
    before an error therefore lands *after* it in the captured log: a partial
    model reported "MISSING FILES: ..." above the `Detected model: small` line
    that says which model was missing them.
    """
    sys.stdout.flush()
    for line in lines:
        print(line, file=sys.stderr)
    sys.stderr.flush()


def _print_missing_file_advice(missing_files: list[str]) -> None:
    """One wording for one condition.

    The unresolved-name branch printed a bare `MISSING FILES:` list without
    the two follow-up lines, so the same problem read differently depending on
    whether the folder name happened to be recognisable.
    """
    _warn(
        f"\nMISSING FILES: {', '.join(missing_files)}",
        "\nEach model requires: config.json, model.bin, tokenizer.json, "
        "and vocabulary.txt (or vocabulary.json).",
        "Download the missing files from the model's HuggingFace page.",
    )


def _print_validation_verdict(is_valid: bool) -> None:
    """ASCII only, on purpose.

    This printed U+2713 and U+2717. `sys.stdout` uses cp1252 on Windows as
    soon as it is redirected, neither glyph exists in cp1252, and `print`
    raises `UnicodeEncodeError` -- so `--validate-only` crashed with a
    traceback and exit 1 on a *complete, valid* model whenever its output was
    captured to a file, which is the situation the flushing above exists for.
    `capsys` swaps in a UTF-8 buffer, so no test could see it.
    """
    if is_valid:
        print("\nOK: all required files are present. Ready for import.")
    else:
        print("\nFAILED: required files are missing. See the errors above.")


def _print_wrong_runtime_hint() -> None:
    """Say why this folder is out of scope, not merely unrecognised.

    Only for a model that is genuinely not a CTranslate2 one. A valid
    faster-whisper folder under an unrecognised name needs `--model`, and
    sending that user to `download_model.py` is the wrong instruction.
    """
    print(
        "This script imports CTranslate2/faster-whisper models only. "
        "Use scripts/download_model.py for ONNX/WebGPU models, including the "
        "default model.",
        file=sys.stderr,
    )


def _print_importable_models() -> None:
    print(f"Available: {', '.join(IMPORTABLE_MODEL_REPO_MAP)}", file=sys.stderr)


def detect_model_name(source_dir: Path) -> str | None:
    """Try to detect the model short name from the source directory name."""
    folder_name = source_dir.name.lower().strip()

    # Direct match: "faster-whisper-small", "small", etc.
    if folder_name in _FOLDER_HINTS:
        return _FOLDER_HINTS[folder_name]

    # Partial match: folder contains a known model name.
    # Sort hints longest-first so "large-v3-turbo" matches before "large-v3".
    for hint, short_name in sorted(
        _FOLDER_HINTS.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if hint in folder_name:
            return short_name

    return None


def is_lfs_pointer(file_path: Path) -> bool:
    """Check if a file is a Git LFS pointer instead of actual content.

    Git LFS pointer files are small text files (~130 bytes) that start with
    'version https://git-lfs.github.com/spec/v1'. When `git clone` is run
    without `git-lfs` installed, large files are replaced with these pointers.
    """
    try:
        size = file_path.stat().st_size
        # LFS pointers are always small text files (typically < 200 bytes)
        if size > 1024:
            return False
        content = file_path.read_text(encoding="utf-8", errors="replace")
        return content.strip().startswith(_LFS_POINTER_HEADER)
    except (OSError, UnicodeDecodeError):
        return False


def validate_model_files(source_dir: Path) -> tuple[bool, list[str], list[str]]:
    """Validate that a directory contains all required model files.

    Returns (is_valid, found_files, missing_files).
    Checks for Git LFS pointer files and suspiciously small model.bin.
    """
    found: list[str] = []
    missing: list[str] = []
    warnings: list[str] = []

    for required in REQUIRED_FILES:
        fpath = source_dir / required
        if fpath.is_file():
            found.append(required)
        else:
            missing.append(required)

    has_vocab = False
    for vocab in VOCABULARY_FILES:
        if (source_dir / vocab).is_file():
            found.append(vocab)
            has_vocab = True
    if not has_vocab:
        missing.append("vocabulary.txt or vocabulary.json")

    found.extend(
        optional for optional in OPTIONAL_FILES if (source_dir / optional).is_file()
    )

    # Check for Git LFS pointers (common when git-lfs is not installed)
    model_bin = source_dir / "model.bin"
    if model_bin.is_file():
        if is_lfs_pointer(model_bin):
            warnings.append(
                "ERROR: model.bin is a Git LFS pointer (not actual model weights).\n"
                "  This happens when you 'git clone' without git-lfs installed.\n"
                "  Fix: install git-lfs, then run 'git lfs pull' in the cloned repo.\n"
                "  Or download the model using the download script instead:\n"
                "    python scripts/download_model.py --model <name>"
            )
            missing.append("model.bin (Git LFS pointer — not real weights)")
            # Remove model.bin from found since it's not usable
            found = [f for f in found if f != "model.bin"]
        elif model_bin.stat().st_size < _MODEL_BIN_MIN_BYTES:
            size_kb = model_bin.stat().st_size / 1024
            warnings.append(
                f"ERROR: model.bin is suspiciously small ({size_kb:.1f} KB).\n"
                f"  Real model weights are at least tens of MB.\n"
                f"  This may be a Git LFS pointer or corrupted download.\n"
                f"  Fix: install git-lfs, then run 'git lfs pull' in the cloned repo.\n"
                f"  Or download the model using the download script instead:\n"
                f"    python scripts/download_model.py --model <name>"
            )
            missing.append("model.bin (too small — likely incomplete download)")
            found = [f for f in found if f != "model.bin"]

    # Print warnings immediately so the user sees them
    for warning in warnings:
        print(f"\n{warning}", file=sys.stderr)

    is_valid = len(missing) == 0
    return is_valid, found, missing


def compute_fake_hash(source_dir: Path) -> str:
    """Compute a deterministic hash for the snapshot directory name.

    HuggingFace uses git commit hashes for snapshot directories. Since we
    don't have one for manually downloaded files, hash every imported file's
    name and content. Reading the weights is unavoidable during import anyway,
    and content hashing prevents different same-sized models from sharing a
    snapshot directory accidentally.
    """
    hasher = hashlib.sha256()

    all_relevant = REQUIRED_FILES | VOCABULARY_FILES | OPTIONAL_FILES
    for filename in sorted(all_relevant):
        path = source_dir / filename
        if not path.is_file():
            continue
        hasher.update(filename.encode("utf-8"))
        hasher.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)

    return hasher.hexdigest()[:40]


def get_default_hf_cache_dir() -> Path:
    """Return the default HuggingFace Hub cache directory."""
    hf_home = os.environ.get("HF_HOME", "")
    if hf_home:
        return Path(hf_home) / "hub"
    hf_cache = os.environ.get("HF_HUB_CACHE", "")
    if hf_cache:
        return Path(hf_cache)
    return Path.home() / ".cache" / "huggingface" / "hub"


def import_model(
    source_dir: Path,
    model_name: str,
    target_dir: Path | None = None,
    dry_run: bool = False,
) -> Path:
    """Import model files into the HuggingFace cache structure.

    Returns the path to the snapshot directory where files were copied.
    """
    repo_id = IMPORTABLE_MODEL_REPO_MAP.get(model_name)
    if repo_id is None:
        print(f"ERROR: Unknown model '{model_name}'.", file=sys.stderr)
        print(
            "This script imports CTranslate2/faster-whisper models only. "
            "Use scripts/download_model.py for ONNX/WebGPU models.",
            file=sys.stderr,
        )
        print(f"Available: {', '.join(IMPORTABLE_MODEL_REPO_MAP)}", file=sys.stderr)
        sys.exit(1)

    cache_dir = target_dir or get_default_hf_cache_dir()

    # Build the HF cache structure:
    # cache_dir/models--Org--RepoName/snapshots/<hash>/
    folder_name = f"models--{repo_id.replace('/', '--')}"
    model_root = cache_dir / folder_name
    refs_dir = model_root / "refs"
    snapshots_dir = model_root / "snapshots"

    if dry_run:
        snapshot_hash = compute_fake_hash(source_dir)
        snapshot_dir = snapshots_dir / snapshot_hash
        print(f"[DRY RUN] Would create: {snapshot_dir}")
        print(f"[DRY RUN] Would copy files from: {source_dir}")
        return snapshot_dir

    # Stage the entire snapshot beside its final location. Publishing the
    # directory and refs/main happens only after every copy and hash succeeds.
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    refs_dir.mkdir(parents=True, exist_ok=True)

    staging_dir = Path(
        tempfile.mkdtemp(prefix=".import-incomplete-", dir=snapshots_dir)
    )
    try:
        all_relevant = REQUIRED_FILES | VOCABULARY_FILES | OPTIONAL_FILES
        for filename in sorted(all_relevant):
            src = source_dir / filename
            if src.is_file():
                shutil.copy2(src, staging_dir / filename)

        snapshot_hash = compute_fake_hash(staging_dir)
        snapshot_dir = snapshots_dir / snapshot_hash
        if snapshot_dir.exists():
            is_valid, _found, _missing = validate_model_files(snapshot_dir)
            existing_hash = compute_fake_hash(snapshot_dir)
            if is_valid and existing_hash == snapshot_hash:
                shutil.rmtree(staging_dir)
            else:
                # Older versions could expose a partial snapshot directly at
                # its final path. Replace such a stale directory while keeping
                # it available for rollback until the staged snapshot is live.
                displaced_dir = Path(
                    tempfile.mkdtemp(
                        prefix=f".{snapshot_hash}.displaced-",
                        dir=snapshots_dir,
                    )
                )
                displaced_dir.rmdir()
                snapshot_dir.replace(displaced_dir)
                try:
                    staging_dir.replace(snapshot_dir)
                except Exception:
                    displaced_dir.replace(snapshot_dir)
                    raise
                else:
                    shutil.rmtree(displaced_dir)
        else:
            try:
                staging_dir.replace(snapshot_dir)
            except FileExistsError:
                # Another importer may have published the identical content.
                shutil.rmtree(staging_dir)
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)

    # Write refs/main to point to our snapshot
    refs_main = refs_dir / "main"
    atomic_write_text(refs_main, snapshot_hash)

    return snapshot_dir


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Import manually downloaded faster-whisper model files into the "
            "HuggingFace cache structure so the app can find them automatically."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "source",
        nargs="?",
        default=None,
        help=(
            "Path to the directory containing the downloaded model files "
            "(config.json, model.bin, tokenizer.json, vocabulary.txt/json). "
            "This can be a git clone, a manually created folder, or an "
            "extracted archive."
        ),
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            f"Which model this is (e.g. small, large-v3-turbo). "
            f"If not specified, the script tries to detect it from the folder name. "
            f"Choices: {', '.join(IMPORTABLE_MODEL_REPO_MAP)}"
        ),
    )
    parser.add_argument(
        "--target-dir",
        default=None,
        help=(
            "Target cache directory. If omitted, uses the default HuggingFace "
            "cache (%%USERPROFILE%%\\.cache\\huggingface\\hub on Windows, "
            "~/.cache/huggingface/hub on Linux). If you set 'Model Dir' in the "
            "app settings, use that same path here."
        ),
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Only validate the source files, do not copy anything.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available model names and exit.",
    )
    args = parser.parse_args()

    if args.list:
        print("Available models:")
        for name, repo_id in IMPORTABLE_MODEL_REPO_MAP.items():
            lang = "English only" if "distil" in name else "multilingual"
            print(f"  {name:20s} -> {repo_id} ({lang})")
        print("\nONNX/WebGPU models are downloaded with scripts/download_model.py.")
        return

    if args.source is None:
        parser.error("source directory is required (unless using --list)")

    source_dir = Path(args.source).resolve()

    # --- Validate source directory ---
    if not source_dir.is_dir():
        print(f"ERROR: Source path is not a directory: {source_dir}", file=sys.stderr)
        sys.exit(1)

    # The file diagnostic runs first so `--validate-only` always reports what
    # it found, and the name gate runs before the *missing-file* advice.
    # `validate_model_files` looks for the CTranslate2 layout, so a folder
    # holding any other runtime's model -- the default model included -- used
    # to be reported as "MISSING FILES: model.bin, tokenizer.json,
    # vocabulary.txt" with advice to download those files from a repository
    # that does not contain them.
    is_valid, found_files, missing_files = validate_model_files(source_dir)

    # Resolving the name only reads the folder name, so it is done before
    # anything is printed and the report reads top-down: which folder, which
    # model, what is wrong with it, verdict.
    model_name: str | None = args.model
    detected = model_name is None
    if model_name is None:
        model_name = detect_model_name(source_dir)

    print(f"Source: {source_dir}")
    print(f"Found files: {', '.join(found_files) if found_files else '(none)'}")
    if model_name is not None:
        print(f"{'Detected model' if detected else 'Model'}: {model_name}")

    # This gate comes before the file advice on purpose. `validate_model_files`
    # looks for the CTranslate2 layout, so a folder holding any other runtime's
    # model -- the default model included -- is "missing" model.bin,
    # tokenizer.json and vocabulary.txt, and telling that user to download them
    # from a repository that does not contain them is worse than saying
    # nothing.
    if model_name is not None and model_name not in IMPORTABLE_MODEL_REPO_MAP:
        if model_name in _NON_IMPORTABLE_MODEL_NAMES:
            # A real model, wrong script. Saying "Unknown model" about a name
            # the app itself offers is misleading, and it contradicted the
            # very next line, which explained the model was out of scope.
            _warn(f"ERROR: '{model_name}' is not a CTranslate2/faster-whisper model.")
            _print_wrong_runtime_hint()
        else:
            # A typo or an invented name. `_print_wrong_runtime_hint`'s own
            # docstring rules it out here, and pointing this user at
            # download_model.py is the wrong instruction -- they need the list.
            _warn(f"ERROR: Unknown model '{model_name}'.")
        _print_importable_models()
        sys.exit(1)

    # Anything still here either resolved to an importable model or has an
    # unrecognised folder name. `found_files` separates those two: an empty
    # list means nothing of the CTranslate2 layout is present, which is the
    # other-runtime case above rather than an incomplete download, so it gets
    # the runtime hint below instead of a list of files to fetch.
    if missing_files and found_files:
        _print_missing_file_advice(missing_files)

    # The verdict is what `--validate-only` was asked for, and it is produced
    # before the name gate, which exits. A folder holding a complete, valid
    # model under a name this script cannot map used to print `Source:` and
    # `Found files:` and then exit 1 with only "Could not auto-detect the
    # model name", never answering the question it was asked.
    if args.validate_only:
        _print_validation_verdict(is_valid)

    if model_name is None:
        _warn(
            "ERROR: Could not auto-detect the model name from the folder name.",
            "Please specify the model explicitly with --model <name>.",
        )
        if not found_files:
            # Nothing of the CTranslate2 layout is here at all, so the likely
            # explanation is a model for another runtime rather than a folder
            # that merely has an unrecognised name.
            _print_wrong_runtime_hint()
        _print_importable_models()
        sys.exit(1)

    if not is_valid:
        sys.exit(1)

    repo_id = IMPORTABLE_MODEL_REPO_MAP[model_name]
    print(f"Repository: {repo_id}")

    if args.validate_only:
        return

    # --- Import ---
    target_dir = Path(args.target_dir) if args.target_dir else None
    effective_target = target_dir or get_default_hf_cache_dir()

    print(f"\nImporting into: {effective_target}")

    snapshot_dir = import_model(
        source_dir=source_dir,
        model_name=model_name,
        target_dir=target_dir,
    )

    print(f"\n{'=' * 60}")
    print("  SUCCESS — Model imported!")
    print(f"{'=' * 60}")
    print(f"  Model:    {model_name}")
    print(f"  Location: {snapshot_dir}")
    print()
    print("  Next steps:")
    print(f"  1. In the app Settings, select model size: {model_name}")
    if target_dir:
        print(f"  2. Set 'Model Dir' in Settings to: {target_dir}")
        print("  3. Enable 'Offline mode' in Settings.")
    else:
        print("  2. Enable 'Offline mode' in Settings (optional).")
    print("\n  The app will now find the model automatically.")


if __name__ == "__main__":
    main()
