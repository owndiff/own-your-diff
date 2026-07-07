from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import SCHEMA_VERSION, read_json, utc_now, write_json
from .config import load_config


def generate_questions(
    diff_path: str | Path,
    risk_path: str | Path,
    tests_path: str | Path | None,
    out_path: str | Path,
    repo: str | Path | None = None,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    diff = read_json(Path(diff_path))
    risk = read_json(Path(risk_path))
    tests = read_json(Path(tests_path)) if tests_path else {}
    root = Path(repo or diff.get("repo") or ".").resolve()
    config, warnings, config_sources = load_config(root, config_path)
    question_config = config.get("questions", {})
    count = int(risk.get("requirements", {}).get("question_count", 1))
    risk_level = risk.get("risk_level", "low")
    domains = risk.get("domains", [])
    changed_files = [item.get("path", "") for item in diff.get("changed_files", [])][:6]
    questions: list[dict[str, Any]] = []

    for domain in domains:
        if len(questions) >= count:
            break
        template = question_config.get("domain", {}).get(domain)
        if not isinstance(template, dict):
            continue
        questions.append(
            {
                "id": f"q{len(questions) + 1}",
                "dimension": f"domain:{domain}",
                "question": str(template.get("question", "")),
                "required": True,
                "expected_evidence": template.get("expected_evidence", []),
                "rationale": f"Risk domain detected: {domain}",
            }
        )

    dimension_order = question_config.get("dimension_order", {}).get(risk_level, question_config.get("dimension_order", {}).get("low", []))
    for dimension in dimension_order:
        if len(questions) >= count:
            break
        if dimension == "tests" and not tests.get("test_gap") and risk.get("risk_level") == "low":
            continue
        template = question_config.get("generic", {}).get(dimension)
        if not isinstance(template, dict):
            continue
        questions.append(
            {
                "id": f"q{len(questions) + 1}",
                "dimension": dimension,
                "question": str(template.get("question", "")),
                "required": risk.get("risk_level") in {"medium", "high", "critical"},
                "expected_evidence": _expected_evidence(template, changed_files),
                "rationale": "Core ownership dimension",
            }
        )

    payload: dict[str, Any] = {
        "schema_version": f"{SCHEMA_VERSION}.questions",
        "created_at": utc_now(),
        "config_sources": config_sources,
        "warnings": warnings,
        "risk_level": risk_level,
        "questions": questions,
    }
    write_json(Path(out_path), payload)
    return payload


def _expected_evidence(template: dict[str, Any], changed_files: list[str]) -> list[str]:
    evidence = [str(item) for item in template.get("expected_evidence", [])]
    if "changed files" in evidence and changed_files:
        evidence[evidence.index("changed files")] = f"changed files: {', '.join(changed_files[:3])}"
    return evidence
