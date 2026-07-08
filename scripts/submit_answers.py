#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from owndifflib.common import OwnDiffError
from owndifflib.mcq import evaluate_answers, write_answers_from_pairs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Submit OwnDiff headless MCQ selections and optionally evaluate the gate.")
    parser.add_argument("selections", nargs="+", help="Headless selections in qid=option format.")
    parser.add_argument("--mcq", default=".owndiff/ownership-mcq.json", help="Public MCQ JSON path.")
    parser.add_argument("--answer-key", default=".owndiff/ownership-answer-key.json", help="Local answer key JSON path.")
    parser.add_argument("--answers-out", default=".owndiff/ownership-answers.json", help="Answers JSON output path.")
    parser.add_argument("--gate-out", default=".owndiff/ownership-gate.json", help="Gate JSON output path.")
    parser.add_argument("--config", help="Optional OwnDiff config override path.")
    parser.add_argument("--evaluate", action="store_true", help="Evaluate answers immediately after writing them.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        answers = write_answers_from_pairs(args.selections, args.answers_out, args.mcq)
        if args.evaluate:
            gate = evaluate_answers(args.mcq, args.answer_key, args.answers_out, args.gate_out, args.config)
        else:
            gate = None
    except (OwnDiffError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    payload = {
        "answers_out": str(Path(args.answers_out)),
        "answers": answers["answers"],
    }
    if gate is not None:
        payload.update(
            {
                "gate_out": str(Path(args.gate_out)),
                "status": gate["status"],
                "score_percent": gate["score_percent"],
                "attempts": gate["attempts"],
                "attempt_summary": gate["attempt_summary"],
                "agent_may_push_merge_request": gate["agent_may_push_merge_request"],
            }
        )
    print(json.dumps(payload, sort_keys=True))
    if gate is None:
        return 0
    return 0 if gate["agent_may_push_merge_request"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
