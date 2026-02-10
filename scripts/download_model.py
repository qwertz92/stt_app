#!/usr/bin/env python3
"""Download faster-whisper models for offline use.

Usage examples:

    # Download the default model (small) to the default HuggingFace cache:
    python scripts/download_model.py

    # Download a specific model:
    python scripts/download_model.py --model medium

    # Download into a custom directory:
    python scripts/download_model.py --model small --output-dir C:\\whisper-models

    # Download all available models:
    python scripts/download_model.py --all

    # List available models without downloading:
    python scripts/download_model.py --list

After downloading, the models are ready for offline use.  Set "Offline mode"
in the app settings, and optionally set "Model Dir" to the --output-dir path.
"""

from __future__ import annotations

import argparse
import sys

# Model short names -> HuggingFace repo IDs (same mapping as faster-whisper).
MODELS: dict[str, str] = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "distil-large-v3.5": "distil-whisper/distil-large-v3.5-ct2",
}

# Only these files are needed by CTranslate2 / faster-whisper.
ALLOW_PATTERNS: list[str] = [
    "config.json",
    "preprocessor_config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.*",
]


def download_model(name: str, output_dir: str | None = None) -> str:
    """Download a single model and return the local snapshot path."""
    try:
        from huggingface_hub import snapshot_download  # type: ignore
    except ImportError:
        print(
            "ERROR: huggingface_hub is not installed. "
            "Install it with: pip install huggingface_hub",
            file=sys.stderr,
        )
        sys.exit(1)

    repo_id = MODELS.get(name)
    if repo_id is None:
        print(f"ERROR: Unknown model '{name}'.", file=sys.stderr)
        print(f"Available: {', '.join(MODELS)}", file=sys.stderr)
        sys.exit(1)

    print(f"Downloading {name} ({repo_id})...")

    kwargs: dict = {
        "allow_patterns": ALLOW_PATTERNS,
    }
    if output_dir:
        kwargs["cache_dir"] = output_dir

    path = snapshot_download(repo_id, **kwargs)
    print(f"  -> {path}")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download faster-whisper models for offline use.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--model",
        default="small",
        help=f"Model to download (default: small). Choices: {', '.join(MODELS)}",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help=(
            "Custom download directory (sets cache_dir for huggingface_hub). "
            "If omitted, uses the default HuggingFace cache "
            "(%%USERPROFILE%%\\.cache\\huggingface\\hub on Windows)."
        ),
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Download ALL available models.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available models and exit.",
    )
    args = parser.parse_args()

    if args.list:
        print("Available models:")
        for name, repo_id in MODELS.items():
            if "distil" in name:
                note = " (English only)"
            else:
                note = " (multilingual)"
            print(f"  {name:20s} -> {repo_id}{note}")
        return

    if args.all:
        models = list(MODELS.keys())
    else:
        models = [args.model]

    for name in models:
        download_model(name, args.output_dir)

    print()
    print("Done! Models are cached and ready for offline use.")
    if args.output_dir:
        print(f"Set 'Model Dir' in the app settings to: {args.output_dir}")
    print("Enable 'Offline mode' in settings to prevent future network access.")


if __name__ == "__main__":
    main()
