from __future__ import annotations

import errno
import json
import os
import pty
import select
import subprocess
import sys
import time
from pathlib import Path

from owndifflib.llm_questions import build_llm_question_prompt, validate_llm_questions

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=True)


def run_no_check(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=False)


def run_in_pty(cmd: list[str], cwd: Path, keys: str, timeout: float = 12.0) -> tuple[int, str]:
    master, slave = pty.openpty()
    env = os.environ.copy()
    env.update({"TERM": "xterm-256color", "COLUMNS": "120", "LINES": "40"})
    proc = subprocess.Popen(cmd, cwd=cwd, stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True)
    os.close(slave)

    output = bytearray()
    deadline = time.monotonic() + timeout
    sent = False
    try:
        while time.monotonic() < deadline:
            if not sent:
                time.sleep(0.25)
                os.write(master, keys.encode("utf-8"))
                sent = True

            ready, _, _ = select.select([master], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                else:
                    if not chunk:
                        break
                    output.extend(chunk)

            if proc.poll() is not None:
                while True:
                    ready, _, _ = select.select([master], [], [], 0)
                    if not ready:
                        break
                    try:
                        chunk = os.read(master, 4096)
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                break
        else:
            proc.kill()
            raise AssertionError("PTY command timed out")
    finally:
        os.close(master)

    return proc.wait(), output.decode("utf-8", errors="replace")


def correct_answer_arrow_keys(mcq: dict[str, object], answer_key: dict[str, object]) -> str:
    down = "\x1b[B"
    keys = []
    key_answers = answer_key["answers"]
    assert isinstance(key_answers, dict)
    for question in mcq["questions"]:
        assert isinstance(question, dict)
        question_id = str(question["id"])
        answer_meta = key_answers[question_id]
        assert isinstance(answer_meta, dict)
        correct_option_id = str(answer_meta["correct_option_ids"][0])
        option_ids = [str(option["id"]) for option in question["options"] if isinstance(option, dict)]
        keys.append(down * option_ids.index(correct_option_id))
        keys.append("\n")
    return "".join(keys)


def init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run(["git", "init"], repo)
    run(["git", "config", "user.email", "test@example.com"], repo)
    run(["git", "config", "user.name", "OwnDiff Test"], repo)
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    run(["git", "add", "."], repo)
    run(["git", "commit", "-m", "init"], repo)
    return repo


def run_owndiff(repo: Path, *extra_args: str) -> dict[str, object]:
    env = os.environ.copy()
    env.pop("OWNDIFF_LLM_COMMAND", None)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "run_owndiff.py"), "--repo", str(repo), *extra_args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )
    payload = json.loads(proc.stdout)
    if not payload.get("awaiting_llm_response"):
        return payload

    prompt_path = Path(str(payload["llm_prompt"]))
    response_path = repo / ".owndiff" / "question-response.json"
    response = subprocess.run(
        [sys.executable, str(ROOT / "tests" / "fixtures" / "llm_provider.py")],
        input=prompt_path.read_text(encoding="utf-8"),
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )
    response_path.write_text(response.stdout, encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "run_owndiff.py"),
            "--repo",
            str(repo),
            *extra_args,
            "--llm-response",
            str(response_path),
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )
    return json.loads(completed.stdout)


def test_high_risk_auth_change_generates_questions(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    auth_dir = repo / "src" / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "session.py").write_text(
        "def refresh(token, store):\n"
        "    if not token:\n"
        "        return None\n"
        "    return store.rotate(token)\n",
        encoding="utf-8",
    )

    result = run_owndiff(repo)

    assert result["risk_level"] in {"high", "critical"}
    assert int(result["questions"]) >= 5
    report = (repo / ".owndiff" / "ownership-report.md").read_text(encoding="utf-8")
    mcq = json.loads((repo / ".owndiff" / "ownership-mcq.json").read_text(encoding="utf-8"))
    assert "authentication" in report.lower() or "session" in report.lower()
    assert "Ownership Questions" in report
    assert "src/auth/session.py" in json.dumps(mcq)
    assert "missing, expired, invalid, or reused" in json.dumps(mcq)
    gate = json.loads((repo / ".owndiff" / "ownership-gate.json").read_text(encoding="utf-8"))
    assert gate["status"] == "pending_answers"
    assert gate["agent_may_push_merge_request"] is False
    assert not (repo / ".owndiff" / "ownership-quiz.html").exists()


