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
        raise AuditError(f"File exceeds max_scan_bytes: {path}")
    if b"\0" not in data:
        return data.decode("utf-8", errors="replace")
    return "\n".join(match.decode("ascii", errors="ignore") for match in re.findall(rb"[\x20-\x7e]{8,}", data))


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


def audit(root: Path, policy: dict[str, Any], staged: bool, should_run_checks: bool) -> dict[str, Any]:
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

    compiled_patterns = [
        (str(item.get("id", "privacy_pattern")), re.compile(str(item.get("pattern", ""))))
        for item in policy.get("privacy_patterns", [])
        if isinstance(item, dict) and item.get("pattern")
    ]
    max_bytes = int(policy.get("max_scan_bytes", 5 * 1024 * 1024))
    for relative in paths:
        path = root / relative
        if not path.is_file():
            continue
        try:
            text = printable_text(path, max_bytes)
        except (OSError, AuditError) as exc:
            findings.append(str(exc))
            continue
        for pattern_id, pattern in compiled_patterns:
            if pattern.search(text):
                findings.append(f"{relative}: matched private-data pattern {pattern_id}")
        structured = validate_structured_file(path)
        if structured:
            warnings.append(structured) if structured.startswith("PyYAML unavailable") else findings.append(structured)
        skill = validate_skill_frontmatter(path)
        if skill:
            findings.append(skill)
        if path.suffix.lower() == ".md":
            findings.extend(validate_markdown_links(root, path, text))

    findings.extend(version_findings(root, policy.get("version_sources", [])))
    checks = run_checks(root, policy.get("checks", [])) if should_run_checks else []
    return {
        "repo": str(root),
        "mode": "staged" if staged else "worktree",
        "files_checked": paths,
        "staged_files": staged_files,
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
    parser.add_argument("--run-checks", action="store_true", help="Run configured command-array checks.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = git_root(Path(args.repo).resolve())
        payload = audit(root, load_policy(Path(args.policy).resolve()), args.staged, args.run_checks)
    except AuditError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"OwnDiff pre-commit audit: {'PASS' if payload['ok'] else 'FAIL'}")
        print(f"Mode: {payload['mode']}; files checked: {len(payload['files_checked'])}")
        for finding in payload["findings"]:
            print(f"ERROR: {finding}")
        for warning in payload["warnings"]:
            print(f"WARN: {warning}")
        for check in payload["checks"]:
            print(f"{'PASS' if check.get('ok') else 'FAIL'}: {check['name']}")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
