#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = SKILL_ROOT / "references" / "policy.json"


class AuditError(RuntimeError):
    pass


def run_git(root: Path, args: list[str]) -> bytes:
    proc = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise AuditError(detail or f"git {' '.join(args)} failed")
    return proc.stdout


def git_root(path: Path) -> Path:
    output = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        text=True,
        capture_output=True,
        check=False,
    )
    if output.returncode != 0:
        raise AuditError(f"Not a git repository: {path}")
    return Path(output.stdout.strip()).resolve()


def changed_paths(root: Path, staged: bool) -> list[str]:
    if staged:
        raw = run_git(root, ["diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"])
        return _nul_paths(raw)

    paths = []
    for args in (
        ["diff", "--name-only", "--diff-filter=ACMR", "-z"],
        ["diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"],
        ["ls-files", "--others", "--exclude-standard", "-z"],
    ):
        paths.extend(_nul_paths(run_git(root, args)))
    return sorted(set(paths))


def staged_paths(root: Path) -> list[str]:
    return _nul_paths(run_git(root, ["diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"]))


def _nul_paths(raw: bytes) -> list[str]:
    return [item.decode("utf-8", errors="surrogateescape") for item in raw.split(b"\0") if item]


def load_policy(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"Failed to load policy {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise AuditError("Policy must be a JSON object")
    return payload


def matches_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def printable_text(path: Path, max_bytes: int) -> str:
    data = path.read_bytes()
    if len(data) > max_bytes:
        raise AuditError("file exceeds max_scan_bytes")
    return printable_bytes(data)


def printable_bytes(data: bytes) -> str:
    if b"\0" not in data:
        return data.decode("utf-8", errors="replace")
    return "\n".join(match.decode("ascii", errors="ignore") for match in re.findall(rb"[\x20-\x7e]{8,}", data))


def compile_patterns(items: list[Any]) -> list[tuple[str, re.Pattern[str]]]:
    patterns = []
    for item in items:
        if not isinstance(item, dict) or not item.get("pattern"):
            continue
        pattern_id = str(item.get("id", "privacy_pattern"))
        try:
            patterns.append((pattern_id, re.compile(str(item["pattern"]))))
        except re.error as exc:
            raise AuditError(f"Invalid privacy pattern {pattern_id}: {exc}") from exc
    return patterns


def compile_allowlist(items: list[Any]) -> list[dict[str, Any]]:
    allowlist = []
    for item in items:
        if not isinstance(item, dict) or not item.get("pattern"):
            continue
        pattern_id = str(item.get("id", ""))
        paths = [str(path) for path in item.get("paths", [])]
        try:
            pattern = re.compile(str(item["pattern"]))
        except re.error as exc:
            raise AuditError(f"Invalid privacy allowlist pattern {pattern_id}: {exc}") from exc
        allowlist.append({"id": pattern_id, "paths": paths, "pattern": pattern})
    return allowlist


def is_allowed_privacy_match(pattern_id: str, relative: str, line: str, allowlist: list[dict[str, Any]]) -> bool:
    for item in allowlist:
        if item["id"] and item["id"] != pattern_id:
            continue
        paths = item.get("paths") or ["*"]
        if not matches_any(relative, paths):
            continue
        if item["pattern"].search(line):
            return True
    return False


def scan_text_for_patterns(
    relative: str,
    text: str,
    patterns: list[tuple[str, re.Pattern[str]]],
    allowlist: list[dict[str, Any]],
    *,
    prefix: str = "",
    label: str = "private-data pattern",
) -> list[str]:
    findings = []
    for pattern_id, pattern in patterns:
        for line_number, line in enumerate(text.splitlines(), 1):
            if not pattern.search(line):
                continue
            if is_allowed_privacy_match(pattern_id, relative, line, allowlist):
                continue
            findings.append(f"{prefix}{relative}: matched {label} {pattern_id} on line {line_number}")
            break
    return findings


def scan_file_for_patterns(
    root: Path,
    relative: str,
    patterns: list[tuple[str, re.Pattern[str]]],
    review_patterns: list[tuple[str, re.Pattern[str]]],
    allowlist: list[dict[str, Any]],
    max_bytes: int,
) -> tuple[list[str], list[str], str | None]:
    path = root / relative
    try:
        text = printable_text(path, max_bytes)
    except OSError as exc:
        return [f"{relative}: failed to read file: {exc.__class__.__name__}"], [], None
    except AuditError as exc:
        return [f"{relative}: {exc}"], [], None

    findings = scan_text_for_patterns(relative, text, patterns, allowlist)
    warnings = scan_text_for_patterns(relative, text, review_patterns, allowlist, label="review pattern")
    return findings, warnings, text


def tracked_paths(root: Path) -> list[str]:
    return _nul_paths(run_git(root, ["ls-files", "-z"]))


def history_commits(root: Path) -> list[str]:
    return run_git(root, ["log", "--all", "--format=%H"]).decode("utf-8", errors="replace").splitlines()


def tree_entries(root: Path, commit: str) -> list[tuple[str, str]]:
    entries = []
    for raw_entry in run_git(root, ["ls-tree", "-r", "-z", commit]).split(b"\0"):
        if not raw_entry:
            continue
        try:
            metadata, raw_path = raw_entry.split(b"\t", 1)
            parts = metadata.split()
            if parts[1] != b"blob":
                continue
            object_id = parts[2].decode("ascii")
            relative = raw_path.decode("utf-8", errors="surrogateescape")
        except (IndexError, ValueError, UnicodeDecodeError):
            continue
        entries.append((object_id, relative))
    return entries


def show_blob(root: Path, object_id: str, max_bytes: int) -> str | None:
    size_raw = run_git(root, ["cat-file", "-s", object_id]).decode("utf-8", errors="replace").strip()
    try:
        size = int(size_raw)
    except ValueError:
        return None
    if size > max_bytes:
        return None
    data = run_git(root, ["cat-file", "-p", object_id])
    return printable_bytes(data)


def scan_history_for_patterns(
    root: Path,
    patterns: list[tuple[str, re.Pattern[str]]],
    allowlist: list[dict[str, Any]],
    excluded_paths: list[str],
    max_bytes: int,
) -> tuple[list[str], int]:
    findings = []
    commits = history_commits(root)
    blob_cache: dict[str, str | None] = {}
    for commit in commits:
        for object_id, relative in tree_entries(root, commit):
            if matches_any(relative, excluded_paths):
                continue
            if object_id not in blob_cache:
                blob_cache[object_id] = show_blob(root, object_id, max_bytes)
            text = blob_cache[object_id]
            if text is None:
                continue
            prefix = f"history {commit[:12]}:"
            findings.extend(scan_text_for_patterns(relative, text, patterns, allowlist, prefix=prefix))
    return findings, len(commits)


def validate_structured_file(path: Path) -> str | None:
    try:
        if path.suffix == ".json":
            json.loads(path.read_text(encoding="utf-8"))
        elif path.suffix == ".toml":
            tomllib.loads(path.read_text(encoding="utf-8"))
        elif path.suffix in {".yaml", ".yml"}:
            try:
                import yaml
            except ImportError:
                return f"PyYAML unavailable; could not validate {path}"
            yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"Invalid structured file {path}: {exc}"
    return None


def validate_skill_frontmatter(path: Path) -> str | None:
    if path.name != "SKILL.md":
        return None
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return f"{path} must start with YAML frontmatter"
    closing = text.find("\n---\n", 4)
    if closing == -1:
        return f"{path} has no closing YAML frontmatter delimiter"
    frontmatter = text[4:closing]
    if not re.search(r"(?m)^name:\s*\S+", frontmatter):
        return f"{path} frontmatter is missing name"
    if not re.search(r"(?m)^description:\s*\S+", frontmatter):
        return f"{path} frontmatter is missing description"
    return None


def validate_markdown_links(root: Path, path: Path, text: str) -> list[str]:
    findings = []
    for target in re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", text):
        target = target.strip().strip("<>")
        if not target or target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        clean = target.split("#", 1)[0]
        if not clean:
            continue
        candidate = (path.parent / clean).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            findings.append(f"{path}: local link escapes repository: {target}")
            continue
        if not candidate.exists():
            findings.append(f"{path}: broken local link: {target}")
    return findings


def version_findings(root: Path, sources: list[dict[str, Any]]) -> list[str]:
    versions: dict[str, str] = {}
    findings = []
    for source in sources:
        path = root / str(source.get("path", ""))
        pattern = str(source.get("pattern", ""))
        if not path.exists():
            findings.append(f"Version source missing: {path.relative_to(root)}")
            continue
        match = re.search(pattern, path.read_text(encoding="utf-8"), flags=re.MULTILINE)
        if not match:
            findings.append(f"Version not found in {path.relative_to(root)}")
            continue
        versions[str(path.relative_to(root))] = match.group(1)
    if len(set(versions.values())) > 1:
        detail = ", ".join(f"{path}={version}" for path, version in versions.items())
        findings.append(f"Version mismatch: {detail}")
    return findings


def run_checks(root: Path, checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = []
    for check in checks:
        name = str(check.get("name", "unnamed"))
        raw_command = check.get("command", [])
        if not isinstance(raw_command, list) or not raw_command:
            results.append({"name": name, "ok": False, "error": "command must be a non-empty array"})
            continue
        command = [str(part).replace("{python}", sys.executable) for part in raw_command]
        try:
            proc = subprocess.run(
                command,
                cwd=root,
                text=True,
                capture_output=True,
                timeout=float(check.get("timeout_seconds", 120)),
                check=False,
            )
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            results.append({"name": name, "ok": False, "command": command, "error": str(exc)})
            continue
        results.append(
            {
                "name": name,
                "ok": proc.returncode == 0,
                "command": command,
                "returncode": proc.returncode,
                "stdout_tail": proc.stdout[-2000:],
                "stderr_tail": proc.stderr[-2000:],
            }
        )
    return results


def read_policy_file(root: Path, relative: str, findings: list[str], label: str) -> str:
    path = root / relative
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        findings.append(f"{label} missing or unreadable: {relative}: {exc.__class__.__name__}")
        return ""


def release_integrity_findings(root: Path, policy: dict[str, Any]) -> list[str]:
    if not policy:
        return []

    findings: list[str] = []
    expected_assets = [str(asset) for asset in policy.get("expected_assets", [])]
    if not expected_assets:
        findings.append("Release integrity policy has no expected_assets")

    installer_path = str(policy.get("installer_path", "install.sh"))
    ci_path = str(policy.get("ci_workflow_path", ".github/workflows/ci.yml"))
    release_path = str(policy.get("release_workflow_path", ".github/workflows/release-binaries.yml"))
    linux_build_path = str(policy.get("linux_build_script_path", "scripts/build_linux_release_binary.sh"))
    verifier_path = str(policy.get("verifier_path", "scripts/verify_release_assets.py"))

    installer = read_policy_file(root, installer_path, findings, "Installer")
    ci = read_policy_file(root, ci_path, findings, "CI workflow")
    release = read_policy_file(root, release_path, findings, "Release workflow")
    linux_build = read_policy_file(root, linux_build_path, findings, "Linux release build script")
    verifier = read_policy_file(root, verifier_path, findings, "Release asset verifier")

    for item in policy.get("forbidden_installer_term_parts", []):
        if not isinstance(item, list):
            continue
        forbidden = "".join(str(part) for part in item)
        if forbidden and forbidden in installer:
            findings.append(f"Installer exposes forbidden remote override: {forbidden}")

    required_installer_terms = [
        "OWNDIFF_LOCAL_ASSET",
        "OWNDIFF_EXPECTED_SHA256",
        'checksum_url="${url}.sha256"',
        "compute_sha256",
        "checksum verification failed",
    ]
    for term in required_installer_terms:
        if term not in installer:
            findings.append(f"Installer missing release-integrity term: {term}")

    required_ci_terms = [
        "sha256sum dist/owndiff > dist/owndiff.sha256",
        "OWNDIFF_LOCAL_ASSET:",
    ]
    for term in required_ci_terms:
        if term not in ci:
            findings.append(f"CI workflow missing release-integrity term: {term}")

    required_release_terms = [
        "dist/${{ matrix.asset }}.sha256",
        'shasum -a 256 "dist/${{ matrix.asset }}" > "dist/${{ matrix.asset }}.sha256"',
        "OWNDIFF_LOCAL_ASSET:",
        "scripts/verify_release_assets.py",
        '--repo "$GITHUB_REPOSITORY"',
        '--tag "$GITHUB_REF_NAME"',
        "release-assets/*",
    ]
    for term in required_release_terms:
        if term not in release:
            findings.append(f"Release workflow missing release-integrity term: {term}")

    required_linux_build_terms = [
        'sha256sum "./dist/${asset}" > "./dist/${asset}.sha256"',
    ]
    for term in required_linux_build_terms:
        if term not in linux_build:
            findings.append(f"Linux release build script missing release-integrity term: {term}")

    required_verifier_terms = [
        "verify_release_dir",
        "verify_github_release",
        "browser_download_url",
        "checksum sidecar",
    ]
    for term in required_verifier_terms:
        if term not in verifier:
            findings.append(f"Release asset verifier missing term: {term}")
    for asset in expected_assets:
        if asset not in verifier:
            findings.append(f"Release asset verifier missing expected asset: {asset}")

    return findings


def audit(
    root: Path,
    policy: dict[str, Any],
    staged: bool,
    should_run_checks: bool,
    scan_history: bool = False,
) -> dict[str, Any]:
    paths = changed_paths(root, staged)
    staged_files = staged_paths(root)
    findings = []
    warnings = []

    forbidden = [str(item) for item in policy.get("forbidden_staged_paths", [])]
    for path in staged_files:
        if matches_any(path, forbidden):
            findings.append(f"Forbidden staged path: {path}")

    for required in policy.get("required_files", []):
        if not (root / str(required)).exists():
            findings.append(f"Required public file missing: {required}")

    compiled_patterns = compile_patterns(policy.get("privacy_patterns", []))
    review_patterns = compile_patterns(policy.get("privacy_review_patterns", []))
    allowlist = compile_allowlist(policy.get("privacy_allowlist", []))
    max_bytes = int(policy.get("max_scan_bytes", 5 * 1024 * 1024))
    scan_paths = sorted(set(paths) | (set(tracked_paths(root)) if scan_history else set()))
    for relative in scan_paths:
        path = root / relative
        if not path.is_file():
            continue
        file_findings, file_warnings, text = scan_file_for_patterns(
            root,
            relative,
            compiled_patterns,
            review_patterns,
            allowlist,
            max_bytes,
        )
        findings.extend(file_findings)
        warnings.extend(file_warnings)
        if text is None:
            continue
        structured = validate_structured_file(path)
        if structured:
            warnings.append(structured) if structured.startswith("PyYAML unavailable") else findings.append(structured)
        skill = validate_skill_frontmatter(path)
        if skill:
            findings.append(skill)
        if path.suffix.lower() == ".md":
            findings.extend(validate_markdown_links(root, path, text))

    findings.extend(version_findings(root, policy.get("version_sources", [])))
    findings.extend(release_integrity_findings(root, policy.get("release_integrity", {})))
    history_checked = 0
    if scan_history:
        history_excluded = [str(item) for item in policy.get("history_excluded_paths", [])]
        history_findings, history_checked = scan_history_for_patterns(
            root,
            compiled_patterns,
            allowlist,
            history_excluded,
            max_bytes,
        )
        findings.extend(history_findings)
    checks = run_checks(root, policy.get("checks", [])) if should_run_checks else []
    return {
        "repo": str(root),
        "mode": ("staged" if staged else "worktree") + ("+history" if scan_history else ""),
        "files_checked": scan_paths,
        "staged_files": staged_files,
        "history_commits_checked": history_checked,
        "findings": findings,
        "warnings": warnings,
        "checks": checks,
        "ok": not findings and all(check.get("ok") for check in checks),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit an OwnDiff commit candidate without modifying it.")
    parser.add_argument("--repo", default=".", help="Repository to audit. Default: current directory.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="Audit policy JSON.")
    parser.add_argument("--staged", action="store_true", help="Audit only staged files.")
    parser.add_argument("--history", action="store_true", help="Scan the tracked tree and all local Git history for leaks.")
    parser.add_argument("--run-checks", action="store_true", help="Run configured command-array checks.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = git_root(Path(args.repo).resolve())
        payload = audit(root, load_policy(Path(args.policy).resolve()), args.staged, args.run_checks, args.history)
    except AuditError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"OwnDiff pre-commit audit: {'PASS' if payload['ok'] else 'FAIL'}")
        print(f"Mode: {payload['mode']}; files checked: {len(payload['files_checked'])}")
        if payload["history_commits_checked"]:
            print(f"History commits checked: {payload['history_commits_checked']}")
        for finding in payload["findings"]:
            print(f"ERROR: {finding}")
        for warning in payload["warnings"]:
            print(f"WARN: {warning}")
        for check in payload["checks"]:
            print(f"{'PASS' if check.get('ok') else 'FAIL'}: {check['name']}")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
