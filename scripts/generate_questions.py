#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from owndifflib.common import OwnDiffError
from owndifflib.questions import generate_questions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate validated LLM ownership questions from OwnDiff risk artifacts.")
    parser.add_argument("--diff", default=".owndiff/diff.json", help="Diff JSON path.")
    parser.add_argument("--risk", default=".owndiff/risk.json", help="Risk JSON path.")
    parser.add_argument("--tests", default=".owndiff/tests.json", help="Test-gap JSON path.")
    parser.add_argument("--repo", default=".", help="Repository path. Defaults to current directory.")
    parser.add_argument("--config", help="Optional OwnDiff config override path.")
    parser.add_argument("--out", default=".owndiff/questions.json", help="Output JSON path.")
    parser.add_argument(
        "--llm-response",
        help="JSON response produced by the current agent LLM/API for the prompt written by a prior agent-provider run.",
    )
    parser.add_argument("--prompt-out", default=".owndiff/question-prompt.md", help="Agent LLM prompt output path.")
    parser.add_argument("--request-out", default=".owndiff/question-request.json", help="Agent LLM request metadata output path.")
    parser.add_argument("--response-out", default=".owndiff/question-response.json", help="Expected agent LLM response path.")
    parser.add_argument(
        "--question-count",
        type=int,
        help="Override the configured ownership question count for this generation run. Default comes from config.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = generate_questions(
            args.diff,
            args.risk,
            args.tests,
            args.out,
            args.repo,
            args.config,
            llm_response_path=args.llm_response,
            prompt_out_path=args.prompt_out,
            request_out_path=args.request_out,
            response_out_path=args.response_out,
            question_count_override=args.question_count,
        )
    except OwnDiffError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "out": str(Path(args.out)),
                "questions": len(payload["questions"]),
                "generation": payload.get("generation", {}).get("method", "unknown"),
                "awaiting_llm_response": payload.get("generation", {}).get("awaiting_llm_response", False),
                "prompt": payload.get("generation", {}).get("prompt_path"),
                "response": payload.get("generation", {}).get("response_path"),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
