from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import SCHEMA_VERSION, ensure_parent, read_json, utc_now, write_json
from .diff_collect import has_source_changes


def generate_report(
    diff_path: str | Path,
    risk_path: str | Path,
    tests_path: str | Path,
    questions_path: str | Path,
    out_path: str | Path,
    record_out: str | Path,
) -> dict[str, Any]:
    diff = read_json(Path(diff_path))
    risk = read_json(Path(risk_path))
    tests = read_json(Path(tests_path))
    questions = read_json(Path(questions_path))
    ownership_status = _ownership_status(risk, questions)

    markdown = render_markdown(diff, risk, tests, questions)
    report_path = Path(out_path)
    ensure_parent(report_path)
    report_path.write_text(markdown, encoding="utf-8")

    record: dict[str, Any] = {
        "schema_version": f"{SCHEMA_VERSION}.record",
        "created_at": utc_now(),
        "diff": diff,
        "risk": risk,
        "tests": tests,
        "questions": questions.get("questions", []),
        "ownership_status": ownership_status,
        "report_path": str(report_path),
    }
    write_json(Path(record_out), record)
    return record


def render_markdown(diff: dict[str, Any], risk: dict[str, Any], tests: dict[str, Any], questions: dict[str, Any]) -> str:
    summary = diff.get("summary", {})
    risk_level = str(risk.get("risk_level", "unknown")).upper()
    ownership_status = _ownership_status(risk, questions)
    lines = [
        "# OwnDiff Ownership Report",
        "",
        f"- Risk: **{risk_level}** ({risk.get('risk_score', 0)}/100)",
        f"- Gate mode: `{risk.get('gate_mode', 'unknown')}`",
        f"- Files changed: {summary.get('files_changed', 0)}",
        f"- Source files changed: {summary.get('source_files_changed', 0)}",
        f"- Lines changed: +{summary.get('insertions', 0)} / -{summary.get('deletions', 0)}",
        f"- Ownership status: `{ownership_status}`",
        "",
        "## Changed Files",
        "",
    ]

    changed_files = diff.get("changed_files", [])
    if changed_files:
        for item in changed_files[:30]:
            lines.append(
                f"- `{item.get('path')}` ({item.get('status')}, +{item.get('additions')}/-{item.get('deletions')}, "
                f"{item.get('language')}, source={str(bool(item.get('is_source'))).lower()})"
            )
        if len(changed_files) > 30:
            lines.append(f"- ... {len(changed_files) - 30} more")
    else:
        lines.append("- No changed files detected.")

    lines.extend(["", "## Why This Was Flagged", ""])
    reasons = risk.get("reasons", [])
    if reasons:
        for reason in reasons:
            lines.append(f"- **{reason.get('severity')}**: {reason.get('message')}")
    else:
        lines.append("- No risk reasons detected.")

    warnings = risk.get("warnings", [])
    if warnings:
        lines.extend(["", "## Warnings", ""])
        for warning in warnings:
            lines.append(f"- {warning}")

    lines.extend(["", "## Test Evidence", ""])
    lines.append(f"- Changed source code files: {len(tests.get('changed_code_files', []))}")
    lines.append(f"- Changed test files: {len(tests.get('changed_test_files', []))}")
    lines.append(f"- Test gap detected: `{str(bool(tests.get('test_gap'))).lower()}`")
    missing = tests.get("missing_test_candidates", [])
    if missing:
        lines.append("")
        lines.append("Files without nearby tests:")
        for item in missing[:10]:
            expected = ", ".join(f"`{path}`" for path in item.get("expected", [])[:3])
            lines.append(f"- `{item.get('path')}`; possible tests: {expected}")

    lines.extend(["", "## Ownership Questions", ""])
    qs = questions.get("questions", [])
    generation = questions.get("generation", {}) if isinstance(questions.get("generation", {}), dict) else {}
    if qs:
        for question in qs:
            lines.append(f"{question.get('id')}. **{question.get('dimension')}**: {question.get('question')}")
    elif generation.get("awaiting_llm_response"):
        lines.append("- Agent LLM question generation is required before multiple choice questions can be answered.")
        if generation.get("prompt_path"):
            lines.append(f"- Prompt: `{generation.get('prompt_path')}`")
        if generation.get("response_path"):
            lines.append(f"- Expected response: `{generation.get('response_path')}`")
    elif not has_source_changes(diff):
        lines.append("- No ownership multiple choice questions generated because no configured source-code extension changed.")
    else:
        lines.append("- No ownership questions generated.")

    lines.extend(["", "## Required Human Answer", ""])
    if qs:
        lines.append(
            "Answer the ownership questions in your own words. A strong answer should explain behavior, affected callers or users, failure mode, test evidence, and rollback path where relevant."
        )
    elif generation.get("awaiting_llm_response"):
        lines.append(
            "No human answer can be collected yet. The active coding agent must use its own LLM/API context to generate validated multiple choice questions first."
        )
    elif not has_source_changes(diff):
        lines.append("No source-code ownership gate is required for this documentation or non-source-only change.")
    else:
        lines.append("No human answer is required for this report-only result.")

    lines.extend(
        [
            "",
            "## Security Note",
            "",
            "OwnDiff does not execute project code or print raw secret-like values. Treat this report as local review evidence, not as a guarantee of correctness or security.",
            "",
        ]
    )
    return "\n".join(lines)


def _ownership_status(risk: dict[str, Any], questions: dict[str, Any]) -> str:
    generation = questions.get("generation", {}) if isinstance(questions.get("generation", {}), dict) else {}
    if generation.get("awaiting_llm_response"):
        return "pending_agent_llm_questions"
    if generation.get("method") == "not_required_no_source_changes":
        return "not_required_no_source_changes"
    if risk.get("gate_mode") == "report_only" or not questions.get("questions"):
        return "report_only"
    return "pending_human_answers"