def test_llm_question_prompt_is_easy_no_web_and_grounded(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    auth_dir = repo / "src" / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "session.py").write_text(
        "def refresh(token, store):\n"
        "    if not token:\n"
        "        return None\n"
        "    return store.rotate(token)\n",
        encoding="utf-8",
    )

    run_owndiff(repo)
    diff = json.loads((repo / ".owndiff" / "diff.json").read_text(encoding="utf-8"))
    risk = json.loads((repo / ".owndiff" / "risk.json").read_text(encoding="utf-8"))
    tests = json.loads((repo / ".owndiff" / "tests.json").read_text(encoding="utf-8"))
    questions = json.loads((repo / ".owndiff" / "questions.json").read_text(encoding="utf-8"))

    prompt = build_llm_question_prompt(diff, risk, tests, questions["questions"])

    assert "Do not use web search" in prompt
    assert "Keep difficulty easy" in prompt
    assert "Do not invent files" in prompt
    assert "Return only valid JSON" in prompt
    assert "src/auth/session.py" in prompt
    assert "sanitized_patch_excerpt" in prompt


def test_llm_question_prompt_redacts_secret_like_patch_lines(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "settings.py").write_text("API_KEY = 'abcdefghijklmnopqrstuvwxyz'\n", encoding="utf-8")

    run_owndiff(repo)
    diff = json.loads((repo / ".owndiff" / "diff.json").read_text(encoding="utf-8"))
    risk = json.loads((repo / ".owndiff" / "risk.json").read_text(encoding="utf-8"))
    tests = json.loads((repo / ".owndiff" / "tests.json").read_text(encoding="utf-8"))
    questions = json.loads((repo / ".owndiff" / "questions.json").read_text(encoding="utf-8"))

    prompt = build_llm_question_prompt(diff, risk, tests, questions["questions"])

    assert "abcdefghijklmnopqrstuvwxyz" not in prompt
    assert "[owndiff:redacted-secret-like-line]" in prompt


def test_llm_validation_accepts_changed_file_basenames() -> None:
    diff = {
        "changed_files": [
            {
                "path": "packages/web-content-core/src/auth/session-token-guard.ts",
                "is_test": False,
            }
        ],
        "summary": {"files_changed": 1, "insertions": 1, "deletions": 0},
    }
    risk = {"risk_level": "high", "domains": ["auth"], "reasons": []}
    tests = {"test_gap": True, "changed_test_files": [], "missing_test_candidates": []}
    plan = [
        {
            "id": "q1",
            "dimension": "intent",
            "context": {"focus": "packages/web-content-core/src/auth/session-token-guard.ts"},
        }
    ]
    payload = {
        "questions": [
            {
                "dimension": "intent",
                "difficulty": "easy",
                "question": "For auth/session-token-guard.ts, what behavior changed?",
                "options": [
                    {"id": "a", "text": "Only mention formatting in session-token-guard.ts."},
                    {
                        "id": "b",
                        "text": "Explain the token guard behavior in session-token-guard.ts and the allowed/reason result.",
                    },
                    {"id": "c", "text": "Approve session-token-guard.ts because the diff is short."},
                    {"id": "d", "text": "Assume session-token-guard.ts is safe without tracing its result."},
                ],
                "correct_option_id": "b",
                "expected_evidence": ["auth/session-token-guard.ts behavior", "allowed/reason result"],
                "rationale": "Checks changed file ownership.",
            }
        ]
    }

    questions = validate_llm_questions(payload, diff, risk, tests, plan)

    assert questions[0]["question"] == "For auth/session-token-guard.ts, what behavior changed?"


def test_repo_config_cannot_enable_command_execution(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    marker = repo / "command-provider-ran"
    command = f"open({str(marker)!r}, 'w').write('ran')"
    (repo / ".owndiff.yml").write_text(
        "questions:\n"
        "  llm:\n"
        "    enabled: true\n"
        "    provider: command\n"
        f"    command: [{json.dumps(sys.executable)}, -c, {json.dumps(command)}]\n",
        encoding="utf-8",
    )
    auth_dir = repo / "src" / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "session.py").write_text(
        "def refresh(token, store):\n"
        "    if not token:\n"
        "        return None\n"
        "    return store.rotate(token)\n",
        encoding="utf-8",
    )
    run(["git", "add", "."], repo)
    run(["git", "commit", "-m", "add auth and owndiff config"], repo)

    (auth_dir / "session.py").write_text(
        "def refresh(token, store):\n"
        "    if not token:\n"
        "        return None\n"
        "    if store.is_reused(token):\n"
        "        return None\n"
        "    return store.rotate(token)\n",
        encoding="utf-8",
    )

    result = run_no_check([sys.executable, str(SCRIPTS / "run_owndiff.py"), "--repo", str(repo)], repo)

    assert result.returncode == 2
    assert "only accepts 'agent'" in result.stderr
    assert not marker.exists()


