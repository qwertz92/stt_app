#!/usr/bin/env python3
"""Download local transcription models for offline use.

Usage examples:

    # Download the app's default model to the default HuggingFace cache:
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
faster-whisper models use CTranslate2. Cohere, Granite 4.0, and Granite Speech
4.1 2B use q4 ONNX/WebGPU snapshots and require the JavaScript runtime from
package.json. Parakeet and Canary use INT8 ONNX through the pure-Python
onnx-asr runtime. Nemotron 3.5 uses the INT4 ONNX Runtime GenAI streaming
export.

If Hugging Face is unreachable (e.g. a corporate proxy such as Zscaler that
blocks the whole "Generative AI and ML Applications" category), most models
fall back automatically to the ModelScope mirror (modelscope.cn), which serves
the same weights from its own CDN. Set the environment variable
STT_APP_DISABLE_MODELSCOPE=1 to turn that fallback off.

Three models are not mirrored there and have Hugging Face as their only
source: the default parakeet-tdt-0.6b-v3, canary-1b-v2, and distil-large-v3.5
(see MODELS_WITHOUT_MODELSCOPE_MIRROR in config.py). On a network that blocks
Hugging Face, clone from a machine that can reach it (`git lfs install` first,
or the clone yields 130-byte pointer files; the repository list is in
docs/models.md), copy the folder over, and set "Model Dir" in the app to the
folder that contains it. --output-dir is an argument of this script and does
not apply to a clone you already have.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

# Add src/ to path so we can import from the package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from stt_app.config import (
    DEFAULT_MODEL_SIZE,
    DOC_MODELS_PATH,
    DOC_SSL_PROXY_PATH,
    FASTER_WHISPER_MODEL_SIZES,
    LOCAL_NEMOTRON_MODEL_SIZES,
    LOCAL_ONNX_MODEL_PRECISION,
    LOCAL_ONNX_MODEL_SIZES,
    MODEL_REPO_MAP,
    MODELS_WITHOUT_MODELSCOPE_MIRROR,
)
from stt_app.model_download_coordinator import (
    run_coordinated_download,
)
from stt_app.ssl_utils import is_ssl_error
from stt_app.transcriber.local_faster_whisper import (
    cleanup_incomplete_model_download,
    download_model_snapshot,
)

# Re-export under the name used throughout this script.
MODELS = MODEL_REPO_MAP

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
        + (
            "NOTE: This model has no ModelScope mirror, so Hugging Face\n"
            "was the only source tried. Every workaround below targets it.\n"
            if model_name in MODELS_WITHOUT_MODELSCOPE_MIRROR
            else "NOTE: The download already tries the ModelScope mirror\n"
            "(modelscope.cn) automatically when Hugging Face fails. If you\n"
            "see this message, both sources were unreachable. The\n"
            "workarounds below target Hugging Face directly; ModelScope\n"
            "needs no extra setup.\n"
        )
        +         "\n"
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
        + (
            f"     Put them in a folder named {repo_id.split(chr(47))[-1]!r} and\n"
            "     set 'Model Dir' in the app to that folder's parent.\n"
            if model_name not in FASTER_WHISPER_MODEL_SIZES
            else f"     See {DOC_MODELS_PATH} for how to arrange the files.\n"
        )
        + "\n"
        f"SSL troubleshooting: {DOC_SSL_PROXY_PATH}\n"
        f"Offline model guide: {DOC_MODELS_PATH}\n"
        "═══════════════════════════════════════════════════════════════",
        file=sys.stderr,
    )


def download_model(name: str, output_dir: str | None = None) -> str:
    """Download a single model and return the local snapshot path."""
    repo_id = MODELS.get(name)
    if repo_id is None:
        print(f"ERROR: Unknown model '{name}'.", file=sys.stderr)
        print(f"Available: {', '.join(MODELS)}", file=sys.stderr)
        sys.exit(1)

    print(f"Downloading {name} ({repo_id})...")
    results: list[str] = []
    # Tracks whether *this* process ever started writing. Ctrl+C while only
    # waiting for the slot must not delete partial files: they belong to
    # whoever holds it, and removing them makes a multi-gigabyte download
    # the app has queued restart from zero. Same rule the app applies to
    # its own preload cancel path through `has_explicit_interest`.
    started_downloading = False

    def _download() -> None:
        nonlocal started_downloading
        started_downloading = True
        results.append(download_model_snapshot(name, output_dir or ""))

    try:
        # Take the same slot the app uses. The lock is machine-wide, so a
        # run of this script while the app is downloading now waits for the
        # app instead of putting two writers in one cache directory.
        if not run_coordinated_download(name, output_dir or "", _download):
            # Another caller in THIS process finished the same model first.
            # A single-threaded script cannot reach that, but re-fetching
            # outside the slot would be the one unlocked write path here,
            # so go through the slot again rather than around it.
            run_coordinated_download(name, output_dir or "", _download)
        path = results[-1]
    except KeyboardInterrupt:
        if not started_downloading:
            print(
                "\nCanceled while waiting for another process to finish "
                "with this cache directory. Nothing was downloaded, and no "
                "partial files were deleted.",
                file=sys.stderr,
            )
            raise SystemExit(130) from None
        removed_files, removed_bytes = cleanup_incomplete_model_download(
            name,
            output_dir or "",
        )
        removed_mb = removed_bytes / 1_000_000.0
        print(
            f"\nDownload canceled. Removed {removed_files} incomplete file"
            f"{'s' if removed_files != 1 else ''} ({removed_mb:.1f} MB).",
            file=sys.stderr,
        )
        raise SystemExit(130) from None
    except Exception as exc:
        # Ask the exception chain, not the wording. Several messages describe
        # the same failure -- `format_model_download_error` has a mirrored and
        # an unmirrored branch, and the ONNX path adds two more shapes -- so
        # matching wordings meant a mirrored ONNX model got a bare "Download
        # failed" and exit 1 where `--model small` got the CA-bundle guidance
        # and exit 2, for one and the same corporate proxy. Every raise on the
        # way here uses `from`, so `is_ssl_error` reaches the original
        # `SSLCertVerificationError`. The two wordings stay as a fallback:
        # they are our own prose and carry no raw SSL marker.
        message = str(exc)
        if (
            is_ssl_error(exc)
            or "SSL certificate verification failed" in message
            or "looked like a certificate error" in message
        ):
            _print_ssl_help(name)
            sys.exit(2)
        print(f"ERROR: Download failed: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"  -> {path}")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download local transcription models for offline use.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--model",
        # Taken from the app's own default so the two cannot drift.
        default=DEFAULT_MODEL_SIZE,
        help=(
            f"Model to download (default: {DEFAULT_MODEL_SIZE}). "
            f"Choices: {', '.join(MODELS)}"
        ),
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help=(
            "Custom download directory. For faster-whisper models this is "
            "huggingface_hub's cache_dir; the ONNX models, including the "
            "default, are written into a flat folder under it instead. "
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

    # Without this the "waiting for another process" message from
    # file_lock is dropped by the root logger, and the script looks hung
    # for as long as the app download takes -- which is what provokes the
    # Ctrl+C the cancel path above has to protect against.
    logging.basicConfig(level=logging.INFO, format="  %(message)s")

    if args.list:
        print("Available models:")
        for name, repo_id in MODELS.items():
            if name in LOCAL_ONNX_MODEL_SIZES:
                precision = LOCAL_ONNX_MODEL_PRECISION.get(name, "q4")
                mode = (
                    "batch and true streaming"
                    if name in LOCAL_NEMOTRON_MODEL_SIZES
                    else "batch only"
                )
                note = f" (multilingual, {precision} ONNX, {mode})"
            elif "distil" in name:
                note = " (English only)"
            else:
                note = " (multilingual)"
            print(f"  {name:20s} -> {repo_id}{note}")
        return

    models = list(MODELS.keys()) if args.all else [args.model]

    for name in models:
        download_model(name, args.output_dir)

    print()
    print("Done! Models are cached and ready for offline use.")
    if args.output_dir:
        print(f"Set 'Model Dir' in the app settings to: {args.output_dir}")
    print("Enable 'Offline mode' in settings to prevent future network access.")


if __name__ == "__main__":
    main()
