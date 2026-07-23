#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from owndifflib.common import OwnDiffError, read_json
from owndifflib.diff_collect import collect_diff, has_source_changes
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
    parser.add_argument(
        "--llm-response",
        help="JSON response produced by the current agent LLM/API for a prior agent-provider question prompt.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Deprecated compatibility flag. Browser review now starts by default when questions are pending.",
    )
    parser.add_argument(
        "--review-mode",
        choices=("web", "none"),
        default="web",
        help=(
            "Review UI when questions are pending. Default: web, a localhost browser review opened in a "
            "private/incognito browser window. Use none only for automated tests or CI checks."
        ),
    )
    parser.add_argument(
        "--no-open-browser",
        action="store_true",
        help="For browser review, print the local URL without opening a browser.",
    )
    parser.add_argument(
        "--web-timeout-seconds",
        type=int,
        default=900,
        help="How long browser review waits for answers. Default: 900 seconds.",
    )
    parser.add_argument(
        "--question-count",
        type=int,
        help="Override the configured ownership question count for this run. Default comes from config.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    interactive_exit: int | None = None
    review_started = False
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
        prompt_path = out_dir / "question-prompt.md"
        request_path = out_dir / "question-request.json"
        response_path = out_dir / "question-response.json"
        answers_path = out_dir / "ownership-answers.json"

        _reset_run_artifacts(
            [
                diff_path,
                patch_path,
                tests_path,
                risk_path,
                questions_path,
                mcq_path,
                answer_key_path,
                answers_template_path,
                answers_path,
                gate_path,
                report_path,
                record_path,
                prompt_path,
                request_path,
                response_path,
            ],
            keep_paths=[Path(args.llm_response)] if args.llm_response else [],
        )

        config_path = args.config or args.config_alias
        diff = collect_diff(root, diff_path, patch_path, base=args.base, head=args.head, staged=args.staged, config_path=config_path)
        tests = scan_test_gaps(root, diff_path, tests_path, config_path)
        risk = score_risk(root, diff_path, tests_path, risk_path, config_path)
        questions = generate_questions(
            diff_path,
            risk_path,
            tests_path,
            questions_path,
            root,
            config_path,
            llm_response_path=args.llm_response,
            prompt_out_path=prompt_path,
            request_out_path=request_path,
            response_out_path=response_path,
            question_count_override=args.question_count,
        )
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
        source_code_changed = has_source_changes(diff)
        if not source_code_changed:
            for stale_path in (answers_path, prompt_path, request_path, response_path):
                stale_path.unlink(missing_ok=True)
        generate_report(diff_path, risk_path, tests_path, questions_path, report_path, record_path)

        review_pending = mcq_bundle["generated"] and mcq_bundle["gate"]["status"] == "pending_answers"
        if review_pending and args.review_mode == "web":
            review_started = True
            interactive_exit = _run_review(
                args,
                config_path,
                mcq_path,
                answer_key_path,
                answers_path,
                gate_path,
            )
            if gate_path.exists():
                mcq_bundle["gate"] = read_json(gate_path)
        elif review_pending and args.interactive and args.review_mode == "none":
            interactive_exit = 2
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
                "question_generation": questions.get("generation", {}).get("method", "unknown"),
                "awaiting_llm_response": questions.get("generation", {}).get("awaiting_llm_response", False),
                "llm_prompt": questions.get("generation", {}).get("prompt_path"),
                "llm_response": questions.get("generation", {}).get("response_path"),
                "test_gap": tests["test_gap"],
                "report": str(report_path),
                "record": str(record_path),
                "mcq": str(mcq_path) if mcq_bundle["generated"] else None,
                "gate": str(gate_path) if mcq_bundle["generated"] else None,
                "mcq_generated": bool(mcq_bundle["generated"]),
                "gate_generated": bool(mcq_bundle["generated"]),
                "gate_status": mcq_bundle["gate"]["status"],
                "agent_may_push_merge_request": mcq_bundle["gate"]["agent_may_push_merge_request"],
                "files_changed": diff["summary"]["files_changed"],
                "source_files_changed": diff["summary"]["source_files_changed"],
                "source_code_changed": source_code_changed,
                "interactive_requested": args.interactive,
                "interactive_exit_code": interactive_exit,
                "review_mode": args.review_mode,
                "review_started": review_started,
            },
            sort_keys=True,
        )
    )
    return interactive_exit if interactive_exit is not None else 0


def _reset_run_artifacts(paths: list[Path], keep_paths: list[Path]) -> None:
    kept = {_normalized_path(path) for path in keep_paths}
    for path in paths:
        if _normalized_path(path) in kept:
            continue
        path.unlink(missing_ok=True)


def _normalized_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _run_review(
    args: argparse.Namespace,
    config_path: str | None,
    mcq_path: Path,
    answer_key_path: Path,
    answers_path: Path,
    gate_path: Path,
) -> int:
    if args.review_mode == "none":
        return 2

    from quiz_web import main as quiz_web_main

    print("OwnDiff starting local browser review.", file=sys.stderr, flush=True)
    return quiz_web_main(
        [
            "--mcq",
            str(mcq_path),
            "--answer-key",
            str(answer_key_path),
            "--answers-out",
            str(answers_path),
            "--gate-out",
            str(gate_path),
            "--timeout-seconds",
            str(args.web_timeout_seconds),
            "--evaluate",
            *(["--no-open-browser"] if args.no_open_browser else []),
            *(["--config", config_path] if config_path else []),
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
