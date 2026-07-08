from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .common import SCHEMA_VERSION, OwnDiffError, ensure_parent, read_json, utc_now, write_json
from .config import load_config
from .llm_questions import build_agent_question_request, validate_llm_questions


def generate_questions(
    diff_path: str | Path,
    risk_path: str | Path,
    tests_path: str | Path | None,
    out_path: str | Path,
    repo: str | Path | None = None,
    config_path: str | Path | None = None,
    llm_response_path: str | Path | None = None,
    prompt_out_path: str | Path | None = None,
    request_out_path: str | Path | None = None,
    response_out_path: str | Path | None = None,
) -> dict[str, Any]:
    diff = read_json(Path(diff_path))
    risk = read_json(Path(risk_path))
    tests = read_json(Path(tests_path)) if tests_path else {}
    root = Path(repo or diff.get("repo") or ".").resolve()
    config, warnings, config_sources = load_config(root, config_path)
    question_config = config.get("questions", {})
    risk_level = str(risk.get("risk_level", "low"))
    enabled_levels = {str(level) for level in question_config.get("enabled_risk_levels", ["medium", "high", "critical"])}
    count = int(risk.get("requirements", {}).get("question_count", 1)) if risk_level in enabled_levels else 0
    domains = _path_grounded_domains(risk.get("domains", []), diff, config)
    context = _question_context(diff, risk, tests, domains)
    question_plan: list[dict[str, Any]] = []
    used_dimensions: set[str] = set()

    dimension_order = question_config.get("dimension_order", {}).get(risk_level, question_config.get("dimension_order", {}).get("low", []))
    starter_dimensions = question_config.get("starter_dimensions", {}).get(risk_level, [])
    for dimension in starter_dimensions:
        if len(question_plan) >= count:
            break
        _append_plan_item(question_plan, used_dimensions, dimension, context, risk, "Core ownership dimension")

    domain_limit = int(question_config.get("max_domain_questions", {}).get(risk_level, 1))
    for domain in domains[: max(0, domain_limit)]:
        if len(question_plan) >= count:
            break
        dimension = f"domain:{domain}"
        domain_context = {**context, **_domain_context(domain)}
        _append_plan_item(question_plan, used_dimensions, dimension, domain_context, risk, f"Risk domain detected: {domain}")

    for dimension in dimension_order:
        if len(question_plan) >= count:
            break
        _append_plan_item(question_plan, used_dimensions, dimension, context, risk, "Core ownership dimension")

    if question_plan:
        provider = _llm_provider(question_config)
        if provider == "agent":
            questions, generation = _agent_questions(
                diff,
                risk,
                tests,
                question_plan,
                question_config,
                out_path,
                llm_response_path,
                prompt_out_path,
                request_out_path,
                response_out_path,
            )
        else:
            raise OwnDiffError(
                f"Unsupported questions.llm.provider {provider!r}. OwnDiff only accepts 'agent' so question generation "
                "uses the active coding agent's current LLM/API context."
            )
    else:
        questions = []
        generation = {
            "method": "not_required",
            "llm_required": False,
            "awaiting_llm_response": False,
            "planned_dimensions": [],
        }

    payload: dict[str, Any] = {
        "schema_version": f"{SCHEMA_VERSION}.questions",
        "created_at": utc_now(),
        "config_sources": config_sources,
        "warnings": warnings,
        "risk_level": risk_level,
        "generation": generation,
        "questions": questions,
    }
    write_json(Path(out_path), payload)
    return payload


def _llm_provider(question_config: dict[str, Any]) -> str:
    llm_config = question_config.get("llm", {})
    if not isinstance(llm_config, dict) or not llm_config.get("enabled", True):
        raise OwnDiffError("LLM question generation is required for ownership questions, but questions.llm.enabled is false.")
    provider = str(llm_config.get("provider", "agent")).strip().lower()
    aliases = {
        "native": "agent",
        "model": "agent",
        "current_agent": "agent",
        "current-agent": "agent",
    }
    return aliases.get(provider, provider)


