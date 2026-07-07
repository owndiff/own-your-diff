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
    proc = run([sys.executable, str(SCRIPTS / "run_owndiff.py"), "--repo", str(repo), *extra_args], repo)
    return json.loads(proc.stdout)


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
    assert "authentication" in report.lower() or "session" in report.lower()
    assert "Ownership Questions" in report
    gate = json.loads((repo / ".owndiff" / "ownership-gate.json").read_text(encoding="utf-8"))
    assert gate["status"] == "pending_answers"
    assert gate["agent_may_push_merge_request"] is False
    assert not (repo / ".owndiff" / "ownership-quiz.html").exists()


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
        "      message: 'Touches feature flag rollout behavior'\n"
        "questions:\n"
        "  domain:\n"
        "    feature_flags:\n"
        "      question: 'Who owns this feature flag rollout and kill switch?'\n"
        "      expected_evidence: ['owner', 'kill switch']\n",
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
    assert "kill switch" in json.dumps(questions)


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


def test_chat_native_mcq_render_and_submit_flow(tmp_path: Path) -> None:
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
    assert "Answer in this chat" in rendered.stdout
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
    assert (repo / ".owndiff" / "ownership-answers.json").exists()


def test_keyboard_tui_accepts_real_pty_answers(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    auth_dir = repo / "src" / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "session.py").write_text("def refresh(token):\n    return token\n", encoding="utf-8")

    run_owndiff(repo)

    mcq = json.loads((repo / ".owndiff" / "ownership-mcq.json").read_text(encoding="utf-8"))
    answer_key = json.loads((repo / ".owndiff" / "ownership-answer-key.json").read_text(encoding="utf-8"))
    keys = "".join(
        answer_key["answers"][question["id"]]["correct_option_ids"][0]
        for question in mcq["questions"]
    )
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
        keys + "s",
    )

    gate = json.loads((repo / ".owndiff" / "ownership-gate.json").read_text(encoding="utf-8"))
    answers = json.loads((repo / ".owndiff" / "ownership-answers.json").read_text(encoding="utf-8"))
    assert returncode == 0, output
    assert gate["agent_may_push_merge_request"] is True
    assert answers["answers"]


def test_keyboard_tui_requires_real_tty_and_points_to_chat_fallback(tmp_path: Path) -> None:
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
    assert "present_mcq.py" in proc.stderr


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
    assert "present_mcq.py" in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "submit_answers.py" in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "Paste the full rendered MCQ text into the chat" in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "collapsed transcript" in (repo / "AGENTS.md").read_text(encoding="utf-8")
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
    assert marketplace["name"] == "owndiff"
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
    assert plugin["version"] == "0.1.0"
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
