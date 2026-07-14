#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OPENCLAW_REF = "9c86529e446197a0fee1850963135aa508bd2891"
DEFAULT_OPENCLAW_SOURCE = "https://github.com/openclaw/openclaw.git"
OPENCLAW_SPARSE_PATHS = ["packages/web-content-core/src"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the OwnDiff OpenClaw release validation flow.")
    parser.add_argument("--source", default=DEFAULT_OPENCLAW_SOURCE, help="OpenClaw git URL or local checkout path.")
    parser.add_argument("--ref", default=DEFAULT_OPENCLAW_REF, help="OpenClaw commit/ref to validate against.")
    parser.add_argument("--work-dir", help="Optional working directory. Defaults to a temporary directory.")
    parser.add_argument("--keep-work-dir", action="store_true", help="Do not delete the temporary working directory.")
    parser.add_argument("--question-count", type=int, default=5, help="Expected question count. Default: 5.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    if args.work_dir:
        work_dir = Path(args.work_dir).resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
    else:
        temp_dir = tempfile.TemporaryDirectory(prefix="owndiff-openclaw-flow.")
        work_dir = Path(temp_dir.name)

    try:
        repo = work_dir / "openclaw"
        clone_openclaw(args.source, args.ref, repo)
        apply_demo_diff(repo)
        initial = run_owndiff(repo, review_mode="none", question_count=args.question_count)
        if not initial.get("awaiting_llm_response"):
            raise RuntimeError("expected OwnDiff to request an agent LLM response")
        response_path = repo / ".owndiff" / "question-response.json"
        write_openclaw_response(response_path)
        pending = run_owndiff(
            repo,
            review_mode="none",
            question_count=args.question_count,
            llm_response=response_path,
        )
        if pending.get("questions") != args.question_count:
            raise RuntimeError(f"expected {args.question_count} questions, got {pending.get('questions')}")
        if pending.get("gate_status") != "pending_answers":
            raise RuntimeError(f"expected pending_answers gate, got {pending.get('gate_status')}")

        passed = run_browser_review(repo, response_path, question_count=args.question_count)
        summary = {
            "openclaw_ref": args.ref,
            "files_changed": passed.get("files_changed"),
            "source_files_changed": passed.get("source_files_changed"),
            "risk_level": passed.get("risk_level"),
            "risk_score": passed.get("risk_score"),
            "questions": passed.get("questions"),
            "gate_status": passed.get("gate_status"),
            "agent_may_push_merge_request": passed.get("agent_may_push_merge_request"),
        }
        print(json.dumps(summary, sort_keys=True))
        return 0
    finally:
        if temp_dir is not None and not args.keep_work_dir:
            temp_dir.cleanup()


def clone_openclaw(source: str, ref: str, repo: Path) -> None:
    if repo.exists():
        shutil.rmtree(repo)
    source_path = Path(source).expanduser()
    if source_path.exists():
        copy_local_checkout(source_path.resolve(), ref, repo)
        return
    repo.mkdir(parents=True)
    run(["git", "init", "-q"], repo)
    run(["git", "remote", "add", "origin", source], repo)
    run(["git", "sparse-checkout", "init", "--cone"], repo)
    run(["git", "sparse-checkout", "set", *OPENCLAW_SPARSE_PATHS], repo)
    run(["git", "fetch", "--depth", "1", "--filter=blob:none", "origin", ref], repo)
    run(["git", "checkout", "--detach", "-q", "FETCH_HEAD"], repo)


def copy_local_checkout(source: Path, ref: str, repo: Path) -> None:
    head = run(["git", "rev-parse", "HEAD"], source, capture=True).stdout.strip()
    if head != ref:
        raise RuntimeError(f"local OpenClaw checkout is at {head}, expected {ref}")
    status = run(["git", "status", "--short", "--", *OPENCLAW_SPARSE_PATHS], source, capture=True).stdout.strip()
    if status:
        raise RuntimeError("local OpenClaw checkout must be clean for release validation")

    repo.mkdir(parents=True)
    files = run(["git", "ls-files", "-z", "--", *OPENCLAW_SPARSE_PATHS], source, capture=True).stdout.split("\0")
    for relative in [file for file in files if file]:
        source_file = source / relative
        target_file = repo / relative
        target_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target_file, follow_symlinks=False)

    run(["git", "init", "-q"], repo)
    run(["git", "add", "-A"], repo)
    run(
        [
            "git",
            "-c",
            "user.email=owndiff-ci@example.invalid",
            "-c",
            "user.name=OwnDiff CI",
            "commit",
            "-q",
            "-m",
            "OpenClaw baseline",
        ],
        repo,
    )


def apply_demo_diff(repo: Path) -> None:
    provider = repo / "packages" / "web-content-core" / "src" / "provider-runtime-shared.ts"
    if not provider.exists():
        raise RuntimeError("OpenClaw provider-runtime-shared.ts was not found")
    text = provider.read_text(encoding="utf-8")
    string_normalizer = "normalize" + "SecretInputString"
    input_normalizer = "normalize" + "SecretInput"
    helper = (
        "export function hasUsableSessionToken(value: unknown): boolean {\n"
        f"  return {string_normalizer}(value) !== undefined;\n"
        "}\n\n"
    )
    if "export function hasUsableSessionToken" not in text:
        marker = f"function {input_normalizer}(value: unknown): string {{\n"
        if marker not in text:
            raise RuntimeError("OpenClaw provider-runtime-shared.ts insertion marker was not found")
        text = text.replace(marker, helper + marker, 1)
        provider.write_text(text, encoding="utf-8")

    auth_dir = repo / "packages" / "web-content-core" / "src" / "auth"
    auth_dir.mkdir(parents=True, exist_ok=True)
    (auth_dir / "session-token-guard.ts").write_text(
        'import { hasUsableSessionToken } from "../provider-runtime-shared.js";\n'
        "\n"
        "export type SessionTokenGuardResult =\n"
        '  | { allowed: true; reason: "valid" }\n'
        '  | { allowed: false; reason: "missing" | "reused" };\n'
        "\n"
        "export function checkSessionToken(params: {\n"
        "  token: unknown;\n"
        "  previouslyUsed: boolean;\n"
        "}): SessionTokenGuardResult {\n"
        "  if (!hasUsableSessionToken(params.token)) {\n"
        '    return { allowed: false, reason: "missing" };\n'
        "  }\n"
        "  if (params.previouslyUsed) {\n"
        '    return { allowed: false, reason: "reused" };\n'
        "  }\n"
        '  return { allowed: true, reason: "valid" };\n'
        "}\n",
        encoding="utf-8",
    )


def write_openclaw_response(path: Path) -> None:
    response = {
        "questions": [
            {
                "dimension": "intent",
                "difficulty": "easy",
                "question": "What ownership-level intent is introduced by session-token-guard.ts?",
                "hint": "Compare the new allowed/reason union in session-token-guard.ts with the hasUsableSessionToken helper it imports.",
                "options": [
                    {"id": "a", "text": "session-token-guard.ts stores session token strings for later reuse in provider-runtime-shared.ts."},
                    {"id": "b", "text": "session-token-guard.ts centralizes an auth allow/deny decision so missing or reused session tokens are rejected and a usable unused token is valid."},
                    {"id": "c", "text": "provider-runtime-shared.ts removes input normalization before auth/session token checks."},
                    {"id": "d", "text": "session-token-guard.ts changes package structure without adding auth/session behavior."},
                ],
                "correct_option_id": "b",
                "expected_evidence": [
                    "session-token-guard.ts returns missing or reused for blocked tokens",
                    "usable unused token returns allowed true with reason valid",
                ],
                "rationale": "The owner should know the purpose of the new auth guard.",
            },
            {
                "dimension": "runtime_behavior",
                "difficulty": "easy",
                "question": "In session-token-guard.ts, what does checkSessionToken return for missing, reused, and usable unused tokens?",
                "hint": "Trace the order in session-token-guard.ts: usable-token check first, previouslyUsed check second, valid result last.",
                "options": [
                    {"id": "a", "text": "session-token-guard.ts returns allowed true for a missing token and reason valid for reused tokens."},
                    {"id": "b", "text": "provider-runtime-shared.ts throws for non-string token input, so session-token-guard.ts never returns allowed false."},
                    {"id": "c", "text": "checkSessionToken in session-token-guard.ts returns reason missing only after the previouslyUsed branch is checked."},
                    {"id": "d", "text": "checkSessionToken in session-token-guard.ts returns missing for unusable input, reused for previouslyUsed tokens, and valid only for a usable unused token."},
                ],
                "correct_option_id": "d",
                "expected_evidence": [
                    "hasUsableSessionToken false returns reason missing",
                    "previouslyUsed true returns reason reused before valid",
                ],
                "rationale": "The owner should be able to trace the runtime branches.",
            },
            {
                "dimension": "failure_modes",
                "difficulty": "easy",
                "question": "What failure matters if hasUsableSessionToken in provider-runtime-shared.ts treats an unexpected value as usable?",
                "hint": "Compare how provider-runtime-shared.ts classifies token input before session-token-guard.ts chooses allowed or missing.",
                "options": [
                    {"id": "a", "text": "session-token-guard.ts could return allowed true with reason valid for token input that should have been rejected as missing."},
                    {"id": "b", "text": "session-token-guard.ts would always return reason reused for empty strings before checking provider-runtime-shared.ts."},
                    {"id": "c", "text": "provider-runtime-shared.ts would only affect the test_gap signal while auth/session allow and deny results stay unchanged."},
                    {"id": "d", "text": "session-token-guard.ts would reject every usable non-empty token as missing after hasUsableSessionToken succeeds."},
                ],
                "correct_option_id": "a",
                "expected_evidence": [
                    "hasUsableSessionToken wraps the existing provider input-normalization helper",
                    "checkSessionToken allows valid only after the usable-token check succeeds",
                ],
                "rationale": "The owner should recognize the security-sensitive failure path.",
            },
            {
                "dimension": "tests",
                "difficulty": "easy",
                "question": "Which missing nearby test would best cover session-token-guard.ts behavior?",
                "hint": "Use OwnDiff's missing_test_candidates for session-token-guard.ts and choose coverage that exercises each returned reason.",
                "options": [
                    {"id": "a", "text": "A provider-runtime-shared.ts snapshot that only confirms hasUsableSessionToken is exported."},
                    {"id": "b", "text": "packages/web-content-core/src/auth/session-token-guard.spec.ts that only checks the TypeScript SessionTokenGuardResult type exists."},
                    {"id": "c", "text": "packages/web-content-core/src/auth/session-token-guard.test.ts with cases for missing token, previouslyUsed true, and usable unused token."},
                    {"id": "d", "text": "packages/web-content-core/src/auth/__tests__/session-token-guard.test.ts that only imports checkSessionToken without token scenarios."},
                ],
                "correct_option_id": "c",
                "expected_evidence": [
                    "missing_test_candidates list session-token-guard.test.ts",
                    "the guard has missing, reused, and valid branches",
                ],
                "rationale": "The owner should identify practical test coverage for the new branches.",
            },
            {
                "dimension": "blast_radius",
                "difficulty": "easy",
                "question": "What blast radius should a reviewer call out for this auth/session diff?",
                "hint": "Check whether session-token-guard.ts affects callers only when they choose to call checkSessionToken, and name the rollback surface.",
                "options": [
                    {"id": "a", "text": "Only documentation under packages/web-content-core is affected because no TypeScript auth/session behavior changed."},
                    {"id": "b", "text": "Any code path that imports checkSessionToken gets a new auth gate for missing or reused tokens; rollback is reverting the new guard/export or its caller use."},
                    {"id": "c", "text": "provider-runtime-shared.ts changes every existing caller of the input normalizer by replacing its return type."},
                    {"id": "d", "text": "session-token-guard.ts automatically applies to all packages without any caller importing checkSessionToken."},
                ],
                "correct_option_id": "b",
                "expected_evidence": [
                    "session-token-guard.ts exports checkSessionToken",
                    "provider-runtime-shared.ts only adds hasUsableSessionToken",
                ],
                "rationale": "The owner should describe who is affected and how to roll back.",
            },
        ]
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(response, indent=2) + "\n", encoding="utf-8")


def run_owndiff(
    repo: Path,
    *,
    review_mode: str | None = None,
    question_count: int,
    llm_response: Path | None = None,
    no_open_browser: bool = False,
) -> dict[str, object]:
    command = [
        sys.executable,
        str(ROOT / "scripts" / "run_owndiff.py"),
        "--repo",
        str(repo),
        "--out-dir",
        ".owndiff",
        "--question-count",
        str(question_count),
    ]
    if review_mode:
        command.extend(["--review-mode", review_mode])
    if llm_response:
        command.extend(["--llm-response", str(llm_response)])
    if no_open_browser:
        command.extend(["--no-open-browser", "--web-timeout-seconds", "120"])
    proc = run(command, ROOT, capture=True)
    return json.loads(proc.stdout)


def run_browser_review(repo: Path, response_path: Path, question_count: int) -> dict[str, object]:
    command = [
        sys.executable,
        str(ROOT / "scripts" / "run_owndiff.py"),
        "--repo",
        str(repo),
        "--out-dir",
        ".owndiff",
        "--question-count",
        str(question_count),
        "--llm-response",
        str(response_path),
        "--no-open-browser",
        "--web-timeout-seconds",
        "120",
    ]
    proc = subprocess.Popen(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None
    assert proc.stderr is not None
    url = wait_for_review_url(proc)
    submit_correct_answers(repo, url)
    stdout, stderr = proc.communicate(timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"OwnDiff browser review failed: {stderr}\n{stdout}")
    lines = [line for line in stdout.splitlines() if line.strip().startswith("{")]
    if not lines:
        raise RuntimeError(f"OwnDiff browser review did not emit JSON: {stdout}")
    return json.loads(lines[-1])


def wait_for_review_url(proc: subprocess.Popen[str]) -> str:
    assert proc.stderr is not None
    deadline = time.monotonic() + 30
    stderr_lines: list[str] = []
    while time.monotonic() < deadline:
        line = proc.stderr.readline()
        if line:
            stderr_lines.append(line)
            if "OwnDiff browser review:" in line:
                return line.rsplit(" ", 1)[-1].strip()
        elif proc.poll() is not None:
            break
        else:
            time.sleep(0.05)
    proc.kill()
    raise RuntimeError("OwnDiff did not print browser review URL\n" + "".join(stderr_lines))


def submit_correct_answers(repo: Path, url: str) -> None:
    key_path = repo / ".owndiff" / "ownership-answer-key.json"
    answer_key = json.loads(key_path.read_text(encoding="utf-8"))["answers"]
    answers = {qid: detail["correct_option_ids"][0] for qid, detail in answer_key.items()}
    parsed = urllib.parse.urlparse(url)
    review_id = urllib.parse.parse_qs(parsed.query)["token"][0]
    with urllib.request.urlopen(url, timeout=10) as response:
        page = response.read().decode("utf-8")
        if response.status != 200 or "OwnDiff Browser Review" not in page:
            raise RuntimeError("OwnDiff review page did not render")
    form = urllib.parse.urlencode({"token": review_id, **answers}).encode("utf-8")
    submit_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/submit", "", "", ""))
    request = urllib.request.Request(submit_url, data=form, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        result_page = response.read().decode("utf-8")
        if response.status != 200 or "OwnDiff Gate Result" not in result_page:
            raise RuntimeError("OwnDiff result page did not render")


def run(command: list[str], cwd: Path, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=capture, timeout=180, check=True)


if __name__ == "__main__":
    raise SystemExit(main())
