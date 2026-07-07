from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import SCHEMA_VERSION, read_json, utc_now, write_json
from .config import load_config


def generate_mcq_bundle(
    diff_path: str | Path,
    risk_path: str | Path,
    tests_path: str | Path,
    questions_path: str | Path,
    mcq_out: str | Path,
    answer_key_out: str | Path,
    answers_template_out: str | Path,
    gate_out: str | Path,
    repo: str | Path | None = None,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    diff = read_json(Path(diff_path))
    risk = read_json(Path(risk_path))
    tests = read_json(Path(tests_path))
    questions = read_json(Path(questions_path))
    root = Path(repo or diff.get("repo") or ".").resolve()
    config, warnings, config_sources = load_config(root, config_path)
    mcq_config = config.get("mcq", {})

    mcq_questions, answer_key = _build_mcq_questions(diff, risk, tests, questions, mcq_config)
    mcq_payload: dict[str, Any] = {
        "schema_version": f"{SCHEMA_VERSION}.mcq",
        "created_at": utc_now(),
        "repo": str(root),
        "config_sources": config_sources,
        "warnings": warnings,
        "risk_level": risk.get("risk_level", "low"),
        "instructions": "Use quiz_tui.py --evaluate when a TTY is available. If no TTY is available, present these questions in chat, collect selections such as q1=c q2=b, then run submit_answers.py --evaluate.",
        "questions": mcq_questions,
    }
    key_payload: dict[str, Any] = {
        "schema_version": f"{SCHEMA_VERSION}.answer_key",
        "created_at": utc_now(),
        "repo": str(root),
        "answers": answer_key,
    }
    answers_template = {
        "schema_version": f"{SCHEMA_VERSION}.answers",
        "created_at": utc_now(),
        "repo": str(root),
        "answers": {question["id"]: "" for question in mcq_questions},
    }
    gate = _initial_gate(risk, mcq_questions, mcq_config)

    write_json(Path(mcq_out), mcq_payload)
    write_json(Path(answer_key_out), key_payload)
    write_json(Path(answers_template_out), answers_template)
    write_json(Path(gate_out), gate)

    return {
        "mcq": mcq_payload,
        "answer_key": key_payload,
        "answers_template": answers_template,
        "gate": gate,
    }


def evaluate_answers(
    mcq_path: str | Path,
    answer_key_path: str | Path,
    answers_path: str | Path,
    out_path: str | Path,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    mcq = read_json(Path(mcq_path))
    answer_key = read_json(Path(answer_key_path))
    submitted = read_json(Path(answers_path))
    repo = Path(str(mcq.get("repo") or ".")).resolve()
    config, warnings, config_sources = load_config(repo, config_path)
    mcq_config = config.get("mcq", {})
    required_score = int(mcq_config.get("required_score_percent", 100))

    expected = answer_key.get("answers", {})
    answers = submitted.get("answers", {})
    results = []
    correct_count = 0
    for question in mcq.get("questions", []):
        question_id = str(question.get("id"))
        selected = _normalize_selected(answers.get(question_id))
        correct = set(expected.get(question_id, {}).get("correct_option_ids", []))
        is_correct = bool(correct) and selected == correct
        if is_correct:
            correct_count += 1
        results.append(
            {
                "id": question_id,
                "selected_option_ids": sorted(selected),
                "correct_option_ids": sorted(correct),
                "correct": is_correct,
                "explanation": expected.get(question_id, {}).get("explanation", ""),
            }
        )

    total = len(mcq.get("questions", []))
    score_percent = 100 if total == 0 else round((correct_count / total) * 100)
    passed = score_percent >= required_score and all(item["correct"] for item in results)
    gate_status = mcq_config.get("gate", {}).get("passed_status" if passed else "failed_status", "passed" if passed else "failed")
    payload: dict[str, Any] = {
        "schema_version": f"{SCHEMA_VERSION}.gate",
        "created_at": utc_now(),
        "repo": str(repo),
        "config_sources": config_sources,
        "warnings": warnings,
        "status": gate_status,
        "score_percent": score_percent,
        "required_score_percent": required_score,
        "correct": correct_count,
        "total": total,
        "merge_allowed": passed,
        "push_allowed": passed,
        "agent_may_push_merge_request": passed,
        "recommendation": (
            "Ownership MCQ gate passed. Agent may push/open the merge request if normal tests and review requirements also pass."
            if passed
            else "Ownership MCQ gate failed. Do not push/open the merge request until all answers are correct."
        ),
        "results": results,
    }
    write_json(Path(out_path), payload)
    return payload


def render_mcq_markdown(mcq_path: str | Path) -> str:
    mcq = read_json(Path(mcq_path))
    questions = mcq.get("questions", [])
    lines = [
        "# OwnDiff Ownership Questions",
        "",
        "Answer in this chat using selections like `q1=c q2=b q3=a`.",
        "The agent will save those selections to `.owndiff/ownership-answers.json` and run `submit_answers.py --evaluate`.",
        "",
    ]
    if not questions:
        lines.extend(
            [
                "No ownership questions are required for this diff.",
                "",
                "The change is report-only unless `.owndiff/ownership-gate.json` says otherwise.",
            ]
        )
        return "\n".join(lines).rstrip() + "\n"

    risk_level = mcq.get("risk_level")
    if risk_level:
        lines.extend([f"Risk level: `{risk_level}`", ""])

    for question in questions:
        question_id = str(question.get("id", "q?"))
        lines.append(f"## {question_id}. {question.get('question', '')}")
        lines.append("")
        for option in question.get("options", []):
            option_id = str(option.get("id", "")).lower()
            lines.append(f"- `{option_id}`. {option.get('text', '')}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_answers_from_pairs(
    pairs: list[str],
    out_path: str | Path,
    mcq_path: str | Path | None = None,
) -> dict[str, Any]:
    answers: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"Answer must use qid=option format: {pair}")
        question_id, option_id = pair.split("=", 1)
        question_id = question_id.strip()
        option_id = option_id.strip().lower()
        if not question_id or not option_id:
            raise ValueError(f"Answer must use qid=option format: {pair}")
        answers[question_id] = option_id

    return write_answers_from_mapping(answers, out_path, mcq_path)


def write_answers_from_mapping(
    answers: dict[str, str],
    out_path: str | Path,
    mcq_path: str | Path | None = None,
) -> dict[str, Any]:
    normalized_answers = {
        str(question_id).strip(): str(option_id).strip().lower()
        for question_id, option_id in answers.items()
        if str(question_id).strip() and str(option_id).strip()
    }

    repo = "."
    if mcq_path:
        mcq = read_json(Path(mcq_path))
        expected_ids = {str(question.get("id")) for question in mcq.get("questions", [])}
        option_ids_by_question = {
            str(question.get("id")): {str(option.get("id", "")).lower() for option in question.get("options", [])}
            for question in mcq.get("questions", [])
        }
        unknown = sorted(set(normalized_answers) - expected_ids)
        if unknown:
            raise ValueError(f"Unknown question id(s): {', '.join(unknown)}")
        missing = sorted(expected_ids - set(normalized_answers))
        if missing:
            raise ValueError(f"Missing answer(s): {', '.join(missing)}")
        invalid_options = [
            f"{question_id}={option_id}"
            for question_id, option_id in sorted(normalized_answers.items())
            if option_id not in option_ids_by_question.get(question_id, set())
        ]
        if invalid_options:
            raise ValueError(f"Unknown option selection(s): {', '.join(invalid_options)}")
        repo = str(mcq.get("repo") or ".")

    payload = {
        "schema_version": f"{SCHEMA_VERSION}.answers",
        "created_at": utc_now(),
        "repo": repo,
        "answers": normalized_answers,
    }
    write_json(Path(out_path), payload)
    return payload


def _build_mcq_questions(
    diff: dict[str, Any],
    risk: dict[str, Any],
    tests: dict[str, Any],
    questions: dict[str, Any],
    mcq_config: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    source_questions = questions.get("questions", [])
    if not source_questions:
        return [], {}

    max_options = int(mcq_config.get("max_options_per_question", 4))
    default_distractors = [str(item) for item in mcq_config.get("default_distractors", [])]
    correct_prefix = str(mcq_config.get("default_correct_prefix", "A complete owner answer should cover"))

    mcq_questions = []
    answer_key: dict[str, dict[str, Any]] = {}
    for index, question in enumerate(source_questions, start=1):
        question_id = str(question.get("id") or f"q{index}")
        expected = [str(item) for item in question.get("expected_evidence", []) if str(item).strip()]
        if not expected:
            expected = _fallback_expected_evidence(diff, risk, tests)
        correct_text = f"{correct_prefix}: {', '.join(expected)}."
        distractors = _distractors_for_question(question, risk, tests, default_distractors)
        options = [{"id": "a", "text": correct_text}]
        for option_index, distractor in enumerate(distractors[: max_options - 1], start=1):
            options.append({"id": chr(ord("a") + option_index), "text": distractor})
        options, correct_option_id = _rotate_options(question_id, options, "a")
        mcq_questions.append(
            {
                "id": question_id,
                "dimension": question.get("dimension"),
                "question": question.get("question"),
                "required": bool(question.get("required", True)),
                "selection": "single",
                "options": options,
            }
        )
        answer_key[question_id] = {
            "correct_option_ids": [correct_option_id],
            "explanation": correct_text,
        }
    return mcq_questions, answer_key


def _fallback_expected_evidence(diff: dict[str, Any], risk: dict[str, Any], tests: dict[str, Any]) -> list[str]:
    changed = [item.get("path", "") for item in diff.get("changed_files", [])[:3]]
    evidence = [f"changed files: {', '.join(changed)}" if changed else "changed files"]
    if risk.get("domains"):
        evidence.append(f"risk domains: {', '.join(risk.get('domains', []))}")
    evidence.append(f"test gap: {bool(tests.get('test_gap'))}")
    return evidence


def _distractors_for_question(
    question: dict[str, Any],
    risk: dict[str, Any],
    tests: dict[str, Any],
    defaults: list[str],
) -> list[str]:
    distractors = list(defaults)
    risk_level = str(risk.get("risk_level", "low"))
    if risk_level in {"high", "critical"}:
        distractors.insert(0, "No rollback or failure-mode explanation is needed for this risk level.")
    if tests.get("test_gap"):
        distractors.insert(0, "The missing-test signal can be ignored because ownership questions are enough.")
    if str(question.get("dimension", "")).startswith("domain:"):
        distractors.insert(0, "The domain-specific risk does not need to be mentioned in the owner answer.")
    return _dedupe(distractors)


def _dedupe(items: list[str]) -> list[str]:
    result = []
    seen = set()
    for item in items:
        if item not in seen:
            result.append(item)
            seen.add(item)
    return result


def _rotate_options(question_id: str, options: list[dict[str, str]], original_correct_id: str) -> tuple[list[dict[str, str]], str]:
    if not options:
        return options, original_correct_id
    rotation = sum(ord(char) for char in question_id) % len(options)
    rotated = options[rotation:] + options[:rotation]
    remapped = []
    correct_option_id = "a"
    for index, option in enumerate(rotated):
        new_id = chr(ord("a") + index)
        if option["id"] == original_correct_id:
            correct_option_id = new_id
        remapped.append({"id": new_id, "text": option["text"]})
    return remapped, correct_option_id


def _normalize_selected(raw: Any) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, str):
        return {raw.strip().lower()} if raw.strip() else set()
    if isinstance(raw, list):
        return {str(item).strip().lower() for item in raw if str(item).strip()}
    return {str(raw).strip().lower()} if str(raw).strip() else set()


def _initial_gate(risk: dict[str, Any], questions: list[dict[str, Any]], mcq_config: dict[str, Any]) -> dict[str, Any]:
    if risk.get("gate_mode") == "report_only" or not questions:
        status = mcq_config.get("gate", {}).get("report_only_status", "report_only")
        allowed = True
        recommendation = "Report-only result. Agent may proceed if normal tests and review requirements pass."
    else:
        status = mcq_config.get("gate", {}).get("pending_status", "pending_answers")
        allowed = False
        recommendation = "Ownership MCQ answers are required before the agent may push/open the merge request."
    return {
        "schema_version": f"{SCHEMA_VERSION}.gate",
        "created_at": utc_now(),
        "status": status,
        "score_percent": 0 if questions else 100,
        "required_score_percent": int(mcq_config.get("required_score_percent", 100)),
        "correct": 0,
        "total": len(questions),
        "merge_allowed": allowed,
        "push_allowed": allowed,
        "agent_may_push_merge_request": allowed,
        "recommendation": recommendation,
        "results": [],
    }
