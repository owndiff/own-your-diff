from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .common import OwnDiffError

PROMPT_VERSION = "owndiff.llm_questions.v1"

_SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*[\"']?[A-Za-z0-9_./+=-]{12,}"),
]
_BANNED_EXTERNAL_TERMS = (
    "web search",
    "internet",
    "google",
    "browse",
    "browser",
    "stackoverflow",
    "search online",
    "look up online",
    "latest version",
    "current version",
)


def build_agent_question_request(
    diff: dict[str, Any],
    risk: dict[str, Any],
    tests: dict[str, Any],
    question_plan: list[dict[str, Any]],
    question_config: dict[str, Any],
) -> dict[str, Any]:
    llm_config = question_config.get("llm", {})
    if not isinstance(llm_config, dict) or not llm_config.get("enabled", True):
        raise OwnDiffError("LLM question generation is required for ownership questions, but questions.llm.enabled is false.")
    prompt = build_llm_question_prompt(diff, risk, tests, question_plan, llm_config)
    return {
        "prompt_version": PROMPT_VERSION,
        "provider": "agent",
        "prompt": prompt,
        "response_shape": {
            "questions": [
                {
                    "dimension": "one of the dimensions in question_plan",
                    "difficulty": "easy",
                    "question": "specific question grounded in the diff facts",
                    "options": [
                        {"id": "a", "text": "distinct answer choice"},
                        {"id": "b", "text": "distinct answer choice"},
                        {"id": "c", "text": "distinct answer choice"},
                        {"id": "d", "text": "distinct answer choice"},
                    ],
                    "correct_option_id": "one of a, b, c, d",
                    "expected_evidence": ["short evidence item"],
                    "rationale": "why this checks ownership",
                }
            ]
        },
    }


def build_llm_question_prompt(
    diff: dict[str, Any],
    risk: dict[str, Any],
    tests: dict[str, Any],
    question_plan: list[dict[str, Any]],
    llm_config: dict[str, Any] | None = None,
) -> str:
    llm_config = llm_config or {}
    count = len(question_plan)
    facts = {
        "risk_level": risk.get("risk_level", "unknown"),
        "risk_score": risk.get("risk_score", 0),
        "risk_domains": risk.get("domains", []),
        "risk_reasons": [
            {"id": item.get("id"), "severity": item.get("severity"), "message": item.get("message")}
            for item in risk.get("reasons", [])
            if isinstance(item, dict)
        ],
        "diff_summary": diff.get("summary", {}),
        "changed_files": _changed_file_facts(diff),
        "test_gap": bool(tests.get("test_gap")),
        "changed_test_files": tests.get("changed_test_files", []),
        "missing_test_candidates": _missing_test_facts(tests),
        "question_plan": [
            {
                "id": item.get("id"),
                "dimension": item.get("dimension"),
                "context": item.get("context", {}),
            }
            for item in question_plan
        ],
        "sanitized_patch_excerpt": _sanitized_patch_excerpt(diff, _int_config(llm_config, "max_patch_chars", 12000)),
    }
    facts_json = json.dumps(facts, indent=2, sort_keys=True)
    dimensions = ", ".join(str(item.get("dimension")) for item in question_plan)

    return f"""You are OwnDiff's local question writer.

Goal:
Generate EASY multiple-choice ownership question specs for a developer reviewing an AI-assisted code diff.

Non-negotiable rules:
- Do not use web search, internet search, browsing, external documentation, package registries, issue trackers, or any fact outside the JSON facts below.
- Do not invent files, APIs, tickets, versions, vulnerabilities, benchmarks, tests, users, incidents, or business requirements.
- Ask questions answerable from the provided diff facts, sanitized patch excerpt, risk domains, and test-gap signal.
- Keep difficulty easy. These are ownership/comprehension checks, not trivia or trick questions.
- Focus on the code diff, behavior, runtime path, architecture assumption, test evidence, failure mode, blast radius, or rollback.
- If a fact is not present, ask what the developer should verify rather than pretending it is known.
- Do not include secrets. The patch excerpt may be redacted; never reconstruct redacted values.
- Return only valid JSON. No Markdown, no comments, no prose outside JSON.

Return exactly this JSON shape:
{{
  "questions": [
    {{
      "dimension": "one of: {dimensions}",
      "difficulty": "easy",
      "question": "specific question anchored to a changed file, risk domain, or test signal",
      "options": [
        {{"id": "a", "text": "plausible answer choice"}},
        {{"id": "b", "text": "plausible answer choice"}},
        {{"id": "c", "text": "plausible answer choice"}},
        {{"id": "d", "text": "plausible answer choice"}}
      ],
      "correct_option_id": "one of a, b, c, d",
      "expected_evidence": ["short evidence item 1", "short evidence item 2"],
      "rationale": "why this question helps ownership"
    }}
  ]
}}

Output requirements:
- Generate exactly {count} question objects.
- Use only the dimensions listed in question_plan.
- Generate all four answer choices yourself; OwnDiff will not add template or fallback choices.
- Give every question exactly four choices with IDs a, b, c, and d and exactly one correct_option_id.
- Make incorrect choices plausible but clearly incomplete, unsafe, or contradicted by the supplied diff facts.
- Do not repeat an answer-choice sentence within or across questions.
- Spread correct_option_id values across different letters when generating multiple questions.
- Every question or correct choice must mention a changed file path, changed file basename, or listed risk domain.
- Keep each question under 220 characters and each answer choice under 300 characters.
- Prefer practical wording a developer can answer after reading the diff.

JSON facts:
{facts_json}
"""