def _agent_questions(
    diff: dict[str, Any],
    risk: dict[str, Any],
    tests: dict[str, Any],
    question_plan: list[dict[str, Any]],
    question_config: dict[str, Any],
    out_path: str | Path,
    llm_response_path: str | Path | None,
    prompt_out_path: str | Path | None,
    request_out_path: str | Path | None,
    response_out_path: str | Path | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    planned_dimensions = [item.get("dimension") for item in question_plan]
    if llm_response_path:
        response = read_json(Path(llm_response_path))
        try:
            questions = validate_llm_questions(response, diff, risk, tests, question_plan)
        except ValueError as exc:
            raise OwnDiffError(f"Agent LLM question response rejected: {exc}") from exc
        return questions, {
            "method": "agent_llm",
            "llm_required": True,
            "awaiting_llm_response": False,
            "planned_dimensions": planned_dimensions,
            "response_path": str(Path(llm_response_path)),
        }

    prompt_path = Path(prompt_out_path) if prompt_out_path else Path(out_path).with_name("question-prompt.md")
    request_path = Path(request_out_path) if request_out_path else Path(out_path).with_name("question-request.json")
    response_path = Path(response_out_path) if response_out_path else Path(out_path).with_name("question-response.json")
    request = build_agent_question_request(diff, risk, tests, question_plan, question_config)

    ensure_parent(prompt_path)
    prompt_path.write_text(request["prompt"], encoding="utf-8")
    write_json(
        request_path,
        {
            "schema_version": f"{SCHEMA_VERSION}.question_request",
            "created_at": utc_now(),
            "provider": "agent",
            "prompt_version": request["prompt_version"],
            "prompt_path": str(prompt_path),
            "response_path": str(response_path),
            "planned_dimensions": planned_dimensions,
            "question_count": len(question_plan),
            "instructions": (
                "Use the current coding agent's own LLM/API context to answer question-prompt.md. "
                "Write only the requested JSON response to question-response.json, then rerun run_owndiff.py "
                "with --llm-response question-response.json."
            ),
        },
    )
    return [], {
        "method": "agent_llm_required",
        "llm_required": True,
        "awaiting_llm_response": True,
        "planned_dimensions": planned_dimensions,
        "prompt_path": str(prompt_path),
        "request_path": str(request_path),
        "response_path": str(response_path),
        "instructions": (
            "The active coding agent must use its own LLM/API context to generate the JSON response. "
            "Do not use web search or deterministic fallback questions."
        ),
    }


def _append_plan_item(
    question_plan: list[dict[str, Any]],
    used_dimensions: set[str],
    dimension: str,
    context: dict[str, str],
    risk: dict[str, Any],
    rationale: str,
) -> None:
    if dimension in used_dimensions:
        return
    question_plan.append(
        {
            "id": f"q{len(question_plan) + 1}",
            "dimension": dimension,
            "required": risk.get("risk_level") in {"medium", "high", "critical"},
            "context": _public_context(context),
            "rationale": rationale,
        }
    )
    used_dimensions.add(dimension)


def _question_context(
    diff: dict[str, Any],
    risk: dict[str, Any],
    tests: dict[str, Any],
    domains: list[str],
) -> dict[str, str]:
    changed_files = [item for item in diff.get("changed_files", []) if isinstance(item, dict)]
    focus_files = _focus_files(changed_files)
    summary = diff.get("summary", {})
    changed_tests = [str(path) for path in tests.get("changed_test_files", [])[:3]]
    missing_tests = tests.get("missing_test_candidates", [])
    reasons = [str(item.get("message", "")) for item in risk.get("reasons", []) if isinstance(item, dict)]

    return {
        "focus": _format_list(focus_files) if focus_files else "the changed code",
        "primary_file": focus_files[0] if focus_files else "the changed code",
        "change_kind": _change_kind(focus_files, domains),
        "domain_label": _format_domains(domains) if domains else "this code path",
        "test_signal": _test_signal(bool(tests.get("test_gap")), changed_tests, missing_tests),
        "risk_summary": reasons[0] if reasons else "OwnDiff flagged ownership risk for this diff",
        "change_scale": (
            f"{int(summary.get('files_changed', 0))} file(s), "
            f"+{int(summary.get('insertions', 0))}/-{int(summary.get('deletions', 0))}"
        ),
    }


def _path_grounded_domains(
    raw_domains: Any,
    diff: dict[str, Any],
    config: dict[str, Any],
) -> list[str]:
    domains = [str(domain) for domain in raw_domains if str(domain).strip()] if isinstance(raw_domains, list) else []
    changed_paths = [
        str(item.get("path", "")).lower()
        for item in diff.get("changed_files", [])
        if isinstance(item, dict) and str(item.get("path", "")).strip()
    ]
    rules = config.get("risk", {}).get("domain_rules", {})
    if not isinstance(rules, dict):
        return []

    grounded = []
    for domain in domains:
        rule = rules.get(domain, {})
        if not isinstance(rule, dict):
            continue
        path_terms = [str(term).lower() for term in rule.get("path_terms", []) if str(term).strip()]
        if any(_path_matches_term(path, term) for term in path_terms for path in changed_paths):
            grounded.append(domain)
    return grounded


def _path_matches_term(path: str, term: str) -> bool:
    normalized_path = path.replace("\\", "/")
    normalized_term = term.replace("\\", "/")
    if "/" in normalized_term or "." in normalized_term:
        return normalized_term in normalized_path

    parts = [part for part in normalized_path.split("/") if part]
    for index, part in enumerate(parts):
        candidate = Path(part).stem if index == len(parts) - 1 else part
        if normalized_term in {token for token in re.split(r"[^a-z0-9]+", candidate) if token}:
            return True
    return False


def _focus_files(changed_files: list[dict[str, Any]]) -> list[str]:
    candidates = [
        item
        for item in changed_files
        if str(item.get("path", "")).strip() and not bool(item.get("is_test"))
    ] or [item for item in changed_files if str(item.get("path", "")).strip()]
    sorted_files = sorted(
        candidates,
        key=lambda item: (-(int(item.get("additions", 0)) + int(item.get("deletions", 0))), str(item.get("path", ""))),
    )
    return [str(item.get("path")) for item in sorted_files[:3]]


def _change_kind(focus_files: list[str], domains: list[str]) -> str:
    if domains:
        return _format_domains(domains)
    joined = " ".join(focus_files).lower()
    if any(term in joined for term in ("workflow", ".github", "terraform", "dockerfile", "deploy", "infra")):
        return "CI, deployment, or infrastructure behavior"
    if any(term in joined for term in ("package.json", "requirements", "pyproject", "setup.py", "poetry.lock", "uv.lock")):
        return "dependency or package behavior"
    if any(term in joined for term in ("config", ".yaml", ".yml", ".json", ".toml")):
        return "configuration behavior"
    return "code behavior"


def _format_domains(domains: list[str]) -> str:
    labels = [_domain_context(domain)["domain_label"] for domain in domains]
    return _format_list(labels)


def _domain_context(domain: str) -> dict[str, str]:
    labels = {
        "auth": "authentication or session behavior",
        "authorization": "authorization, permissions, or tenant boundaries",
        "payments": "payment or billing behavior",
        "database": "database schema or data behavior",
        "execution": "command or dynamic-code execution",
        "crypto": "cryptography or sensitive hashing",
        "concurrency": "concurrency, async, queue, or transaction behavior",
        "infra": "CI, deployment, or infrastructure permissions",
        "dependencies": "dependency or package metadata",
    }
    return {"domain_label": labels.get(domain, domain.replace("_", " "))}


def _test_signal(test_gap: bool, changed_tests: list[str], missing_tests: list[Any]) -> str:
    if changed_tests:
        return f"tests changed in {_format_list(changed_tests)}"
    if test_gap and missing_tests:
        paths = [str(item.get("path", "")) for item in missing_tests if isinstance(item, dict) and item.get("path")]
        if paths:
            return f"OwnDiff did not find a nearby test change for {_format_list(paths[:2])}"
    if test_gap:
        return "OwnDiff did not find a changed or nearby test for the changed code"
    return "OwnDiff did not flag a nearby-test gap"


def _format_list(items: list[str]) -> str:
    clean = [item for item in items if item]
    if not clean:
        return ""
    if len(clean) == 1:
        return clean[0]
    if len(clean) == 2:
        return f"{clean[0]} and {clean[1]}"
    return f"{', '.join(clean[:-1])}, and {clean[-1]}"


def _public_context(context: dict[str, str]) -> dict[str, str]:
    keys = ("focus", "change_kind", "domain_label", "test_signal", "risk_summary", "change_scale")
    return {key: context[key] for key in keys if key in context}
