#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from owndifflib.common import OwnDiffError
from owndifflib.diff_collect import collect_diff
from owndifflib.git_utils import git_root
from owndifflib.mcq import generate_mcq_bundle
from owndifflib.questions import generate_questions
from owndifflib.report import generate_report
from owndifflib.risk import score_risk
from owndifflib.test_gap import scan_test_gaps


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the complete OwnDiff ownership-check pipeline.")
    parser.add_argument("--repo", default=".", help="Repository path. Default: current directory.")
    parser.add_argument("--base", help="Optional base ref. Uses base...HEAD when set.")
    parser.add_argument("--head", default="HEAD", help="Head ref used with --base. Default: HEAD.")
    parser.add_argument("--staged", action="store_true", help="Analyze staged changes only.")
    parser.add_argument("--config", help="Optional OwnDiff config override path.")
    parser.add_argument("--policy", dest="config_alias", help="Deprecated alias for --config.")
    parser.add_argument("--out-dir", default=".owndiff", help="Output directory inside the target repo.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = git_root(args.repo)
        out_dir = Path(args.out_dir)
        if not out_dir.is_absolute():
            out_dir = root / out_dir
        out_dir.mkdir(parents=True, exist_ok=True)

        diff_path = out_dir / "diff.json"
        patch_path = out_dir / "diff.patch"
        tests_path = out_dir / "tests.json"
        risk_path = out_dir / "risk.json"
        questions_path = out_dir / "questions.json"
        mcq_path = out_dir / "ownership-mcq.json"
        answer_key_path = out_dir / "ownership-answer-key.json"
        answers_template_path = out_dir / "ownership-answers-template.json"
        gate_path = out_dir / "ownership-gate.json"
        report_path = out_dir / "ownership-report.md"
        record_path = out_dir / "ownership-record.json"

        config_path = args.config or args.config_alias
        diff = collect_diff(root, diff_path, patch_path, base=args.base, head=args.head, staged=args.staged, config_path=config_path)
        tests = scan_test_gaps(root, diff_path, tests_path, config_path)
        risk = score_risk(root, diff_path, tests_path, risk_path, config_path)
        questions = generate_questions(diff_path, risk_path, tests_path, questions_path, root, config_path)
        mcq_bundle = generate_mcq_bundle(
            diff_path,
            risk_path,
            tests_path,
            questions_path,
            mcq_path,
            answer_key_path,
            answers_template_path,
            gate_path,
            root,
            config_path,
        )
        generate_report(diff_path, risk_path, tests_path, questions_path, report_path, record_path)
    except OwnDiffError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "repo": str(root),
                "risk_level": risk["risk_level"],
                "risk_score": risk["risk_score"],
                "questions": len(questions["questions"]),
                "test_gap": tests["test_gap"],
                "report": str(report_path),
                "record": str(record_path),
                "mcq": str(mcq_path),
                "gate": str(gate_path),
                "gate_status": mcq_bundle["gate"]["status"],
                "agent_may_push_merge_request": mcq_bundle["gate"]["agent_may_push_merge_request"],
                "files_changed": diff["summary"]["files_changed"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
