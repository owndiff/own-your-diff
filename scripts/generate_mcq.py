#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from owndifflib.common import OwnDiffError
from owndifflib.mcq import generate_mcq_bundle


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate OwnDiff multiple choice question artifacts and an initial ownership gate.")
    parser.add_argument("--repo", default=".", help="Repository path. Defaults to current directory.")
    parser.add_argument("--diff", default=".owndiff/diff.json", help="Diff JSON path.")
    parser.add_argument("--risk", default=".owndiff/risk.json", help="Risk JSON path.")
    parser.add_argument("--tests", default=".owndiff/tests.json", help="Test-gap JSON path.")
    parser.add_argument("--questions", default=".owndiff/questions.json", help="Ownership questions JSON path.")
    parser.add_argument(
        "--mcq-out",
        default=".owndiff/ownership-mcq.json",
        metavar="QUESTIONS_JSON",
        help="Public multiple choice question JSON output path.",
    )
    parser.add_argument("--answer-key-out", default=".owndiff/ownership-answer-key.json", help="Local answer key JSON output path.")
    parser.add_argument("--answers-template-out", default=".owndiff/ownership-answers-template.json", help="Blank answers JSON output path.")
    parser.add_argument("--gate-out", default=".owndiff/ownership-gate.json", help="Initial gate JSON output path.")
    parser.add_argument("--config", help="Optional OwnDiff config override path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = generate_mcq_bundle(
            args.diff,
            args.risk,
            args.tests,
            args.questions,
            args.mcq_out,
            args.answer_key_out,
            args.answers_template_out,
            args.gate_out,
            args.repo,
            args.config,
        )
    except OwnDiffError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "artifacts_generated": bool(payload["generated"]),
                "mcq_out": str(Path(args.mcq_out)) if payload["generated"] else None,
                "gate_out": str(Path(args.gate_out)) if payload["generated"] else None,
                "gate_status": payload["gate"]["status"],
                "questions": len(payload["mcq"]["questions"]) if payload["mcq"] else 0,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
