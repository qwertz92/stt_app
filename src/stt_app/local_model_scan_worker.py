from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .transcriber.local_faster_whisper import find_cached_models


def scan_cached_models(model_dir: str = "") -> list[str]:
    return find_cached_models(str(model_dir or "").strip())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan cached local STT models.")
    parser.add_argument(
        "--model-dir",
        default="",
        help="Optional custom model cache directory.",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Optional JSON output path. Defaults to stdout.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cached = scan_cached_models(args.model_dir)
    payload = json.dumps({"cached_models": cached})
    if args.output:
        Path(args.output).write_text(f"{payload}\n", encoding="utf-8")
        return 0
    if sys.stdout is None:
        return 1
    sys.stdout.write(f"{payload}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
