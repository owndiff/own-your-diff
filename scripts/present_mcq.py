#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from owndifflib.common import OwnDiffError
from owndifflib.mcq import render_mcq_markdown


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render OwnDiff MCQ questions for explicit headless automation.")
    parser.add_argument("--mcq", default=".owndiff/ownership-mcq.json", help="Public MCQ JSON path.")
    parser.add_argument("--out", help="Optional Markdown output path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        markdown = render_mcq_markdown(args.mcq)
    except OwnDiffError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(markdown, encoding="utf-8")
    print(markdown, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
