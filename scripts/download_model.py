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
import os
import sys

# Add src/ to path so we can import from the package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from stt_app.config import DOC_MODELS_PATH, DOC_SSL_PROXY_PATH, MODEL_REPO_MAP  # noqa: E402
from stt_app.ssl_utils import is_ssl_error as _is_ssl_error  # noqa: E402

# Re-export under the name used throughout this script.
MODELS = MODEL_REPO_MAP

# Only these files are needed by CTranslate2 / faster-whisper.
ALLOW_PATTERNS: list[str] = [
    "config.json",
    "preprocessor_config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.*",
]


def _print_ssl_help(model_name: str) -> None:
    """Print actionable guidance when SSL verification fails."""
    repo_id = MODELS.get(model_name, f"Systran/faster-whisper-{model_name}")
    print(
        "\n"
        "═══════════════════════════════════════════════════════════════\n"
        "  SSL CERTIFICATE ERROR — likely a corporate proxy (Zscaler)\n"
        "═══════════════════════════════════════════════════════════════\n"
        "\n"
        "Your network intercepts HTTPS connections, which breaks the\n"
        "SSL certificate chain that Python / huggingface_hub expects.\n"
        "\n"
        "Workarounds (pick one):\n"
        "\n"
        "  1. SET YOUR CORPORATE CA BUNDLE (best fix):\n"
        "     Ask your IT team for the corporate root CA certificate\n"
        "     (.pem file), then set this environment variable before\n"
        "     running the script:\n"
        "\n"
        "       $env:REQUESTS_CA_BUNDLE = 'C:\\path\\to\\corporate-ca.pem'\n"
        "       $env:CURL_CA_BUNDLE     = 'C:\\path\\to\\corporate-ca.pem'\n"
        "\n"
        "  2. DOWNLOAD ON ANOTHER MACHINE:\n"
        "     Run the script on a machine without SSL interception:\n"
        f"       python scripts/download_model.py --model {model_name}"
        f" --output-dir ./whisper-export\n"
        "     Then copy the output folder to this machine.\n"
        "\n"
        "  3. GIT CLONE (may bypass proxy for git traffic):\n"
        f"     git clone https://huggingface.co/{repo_id}\n"
        "     Then set 'Model Dir' in the app to the cloned folder's parent.\n"
        "\n"
        "  4. MANUAL BROWSER DOWNLOAD:\n"
        f"     Download files from https://huggingface.co/{repo_id}/tree/main\n"
        f"     See {DOC_MODELS_PATH} for how to arrange the files.\n"
        "\n"
        f"SSL troubleshooting: {DOC_SSL_PROXY_PATH}\n"
        f"Offline model guide: {DOC_MODELS_PATH}\n"
        "═══════════════════════════════════════════════════════════════",
        file=sys.stderr,
    )


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

    try:
        path = snapshot_download(repo_id, **kwargs)
    except Exception as exc:
        if _is_ssl_error(exc):
            _print_ssl_help(name)
            sys.exit(2)
        # Re-raise with context for other errors.
        print(f"ERROR: Download failed: {exc}", file=sys.stderr)
        sys.exit(1)

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
