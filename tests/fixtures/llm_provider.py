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
    domain_names = [str(item) for item in domains] if isinstance(domains, list) else []
    auth_related = "auth" in domain_names
    domain_text = ", ".join(str(item) for item in domains) if isinstance(domains, list) and domains else "this risk area"
    if dimension == "intent":
        question = f"For {focus}, what behavior changed and why?"
        if auth_related:
            hint = f"Compare how {focus} handles missing, expired, invalid, or reused session input before trusting a caller."
            answer = (
                f"Explain how {focus} treats missing, expired, invalid, or reused session input and why that "
                "fail-closed behavior matters."
            )
            distractors = [
                f"Say {focus} allows missing session input so the caller can continue through the auth path.",
                f"Say {focus} changes session error wording but keeps the same allow/deny behavior.",
                f"Say {focus} denies valid session input and leaves invalid or reused tokens untouched.",
            ]
            evidence = [f"session behavior in {focus}", "missing, expired, invalid, or reused input"]
        else:
            hint = f"Compare the before/after behavior in {focus} and look for the reason the changed branch matters."
            answer = f"Explain the before and after behavior in {focus} and why the change is useful."
            distractors = [
                f"Say {focus} keeps the same behavior and only moves where the existing branch is checked.",
                f"Say {focus} changes the caller-facing result but not the condition that decides it.",
                f"Say {focus} adds a new path but leave out why that path changes ownership risk.",
            ]
            evidence = [f"behavior in {focus}", "reason for the change"]
    elif dimension == "runtime_behavior":
        question = f"When does the changed path in {focus} run?"
        hint = f"Trace the trigger or caller that reaches {focus}, then compare which decision or output changes."
        answer = f"Name the trigger or call path for {focus} and the decision or output that changed."
        distractors = [
            f"Describe {focus} as startup-only even though the owner must trace the changed runtime branch.",
            f"Say {focus} runs after the result is already returned, so the changed decision cannot affect callers.",
            f"Treat {focus} as a build-time path and ignore the changed runtime input or output.",
        ]
        evidence = [f"trigger for {focus}", "changed decision or output"]
    elif dimension == "failure_modes":
        question = f"What easy failure would show {focus} is wrong?"
        hint = f"Look for the wrong assumption in {focus} and the symptom a caller would notice first."
        answer = f"Name one wrong assumption in {focus} and the symptom a caller or user would see."
        distractors = [
            f"Watch only for a harmless log difference in {focus} and ignore caller-visible behavior.",
            f"Say a failure in {focus} would be invisible even if its changed decision reaches callers.",
            f"Check only the successful branch in {focus} and leave the rejected or missing-input branch unowned.",
        ]
        evidence = [f"assumption in {focus}", "visible symptom"]
    elif dimension == "tests":
        question = f"What check proves the changed behavior in {focus}?"
        hint = f"Match each choice to the branch in {focus} that a test or manual check would actually exercise."
        answer = f"Name the automated test, manual check, or missing test for {focus} and what it proves."
        distractors = [
            f"Check only that {focus} can be imported and do not exercise the changed branch.",
            f"Verify an unrelated caller while leaving the changed behavior in {focus} untested.",
            f"Test the old expected result for {focus} without asserting the new decision or output.",
        ]
        evidence = [f"check for {focus}", "behavior proved"]
    elif dimension == "blast_radius":
        question = f"Who or what could notice the change in {focus}?"
        hint = f"Follow the value leaving {focus} to callers, jobs, users, services, or data that consume it."
        answer = f"Name the users, callers, jobs, services, or data affected by {focus}."
        distractors = [
            f"Limit blast radius for {focus} to the edited file and ignore callers that read its result.",
            f"Assume {focus} affects only local development even though changed behavior can reach consumers.",
            f"Name the package owning {focus} but omit the caller, service, or data path that sees the result.",
        ]
        evidence = [f"affected path {focus}", "affected users or callers"]
    elif dimension == "rollback":
        question = f"What is the safest rollback if {focus} causes trouble?"
        hint = f"Compare rollback choices for {focus} by whether they disable the changed behavior safely."
        answer = f"Name the revert, flag, config, data compatibility step, or mitigation for {focus}."
        distractors = [
            f"Rollback {focus} by deleting the changed branch without considering callers that already depend on it.",
            f"Mitigate {focus} only by changing logs while leaving the risky behavior active.",
            f"Revert a neighboring file and leave the changed decision in {focus} deployed.",
        ]
        evidence = [f"rollback for {focus}", "compatibility concern"]
    elif dimension == "domain:auth":
        question = f"In {focus}, what happens when a token or session is missing, expired, invalid, or reused?"
        hint = f"Check the fail-closed path in {focus} and who receives the denial for bad session input."
        answer = f"Explain the fail-closed behavior in {focus} and who receives the denial."
        distractors = [
            f"Allow a missing token in {focus} so the caller can continue without an authenticated session.",
            f"Refresh a reused token in {focus} and return success without denying the caller.",
            f"Treat an invalid session in {focus} as anonymous success instead of a fail-closed denial.",
        ]
        evidence = [f"fail-closed behavior in {focus}", "affected caller"]
    elif dimension.startswith("domain:"):
        question = f"For {focus}, what {domain_text} behavior must the owner understand?"
        hint = f"Focus on the {domain_text} condition in {focus} and the failure case each option accepts or rejects."
        answer = f"Explain the {domain_text} behavior in {focus} and the easy failure case to check."
        distractors = [
            f"Describe {focus} without naming the {domain_text} condition that changes the result.",
            f"Treat the {domain_text} path in {focus} as unaffected and skip its failure case.",
            f"Name the {domain_text} area in {focus} but omit the caller-visible behavior to verify.",
        ]
        evidence = [f"{domain_text} behavior in {focus}", "failure case"]
    else:
        question = f"What should the owner understand about {focus}?"
        hint = f"Anchor the answer in {focus} and compare the changed input, branch, or result."
        answer = f"Explain the changed behavior in {focus} using only this diff."
        distractors = [
            f"Describe {focus} without naming the changed input, branch, or result.",
            f"Say {focus} has no caller-visible effect while leaving the changed behavior unexplained.",
            f"Focus on a neighboring path and omit what changed inside {focus}.",
        ]
        evidence = [f"changed behavior in {focus}"]
    correct_option_id = "abcd"[index % 4]
    option_texts = iter(distractors)
    options = [
        {"id": option_id, "text": answer if option_id == correct_option_id else next(option_texts)}
        for option_id in "abcd"
    ]
    return {
        "dimension": dimension,
        "difficulty": "easy",
        "question": question,
        "hint": hint,
        "options": options,
        "correct_option_id": correct_option_id,
        "expected_evidence": evidence,
        "rationale": "Test provider output grounded in OwnDiff facts.",
    }


if __name__ == "__main__":
    raise SystemExit(main())
