from __future__ import annotations

import json
import os
import platform
import shlex
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

import quiz_web
from owndifflib import config as config_lib
from owndifflib.diff_collect import is_source_file
from owndifflib.llm_questions import build_llm_question_prompt, validate_llm_questions
from quiz_web import terminal_app_from_env

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
RUNTIME = SCRIPTS / "owndiff_runtime.py"


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=True)


def run_no_check(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=False)


def submit_browser_review(cmd: list[str], cwd: Path, selections: dict[str, str], timeout: float = 12.0) -> tuple[int, str, str]:
    proc = subprocess.Popen(cmd, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stderr is not None
    assert proc.stdout is not None
    stderr_prefix: list[str] = []
    url = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stderr.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        stderr_prefix.append(line)
        if "OwnDiff browser review:" in line:
            url = line.rsplit(" ", 1)[-1].strip()
            break
    if not url:
        proc.kill()
        raise AssertionError("Browser review URL was not printed")

    with urllib.request.urlopen(url, timeout=timeout) as response:
        assert response.status == 200
        assert "OwnDiff Browser Review" in response.read().decode("utf-8")

    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    token = query["token"][0]
    form = urllib.parse.urlencode({"token": token, **selections}).encode("utf-8")
    submit_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/submit", "", "", ""))
    request = urllib.request.Request(submit_url, data=form, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        assert response.status == 200
        assert "OwnDiff Gate Result" in response.read().decode("utf-8")

    stdout, stderr = proc.communicate(timeout=timeout)
    return proc.returncode, stdout, "".join(stderr_prefix) + stderr


def submit_browser_review_with_pages(
    cmd: list[str],
    cwd: Path,
    selections: dict[str, str],
    timeout: float = 12.0,
) -> tuple[int, str, str, str, str]:
    proc = subprocess.Popen(cmd, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stderr is not None
    assert proc.stdout is not None
    stderr_prefix: list[str] = []
    url = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stderr.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        stderr_prefix.append(line)
        if "OwnDiff browser review:" in line:
            url = line.rsplit(" ", 1)[-1].strip()
            break
    if not url:
        proc.kill()
        raise AssertionError("Browser review URL was not printed")

    with urllib.request.urlopen(url, timeout=timeout) as response:
        assert response.status == 200
        review_html = response.read().decode("utf-8")

    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    token = query["token"][0]
    form = urllib.parse.urlencode({"token": token, **selections}).encode("utf-8")
    submit_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/submit", "", "", ""))
    request = urllib.request.Request(submit_url, data=form, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        assert response.status == 200
        result_html = response.read().decode("utf-8")

    stdout, stderr = proc.communicate(timeout=timeout)
    return proc.returncode, stdout, "".join(stderr_prefix) + stderr, review_html, result_html


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


def test_default_source_extension_classification() -> None:
    for path in (
        "app.py",
        "Service.java",
        "worker.ts",
        "component.tsx",
        "main.go",
        "lib.rs",
        "deploy.sh",
        "module.tf",
    ):
        assert is_source_file(path) is True

    for path in (
        "README.md",
        "notes.txt",
        "docs/guide.rst",
        "workflow.yaml",
        "package.json",
        "pyproject.toml",
    ):
        assert is_source_file(path) is False


def test_default_config_path_supports_pyinstaller_bundle(tmp_path: Path, monkeypatch) -> None:
    bundle = tmp_path / "bundle"
    (bundle / "configs").mkdir(parents=True)
    monkeypatch.setattr(config_lib.sys, "_MEIPASS", str(bundle), raising=False)

    assert config_lib.default_config_path() == bundle / "configs" / "default_config.yaml"


def test_owndiff_cli_dispatches_help() -> None:
    top = run([sys.executable, str(SCRIPTS / "owndiff_cli.py"), "--help"], ROOT)
    run_help = run([sys.executable, str(SCRIPTS / "owndiff_cli.py"), "run", "--help"], ROOT)
    install_help = run([sys.executable, str(SCRIPTS / "owndiff_cli.py"), "install-agent-rules", "--help"], ROOT)
    quiz_web_help = run([sys.executable, str(SCRIPTS / "owndiff_cli.py"), "quiz-web", "--help"], ROOT)
    version = run([sys.executable, str(SCRIPTS / "owndiff_cli.py"), "--version"], ROOT)

    assert "usage: owndiff <command>" in top.stdout
    assert "run" in top.stdout
    assert "install-agent-rules" in top.stdout
    assert "quiz-web" in top.stdout
    assert "present-mcq" not in top.stdout
    assert "submit-answers" not in top.stdout
    assert "evaluate-answers" not in top.stdout
    assert "usage: owndiff run" in run_help.stdout
    assert "Run the complete OwnDiff ownership-check pipeline" in run_help.stdout
    assert "usage: owndiff install-agent-rules" in install_help.stdout
    assert "usage: owndiff quiz-web" in quiz_web_help.stdout
    assert version.stdout.startswith("owndiff ")


def test_install_script_detects_current_platform_asset() -> None:
    env = os.environ.copy()
    env["OWNDIFF_DRY_RUN"] = "1"
    env["OWNDIFF_BIN_DIR"] = "/tmp/owndiff-bin"
    result = subprocess.run(
        ["sh", str(ROOT / "install.sh")],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    system = platform.system()
    machine = platform.machine().lower()
    expected_os = {"Darwin": "darwin", "Linux": "linux"}[system]
    expected_arch = "arm64" if machine in {"arm64", "aarch64"} else "x86_64"

    assert f"asset=owndiff-{expected_os}-{expected_arch}" in result.stdout
    assert "github.com/owndiff/own-your-diff/releases/latest/download" in result.stdout
    assert "target=/tmp/owndiff-bin/owndiff" in result.stdout


def test_install_script_can_install_from_override_url(tmp_path: Path) -> None:
    source = tmp_path / "owndiff-source"
    source.write_text("#!/usr/bin/env sh\necho owndiff test-build\n", encoding="utf-8")
    source.chmod(0o755)
    bin_dir = tmp_path / "bin"
    env = os.environ.copy()
    env["OWNDIFF_DOWNLOAD_URL"] = source.as_uri()
    env["OWNDIFF_BIN_DIR"] = str(bin_dir)

    result = subprocess.run(
        ["sh", str(ROOT / "install.sh")],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    installed = bin_dir / "owndiff"
    assert installed.exists()
    assert "owndiff test-build" in result.stdout
    assert f"OwnDiff installed at {installed}" in result.stdout


def test_binary_workflows_validate_openclaw_before_building() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    release = (ROOT / ".github" / "workflows" / "release-binaries.yml").read_text(encoding="utf-8")

    assert "python scripts/ci_openclaw_flow.py" in ci
    assert ci.index("python scripts/ci_openclaw_flow.py") < ci.index("python scripts/build_binary.py --name owndiff")
    assert "python scripts/ci_openclaw_flow.py" in release
    assert release.index("python scripts/ci_openclaw_flow.py") < release.index(
        'python scripts/build_binary.py --name "${{ matrix.asset }}"'
    )


def run_owndiff(repo: Path, *extra_args: str) -> dict[str, object]:
    env = os.environ.copy()
    env.pop("OWNDIFF_LLM_COMMAND", None)
    args = [*extra_args]
    if "--review-mode" not in args and not any(item.startswith("--review-mode=") for item in args):
        args.extend(["--review-mode", "none"])
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "run_owndiff.py"), "--repo", str(repo), *args],
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
            *args,
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
    assert all(str(question.get("hint", "")).strip() for question in mcq["questions"])
    hints = [str(question.get("hint", "")).strip() for question in mcq["questions"]]
    assert len(hints) == len(set(hints))
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
    field_name = "API_" + "KEY"
    sample_value = "abcdefghijklmnopqrstuvwxyz"
    (repo / "src" / "settings.py").write_text(f"{field_name} = '{sample_value}'\n", encoding="utf-8")

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
                "hint": "Compare the allow/deny result in session-token-guard.ts with the token guard branch.",
                "options": [
                    {
                        "id": "a",
                        "text": "Say session-token-guard.ts allows a reused token and returns an allowed result.",
                    },
                    {
                        "id": "b",
                        "text": "Explain the token guard behavior in session-token-guard.ts and the allowed/reason result.",
                    },
                    {
                        "id": "c",
                        "text": "Say session-token-guard.ts denies every request even when a token is usable.",
                    },
                    {
                        "id": "d",
                        "text": "Say session-token-guard.ts changes the reason text but leaves allow/deny unchanged.",
                    },
                ],
                "correct_option_id": "b",
                "expected_evidence": ["auth/session-token-guard.ts behavior", "allowed/reason result"],
                "rationale": "Checks changed file ownership.",
            }
        ]
    }

    questions = validate_llm_questions(payload, diff, risk, tests, plan)

    assert questions[0]["question"] == "For auth/session-token-guard.ts, what behavior changed?"
    assert questions[0]["hint"] == "Compare the allow/deny result in session-token-guard.ts with the token guard branch."


def test_llm_validation_rejects_generic_answer_choices() -> None:
    diff = {
        "changed_files": [{"path": "src/auth/session.py", "is_test": False}],
        "summary": {"files_changed": 1, "insertions": 1, "deletions": 0},
    }
    risk = {"risk_level": "high", "domains": ["auth"], "reasons": []}
    tests = {"test_gap": True, "changed_test_files": [], "missing_test_candidates": []}
    plan = [{"id": "q1", "dimension": "intent", "context": {"focus": "src/auth/session.py"}}]
    payload = {
        "questions": [
            {
                "dimension": "intent",
                "difficulty": "easy",
                "question": "For src/auth/session.py, what behavior changed?",
                "hint": "Compare the invalid session path in src/auth/session.py with the returned user behavior.",
                "options": [
                    {
                        "id": "a",
                        "text": "Explain how src/auth/session.py now rejects invalid session input before returning a user.",
                    },
                    {
                        "id": "b",
                        "text": "Approve src/auth/session.py because the AI generated the change.",
                    },
                    {
                        "id": "c",
                        "text": "Say src/auth/session.py accepts an expired session and still returns the user.",
                    },
                    {
                        "id": "d",
                        "text": "Say src/auth/session.py changes the denial reason but not the allow/deny behavior.",
                    },
                ],
                "correct_option_id": "a",
                "expected_evidence": ["src/auth/session.py invalid session behavior"],
                "rationale": "Checks changed auth behavior.",
            }
        ]
    }

    with pytest.raises(ValueError, match="generic distractor idea"):
        validate_llm_questions(payload, diff, risk, tests, plan)


def test_llm_validation_rejects_answer_choices_not_specific_to_diff() -> None:
    diff = {
        "changed_files": [{"path": "src/auth/session.py", "is_test": False}],
        "summary": {"files_changed": 1, "insertions": 1, "deletions": 0},
    }
    risk = {"risk_level": "high", "domains": ["auth"], "reasons": []}
    tests = {"test_gap": True, "changed_test_files": [], "missing_test_candidates": []}
    plan = [{"id": "q1", "dimension": "intent", "context": {"focus": "src/auth/session.py"}}]
    payload = {
        "questions": [
            {
                "dimension": "intent",
                "difficulty": "easy",
                "question": "For src/auth/session.py, what behavior changed?",
                "hint": "Compare the invalid session path in src/auth/session.py with the returned user behavior.",
                "options": [
                    {
                        "id": "a",
                        "text": "Explain how src/auth/session.py now rejects invalid session input before returning a user.",
                    },
                    {"id": "b", "text": "Describe the relevant owner behavior and why it matters."},
                    {
                        "id": "c",
                        "text": "Say src/auth/session.py accepts an expired session and still returns the user.",
                    },
                    {
                        "id": "d",
                        "text": "Say src/auth/session.py changes the denial reason but not the allow/deny behavior.",
                    },
                ],
                "correct_option_id": "a",
                "expected_evidence": ["src/auth/session.py invalid session behavior"],
                "rationale": "Checks changed auth behavior.",
            }
        ]
    }

    with pytest.raises(ValueError, match="option b is generic"):
        validate_llm_questions(payload, diff, risk, tests, plan)


def test_llm_validation_rejects_repeated_or_generic_hints() -> None:
    diff = {
        "changed_files": [{"path": "src/auth/session.py", "is_test": False}],
        "summary": {"files_changed": 1, "insertions": 1, "deletions": 0},
    }
    risk = {"risk_level": "high", "domains": ["auth"], "reasons": []}
    tests = {"test_gap": True, "changed_test_files": [], "missing_test_candidates": []}
    plan = [
        {"id": "q1", "dimension": "intent", "context": {"focus": "src/auth/session.py"}},
        {"id": "q2", "dimension": "runtime_behavior", "context": {"focus": "src/auth/session.py"}},
    ]
    payload = {
        "questions": [
            {
                "dimension": "intent",
                "difficulty": "easy",
                "question": "For src/auth/session.py, what behavior changed?",
                "hint": "Compare the invalid session path in src/auth/session.py with the returned user behavior.",
                "options": [
                    {
                        "id": "a",
                        "text": "Explain how src/auth/session.py now rejects invalid session input before returning a user.",
                    },
                    {
                        "id": "b",
                        "text": "Say src/auth/session.py accepts an expired session and still returns the user.",
                    },
                    {
                        "id": "c",
                        "text": "Say src/auth/session.py changes the denial reason but not the allow/deny behavior.",
                    },
                    {
                        "id": "d",
                        "text": "Say src/auth/session.py returns a user before checking the session token.",
                    },
                ],
                "correct_option_id": "a",
                "expected_evidence": ["src/auth/session.py invalid session behavior"],
                "rationale": "Checks changed auth behavior.",
            },
            {
                "dimension": "runtime_behavior",
                "difficulty": "easy",
                "question": "When does src/auth/session.py run?",
                "hint": "Compare the invalid session path in src/auth/session.py with the returned user behavior.",
                "options": [
                    {
                        "id": "a",
                        "text": "Say src/auth/session.py runs only after the caller already received the user.",
                    },
                    {
                        "id": "b",
                        "text": "Name the request path that calls src/auth/session.py and the allow/deny result it returns.",
                    },
                    {
                        "id": "c",
                        "text": "Treat src/auth/session.py as a build-time check with no runtime session input.",
                    },
                    {
                        "id": "d",
                        "text": "Say src/auth/session.py changes token storage but does not affect request handling.",
                    },
                ],
                "correct_option_id": "b",
                "expected_evidence": ["src/auth/session.py request path"],
                "rationale": "Checks runtime path ownership.",
            },
        ]
    }

    with pytest.raises(ValueError, match="repeats a hint"):
        validate_llm_questions(payload, diff, risk, tests, plan)


def test_llm_validation_rejects_unspread_correct_letters() -> None:
    diff = {
        "changed_files": [{"path": "src/auth/session.py", "is_test": False}],
        "summary": {"files_changed": 1, "insertions": 1, "deletions": 0},
    }
    risk = {"risk_level": "high", "domains": ["auth"], "reasons": []}
    tests = {"test_gap": True, "changed_test_files": [], "missing_test_candidates": []}
    plan = [
        {"id": "q1", "dimension": "intent", "context": {"focus": "src/auth/session.py"}},
        {"id": "q2", "dimension": "runtime_behavior", "context": {"focus": "src/auth/session.py"}},
    ]
    payload = {
        "questions": [
            {
                "dimension": "intent",
                "difficulty": "easy",
                "question": "For src/auth/session.py, what behavior changed?",
                "hint": "Compare the invalid session path in src/auth/session.py with the returned user behavior.",
                "options": [
                    {
                        "id": "a",
                        "text": "Explain how src/auth/session.py now rejects invalid session input before returning a user.",
                    },
                    {
                        "id": "b",
                        "text": "Say src/auth/session.py accepts an expired session and still returns the user.",
                    },
                    {
                        "id": "c",
                        "text": "Say src/auth/session.py changes the denial reason but not the allow/deny behavior.",
                    },
                    {
                        "id": "d",
                        "text": "Say src/auth/session.py returns a user before checking the session token.",
                    },
                ],
                "correct_option_id": "a",
                "expected_evidence": ["src/auth/session.py invalid session behavior"],
                "rationale": "Checks changed auth behavior.",
            },
            {
                "dimension": "runtime_behavior",
                "difficulty": "easy",
                "question": "When does src/auth/session.py run?",
                "hint": "Trace the request path into src/auth/session.py and compare the allow/deny result.",
                "options": [
                    {
                        "id": "a",
                        "text": "Name the request path that calls src/auth/session.py and the allow/deny result it returns.",
                    },
                    {
                        "id": "b",
                        "text": "Say src/auth/session.py runs only after the caller already received the user.",
                    },
                    {
                        "id": "c",
                        "text": "Treat src/auth/session.py as a build-time check with no runtime session input.",
                    },
                    {
                        "id": "d",
                        "text": "Say src/auth/session.py changes token storage but does not affect request handling.",
                    },
                ],
                "correct_option_id": "a",
                "expected_evidence": ["src/auth/session.py request path"],
                "rationale": "Checks runtime path ownership.",
            },
        ]
    }

    with pytest.raises(ValueError, match="not spread across questions"):
        validate_llm_questions(payload, diff, risk, tests, plan)


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
                        "hint": "Inspect src/unrelated.py using web search before choosing an option.",
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
    assert not (repo / ".owndiff" / "ownership-gate.json").exists()
    assert not (repo / ".owndiff" / "ownership-mcq.json").exists()
    assert not (repo / ".owndiff" / "questions.json").exists()


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
                "--review-mode",
                "none",
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


def test_run_owndiff_clears_stale_answers_and_gate_each_run(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    auth_dir = repo / "src" / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "session.py").write_text("def refresh(token):\n    return token\n", encoding="utf-8")

    first = run_owndiff(repo)
    assert first["gate_status"] == "pending_answers"

    stale_answers = {
        "schema_version": "owndiff.v1.answers",
        "answers": {"q1": "a"},
        "stale_marker": "must be removed",
    }
    stale_gate = {
        "schema_version": "owndiff.v1.gate",
        "status": "passed",
        "attempts": 7,
        "attempt_summary": "stale passed gate",
        "agent_may_push_merge_request": True,
    }
    (repo / ".owndiff" / "ownership-answers.json").write_text(json.dumps(stale_answers), encoding="utf-8")
    (repo / ".owndiff" / "ownership-gate.json").write_text(json.dumps(stale_gate), encoding="utf-8")

    second = run_owndiff(repo)
    gate = json.loads((repo / ".owndiff" / "ownership-gate.json").read_text(encoding="utf-8"))

    assert second["gate_status"] == "pending_answers"
    assert not (repo / ".owndiff" / "ownership-answers.json").exists()
    assert gate["status"] == "pending_answers"
    assert gate["attempts"] == 0
    assert gate["agent_may_push_merge_request"] is False
    assert "stale" not in json.dumps(gate)


def test_run_owndiff_removes_stale_llm_response_when_preparing_new_prompt(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    auth_dir = repo / "src" / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "session.py").write_text("def refresh(token):\n    return token\n", encoding="utf-8")
    out_dir = repo / ".owndiff"
    out_dir.mkdir()
    (out_dir / "question-response.json").write_text('{"stale": true}\n', encoding="utf-8")
    (out_dir / "ownership-answers.json").write_text('{"stale": true}\n', encoding="utf-8")
    (out_dir / "ownership-gate.json").write_text(
        json.dumps({"status": "passed", "agent_may_push_merge_request": True}),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("OWNDIFF_LLM_COMMAND", None)
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "run_owndiff.py"), "--repo", str(repo), "--review-mode", "none"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )
    payload = json.loads(result.stdout)
    gate = json.loads((out_dir / "ownership-gate.json").read_text(encoding="utf-8"))

    assert payload["awaiting_llm_response"] is True
    assert payload["gate_status"] == "question_generation_required"
    assert not (out_dir / "question-response.json").exists()
    assert not (out_dir / "ownership-answers.json").exists()
    assert gate["status"] == "question_generation_required"
    assert gate["agent_may_push_merge_request"] is False


