#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from owndifflib.common import OwnDiffError
from owndifflib.git_utils import git_root
from owndifflib.test_gap import scan_test_gaps


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Detect whether changed code files have nearby or changed tests.")
    parser.add_argument("--repo", default=".", help="Repository path. Default: current directory.")
    parser.add_argument("--diff", default=".owndiff/diff.json", help="Diff JSON path from collect_diff.py.")
    parser.add_argument("--config", help="Optional OwnDiff config override path.")
    parser.add_argument("--out", default=".owndiff/tests.json", help="Output JSON path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = git_root(args.repo)
        payload = scan_test_gaps(root, args.diff, args.out, args.config)
    except OwnDiffError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"out": str(Path(args.out)), "test_gap": payload["test_gap"], "summary": payload["summary"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
