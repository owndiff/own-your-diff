#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from owndifflib.common import OwnDiffError
from owndifflib.diff_collect import collect_diff


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect git diff facts into JSON and write a patch file.")
    parser.add_argument("--repo", default=".", help="Repository path. Default: current directory.")
    parser.add_argument("--base", help="Optional base ref. Uses base...HEAD when set.")
    parser.add_argument("--head", default="HEAD", help="Head ref used with --base. Default: HEAD.")
    parser.add_argument("--staged", action="store_true", help="Analyze staged changes only.")
    parser.add_argument("--config", help="Optional OwnDiff config override path.")
    parser.add_argument("--out", default=".owndiff/diff.json", help="Output JSON path.")
    parser.add_argument("--patch-out", default=".owndiff/diff.patch", help="Output patch path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = collect_diff(
            repo=args.repo,
            out_path=args.out,
            patch_out=args.patch_out,
            base=args.base,
            head=args.head,
            staged=args.staged,
            config_path=args.config,
        )
    except OwnDiffError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"out": str(Path(args.out)), "summary": payload["summary"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