def test_docs_only_change_stays_low_risk(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("docs only\n", encoding="utf-8")
    (repo / "notes.txt").write_text("release notes only\n", encoding="utf-8")
    (repo / "docs" / "reference.rst").write_text("reference only\n", encoding="utf-8")

    result = run_owndiff(repo)

    assert result["risk_level"] == "low"
    assert int(result["risk_score"]) <= 10
    report = (repo / ".owndiff" / "ownership-report.md").read_text(encoding="utf-8")
    assert "No source-code ownership gate is required" in report
    questions = json.loads((repo / ".owndiff" / "questions.json").read_text(encoding="utf-8"))
    assert int(result["questions"]) == 0
    assert result["source_code_changed"] is False
    assert result["source_files_changed"] == 0
    assert result["mcq_generated"] is False
    assert result["gate_generated"] is False
    assert result["mcq"] is None
    assert result["gate"] is None
    assert result["gate_status"] == "not_required_no_source_changes"
    assert questions["questions"] == []
    assert questions["generation"]["method"] == "not_required_no_source_changes"
    assert not (repo / ".owndiff" / "ownership-mcq.json").exists()
    assert not (repo / ".owndiff" / "ownership-answer-key.json").exists()
    assert not (repo / ".owndiff" / "ownership-answers-template.json").exists()
    assert not (repo / ".owndiff" / "ownership-gate.json").exists()


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

    assert result["risk_level"] == "low"
    assert int(result["questions"]) == 0
    assert result["gate_status"] == "not_required_no_source_changes"
    assert result["gate_generated"] is False
    assert not (repo / ".owndiff" / "ownership-mcq.json").exists()
    assert not (repo / ".owndiff" / "ownership-gate.json").exists()


def test_low_risk_source_change_still_generates_report_only_gate(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "value.py").write_text("VALUE = 1\n", encoding="utf-8")

    result = run_owndiff(repo)

    gate = json.loads((repo / ".owndiff" / "ownership-gate.json").read_text(encoding="utf-8"))
    assert result["risk_level"] == "low"
    assert result["source_code_changed"] is True
    assert result["source_files_changed"] == 1
    assert result["mcq_generated"] is True
    assert result["gate_generated"] is True
    assert result["questions"] == 0
    assert gate["status"] == "report_only"
    assert gate["agent_may_push_merge_request"] is True


def test_docs_only_run_removes_stale_source_gate_artifacts(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    auth_dir = repo / "src" / "auth"
    auth_dir.mkdir(parents=True)
    source = auth_dir / "session.py"
    source.write_text("def refresh(token):\n    return token\n", encoding="utf-8")

    first = run_owndiff(repo)
    assert first["gate_generated"] is True
    assert (repo / ".owndiff" / "ownership-gate.json").exists()
    assert (repo / ".owndiff" / "ownership-mcq.json").exists()

    run(["git", "add", "src/auth/session.py"], repo)
    run(["git", "commit", "-m", "add session source"], repo)
    (repo / "guide.txt").write_text("documentation only\n", encoding="utf-8")

    second = run_owndiff(repo)

    assert second["source_code_changed"] is False
    assert second["gate_generated"] is False
    assert not (repo / ".owndiff" / "ownership-gate.json").exists()
    assert not (repo / ".owndiff" / "ownership-mcq.json").exists()
    assert not (repo / ".owndiff" / "ownership-answer-key.json").exists()
    assert not (repo / ".owndiff" / "ownership-answers-template.json").exists()
    assert not (repo / ".owndiff" / "ownership-answers.json").exists()
    assert not (repo / ".owndiff" / "question-prompt.md").exists()
    assert not (repo / ".owndiff" / "question-request.json").exists()
    assert not (repo / ".owndiff" / "question-response.json").exists()


def test_mixed_diff_gates_on_source_and_excludes_docs_from_llm_prompt(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    auth_dir = repo / "src" / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "session.ts").write_text(
        "export function refresh(token: string) {\n"
        "  if (!token) throw new Error('missing');\n"
        "  return token;\n"
        "}\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("# demo\nDOCS_ONLY_SENTINEL authentication prose\n", encoding="utf-8")

    env = os.environ.copy()
    env.pop("OWNDIFF_LLM_COMMAND", None)
    prepared = subprocess.run(
        [sys.executable, str(SCRIPTS / "run_owndiff.py"), "--repo", str(repo), "--review-mode", "none"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )
    prepared_payload = json.loads(prepared.stdout)
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
            "--review-mode",
            "none",
            "--llm-response",
            ".owndiff/question-response.json",
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )
    result = json.loads(completed.stdout)
    diff = json.loads((repo / ".owndiff" / "diff.json").read_text(encoding="utf-8"))

    assert prepared_payload["awaiting_llm_response"] is True
    assert result["source_code_changed"] is True
    assert result["source_files_changed"] == 1
    assert result["gate_generated"] is True
    assert int(result["questions"]) >= 5
    assert "src/auth/session.ts" in prompt
    assert '"files_changed": 1' in prompt
    assert "1 source file(s)" in prompt
    assert "DOCS_ONLY_SENTINEL" not in prompt
    assert "README.md" not in prompt
    assert {item["path"]: item["is_source"] for item in diff["changed_files"]} == {
        "README.md": False,
        "src/auth/session.ts": True,
    }


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
    questions = json.loads((repo / ".owndiff" / "questions.json").read_text(encoding="utf-8"))
    mcq = json.loads((repo / ".owndiff" / "ownership-mcq.json").read_text(encoding="utf-8"))
    answer_key = json.loads((repo / ".owndiff" / "ownership-answer-key.json").read_text(encoding="utf-8"))
    option_texts = [
        str(option["text"])
        for question in mcq["questions"]
        for option in question["options"]
        if isinstance(option, dict)
    ]
    correct_letters = {
        answer["correct_option_ids"][0]
        for answer in answer_key["answers"].values()
        if answer.get("correct_option_ids")
    }

    assert result["risk_level"] == "medium"
    assert int(result["questions"]) == 5
    assert result["question_generation"] == "agent_llm"
    mcq_text = json.dumps(mcq)
    assert "For setup.py" in mcq_text
    assert "package" in mcq_text or "dependency" in mcq_text or "dependencies" in mcq_text
    assert "before and after behavior in setup.py" in mcq_text
    assert "changed files:" not in mcq_text
    assert "changed line count" not in mcq_text
    assert "Skip the explanation because the AI generated the change." not in mcq_text
    assert all(question["context"]["focus"] == "setup.py" for question in questions["questions"])
    assert all(question["source"] == "llm" and len(question["options"]) == 4 for question in questions["questions"])
    hints = [str(question["hint"]) for question in mcq["questions"]]
    assert len(hints) == 5
    assert len(hints) == len(set(hints))
    assert len(option_texts) == len(set(option_texts))
    assert len(correct_letters) >= 3


def test_run_owndiff_question_count_override_controls_prompt_and_mcq_count(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "setup.py").write_text("install_requires = ['Flask']\n", encoding="utf-8")
    run(["git", "add", "."], repo)
    run(["git", "commit", "-m", "add setup"], repo)

    (repo / "setup.py").write_text("install_requires = ['Flask', 'requests']\n", encoding="utf-8")

    result = run_owndiff(repo, "--question-count", "4")
    questions = json.loads((repo / ".owndiff" / "questions.json").read_text(encoding="utf-8"))
    mcq = json.loads((repo / ".owndiff" / "ownership-mcq.json").read_text(encoding="utf-8"))

    assert result["question_generation"] == "agent_llm"
    assert int(result["questions"]) == 4
    assert len(questions["questions"]) == 4
    assert len(mcq["questions"]) == 4


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
    assert "feature_flags" in json.dumps(questions)
    assert {item["dimension"] for item in questions["questions"]} >= {"intent", "runtime_behavior", "failure_modes", "tests", "blast_radius"}
    assert "src/flags.py" in json.dumps(questions)


def test_repo_config_can_add_language_and_test_mapping(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / ".owndiff.yml").write_text(
        "diff:\n"
        "  language_extensions:\n"
        "    '.foo': 'foo'\n"
        "  source_extensions:\n"
        "    '.foo': true\n"
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
    assert diff["changed_files"][0]["is_source"] is True
    assert result["source_code_changed"] is True
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



def test_mcq_gate_records_failed_then_passed_attempts(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    auth_dir = repo / "src" / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "session.py").write_text("def refresh(token):\n    return token\n", encoding="utf-8")

    run_owndiff(repo)

    mcq = json.loads((repo / ".owndiff" / "ownership-mcq.json").read_text(encoding="utf-8"))
    answer_key = json.loads((repo / ".owndiff" / "ownership-answer-key.json").read_text(encoding="utf-8"))

    wrong_answers = {"schema_version": "owndiff.v1.answers", "answers": {}}
    correct_answers = {"schema_version": "owndiff.v1.answers", "answers": {}}
    for question in mcq["questions"]:
        question_id = question["id"]
        correct = answer_key["answers"][question_id]["correct_option_ids"][0]
        wrong = next(option["id"] for option in question["options"] if option["id"] != correct)
        wrong_answers["answers"][question_id] = wrong
        correct_answers["answers"][question_id] = correct

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
    assert failed_payload["attempts"] == 1
    assert failed_payload["attempt_summary"].startswith("Attempt 1 failed:")

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
    gate = json.loads((repo / ".owndiff" / "ownership-gate.json").read_text(encoding="utf-8"))
    assert passed_payload["agent_may_push_merge_request"] is True
    assert passed_payload["attempts"] == 2
    assert passed_payload["attempt_summary"] == "Passed after 2 attempts."
    assert gate["attempts_to_pass"] == 2
    assert len(gate["attempt_history"]) == 2


def test_run_owndiff_opens_browser_review_by_default_after_llm_response(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    auth_dir = repo / "src" / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "session.py").write_text("def refresh(token):\n    return token\n", encoding="utf-8")

    run_owndiff(repo)

    answer_key = json.loads((repo / ".owndiff" / "ownership-answer-key.json").read_text(encoding="utf-8"))
    selections = {
        question_id: value["correct_option_ids"][0]
        for question_id, value in answer_key["answers"].items()
    }
    returncode, stdout, stderr = submit_browser_review(
        [
            sys.executable,
            str(SCRIPTS / "run_owndiff.py"),
            "--repo",
            str(repo),
            "--out-dir",
            ".owndiff",
            "--no-open-browser",
            "--web-timeout-seconds",
            "10",
            "--llm-response",
            ".owndiff/question-response.json",
        ],
        repo,
        selections,
    )

    assert returncode == 0, stderr
    payload = json.loads(stdout.strip().splitlines()[-1])
    gate = json.loads((repo / ".owndiff" / "ownership-gate.json").read_text(encoding="utf-8"))
    assert payload["interactive_requested"] is False
    assert payload["interactive_exit_code"] == 0
    assert payload["review_mode"] == "web"
    assert payload["review_started"] is True
    assert "OwnDiff starting local browser review." in stderr
    assert gate["agent_may_push_merge_request"] is True
    assert gate["attempt_summary"] == "Passed after 1 attempt."


def test_run_owndiff_interactive_none_leaves_gate_blocked(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    auth_dir = repo / "src" / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "session.py").write_text("def refresh(token):\n    return token\n", encoding="utf-8")

    run_owndiff(repo)

    proc = run_no_check(
        [
            sys.executable,
            str(SCRIPTS / "run_owndiff.py"),
            "--repo",
            str(repo),
            "--out-dir",
            ".owndiff",
            "--interactive",
            "--review-mode",
            "none",
            "--llm-response",
            ".owndiff/question-response.json",
        ],
        repo,
    )

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["interactive_requested"] is True
    assert payload["interactive_exit_code"] == 2
    assert payload["review_mode"] == "none"
    assert payload["review_started"] is False


def test_browser_review_accepts_click_style_submission(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    auth_dir = repo / "src" / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "session.py").write_text("def refresh(token):\n    return token\n", encoding="utf-8")

    run_owndiff(repo)

    answer_key = json.loads((repo / ".owndiff" / "ownership-answer-key.json").read_text(encoding="utf-8"))
    selections = {
        question_id: value["correct_option_ids"][0]
        for question_id, value in answer_key["answers"].items()
    }
    returncode, stdout, stderr = submit_browser_review(
        [
            sys.executable,
            str(SCRIPTS / "quiz_web.py"),
            "--mcq",
            ".owndiff/ownership-mcq.json",
            "--answer-key",
            ".owndiff/ownership-answer-key.json",
            "--answers-out",
            ".owndiff/ownership-answers.json",
            "--gate-out",
            ".owndiff/ownership-gate.json",
            "--timeout-seconds",
            "10",
            "--no-open-browser",
            "--evaluate",
        ],
        repo,
        selections,
    )

    payload = json.loads(stdout)
    gate = json.loads((repo / ".owndiff" / "ownership-gate.json").read_text(encoding="utf-8"))
    assert returncode == 0, stderr
    assert payload["agent_may_push_merge_request"] is True
    assert gate["attempt_summary"] == "Passed after 1 attempt."


def test_browser_review_has_retry_hints_and_close_after_submit(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    auth_dir = repo / "src" / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "session.py").write_text("def refresh(token):\n    return token\n", encoding="utf-8")

    run_owndiff(repo)

    answer_key = json.loads((repo / ".owndiff" / "ownership-answer-key.json").read_text(encoding="utf-8"))
    selections = {
        question_id: value["correct_option_ids"][0]
        for question_id, value in answer_key["answers"].items()
    }
    returncode, stdout, stderr, review_html, result_html = submit_browser_review_with_pages(
        [
            sys.executable,
            str(SCRIPTS / "quiz_web.py"),
            "--mcq",
            ".owndiff/ownership-mcq.json",
            "--answer-key",
            ".owndiff/ownership-answer-key.json",
            "--answers-out",
            ".owndiff/ownership-answers.json",
            "--gate-out",
            ".owndiff/ownership-gate.json",
            "--timeout-seconds",
            "10",
            "--no-open-browser",
            "--evaluate",
        ],
        repo,
        selections,
    )

    payload = json.loads(stdout)
    assert returncode == 0, stderr
    assert payload["agent_may_push_merge_request"] is True
    assert "id='hint-toggle' checked" in review_html
    assert "Show hints" in review_html
    assert "data-hint" in review_html
    assert "src/auth/session.py" in review_html
    assert "missing, expired, invalid, or reused session input" in review_html
    assert "id='retry-button'" in review_html
    assert "Retry quiz" in review_html
    assert "form.reset()" in review_html
    assert "id='close-browser-button'" in result_html
    assert "Close browser review" in result_html
    assert "window.close()" in result_html


def test_browser_review_detects_known_terminal_apps() -> None:
    assert terminal_app_from_env({"TERM_PROGRAM": "WarpTerminal"}) == "Warp"
    assert terminal_app_from_env({"TERM_PROGRAM": "Apple_Terminal"}) == "Terminal"
    assert terminal_app_from_env({"TERM_PROGRAM": "iTerm.app"}) == "iTerm"
    assert terminal_app_from_env({"TERM_PROGRAM": "vscode"}) == "Visual Studio Code"
    assert terminal_app_from_env({"TERM_PROGRAM": "unknown"}) is None
    assert terminal_app_from_env({}) is None


def test_browser_review_can_refocus_known_macos_terminal(monkeypatch) -> None:
    calls: list[list[str]] = []

    def capture_run(command: list[str], **_kwargs: object) -> object:
        calls.append(command)
        return object()

    monkeypatch.setattr(quiz_web.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(quiz_web.sys, "platform", "darwin")
    monkeypatch.setattr(quiz_web.subprocess, "run", capture_run)

    quiz_web._return_to_terminal_after_delay({"TERM_PROGRAM": "WarpTerminal"})

    assert calls == [["open", "-a", "Warp"]]


def test_browser_review_opens_browser_by_default(tmp_path: Path, monkeypatch) -> None:
    mcq_path = tmp_path / "ownership-mcq.json"
    mcq_path.write_text(
        json.dumps(
            {
                "schema_version": "owndiff.v1.mcq",
                "risk_level": "high",
                "questions": [
                    {
                        "id": "q1",
                        "dimension": "intent",
                        "question": "What changed?",
                        "options": [
                            {"id": "a", "text": "A"},
                            {"id": "b", "text": "B"},
                            {"id": "c", "text": "C"},
                            {"id": "d", "text": "D"},
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    opened: list[str] = []

    def capture_open(url: str) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(quiz_web, "_open_browser", capture_open)
    result = quiz_web.main(
        [
            "--mcq",
            str(mcq_path),
            "--answer-key",
            str(tmp_path / "answer-key.json"),
            "--answers-out",
            str(tmp_path / "answers.json"),
            "--gate-out",
            str(tmp_path / "gate.json"),
            "--timeout-seconds",
            "1",
        ]
    )

    assert result == 2
    assert opened
    assert opened[0].startswith("http://127.0.0.1:")
    assert "token=" in opened[0]


def test_browser_review_uses_native_browser_opener_first(monkeypatch) -> None:
    calls: list[list[str]] = []

    class Result:
        returncode = 0

    def capture_run(command: list[str], **_kwargs: object) -> Result:
        calls.append(command)
        return Result()

    monkeypatch.setattr(quiz_web.sys, "platform", "darwin")
    monkeypatch.setattr(quiz_web.subprocess, "run", capture_run)

    assert quiz_web._open_browser("http://127.0.0.1:12345/?token=test") is True
    assert calls == [["/usr/bin/open", "http://127.0.0.1:12345/?token=test"]]


def test_browser_review_does_not_fail_when_default_browser_does_not_open(tmp_path: Path, monkeypatch, capsys) -> None:
    mcq_path = tmp_path / "ownership-mcq.json"
    mcq_path.write_text(
        json.dumps(
            {
                "schema_version": "owndiff.v1.mcq",
                "risk_level": "high",
                "questions": [
                    {
                        "id": "q1",
                        "dimension": "intent",
                        "question": "What changed?",
                        "options": [
                            {"id": "a", "text": "A"},
                            {"id": "b", "text": "B"},
                            {"id": "c", "text": "C"},
                            {"id": "d", "text": "D"},
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(quiz_web, "_open_browser", lambda _url: False)
    result = quiz_web.main(
        [
            "--mcq",
            str(mcq_path),
            "--answer-key",
            str(tmp_path / "answer-key.json"),
            "--answers-out",
            str(tmp_path / "answers.json"),
            "--gate-out",
            str(tmp_path / "gate.json"),
            "--timeout-seconds",
            "1",
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert "could not open the default browser automatically" in captured.err
    assert "OwnDiff browser review:" in captured.err


def test_run_owndiff_opens_browser_review_without_terminal_interaction(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    auth_dir = repo / "src" / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "session.py").write_text("def refresh(token):\n    return token\n", encoding="utf-8")

    run_owndiff(repo)

    answer_key = json.loads((repo / ".owndiff" / "ownership-answer-key.json").read_text(encoding="utf-8"))
    selections = {
        question_id: value["correct_option_ids"][0]
        for question_id, value in answer_key["answers"].items()
    }
    returncode, stdout, stderr = submit_browser_review(
        [
            sys.executable,
            str(SCRIPTS / "run_owndiff.py"),
            "--repo",
            str(repo),
            "--out-dir",
            ".owndiff",
            "--no-open-browser",
            "--web-timeout-seconds",
            "10",
            "--llm-response",
            ".owndiff/question-response.json",
        ],
        repo,
        selections,
    )

    payload = json.loads(stdout.strip().splitlines()[-1])
    gate = json.loads((repo / ".owndiff" / "ownership-gate.json").read_text(encoding="utf-8"))
    assert returncode == 0, stderr
    assert "OwnDiff starting local browser review." in stderr
    assert "OwnDiff browser review:" in stderr
    assert payload["interactive_exit_code"] == 0
    assert payload["interactive_requested"] is False
    assert payload["review_started"] is True
    assert gate["agent_may_push_merge_request"] is True


def test_runtime_launcher_opens_browser_review_by_default(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    auth_dir = repo / "src" / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "session.py").write_text("def refresh(token):\n    return token\n", encoding="utf-8")

    run_owndiff(repo)

    answer_key = json.loads((repo / ".owndiff" / "ownership-answer-key.json").read_text(encoding="utf-8"))
    selections = {
        question_id: value["correct_option_ids"][0]
        for question_id, value in answer_key["answers"].items()
    }
    returncode, stdout, stderr = submit_browser_review(
        [
            sys.executable,
            str(RUNTIME),
            "run",
            "--repo",
            str(repo),
            "--out-dir",
            ".owndiff",
            "--no-open-browser",
            "--web-timeout-seconds",
            "10",
            "--llm-response",
            ".owndiff/question-response.json",
        ],
        repo,
        selections,
    )

    payload = json.loads(stdout.strip().splitlines()[-1])
    gate = json.loads((repo / ".owndiff" / "ownership-gate.json").read_text(encoding="utf-8"))
    assert returncode == 0, stderr
    assert "OwnDiff browser review:" in stderr
    assert "OwnDiff starting local browser review." in stderr
    assert payload["interactive_exit_code"] == 0
    assert payload["interactive_requested"] is False
    assert payload["review_started"] is True
    assert gate["agent_may_push_merge_request"] is True


def test_agent_installer_writes_verified_agent_files(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    bin_dir = tmp_path / "bin"
    shim = bin_dir / "owndiff"
    log = tmp_path / "owndiff-installer-shim.log"
    bin_dir.mkdir()
    shim.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {shlex.quote(str(log))}\nexit 0\n", encoding="utf-8")
    shim.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "install_agent_rules.py"),
            "--repo",
            str(repo),
            "--agents",
            "claude-code,codex,opencode,gemini-cli,pi,hermes,devin",
            "--verify",
            "--json",
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )
    payload = json.loads(proc.stdout)

    assert payload["verified"] is True
    assert (repo / ".claude" / "skills" / "owndiff").resolve() == ROOT
    assert (repo / ".agents" / "skills" / "owndiff" / "SKILL.md").exists()
    assert "agent_may_push_merge_request" in (repo / "CLAUDE.md").read_text(encoding="utf-8")
    assert "agent_may_push_merge_request" in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "owndiff run --repo . --out-dir .owndiff" in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "--interactive" not in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "owndiff_runtime.py" not in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "quiz-web" not in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "present_mcq.py" not in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "submit_answers.py" not in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "q1=c" not in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "Do not print MCQs in chat" in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "default browser" in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "localhost review server" in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "attempt_summary" in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "question-prompt.md" in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "owndiff run --repo . --out-dir .owndiff --llm-response .owndiff/question-response.json" in (
        repo / "AGENTS.md"
    ).read_text(encoding="utf-8")
    assert "Do not invent facts" in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "do not use deterministic fallback questions" in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "agent_may_push_merge_request" in (repo / "GEMINI.md").read_text(encoding="utf-8")
    assert "agent_may_push_merge_request" in (repo / ".devin" / "rules" / "owndiff.md").read_text(
        encoding="utf-8"
    )

    second = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "install_agent_rules.py"),
            "--repo",
            str(repo),
            "--agents",
            "opencode,codex",
            "--verify",
            "--json",
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )
    second_payload = json.loads(second.stdout)
    assert second_payload["verified"] is True
    assert (repo / "AGENTS.md").read_text(encoding="utf-8").count("BEGIN OWNDIFF AGENT RULE") == 1


def test_agent_installer_verifies_custom_owndiff_command(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    shim = tmp_path / "bin" / "owndiff"
    log = tmp_path / "owndiff-shim.log"
    shim.parent.mkdir()
    shim.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {shlex.quote(str(log))}\nexit 0\n", encoding="utf-8")
    shim.chmod(0o755)

    proc = run(
        [
            sys.executable,
            str(SCRIPTS / "install_agent_rules.py"),
            "--repo",
            str(repo),
            "--agents",
            "codex",
            "--verify",
            "--python-command",
            "/no/such/python",
            "--owndiff-command",
            str(shim),
            "--json",
        ],
        repo,
    )
    payload = json.loads(proc.stdout)
    installed = (repo / "AGENTS.md").read_text(encoding="utf-8")
    calls = log.read_text(encoding="utf-8").splitlines()

    assert payload["verified"] is True
    assert f"{shim} run --repo . --out-dir .owndiff" in installed
    assert "--interactive" not in installed
    assert "--help" in calls
    assert "run --help" in calls
    assert "install-agent-rules --help" in calls


def test_agent_installer_supports_executable_only_project_rules(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    bin_dir = tmp_path / "bin"
    shim = bin_dir / "owndiff"
    log = tmp_path / "owndiff-path-shim.log"
    bin_dir.mkdir()
    shim.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {shlex.quote(str(log))}\nexit 0\n", encoding="utf-8")
    shim.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "install_agent_rules.py"),
            "--repo",
            str(repo),
            "--agents",
            "codex",
            "--verify",
            "--owndiff-command",
            "owndiff",
            "--skip-skill-links",
            "--json",
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )
    payload = json.loads(proc.stdout)
    installed = (repo / "AGENTS.md").read_text(encoding="utf-8")
    calls = log.read_text(encoding="utf-8").splitlines()

    assert payload["verified"] is True
    assert "owndiff run --repo . --out-dir .owndiff" in installed
    assert "--interactive" not in installed
    assert not (repo / ".agents" / "skills" / "owndiff").exists()
    assert any(action.get("skipped") == "standalone executable mode" for action in payload["agents"][0]["actions"])
    assert "--help" in calls
    assert "install-agent-rules --help" in calls


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
        ROOT / ".github" / "workflows" / "release-binaries.yml",
        ROOT / ".github" / "dependabot.yml",
        ROOT / "scripts" / "ci_openclaw_flow.py",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in checked_files)

    forbidden_path = "/" + "Users" + "/"
    forbidden_user = "ma" + "yur"
    assert forbidden_path not in combined
    assert forbidden_user not in combined.lower()
