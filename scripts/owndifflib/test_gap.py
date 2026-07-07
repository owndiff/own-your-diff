from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import SCHEMA_VERSION, read_json, utc_now, write_json
from .config import load_config
from .diff_collect import is_test_file


def candidate_tests(path: str, config: dict[str, Any]) -> list[str]:
    p = Path(path)
    stem = p.stem
    suffix = p.suffix
    parent = p.parent.as_posix()
    name = p.name
    parts = path.split("/")
    package = parts[1].replace("-", "_") if len(parts) >= 3 and parts[0] == "src" else ""
    stripped_path = path
    candidates: list[str] = []

    def add(value: str) -> None:
        normalized = value.replace("\\", "/").lstrip("./")
        if normalized not in candidates:
            candidates.append(normalized)

    test_gap_config = config.get("test_gap", {})
    context = {
        "path": path,
        "parent": parent,
        "stem": stem,
        "suffix": suffix,
        "name": name,
        "package": package,
        "stripped_path": stripped_path,
    }
    for pattern in test_gap_config.get("candidate_patterns", {}).get(suffix, test_gap_config.get("default_candidate_patterns", [])):
        if "{package}" in pattern and not package:
            continue
        add(pattern.format(**context))

    for prefix in test_gap_config.get("source_prefixes", []):
        if path.startswith(prefix):
            context["stripped_path"] = path[len(prefix) :]
            for pattern in test_gap_config.get("source_prefix_test_patterns", []):
                add(pattern.format(**context))
            break
    return candidates


def scan_test_gaps(
    repo: str | Path,
    diff_path: str | Path,
    out_path: str | Path,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(repo).resolve()
    config, warnings, config_sources = load_config(root, config_path)
    diff = read_json(Path(diff_path))
    changed_files = diff.get("changed_files", [])
    code_languages = set(config.get("test_gap", {}).get("code_languages", []))
    changed_tests = [item["path"] for item in changed_files if item.get("is_test") or is_test_file(item.get("path", ""), config)]
    changed_code = [
        item["path"]
        for item in changed_files
        if item.get("language") in code_languages and not (item.get("is_test") or is_test_file(item.get("path", ""), config))
    ]

    missing = []
    matched_existing: dict[str, list[str]] = {}
    changed_tests_set = set(changed_tests)
    for path in changed_code:
        candidates = candidate_tests(path, config)
        existing = [candidate for candidate in candidates if (root / candidate).exists()]
        candidate_changed = [candidate for candidate in candidates if candidate in changed_tests_set]
        matched_existing[path] = existing
        if not existing and not candidate_changed:
            missing.append({"path": path, "expected": candidates[:6], "matched_existing": []})

    payload: dict[str, Any] = {
        "schema_version": f"{SCHEMA_VERSION}.tests",
        "created_at": utc_now(),
        "repo": str(root),
        "config_sources": config_sources,
        "warnings": warnings,
        "changed_code_files": changed_code,
        "changed_test_files": changed_tests,
        "test_gap": bool(changed_code and not changed_tests and missing),
        "missing_test_candidates": missing,
        "matched_existing_tests": matched_existing,
        "summary": {
            "code_files_changed": len(changed_code),
            "test_files_changed": len(changed_tests),
            "files_without_nearby_tests": len(missing),
        },
    }
    write_json(Path(out_path), payload)
    return payload
