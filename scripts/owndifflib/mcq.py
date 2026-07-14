from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import SCHEMA_VERSION, OwnDiffError, read_json, utc_now, write_json
from .config import load_config
from .diff_collect import has_source_changes


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
    read_json(Path(tests_path))
    questions = read_json(Path(questions_path))
    root = Path(repo or diff.get("repo") or ".").resolve()
    config, warnings, config_sources = load_config(root, config_path)
    mcq_config = config.get("mcq", {})

    if not has_source_changes(diff):
        for path in (mcq_out, answer_key_out, answers_template_out, gate_out):
            Path(path).unlink(missing_ok=True)
        return {
            "generated": False,
            "mcq": None,
            "answer_key": None,
            "answers_template": None,
            "gate": _no_source_decision(),
        }

    mcq_questions, answer_key = _build_mcq_questions(risk, questions, mcq_config)
    mcq_payload: dict[str, Any] = {
        "schema_version": f"{SCHEMA_VERSION}.mcq",
        "created_at": utc_now(),
        "repo": str(root),
        "config_sources": config_sources,
        "warnings": warnings,
        "risk_level": risk.get("risk_level", "low"),
        "instructions": (
            "Use owndiff run as the normal flow. It opens localhost browser review in the user's default browser when questions are pending. "
            "Do not print MCQs in chat or route the human to a separate MCQ command."
        ),
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
    gate = _initial_gate(risk, mcq_questions, mcq_config, questions)

    write_json(Path(mcq_out), mcq_payload)
    write_json(Path(answer_key_out), key_payload)
    write_json(Path(answers_template_out), answers_template)
    write_json(Path(gate_out), gate)

    return {
        "generated": True,
        "mcq": mcq_payload,
        "answer_key": key_payload,
        "answers_template": answers_template,
        "gate": gate,
    }


def _no_source_decision() -> dict[str, Any]:
    return {
        "status": "not_required_no_source_changes",
        "agent_may_push_merge_request": True,
        "recommendation": "No configured source-code extensions changed. No ownership MCQ or gate artifact was generated.",
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
    attempts, attempt_history = _attempts(Path(out_path), total, correct_count, score_percent, passed)
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
        "attempts": attempts,
        "attempts_to_pass": attempts if passed and total else None,
        "attempt_summary": _attempt_summary(attempts, correct_count, total, passed),
        "attempt_history": attempt_history,
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
    risk: dict[str, Any],
    questions: dict[str, Any],
    mcq_config: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    source_questions = questions.get("questions", [])
    risk_level = str(risk.get("risk_level", "low"))
    enabled_levels = {str(level) for level in mcq_config.get("enabled_risk_levels", ["medium", "high", "critical"])}
    if not mcq_config.get("enabled", True) or risk_level not in enabled_levels or not source_questions:
        return [], {}

    mcq_questions = []
    answer_key: dict[str, dict[str, Any]] = {}
    for index, question in enumerate(source_questions, start=1):
        question_id = str(question.get("id") or f"q{index}")
        raw_options = question.get("options")
        if not isinstance(raw_options, list) or len(raw_options) != 4:
            raise OwnDiffError(f"Question {question_id} is missing four LLM-generated answer choices")
        options = [
            {"id": str(option.get("id", "")).lower(), "text": str(option.get("text", "")).strip()}
            for option in raw_options
            if isinstance(option, dict)
        ]
        option_texts = [option["text"].casefold() for option in options]
        if (
            len(options) != 4
            or {option["id"] for option in options} != {"a", "b", "c", "d"}
            or any(not text for text in option_texts)
            or len(set(option_texts)) != 4
        ):
            raise OwnDiffError(f"Question {question_id} has invalid LLM-generated answer choices")
        original_correct_id = str(question.get("correct_option_id", "")).lower()
        if original_correct_id not in {"a", "b", "c", "d"}:
            raise OwnDiffError(f"Question {question_id} has no valid LLM-generated correct choice")
        correct_option_id = original_correct_id
        correct_text = next(option["text"] for option in options if option["id"] == correct_option_id)
        hint = str(question.get("hint", "")).strip()
        if not hint:
            raise OwnDiffError(f"Question {question_id} is missing an LLM-generated hint")
        mcq_questions.append(
            {
                "id": question_id,
                "dimension": question.get("dimension"),
                "question": question.get("question"),
                "hint": hint,
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


def _normalize_selected(raw: Any) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, str):
        return {raw.strip().lower()} if raw.strip() else set()
    if isinstance(raw, list):
        return {str(item).strip().lower() for item in raw if str(item).strip()}
    return {str(raw).strip().lower()} if str(raw).strip() else set()


def _initial_gate(
    risk: dict[str, Any],
    questions: list[dict[str, Any]],
    mcq_config: dict[str, Any],
    questions_payload: dict[str, Any],
) -> dict[str, Any]:
    generation = questions_payload.get("generation", {}) if isinstance(questions_payload.get("generation", {}), dict) else {}
    if generation.get("awaiting_llm_response"):
        status = mcq_config.get("gate", {}).get("question_generation_status", "question_generation_required")
        allowed = False
        recommendation = (
            "Agent LLM question generation is required before ownership MCQs can be answered. "
            "Do not push/open the merge request until the agent writes and validates the LLM response."
        )
    elif risk.get("gate_mode") == "report_only" or not questions:
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
        "attempts": 0,
        "attempts_to_pass": None,
        "attempt_summary": "No MCQ attempts have been submitted yet." if questions else "No MCQ attempts required.",
        "attempt_history": [],
        "merge_allowed": allowed,
        "push_allowed": allowed,
        "agent_may_push_merge_request": allowed,
        "recommendation": recommendation,
        "results": [],
    }


def _attempts(out_path: Path, total: int, correct_count: int, score_percent: int, passed: bool) -> tuple[int, list[dict[str, Any]]]:
    if total == 0:
        return 0, []

    previous_history: list[dict[str, Any]] = []
    attempts = 0
    if out_path.exists():
        try:
            previous = read_json(out_path)
        except OwnDiffError:
            previous = {}
        attempts = int(previous.get("attempts", 0) or 0)
        raw_history = previous.get("attempt_history", [])
        if isinstance(raw_history, list):
            previous_history = [item for item in raw_history if isinstance(item, dict)]

    attempt_number = attempts + 1
    history = [
        *previous_history,
        {
            "attempt": attempt_number,
            "created_at": utc_now(),
            "correct": correct_count,
            "total": total,
            "score_percent": score_percent,
            "passed": passed,
        },
    ]
    return attempt_number, history


def _attempt_summary(attempts: int, correct_count: int, total: int, passed: bool) -> str:
    if total == 0:
        return "No MCQ attempts required."
    suffix = "attempt" if attempts == 1 else "attempts"
    if passed:
        return f"Passed after {attempts} {suffix}."
    return f"Attempt {attempts} failed: {correct_count}/{total} correct."
