#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from owndifflib.common import OwnDiffError
from owndifflib.questions import generate_questions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate ownership questions from OwnDiff risk artifacts.")
    parser.add_argument("--diff", default=".owndiff/diff.json", help="Diff JSON path.")
    parser.add_argument("--risk", default=".owndiff/risk.json", help="Risk JSON path.")
    parser.add_argument("--tests", default=".owndiff/tests.json", help="Test-gap JSON path.")
    parser.add_argument("--repo", default=".", help="Repository path. Defaults to current directory.")
    parser.add_argument("--config", help="Optional OwnDiff config override path.")
    parser.add_argument("--out", default=".owndiff/questions.json", help="Output JSON path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = generate_questions(args.diff, args.risk, args.tests, args.out, args.repo, args.config)
    except OwnDiffError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"out": str(Path(args.out)), "questions": len(payload["questions"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
