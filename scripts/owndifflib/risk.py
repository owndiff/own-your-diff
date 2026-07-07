from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .common import SCHEMA_VERSION, read_json, utc_now, write_json
from .config import load_config
from .matching import matches_any


def _patch_text(diff: dict[str, Any]) -> str:
    patch_path = diff.get("patch_path")
    if not patch_path:
        return ""
    path = Path(patch_path)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _added_lines(patch: str) -> list[str]:
    return [
        line[1:]
        for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++") and len(line) > 1
    ]


def _domain_matches(domain_rule: dict[str, Any], changed_paths: list[str], patch_lower: str) -> bool:
    path_terms = [term.lower() for term in domain_rule.get("path_terms", [])]
    content_terms = [term.lower() for term in domain_rule.get("content_terms", [])]
    path_hit = any(term in path.lower() for path in changed_paths for term in path_terms)
    content_hit = any(term in patch_lower for term in content_terms)
    return path_hit or content_hit


def _reason(reason_id: str, severity: str, score: int, message: str) -> dict[str, Any]:
    return {"id": reason_id, "severity": severity, "score": score, "message": message}


def _level(score: int, thresholds: dict[str, int]) -> str:
    if score >= int(thresholds.get("critical", 80)):
        return "critical"
    if score >= int(thresholds.get("high", 50)):
        return "high"
    if score >= int(thresholds.get("medium", 25)):
        return "medium"
    return "low"


def _format_message(template: str, **values: Any) -> str:
    try:
        return template.format(**values)
    except KeyError:
        return template


def _first_matching_line_rule(lines_changed: int, rules: list[dict[str, Any]]) -> dict[str, Any] | None:
    for rule in sorted(rules, key=lambda item: int(item.get("min", 0)), reverse=True):
        if lines_changed >= int(rule.get("min", 0)):
            return rule
    return None


def _compiled_secret_patterns(risk_config: dict[str, Any]) -> list[re.Pattern[str]]:
    patterns = []
    for item in risk_config.get("secret_patterns", []):
        try:
            patterns.append(re.compile(str(item.get("pattern", ""))))
        except re.error:
            continue
    return patterns


