#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from owndifflib.common import OwnDiffError
from owndifflib.mcq import evaluate_answers


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate OwnDiff multiple choice question answers and write the merge/push gate result.")
    parser.add_argument(
        "--mcq",
        default=".owndiff/ownership-mcq.json",
        metavar="QUESTIONS_JSON",
        help="Public multiple choice question JSON path.",
    )
    parser.add_argument("--answer-key", default=".owndiff/ownership-answer-key.json", help="Local answer key JSON path.")
    parser.add_argument("--answers", default=".owndiff/ownership-answers.json", help="Submitted answers JSON path.")
    parser.add_argument("--out", default=".owndiff/ownership-gate.json", help="Gate JSON output path.")
    parser.add_argument("--config", help="Optional OwnDiff config override path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = evaluate_answers(args.mcq, args.answer_key, args.answers, args.out, args.config)
    except OwnDiffError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "out": str(Path(args.out)),
                "status": payload["status"],
                "score_percent": payload["score_percent"],
                "attempts": payload["attempts"],
                "attempt_summary": payload["attempt_summary"],
                "agent_may_push_merge_request": payload["agent_may_push_merge_request"],
            },
            sort_keys=True,
        )
    )
    return 0 if payload["agent_may_push_merge_request"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
