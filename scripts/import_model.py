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
from pathlib import Path

# Add src/ to path so we can import from the package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tts_app.config import MODEL_REPO_MAP  # noqa: E402

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
_FOLDER_HINTS: dict[str, str] = {}
for _short, _repo in MODEL_REPO_MAP.items():
    # "Systran/faster-whisper-small" → "faster-whisper-small"
    _repo_name = _repo.split("/")[-1]
    _FOLDER_HINTS[_repo_name.lower()] = _short
    _FOLDER_HINTS[_short.lower()] = _short


def detect_model_name(source_dir: Path) -> str | None:
    """Try to detect the model short name from the source directory name."""
    folder_name = source_dir.name.lower().strip()

    # Direct match: "faster-whisper-small", "small", etc.
    if folder_name in _FOLDER_HINTS:
        return _FOLDER_HINTS[folder_name]

    # Partial match: folder contains a known model name
    for hint, short_name in _FOLDER_HINTS.items():
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

    for optional in OPTIONAL_FILES:
        if (source_dir / optional).is_file():
            found.append(optional)

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
    don't have one for manually downloaded files, we create a deterministic
    hash from the model.bin file size and config.json content. This ensures
    the same files always produce the same snapshot directory name.
    """
    hasher = hashlib.sha256()

    config_path = source_dir / "config.json"
    if config_path.is_file():
        hasher.update(config_path.read_bytes())

    model_path = source_dir / "model.bin"
    if model_path.is_file():
        # Use file size (not content) to avoid reading multi-GB files
        hasher.update(str(model_path.stat().st_size).encode())

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
    repo_id = MODEL_REPO_MAP.get(model_name)
    if repo_id is None:
        print(f"ERROR: Unknown model '{model_name}'.", file=sys.stderr)
        print(f"Available: {', '.join(MODEL_REPO_MAP)}", file=sys.stderr)
        sys.exit(1)

    cache_dir = target_dir or get_default_hf_cache_dir()

    # Build the HF cache structure:
    # cache_dir/models--Org--RepoName/snapshots/<hash>/
    folder_name = f"models--{repo_id.replace('/', '--')}"
    model_root = cache_dir / folder_name
    refs_dir = model_root / "refs"
    snapshots_dir = model_root / "snapshots"

    snapshot_hash = compute_fake_hash(source_dir)
    snapshot_dir = snapshots_dir / snapshot_hash

    if dry_run:
        print(f"[DRY RUN] Would create: {snapshot_dir}")
        print(f"[DRY RUN] Would copy files from: {source_dir}")
        return snapshot_dir

    # Create directory structure
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    refs_dir.mkdir(parents=True, exist_ok=True)

    # Copy model files
    all_relevant = REQUIRED_FILES | VOCABULARY_FILES | OPTIONAL_FILES
    copied = []
    for filename in sorted(all_relevant):
        src = source_dir / filename
        if src.is_file():
            dst = snapshot_dir / filename
            shutil.copy2(src, dst)
            size_mb = src.stat().st_size / (1024 * 1024)
            copied.append(f"  {filename} ({size_mb:.1f} MB)")

    # Write refs/main to point to our snapshot
    refs_main = refs_dir / "main"
    refs_main.write_text(snapshot_hash, encoding="utf-8")

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
            f"Choices: {', '.join(MODEL_REPO_MAP)}"
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
        for name, repo_id in MODEL_REPO_MAP.items():
            lang = "English only" if "distil" in name else "multilingual"
            print(f"  {name:20s} -> {repo_id} ({lang})")
        return

    if args.source is None:
        parser.error("source directory is required (unless using --list)")

    source_dir = Path(args.source).resolve()

    # --- Validate source directory ---
    if not source_dir.is_dir():
        print(f"ERROR: Source path is not a directory: {source_dir}", file=sys.stderr)
        sys.exit(1)

    is_valid, found_files, missing_files = validate_model_files(source_dir)

    print(f"Source: {source_dir}")
    print(f"Found files: {', '.join(found_files)}")

    if missing_files:
        print(f"\nMISSING FILES: {', '.join(missing_files)}", file=sys.stderr)
        print(
            "\nEach model requires: config.json, model.bin, tokenizer.json, "
            "and vocabulary.txt (or vocabulary.json).",
            file=sys.stderr,
        )
        print(
            "Download the missing files from the model's HuggingFace page.",
            file=sys.stderr,
        )
        if not is_valid:
            sys.exit(1)

    # --- Determine model name ---
    model_name: str | None = args.model
    if model_name is None:
        model_name = detect_model_name(source_dir)
        if model_name is None:
            print(
                "\nERROR: Could not auto-detect the model name from the folder name.",
                file=sys.stderr,
            )
            print(
                "Please specify the model explicitly with --model <name>.",
                file=sys.stderr,
            )
            print(
                f"Available models: {', '.join(MODEL_REPO_MAP)}",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"Detected model: {model_name}")
    else:
        if model_name not in MODEL_REPO_MAP:
            print(f"ERROR: Unknown model '{model_name}'.", file=sys.stderr)
            print(
                f"Available: {', '.join(MODEL_REPO_MAP)}", file=sys.stderr
            )
            sys.exit(1)
        print(f"Model: {model_name}")

    repo_id = MODEL_REPO_MAP[model_name]
    print(f"Repository: {repo_id}")

    if args.validate_only:
        if is_valid:
            print("\n✓ All required files are present. Ready for import.")
        else:
            print("\n✗ Missing required files. See errors above.")
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
    print(f"  SUCCESS — Model imported!")
    print(f"{'=' * 60}")
    print(f"  Model:    {model_name}")
    print(f"  Location: {snapshot_dir}")
    print()
    print("  Next steps:")
    print(f"  1. In the app Settings, select model size: {model_name}")
    if target_dir:
        print(f"  2. Set 'Model Dir' in Settings to: {target_dir}")
        print(f"  3. Enable 'Offline mode' in Settings.")
    else:
        print(f"  2. Enable 'Offline mode' in Settings (optional).")
    print(f"\n  The app will now find the model automatically.")


if __name__ == "__main__":
    main()
