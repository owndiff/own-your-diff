#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from owndifflib.common import OwnDiffError
from owndifflib.report import generate_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a human-readable OwnDiff report and JSON ownership record.")
    parser.add_argument("--diff", default=".owndiff/diff.json", help="Diff JSON path.")
    parser.add_argument("--risk", default=".owndiff/risk.json", help="Risk JSON path.")
    parser.add_argument("--tests", default=".owndiff/tests.json", help="Test-gap JSON path.")
    parser.add_argument("--questions", default=".owndiff/questions.json", help="Questions JSON path.")
    parser.add_argument("--out", default=".owndiff/ownership-report.md", help="Markdown report output path.")
    parser.add_argument("--record-out", default=".owndiff/ownership-record.json", help="JSON ownership record output path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = generate_report(args.diff, args.risk, args.tests, args.questions, args.out, args.record_out)
    except OwnDiffError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps({"out": str(Path(args.out)), "record_out": str(Path(args.record_out)), "status": payload["ownership_status"]}, sort_keys=True)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