def validate_llm_questions(
    payload: Any,
    diff: dict[str, Any],
    risk: dict[str, Any],
    tests: dict[str, Any],
    question_plan: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("top-level response must be a JSON object")
    raw_questions = payload.get("questions")
    if not isinstance(raw_questions, list):
        raise ValueError("response must contain a questions list")
    if len(raw_questions) != len(question_plan):
        raise ValueError(f"expected {len(question_plan)} questions, got {len(raw_questions)}")

    allowed_dimensions = [str(item.get("dimension")) for item in question_plan]
    allowed_dimension_set = set(allowed_dimensions)
    anchors = _allowed_anchors(diff, risk, tests)
    path_like_allowed = _allowed_paths(diff, tests)
    context_by_dimension = {
        str(item.get("dimension")): item.get("context", {})
        for item in question_plan
        if isinstance(item.get("context", {}), dict)
    }

    result = []
    used_dimensions: set[str] = set()
    used_option_texts: set[str] = set()
    for index, raw_question in enumerate(raw_questions, start=1):
        if len(result) >= len(question_plan):
            break
        if not isinstance(raw_question, dict):
            raise ValueError(f"question {index} must be an object")

        dimension = str(raw_question.get("dimension", "")).strip()
        if dimension not in allowed_dimension_set:
            raise ValueError(f"question {index} uses unknown dimension {dimension!r}")
        if dimension in used_dimensions:
            raise ValueError(f"question {index} repeats dimension {dimension!r}")

        difficulty = str(raw_question.get("difficulty", "easy")).strip().lower()
        if difficulty != "easy":
            raise ValueError(f"question {index} must be easy, got {difficulty!r}")

        question = _clean_text(raw_question.get("question"), 220, f"question {index}")
        options = _clean_options(raw_question.get("options"), index, used_option_texts)
        correct_option_id = str(raw_question.get("correct_option_id", "")).strip().lower()
        option_ids = {option["id"] for option in options}
        if correct_option_id not in option_ids:
            raise ValueError(f"question {index} correct_option_id must be one of a, b, c, or d")
        correct_answer = next(option["text"] for option in options if option["id"] == correct_option_id)
        expected_evidence = _clean_evidence(raw_question.get("expected_evidence"), index)
        rationale = _clean_optional_text(raw_question.get("rationale"), 220)

        option_text = " ".join(option["text"] for option in options)
        combined = f"{dimension} {question} {option_text} {' '.join(expected_evidence)}".lower()
        if any(term in combined for term in _BANNED_EXTERNAL_TERMS):
            raise ValueError(f"question {index} asks for external/web knowledge")
        grounded_text = f"{dimension} {question} {correct_answer}".lower()
        if not any(anchor in grounded_text for anchor in anchors):
            raise ValueError(f"question {index} is not anchored to changed files or risk domains")

        unknown_paths = sorted(_path_like_terms(combined) - path_like_allowed)
        if unknown_paths:
            raise ValueError(f"question {index} mentions unknown path(s): {', '.join(unknown_paths[:3])}")

        result.append(
            {
                "id": f"q{len(result) + 1}",
                "dimension": dimension,
                "question": question,
                "required": True,
                "correct_answer": correct_answer,
                "options": options,
                "correct_option_id": correct_option_id,
                "expected_evidence": expected_evidence,
                "context": context_by_dimension.get(dimension, {}),
                "rationale": rationale or "LLM-generated easy ownership question grounded in OwnDiff facts",
                "difficulty": "easy",
                "source": "llm",
            }
        )
        used_dimensions.add(dimension)

    if len(result) != len(question_plan):
        raise ValueError(f"expected {len(question_plan)} validated questions, got {len(result)}")
    return result


def _int_config(config: dict[str, Any], key: str, default: int) -> int:
    try:
        value = int(config.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _changed_file_facts(diff: dict[str, Any]) -> list[dict[str, Any]]:
    facts = []
    for item in diff.get("changed_files", [])[:20]:
        if not isinstance(item, dict):
            continue
        facts.append(
            {
                "path": item.get("path"),
                "status": item.get("status"),
                "additions": item.get("additions"),
                "deletions": item.get("deletions"),
                "language": item.get("language"),
                "is_test": bool(item.get("is_test")),
            }
        )
    return facts


def _missing_test_facts(tests: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for item in tests.get("missing_test_candidates", [])[:10]:
        if not isinstance(item, dict):
            continue
        result.append({"path": item.get("path"), "expected": item.get("expected", [])[:3]})
    return result


def _sanitized_patch_excerpt(diff: dict[str, Any], max_chars: int) -> str:
    patch_path = diff.get("patch_path")
    if not patch_path:
        return ""
    path = Path(str(patch_path))
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    sanitized = []
    for line in text.splitlines():
        if any(pattern.search(line) for pattern in _SECRET_PATTERNS):
            prefix = line[:1] if line[:1] in {"+", "-", " "} else ""
            sanitized.append(f"{prefix}[owndiff:redacted-secret-like-line]")
        else:
            sanitized.append(line)
    excerpt = "\n".join(sanitized)
    if len(excerpt) > max_chars:
        return excerpt[:max_chars] + "\n[owndiff:patch excerpt truncated]"
    return excerpt


def _clean_text(raw: Any, max_length: int, label: str) -> str:
    if not isinstance(raw, str):
        raise ValueError(f"{label} must be a string")
    text = " ".join(raw.strip().split())
    if not text:
        raise ValueError(f"{label} must not be empty")
    if len(text) > max_length:
        raise ValueError(f"{label} is too long")
    return text


def _clean_optional_text(raw: Any, max_length: int) -> str:
    if not isinstance(raw, str):
        return ""
    return " ".join(raw.strip().split())[:max_length]


def _clean_options(raw: Any, index: int, used_option_texts: set[str]) -> list[dict[str, str]]:
    if not isinstance(raw, list) or len(raw) != 4:
        raise ValueError(f"question {index} options must contain exactly four choices")

    options: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    local_texts: set[str] = set()
    for option_number, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"question {index} option {option_number} must be an object")
        option_id = str(item.get("id", "")).strip().lower()
        if option_id not in {"a", "b", "c", "d"} or option_id in seen_ids:
            raise ValueError(f"question {index} options must use unique IDs a, b, c, and d")
        text = _clean_text(item.get("text"), 300, f"question {index} option {option_id}")
        normalized = text.casefold()
        if normalized in local_texts:
            raise ValueError(f"question {index} repeats an answer choice")
        if normalized in used_option_texts:
            raise ValueError(f"question {index} repeats an answer choice used by another question")
        options.append({"id": option_id, "text": text})
        seen_ids.add(option_id)
        local_texts.add(normalized)

    if seen_ids != {"a", "b", "c", "d"}:
        raise ValueError(f"question {index} options must use IDs a, b, c, and d")
    used_option_texts.update(local_texts)
    return options


def _clean_evidence(raw: Any, index: int) -> list[str]:
    if not isinstance(raw, list):
        raise ValueError(f"question {index} expected_evidence must be a list")
    evidence = []
    for item in raw[:4]:
        if isinstance(item, str) and item.strip():
            evidence.append(" ".join(item.strip().split())[:120])
    if not evidence:
        raise ValueError(f"question {index} expected_evidence must not be empty")
    return evidence


def _allowed_anchors(diff: dict[str, Any], risk: dict[str, Any], tests: dict[str, Any]) -> set[str]:
    anchors: set[str] = set()
    for path in _allowed_paths(diff, tests):
        anchors.add(path.lower())
        anchors.add(Path(path).name.lower())
    for domain in risk.get("domains", []):
        domain_text = str(domain).lower()
        anchors.add(domain_text)
        if domain_text == "auth":
            anchors.add("authentication")
            anchors.add("session")
        if domain_text == "authorization":
            anchors.add("permission")
            anchors.add("tenant")
        if domain_text == "dependencies":
            anchors.add("dependency")
            anchors.add("package")
    return {anchor for anchor in anchors if anchor}


def _allowed_paths(diff: dict[str, Any], tests: dict[str, Any]) -> set[str]:
    paths = {
        str(item.get("path", "")).lower()
        for item in diff.get("changed_files", [])
        if isinstance(item, dict) and str(item.get("path", "")).strip()
    }
    paths.update(_path_aliases(paths))
    paths.update(str(path).lower() for path in tests.get("changed_test_files", []) if str(path).strip())
    paths.update(_path_aliases(paths))
    for item in tests.get("missing_test_candidates", []):
        if not isinstance(item, dict):
            continue
        if item.get("path"):
            paths.add(str(item["path"]).lower())
            paths.update(_path_aliases({str(item["path"]).lower()}))
        for expected in item.get("expected", [])[:5]:
            if str(expected).strip():
                paths.add(str(expected).lower())
                paths.update(_path_aliases({str(expected).lower()}))
    return paths


def _path_aliases(paths: set[str]) -> set[str]:
    aliases: set[str] = set()
    for path in paths:
        parts = [part for part in path.replace("\\", "/").split("/") if part]
        if not parts:
            continue
        aliases.add(Path(path).name.lower())
        for index in range(1, len(parts)):
            aliases.add("/".join(parts[index:]).lower())
    return aliases


def _path_like_terms(text: str) -> set[str]:
    terms = set()
    extension_pattern = r"(?:py|js|jsx|ts|tsx|mjs|cjs|mts|cts|go|java|rb|rs|php|cs|kt|swift|sql|ya?ml|json|toml|tf|sh)"
    pattern = rf"\b[\w.-]+(?:/[\w.-]+)+\.{extension_pattern}\b|\b[\w.-]+\.{extension_pattern}\b"
    for match in re.finditer(pattern, text):
        terms.add(match.group(0).lower())
    return terms
