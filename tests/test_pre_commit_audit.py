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


def release_policy() -> dict[str, object]:
    payload = policy()
    payload["release_integrity"] = {
        "expected_assets": ["owndiff-linux-x86_64"],
        "installer_path": "install.sh",
        "ci_workflow_path": ".github/workflows/ci.yml",
        "release_workflow_path": ".github/workflows/release-binaries.yml",
        "linux_build_script_path": "scripts/build_linux_release_binary.sh",
        "verifier_path": "scripts/verify_release_assets.py",
        "forbidden_installer_term_parts": [["OWNDIFF_", "DOWNLOAD_URL"]],
    }
    return payload


def write_release_integrity_files(repo: Path, *, include_verifier_call: bool = True) -> None:
    (repo / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    (repo / "scripts").mkdir(exist_ok=True)
    (repo / "install.sh").write_text(
        "\n".join(
            [
                "OWNDIFF_LOCAL_ASSET=/tmp/owndiff",
                "OWNDIFF_EXPECTED_SHA256=abc",
                'checksum_url="${url}.sha256"',
                "compute_sha256() { :; }",
                "echo checksum verification failed",
            ]
        ),
        encoding="utf-8",
    )
    (repo / ".github" / "workflows" / "ci.yml").write_text(
        "steps:\n  - run: sha256sum dist/owndiff > dist/owndiff.sha256\n  - env:\n      OWNDIFF_LOCAL_ASSET: asset\n",
        encoding="utf-8",
    )
    verifier_call = (
        'python scripts/verify_release_assets.py --repo "$GITHUB_REPOSITORY" --tag "$GITHUB_REF_NAME"\n'
        if include_verifier_call
        else ""
    )
    (repo / ".github" / "workflows" / "release-binaries.yml").write_text(
        "\n".join(
            [
                "steps:",
                "  - run: |",
                "      dist/${{ matrix.asset }}.sha256",
                '      shasum -a 256 "dist/${{ matrix.asset }}" > "dist/${{ matrix.asset }}.sha256"',
                "      release-assets/*",
                f"      {verifier_call}".rstrip(),
                "    env:",
                "      OWNDIFF_LOCAL_ASSET: asset",
            ]
        ),
        encoding="utf-8",
    )
    (repo / "scripts" / "build_linux_release_binary.sh").write_text(
        'sha256sum "./dist/${asset}" > "./dist/${asset}.sha256"\n',
        encoding="utf-8",
    )
    (repo / "scripts" / "verify_release_assets.py").write_text(
        "\n".join(
            [
                "owndiff-linux-x86_64",
                "def verify_release_dir(): pass",
                "def verify_github_release(): pass",
                "browser_download_url = True",
                "message = 'checksum sidecar'",
            ]
        ),
        encoding="utf-8",
    )


def test_release_integrity_policy_requires_published_asset_verification(tmp_path: Path) -> None:
    audit = load_audit_module()
    repo = init_repo(tmp_path)
    write_release_integrity_files(repo, include_verifier_call=True)

    passing = audit.audit(repo, release_policy(), staged=False, should_run_checks=False, scan_history=False)
    assert passing["ok"] is True

    write_release_integrity_files(repo, include_verifier_call=False)
    failing = audit.audit(repo, release_policy(), staged=False, should_run_checks=False, scan_history=False)

    assert failing["ok"] is False
    assert any("scripts/verify_release_assets.py" in finding for finding in failing["findings"])


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
