#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from owndifflib.common import OwnDiffError
from owndifflib.git_utils import git_root
from owndifflib.risk import score_risk


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Score ownership risk from diff facts, policy, and test-gap data.")
    parser.add_argument("--repo", default=".", help="Repository path. Default: current directory.")
    parser.add_argument("--diff", default=".owndiff/diff.json", help="Diff JSON path.")
    parser.add_argument("--tests", default=".owndiff/tests.json", help="Test-gap JSON path.")
    parser.add_argument("--config", help="Optional OwnDiff config override path.")
    parser.add_argument("--policy", dest="config_alias", help="Deprecated alias for --config.")
    parser.add_argument("--out", default=".owndiff/risk.json", help="Output JSON path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = git_root(args.repo)
        payload = score_risk(root, args.diff, args.tests, args.out, args.config or args.config_alias)
    except OwnDiffError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {"out": str(Path(args.out)), "risk_level": payload["risk_level"], "risk_score": payload["risk_score"]},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