def test_llm_questions_reject_hallucinated_output_without_template_fallback(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    auth_dir = repo / "src" / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "session.py").write_text(
        "def refresh(token, store):\n"
        "    if not token:\n"
        "        return None\n"
        "    return store.rotate(token)\n",
        encoding="utf-8",
    )
    prepared = run([sys.executable, str(SCRIPTS / "run_owndiff.py"), "--repo", str(repo)], repo)
    assert json.loads(prepared.stdout)["awaiting_llm_response"] is True
    response_path = repo / ".owndiff" / "question-response.json"
    response_path.write_text(
        json.dumps(
            {
                "questions": [
                    {
                        "dimension": "intent",
                        "difficulty": "easy",
                        "question": "After web search, what changed in src/unrelated.py?",
                        "options": [
                            {"id": "a", "text": "Use internet results about src/unrelated.py."},
                            {"id": "b", "text": "Guess from src/unrelated.py."},
                            {"id": "c", "text": "Browse for src/unrelated.py."},
                            {"id": "d", "text": "Ignore src/unrelated.py."},
                        ],
                        "correct_option_id": "a",
                        "expected_evidence": ["web search"],
                        "rationale": "bad",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    result = run_no_check(
        [
            sys.executable,
            str(SCRIPTS / "run_owndiff.py"),
            "--repo",
            str(repo),
            "--llm-response",
            str(response_path),
        ],
        repo,
    )

    assert result.returncode == 2
    assert "Agent LLM question response rejected" in result.stderr
    gate = json.loads((repo / ".owndiff" / "ownership-gate.json").read_text(encoding="utf-8"))
    assert gate["status"] == "question_generation_required"
    assert gate["agent_may_push_merge_request"] is False


def test_agent_llm_default_writes_prompt_and_blocks_until_response(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    auth_dir = repo / "src" / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "session.py").write_text("def refresh(token):\n    return token\n", encoding="utf-8")

    env = os.environ.copy()
    env.pop("OWNDIFF_LLM_COMMAND", None)
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "run_owndiff.py"), "--repo", str(repo)],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["question_generation"] == "agent_llm_required"
    assert payload["awaiting_llm_response"] is True
    assert payload["agent_may_push_merge_request"] is False
    assert (repo / ".owndiff" / "question-prompt.md").exists()
    assert (repo / ".owndiff" / "question-request.json").exists()
    questions = json.loads((repo / ".owndiff" / "questions.json").read_text(encoding="utf-8"))
    gate = json.loads((repo / ".owndiff" / "ownership-gate.json").read_text(encoding="utf-8"))
    report = (repo / ".owndiff" / "ownership-report.md").read_text(encoding="utf-8")
    assert questions["generation"]["method"] == "agent_llm_required"
    assert questions["generation"]["awaiting_llm_response"] is True
    assert gate["status"] == "question_generation_required"
    assert gate["agent_may_push_merge_request"] is False
    assert "pending_agent_llm_questions" in report


def test_agent_llm_response_completes_questions_without_command(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    auth_dir = repo / "src" / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "session.py").write_text("def refresh(token):\n    return token\n", encoding="utf-8")

    env = os.environ.copy()
    env.pop("OWNDIFF_LLM_COMMAND", None)
    prepared = subprocess.run(
        [sys.executable, str(SCRIPTS / "run_owndiff.py"), "--repo", str(repo)],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )
    prepared_payload = json.loads(prepared.stdout)
    assert prepared_payload["question_generation"] == "agent_llm_required"

    prompt = (repo / ".owndiff" / "question-prompt.md").read_text(encoding="utf-8")
    response = subprocess.run(
        [sys.executable, str(ROOT / "tests" / "fixtures" / "llm_provider.py")],
        input=prompt,
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )
    (repo / ".owndiff" / "question-response.json").write_text(response.stdout, encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "run_owndiff.py"),
            "--repo",
            str(repo),
            "--llm-response",
            ".owndiff/question-response.json",
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )
    payload = json.loads(completed.stdout)
    questions = json.loads((repo / ".owndiff" / "questions.json").read_text(encoding="utf-8"))
    gate = json.loads((repo / ".owndiff" / "ownership-gate.json").read_text(encoding="utf-8"))

    assert payload["question_generation"] == "agent_llm"
    assert payload["awaiting_llm_response"] is False
    assert int(payload["questions"]) >= 5
    assert questions["generation"]["method"] == "agent_llm"
    assert all(question["source"] == "llm" for question in questions["questions"])
    assert gate["status"] == "pending_answers"
    assert gate["agent_may_push_merge_request"] is False


def test_docs_only_change_stays_low_risk(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("docs only\n", encoding="utf-8")

    result = run_owndiff(repo)

    assert result["risk_level"] == "low"
    assert int(result["risk_score"]) <= 10
    report = (repo / ".owndiff" / "ownership-report.md").read_text(encoding="utf-8")
    assert "No human answer is required" in report
    gate = json.loads((repo / ".owndiff" / "ownership-gate.json").read_text(encoding="utf-8"))
    mcq = json.loads((repo / ".owndiff" / "ownership-mcq.json").read_text(encoding="utf-8"))
    questions = json.loads((repo / ".owndiff" / "questions.json").read_text(encoding="utf-8"))
    assert int(result["questions"]) == 0
    assert questions["questions"] == []
    assert mcq["questions"] == []
    assert gate["status"] == "report_only"
    assert gate["agent_may_push_merge_request"] is True


def test_low_risk_mcqs_stay_disabled_even_if_question_count_is_overridden(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / ".owndiff.yml").write_text(
        "questions:\n"
        "  question_counts:\n"
        "    low: 2\n",
        encoding="utf-8",
    )
    run(["git", "add", "."], repo)
    run(["git", "commit", "-m", "add owndiff config"], repo)

    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("docs only\n", encoding="utf-8")

    result = run_owndiff(repo)
    mcq = json.loads((repo / ".owndiff" / "ownership-mcq.json").read_text(encoding="utf-8"))
    gate = json.loads((repo / ".owndiff" / "ownership-gate.json").read_text(encoding="utf-8"))

    assert result["risk_level"] == "low"
    assert int(result["questions"]) == 0
    assert mcq["questions"] == []
    assert gate["status"] == "report_only"
    assert gate["agent_may_push_merge_request"] is True


def test_secret_like_value_is_redacted_in_reason(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    sample_value = "not-a-real-" + "token-value-123456"
    (repo / "settings.py").write_text(f"API_KEY = '{sample_value}'\n", encoding="utf-8")

    result = run_owndiff(repo)
    risk = json.loads((repo / ".owndiff" / "risk.json").read_text(encoding="utf-8"))
    reasons = "\n".join(reason["message"] for reason in risk["reasons"])

    assert result["risk_level"] in {"high", "critical"}
    assert "secret-like" in reasons
    assert sample_value not in reasons


def test_security_signature_change_is_high_risk_with_existing_package_tests(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    source = repo / "src" / "itsdangerous"
    tests = repo / "tests" / "test_itsdangerous"
    source.mkdir(parents=True)
    tests.mkdir(parents=True)
    (source / "timed.py").write_text(
        "def validate_age(age, max_age):\n"
        "    if age > max_age:\n"
        "        raise SignatureExpired('expired')\n"
        "    if age < 0:\n"
        "        raise SignatureExpired('future')\n",
        encoding="utf-8",
    )
    (tests / "test_timed.py").write_text("def test_validate_age():\n    assert True\n", encoding="utf-8")
    run(["git", "add", "."], repo)
    run(["git", "commit", "-m", "add timed signer"], repo)

    (source / "timed.py").write_text(
        "def validate_age(age, max_age):\n"
        "    if age > max_age:\n"
        "        raise SignatureExpired('expired')\n"
        "    if age < -30:\n"
        "        raise SignatureExpired('future')\n",
        encoding="utf-8",
    )

    result = run_owndiff(repo)

    assert result["risk_level"] in {"high", "critical"}
    assert result["test_gap"] is False


def test_setup_py_dependency_change_is_medium_or_higher(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "setup.py").write_text("install_requires = ['Flask']\n", encoding="utf-8")
    run(["git", "add", "."], repo)
    run(["git", "commit", "-m", "add setup"], repo)

    (repo / "setup.py").write_text("install_requires = ['Flask', 'requests']\n", encoding="utf-8")

    result = run_owndiff(repo)

    assert result["risk_level"] in {"medium", "high", "critical"}


def test_medium_mcqs_use_conceptual_easy_prompts(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "setup.py").write_text("install_requires = ['Flask']\n", encoding="utf-8")
    run(["git", "add", "."], repo)
    run(["git", "commit", "-m", "add setup"], repo)

    (repo / "setup.py").write_text("install_requires = ['Flask', 'requests']\n", encoding="utf-8")

    result = run_owndiff(repo)
    rendered = run([sys.executable, str(SCRIPTS / "present_mcq.py"), "--mcq", ".owndiff/ownership-mcq.json"], repo).stdout
    questions = json.loads((repo / ".owndiff" / "questions.json").read_text(encoding="utf-8"))
    mcq = json.loads((repo / ".owndiff" / "ownership-mcq.json").read_text(encoding="utf-8"))
    option_texts = [
        str(option["text"])
        for question in mcq["questions"]
        for option in question["options"]
        if isinstance(option, dict)
    ]

    assert result["risk_level"] == "medium"
    assert int(result["questions"]) == 3
    assert result["question_generation"] == "agent_llm"
    assert "For setup.py" in rendered
    assert "package" in rendered or "dependency" in rendered or "dependencies" in rendered
    assert "before and after behavior in setup.py" in rendered
    assert "changed files:" not in rendered
    assert "changed line count" not in rendered
    assert "Skip the explanation because the AI generated the change." not in rendered
    assert all(question["context"]["focus"] == "setup.py" for question in questions["questions"])
    assert all(question["source"] == "llm" and len(question["options"]) == 4 for question in questions["questions"])
    assert len(option_texts) == len(set(option_texts))


def test_pipeline_ignores_previous_owndiff_artifacts(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "feature.py").write_text("def ok():\n    return True\n", encoding="utf-8")

    first = run_owndiff(repo)
    second = run_owndiff(repo)

    assert first["files_changed"] == 1
    assert second["files_changed"] == 1


def test_repo_config_can_add_domain_rule_and_question(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / ".owndiff.yml").write_text(
        "risk:\n"
        "  thresholds:\n"
        "    medium: 10\n"
        "    high: 25\n"
        "    critical: 80\n"
        "  domain_rules:\n"
        "    feature_flags:\n"
        "      score: 30\n"
        "      severity: high\n"
        "      path_terms: ['flags']\n"
        "      content_terms: ['feature_flag']\n"
        "      message: 'Touches feature flag rollout behavior'\n",
        encoding="utf-8",
    )
    (repo / "src").mkdir()
    (repo / "src" / "flags.py").write_text("FEATURE_FLAG = False\n", encoding="utf-8")
    run(["git", "add", "."], repo)
    run(["git", "commit", "-m", "add flags"], repo)

    (repo / "src" / "flags.py").write_text("FEATURE_FLAG = True  # feature_flag rollout\n", encoding="utf-8")

    result = run_owndiff(repo)
    questions = json.loads((repo / ".owndiff" / "questions.json").read_text(encoding="utf-8"))

    assert result["risk_level"] in {"high", "critical"}
    assert "feature_flags" in {item["dimension"].removeprefix("domain:") for item in questions["questions"]}
    assert "src/flags.py" in json.dumps(questions)


def test_repo_config_can_add_language_and_test_mapping(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / ".owndiff.yml").write_text(
        "diff:\n"
        "  language_extensions:\n"
        "    '.foo': 'foo'\n"
        "test_gap:\n"
        "  code_languages:\n"
        "    - 'foo'\n"
        "  candidate_patterns:\n"
        "    '.foo':\n"
        "      - 'tests/{stem}_foo_test.foo'\n",
        encoding="utf-8",
    )
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src" / "service.foo").write_text("value = 1\n", encoding="utf-8")
    (repo / "tests" / "service_foo_test.foo").write_text("assert value\n", encoding="utf-8")
    run(["git", "add", "."], repo)
    run(["git", "commit", "-m", "add foo service"], repo)

    (repo / "src" / "service.foo").write_text("value = 2\n", encoding="utf-8")

    result = run_owndiff(repo)
    diff = json.loads((repo / ".owndiff" / "diff.json").read_text(encoding="utf-8"))

    assert diff["changed_files"][0]["language"] == "foo"
    assert result["test_gap"] is False


def test_mcq_gate_accepts_only_all_correct_answers(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    auth_dir = repo / "src" / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "session.py").write_text("def refresh(token):\n    return token\n", encoding="utf-8")

    run_owndiff(repo)

    mcq = json.loads((repo / ".owndiff" / "ownership-mcq.json").read_text(encoding="utf-8"))
    answer_key = json.loads((repo / ".owndiff" / "ownership-answer-key.json").read_text(encoding="utf-8"))
    assert mcq["questions"]
    assert "correct_option_ids" not in json.dumps(mcq["questions"])

    correct_answers = {
        "schema_version": "owndiff.v1.answers",
        "answers": {
            question_id: value["correct_option_ids"][0]
            for question_id, value in answer_key["answers"].items()
        },
    }
    (repo / ".owndiff" / "ownership-answers.json").write_text(json.dumps(correct_answers), encoding="utf-8")

    passed = run(
        [
            sys.executable,
            str(SCRIPTS / "evaluate_answers.py"),
            "--mcq",
            ".owndiff/ownership-mcq.json",
            "--answer-key",
            ".owndiff/ownership-answer-key.json",
            "--answers",
            ".owndiff/ownership-answers.json",
            "--out",
            ".owndiff/ownership-gate.json",
        ],
        repo,
    )
    passed_payload = json.loads(passed.stdout)
    assert passed_payload["agent_may_push_merge_request"] is True

    wrong_answers = {
        "schema_version": "owndiff.v1.answers",
        "answers": {question["id"]: "not-an-option" for question in mcq["questions"]},
    }
    (repo / ".owndiff" / "ownership-answers.json").write_text(json.dumps(wrong_answers), encoding="utf-8")

    failed = run_no_check(
        [
            sys.executable,
            str(SCRIPTS / "evaluate_answers.py"),
            "--mcq",
            ".owndiff/ownership-mcq.json",
            "--answer-key",
            ".owndiff/ownership-answer-key.json",
            "--answers",
            ".owndiff/ownership-answers.json",
            "--out",
            ".owndiff/ownership-gate.json",
        ],
        repo,
    )
    failed_payload = json.loads(failed.stdout)
    assert failed.returncode == 3
    assert failed_payload["agent_may_push_merge_request"] is False


def test_headless_mcq_render_and_submit_flow(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    auth_dir = repo / "src" / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "session.py").write_text("def refresh(token):\n    return token\n", encoding="utf-8")

    run_owndiff(repo)

    rendered = run(
        [
            sys.executable,
            str(SCRIPTS / "present_mcq.py"),
            "--mcq",
            ".owndiff/ownership-mcq.json",
        ],
        repo,
    )
    assert "Headless Fallback" in rendered.stdout
    assert "Use `quiz_tui.py --evaluate` for normal human review" in rendered.stdout
    assert "## q1." in rendered.stdout
    assert "correct_option_ids" not in rendered.stdout

    answer_key = json.loads((repo / ".owndiff" / "ownership-answer-key.json").read_text(encoding="utf-8"))
    selections = [
        f"{question_id}={value['correct_option_ids'][0]}"
        for question_id, value in answer_key["answers"].items()
    ]
    submitted = run(
        [
            sys.executable,
            str(SCRIPTS / "submit_answers.py"),
            "--mcq",
            ".owndiff/ownership-mcq.json",
            "--answer-key",
            ".owndiff/ownership-answer-key.json",
            "--answers-out",
            ".owndiff/ownership-answers.json",
            "--gate-out",
            ".owndiff/ownership-gate.json",
            "--evaluate",
            *selections,
        ],
        repo,
    )
    payload = json.loads(submitted.stdout)
    assert payload["agent_may_push_merge_request"] is True
    assert payload["attempts"] == 1
    assert payload["attempt_summary"] == "Passed after 1 attempt."
    assert (repo / ".owndiff" / "ownership-answers.json").exists()


def test_mcq_gate_records_failed_then_passed_attempts(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    auth_dir = repo / "src" / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "session.py").write_text("def refresh(token):\n    return token\n", encoding="utf-8")

    run_owndiff(repo)

    mcq = json.loads((repo / ".owndiff" / "ownership-mcq.json").read_text(encoding="utf-8"))
    answer_key = json.loads((repo / ".owndiff" / "ownership-answer-key.json").read_text(encoding="utf-8"))

    wrong_selections = []
    correct_selections = []
    for question in mcq["questions"]:
        question_id = question["id"]
        correct = answer_key["answers"][question_id]["correct_option_ids"][0]
        wrong = next(option["id"] for option in question["options"] if option["id"] != correct)
        wrong_selections.append(f"{question_id}={wrong}")
        correct_selections.append(f"{question_id}={correct}")

    failed = run_no_check(
        [
            sys.executable,
            str(SCRIPTS / "submit_answers.py"),
            "--mcq",
            ".owndiff/ownership-mcq.json",
            "--answer-key",
            ".owndiff/ownership-answer-key.json",
            "--answers-out",
            ".owndiff/ownership-answers.json",
            "--gate-out",
            ".owndiff/ownership-gate.json",
            "--evaluate",
            *wrong_selections,
        ],
        repo,
    )
    failed_payload = json.loads(failed.stdout)
    assert failed.returncode == 3
    assert failed_payload["attempts"] == 1
    assert failed_payload["attempt_summary"].startswith("Attempt 1 failed:")

    passed = run(
        [
            sys.executable,
            str(SCRIPTS / "submit_answers.py"),
            "--mcq",
            ".owndiff/ownership-mcq.json",
            "--answer-key",
            ".owndiff/ownership-answer-key.json",
            "--answers-out",
            ".owndiff/ownership-answers.json",
            "--gate-out",
            ".owndiff/ownership-gate.json",
            "--evaluate",
            *correct_selections,
        ],
        repo,
    )
    passed_payload = json.loads(passed.stdout)
    gate = json.loads((repo / ".owndiff" / "ownership-gate.json").read_text(encoding="utf-8"))
    assert passed_payload["agent_may_push_merge_request"] is True
    assert passed_payload["attempts"] == 2
    assert passed_payload["attempt_summary"] == "Passed after 2 attempts."
    assert gate["attempts_to_pass"] == 2
    assert len(gate["attempt_history"]) == 2


def test_keyboard_tui_accepts_real_pty_answers(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    auth_dir = repo / "src" / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "session.py").write_text("def refresh(token):\n    return token\n", encoding="utf-8")

    run_owndiff(repo)

    mcq = json.loads((repo / ".owndiff" / "ownership-mcq.json").read_text(encoding="utf-8"))
    answer_key = json.loads((repo / ".owndiff" / "ownership-answer-key.json").read_text(encoding="utf-8"))
    keys = correct_answer_arrow_keys(mcq, answer_key)
    returncode, output = run_in_pty(
        [
            sys.executable,
            str(SCRIPTS / "quiz_tui.py"),
            "--mcq",
            ".owndiff/ownership-mcq.json",
            "--answer-key",
            ".owndiff/ownership-answer-key.json",
            "--answers-out",
            ".owndiff/ownership-answers.json",
            "--gate-out",
            ".owndiff/ownership-gate.json",
            "--evaluate",
        ],
        repo,
        keys + "\n",
    )

    gate = json.loads((repo / ".owndiff" / "ownership-gate.json").read_text(encoding="utf-8"))
    answers = json.loads((repo / ".owndiff" / "ownership-answers.json").read_text(encoding="utf-8"))
    assert returncode == 0, output
    assert gate["agent_may_push_merge_request"] is True
    assert gate["attempt_summary"] == "Passed after 1 attempt."
    assert answers["answers"]


def test_keyboard_tui_can_cancel_from_review_without_writing_answers(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    auth_dir = repo / "src" / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "session.py").write_text("def refresh(token):\n    return token\n", encoding="utf-8")

    run_owndiff(repo)

    mcq = json.loads((repo / ".owndiff" / "ownership-mcq.json").read_text(encoding="utf-8"))
    answer_key = json.loads((repo / ".owndiff" / "ownership-answer-key.json").read_text(encoding="utf-8"))
    right = "\x1b[C"
    returncode, output = run_in_pty(
        [
            sys.executable,
            str(SCRIPTS / "quiz_tui.py"),
            "--mcq",
            ".owndiff/ownership-mcq.json",
            "--answer-key",
            ".owndiff/ownership-answer-key.json",
            "--answers-out",
            ".owndiff/ownership-answers.json",
            "--gate-out",
            ".owndiff/ownership-gate.json",
            "--evaluate",
        ],
        repo,
        correct_answer_arrow_keys(mcq, answer_key) + right + right + "\n",
    )

    assert returncode == 130, output
    assert not (repo / ".owndiff" / "ownership-answers.json").exists()


def test_keyboard_tui_requires_real_tty_and_points_to_terminal_command(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    auth_dir = repo / "src" / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "session.py").write_text("def refresh(token):\n    return token\n", encoding="utf-8")

    run_owndiff(repo)

    proc = run_no_check(
        [
            sys.executable,
            str(SCRIPTS / "quiz_tui.py"),
            "--mcq",
            ".owndiff/ownership-mcq.json",
            "--answer-key",
            ".owndiff/ownership-answer-key.json",
            "--answers-out",
            ".owndiff/ownership-answers.json",
            "--gate-out",
            ".owndiff/ownership-gate.json",
            "--evaluate",
        ],
        repo,
    )

    assert proc.returncode == 2
    assert "requires an interactive terminal" in proc.stderr
    assert "No answers were written" in proc.stderr
    assert "Do not ask the human to type q1=a" in proc.stderr
    assert "present_mcq.py" not in proc.stderr


def test_agent_installer_writes_verified_agent_files(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)

    proc = run(
        [
            sys.executable,
            str(SCRIPTS / "install_agent_rules.py"),
            "--repo",
            str(repo),
            "--agents",
            "claude-code,codex,opencode,gemini-cli,pi,hermes,devin",
            "--verify",
            "--python-command",
            sys.executable,
            "--json",
        ],
        repo,
    )
    payload = json.loads(proc.stdout)

    assert payload["verified"] is True
    assert (repo / ".claude" / "skills" / "owndiff").resolve() == ROOT
    assert (repo / ".agents" / "skills" / "owndiff" / "SKILL.md").exists()
    assert "agent_may_push_merge_request" in (repo / "CLAUDE.md").read_text(encoding="utf-8")
    assert "agent_may_push_merge_request" in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "quiz_tui.py" in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "present_mcq.py" not in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "submit_answers.py" not in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "q1=c" not in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "do not print MCQs in chat" in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "attempt_summary" in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "question-prompt.md" in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "--llm-response .owndiff/question-response.json" in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "Do not invent facts" in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "do not use deterministic fallback questions" in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "agent_may_push_merge_request" in (repo / "GEMINI.md").read_text(encoding="utf-8")
    assert "agent_may_push_merge_request" in (repo / ".devin" / "rules" / "owndiff.md").read_text(
        encoding="utf-8"
    )

    second = run(
        [
            sys.executable,
            str(SCRIPTS / "install_agent_rules.py"),
            "--repo",
            str(repo),
            "--agents",
            "opencode,codex",
            "--verify",
            "--python-command",
            sys.executable,
            "--json",
        ],
        repo,
    )
    second_payload = json.loads(second.stdout)
    assert second_payload["verified"] is True
    assert (repo / "AGENTS.md").read_text(encoding="utf-8").count("BEGIN OWNDIFF AGENT RULE") == 1


def test_claude_marketplace_metadata_is_valid() -> None:
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))

    assert plugin["name"] == "owndiff"
    assert plugin["version"]
    assert plugin["author"]["name"] == "OwnDiff"
    assert (ROOT / "skills" / "owndiff" / "SKILL.md").exists()
    assert marketplace["name"] == "owndiff"
    assert marketplace["description"]
    assert marketplace["plugins"] == [
        {
            "name": "owndiff",
            "source": "./",
            "description": "Local ownership gate and MCQ approval skill for AI-assisted code diffs.",
        }
    ]


def test_codex_marketplace_metadata_is_valid() -> None:
    plugin = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    marketplace = json.loads((ROOT / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8"))

    assert plugin["name"] == "owndiff"
    assert plugin["version"] == "0.2.0"
    assert plugin["interface"]["displayName"] == "OwnDiff"
    assert plugin["interface"]["defaultPrompt"]
    assert marketplace["name"] == "owndiff"
    assert marketplace["plugins"] == [
        {
            "name": "owndiff",
            "source": {
                "source": "url",
                "url": "https://github.com/owndiff/own-your-diff.git",
                "ref": "main",
            },
            "policy": {
                "installation": "AVAILABLE",
                "authentication": "ON_INSTALL",
            },
            "category": "Productivity",
        }
    ]


def test_public_files_do_not_embed_local_user_paths() -> None:
    checked_files = [
        ROOT / "README.md",
        ROOT / "SKILL.md",
        ROOT / "skills" / "owndiff" / "SKILL.md",
        ROOT / ".claude-plugin" / "plugin.json",
        ROOT / ".claude-plugin" / "marketplace.json",
        ROOT / ".codex-plugin" / "plugin.json",
        ROOT / ".agents" / "plugins" / "marketplace.json",
        ROOT / "configs" / "agent_install.yaml",
        ROOT / "CONTRIBUTING.md",
        ROOT / "SECURITY.md",
        ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md",
        ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml",
        ROOT / ".github" / "ISSUE_TEMPLATE" / "feature_request.yml",
        ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml",
        ROOT / ".github" / "workflows" / "ci.yml",
        ROOT / ".github" / "dependabot.yml",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in checked_files)

    forbidden_path = "/" + "Users" + "/"
    forbidden_user = "ma" + "yur"
    assert forbidden_path not in combined
    assert forbidden_user not in combined.lower()