def score_risk(
    repo: str | Path,
    diff_path: str | Path,
    tests_path: str | Path | None,
    out_path: str | Path,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(repo).resolve()
    diff = read_json(Path(diff_path))
    tests = read_json(Path(tests_path)) if tests_path else {}
    config, warnings, config_sources = load_config(root, config_path)
    risk_config = config.get("risk", {})
    changed_files = diff.get("changed_files", [])
    changed_paths = [item.get("path", "") for item in changed_files]
    skip_paths = risk_config.get("skip", {}).get("paths", [])
    non_skipped = [path for path in changed_paths if not matches_any(path, skip_paths)]

    score = 0
    reasons: list[dict[str, Any]] = []

    summary = diff.get("summary", {})
    lines_changed = int(summary.get("lines_changed", 0))
    files_changed = int(summary.get("files_changed", 0))
    for rule in risk_config.get("size_rules", {}).get("files", []):
        if files_changed >= int(rule.get("min", 0)):
            rule_score = int(rule.get("score", 0))
            score += rule_score
            reasons.append(
                _reason(
                    str(rule.get("id", "size.files")),
                    str(rule.get("severity", "medium")),
                    rule_score,
                    _format_message(str(rule.get("message", "Large surface area")), files_changed=files_changed),
                )
            )
            break

    line_rule = _first_matching_line_rule(lines_changed, risk_config.get("size_rules", {}).get("lines", []))
    if line_rule:
        rule_score = int(line_rule.get("score", 0))
        score += rule_score
        reasons.append(
            _reason(
                str(line_rule.get("id", "size.lines")),
                str(line_rule.get("severity", "medium")),
                rule_score,
                _format_message(str(line_rule.get("message", "Large diff")), lines_changed=lines_changed),
            )
        )

    for level_name, rule in risk_config.get("path_rules", {}).items():
        patterns = rule.get("paths", [])
        matches = [path for path in non_skipped if matches_any(path, patterns)]
        if matches:
            level_score = int(rule.get("score", 0))
            severity = str(rule.get("severity", level_name))
            score += level_score
            sample = ", ".join(matches[:3])
            more = "" if len(matches) <= 3 else f", +{len(matches) - 3} more"
            match_summary = f"{sample}{more}"
            reasons.append(
                _reason(
                    f"path.{level_name}",
                    severity,
                    level_score,
                    _format_message(str(rule.get("message", "Changed path pattern: {matches}")), matches=match_summary),
                )
            )

    patch = _patch_text(diff)
    patch_lower = patch.lower()
    domains = []
    for domain, rule in risk_config.get("domain_rules", {}).items():
        if _domain_matches(rule, non_skipped, patch_lower):
            domains.append(domain)
            domain_score = int(rule["score"])
            score += domain_score
            reasons.append(
                _reason(
                    f"domain.{domain}",
                    str(rule.get("severity", "high" if domain_score >= 18 else "medium")),
                    domain_score,
                    str(rule["message"]),
                )
            )

    secret_hits = 0
    secret_patterns = _compiled_secret_patterns(risk_config)
    for line in _added_lines(patch):
        if any(pattern.search(line) for pattern in secret_patterns):
            secret_hits += 1
    if secret_hits:
        rule = risk_config.get("secret_addition", {})
        secret_score = int(rule.get("score", 45))
        score += secret_score
        reasons.append(
            _reason(
                "secret.like.addition",
                str(rule.get("severity", "critical")),
                secret_score,
                _format_message(
                    str(rule.get("message", "Detected {secret_hits} secret-like added lines; values are redacted")),
                    secret_hits=secret_hits,
                ),
            )
        )

    changed_test_deletions = [
        item.get("path", "")
        for item in changed_files
        if item.get("is_test") and item.get("status") == "D"
    ]
    if changed_test_deletions:
        rule = risk_config.get("deleted_tests", {})
        rule_score = int(rule.get("score", 12))
        score += rule_score
        reasons.append(
            _reason("tests.deleted", str(rule.get("severity", "medium")), rule_score, str(rule.get("message", "Deletes test files")))
        )

    if tests.get("test_gap") and domains:
        rule = risk_config.get("test_gap", {}).get("risky", {})
        rule_score = int(rule.get("score", 16))
        score += rule_score
        reasons.append(
            _reason(
                "tests.gap",
                str(rule.get("severity", "high")),
                rule_score,
                str(rule.get("message", "Risky code changed without a changed or nearby test file")),
            )
        )
    elif tests.get("test_gap"):
        rule = risk_config.get("test_gap", {}).get("standard", {})
        rule_score = int(rule.get("score", 8))
        score += rule_score
        reasons.append(
            _reason(
                "tests.gap.low",
                str(rule.get("severity", "medium")),
                rule_score,
                str(rule.get("message", "Code changed without a changed or nearby test file")),
            )
        )

    if changed_files and not non_skipped and not secret_hits:
        rule = risk_config.get("docs_only", {})
        score = min(score, int(rule.get("max_score", 10)))
        reasons.append(
            _reason("docs.only", str(rule.get("severity", "low")), 0, str(rule.get("message", "Only skipped paths changed")))
        )

    thresholds = risk_config.get("thresholds", {})
    score = max(0, min(int(risk_config.get("max_score", 100)), score))
    risk_level = _level(score, thresholds)
    question_counts = config.get("questions", {}).get("question_counts", {})
    ownership_requirement = risk_config.get("ownership_requirements", {}).get(risk_level, {})
    requirements = {
        "question_count": int(question_counts.get(risk_level, 1)),
        "require_tests": bool(ownership_requirement.get("require_tests", risk_level in {"high", "critical"})),
        "require_rollback_plan": bool(ownership_requirement.get("require_rollback_plan", risk_level in {"high", "critical"})),
        "min_ownership_score": int(ownership_requirement.get("min_ownership_score", 0)),
    }
    gate_mode = str(risk_config.get("gate_modes", {}).get(risk_level, "report_only"))

    payload: dict[str, Any] = {
        "schema_version": f"{SCHEMA_VERSION}.risk",
        "created_at": utc_now(),
        "repo": str(root),
        "config_sources": config_sources,
        "risk_level": risk_level,
        "risk_score": score,
        "gate_mode": gate_mode,
        "domains": sorted(set(domains)),
        "reasons": reasons,
        "requirements": requirements,
        "warnings": warnings,
    }
    write_json(Path(out_path), payload)
    return payload
