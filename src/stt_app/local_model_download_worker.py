from __future__ import annotations

import argparse
import sys

from .ssl_utils import inject_system_trust_store, sync_ca_bundle_env_vars
from .transcriber.local_faster_whisper import download_model_snapshot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download one local STT model.")
    parser.add_argument("--model", required=True, help="Local model name.")
    parser.add_argument(
        "--model-dir",
        default="",
        help="Optional custom model cache directory.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    inject_system_trust_store()
    sync_ca_bundle_env_vars()
    try:
        download_model_snapshot(args.model, args.model_dir)
    except Exception as exc:
        if sys.stderr is not None:
            sys.stderr.write(f"{exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
