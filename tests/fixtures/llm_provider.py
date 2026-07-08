#!/usr/bin/env python3
from __future__ import annotations

import json
import sys


def main() -> int:
    prompt = sys.stdin.read()
    facts = json.loads(prompt.split("JSON facts:\n", 1)[1])
    focus = _focus(facts)
    questions = []
    for index, item in enumerate(facts["question_plan"]):
        dimension = str(item["dimension"])
        questions.append(_question(dimension, focus, facts, index))
    json.dump({"questions": questions}, sys.stdout)
    return 0


def _focus(facts: dict[str, object]) -> str:
    changed = facts.get("changed_files", [])
    if isinstance(changed, list):
        for item in changed:
            if isinstance(item, dict) and item.get("path") and not item.get("is_test"):
                return str(item["path"])
        for item in changed:
            if isinstance(item, dict) and item.get("path"):
                return str(item["path"])
    return "the changed code"


def _question(dimension: str, focus: str, facts: dict[str, object], index: int) -> dict[str, object]:
    domains = facts.get("risk_domains", [])
    domain_text = ", ".join(str(item) for item in domains) if isinstance(domains, list) and domains else "this risk area"
    if dimension == "intent":
        question = f"For {focus}, what behavior changed and why?"
        answer = f"Explain the before and after behavior in {focus} and why the change is useful."
        evidence = [f"behavior in {focus}", "reason for the change"]
    elif dimension == "runtime_behavior":
        question = f"When does the changed path in {focus} run?"
        answer = f"Name the trigger or call path for {focus} and the decision or output that changed."
        evidence = [f"trigger for {focus}", "changed decision or output"]
    elif dimension == "failure_modes":
        question = f"What easy failure would show {focus} is wrong?"
        answer = f"Name one wrong assumption in {focus} and the symptom a caller or user would see."
        evidence = [f"assumption in {focus}", "visible symptom"]
    elif dimension == "tests":
        question = f"What check proves the changed behavior in {focus}?"
        answer = f"Name the automated test, manual check, or missing test for {focus} and what it proves."
        evidence = [f"check for {focus}", "behavior proved"]
    elif dimension == "blast_radius":
        question = f"Who or what could notice the change in {focus}?"
        answer = f"Name the users, callers, jobs, services, or data affected by {focus}."
        evidence = [f"affected path {focus}", "affected users or callers"]
    elif dimension == "rollback":
        question = f"What is the safest rollback if {focus} causes trouble?"
        answer = f"Name the revert, flag, config, data compatibility step, or mitigation for {focus}."
        evidence = [f"rollback for {focus}", "compatibility concern"]
    elif dimension == "domain:auth":
        question = f"In {focus}, what happens when a token or session is missing, expired, invalid, or reused?"
        answer = f"Explain the fail-closed behavior in {focus} and who receives the denial."
        evidence = [f"fail-closed behavior in {focus}", "affected caller"]
    elif dimension.startswith("domain:"):
        question = f"For {focus}, what {domain_text} behavior must the owner understand?"
        answer = f"Explain the {domain_text} behavior in {focus} and the easy failure case to check."
        evidence = [f"{domain_text} behavior in {focus}", "failure case"]
    else:
        question = f"What should the owner understand about {focus}?"
        answer = f"Explain the changed behavior in {focus} using only this diff."
        evidence = [f"changed behavior in {focus}"]
    correct_option_id = "abcd"[index % 4]
    distractors = [
        f"For {dimension}, infer safety from the {focus} filename without tracing changed behavior.",
        f"For {dimension}, approve {focus} because an agent produced the change.",
        f"For {dimension}, discuss formatting in {focus} instead of its runtime effect.",
    ]
    option_texts = iter(distractors)
    options = [
        {"id": option_id, "text": answer if option_id == correct_option_id else next(option_texts)}
        for option_id in "abcd"
    ]
    return {
        "dimension": dimension,
        "difficulty": "easy",
        "question": question,
        "options": options,
        "correct_option_id": correct_option_id,
        "expected_evidence": evidence,
        "rationale": "Test provider output grounded in OwnDiff facts.",
    }


if __name__ == "__main__":
    raise SystemExit(main())
