from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
AUDIT_PATH = ROOT / "skills" / "owndiff-pre-commit" / "scripts" / "audit.py"


def load_audit_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pre_commit_audit", AUDIT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=True)


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


def policy() -> dict[str, object]:
    return {
        "max_scan_bytes": 1024 * 1024,
        "forbidden_staged_paths": [],
        "privacy_patterns": [
            {"id": "home_path", "pattern": "/Users/[A-Za-z0-9._-]+/"},
            {
                "id": "api_secret",
                "pattern": "(?i)(api[_-]?key|secret|password|token)\\s*[:=]\\s*[\"']?[A-Za-z0-9_./+=-]{20,}",
            },
        ],
        "privacy_review_patterns": [
            {"id": "redaction_marker", "pattern": "(?i)\\bredacted\\b"},
        ],
        "privacy_allowlist": [
            {
                "id": "api_secret",
                "paths": ["tests/test_sample.py"],
                "pattern": "not-a-real-[A-Za-z0-9-]+",
            },
        ],
        "required_files": [],
        "version_sources": [],
        "checks": [],
    }


def test_history_scan_blocks_secret_without_echoing_value(tmp_path: Path) -> None:
    audit = load_audit_module()
    repo = init_repo(tmp_path)
    secret_value = "".join(["abcdefghijklmnopqrstuvwxyz", "123456"])
    (repo / "settings.py").write_text(f"API_KEY = '{secret_value}'\n", encoding="utf-8")
    run(["git", "add", "."], repo)
    run(["git", "commit", "-m", "add config"], repo)

    result = audit.audit(repo, policy(), staged=False, should_run_checks=False, scan_history=True)
    payload = json.dumps(result)

    assert result["ok"] is False
    assert "api_secret" in payload
    assert "settings.py" in payload
    assert secret_value not in payload


def test_history_scan_allows_intentional_test_placeholder(tmp_path: Path) -> None:
    audit = load_audit_module()
    repo = init_repo(tmp_path)
    tests = repo / "tests"
    tests.mkdir()
    placeholder = "".join(["not-a-real-", "token-value-123456"])
    (tests / "test_sample.py").write_text(f"API_KEY = '{placeholder}'\n", encoding="utf-8")
    run(["git", "add", "."], repo)
    run(["git", "commit", "-m", "add placeholder test"], repo)

    result = audit.audit(repo, policy(), staged=False, should_run_checks=False, scan_history=True)

    assert result["ok"] is True
    assert result["findings"] == []


def test_redaction_marker_is_review_warning_not_blocker(tmp_path: Path) -> None:
    audit = load_audit_module()
    repo = init_repo(tmp_path)
    (repo / "SECURITY.md").write_text("Secret-like values are redacted before prompt generation.\n", encoding="utf-8")

    result = audit.audit(repo, policy(), staged=False, should_run_checks=False, scan_history=False)

    assert result["ok"] is True
    assert result["findings"] == []
    assert any("redaction_marker" in warning for warning in result["warnings"])
