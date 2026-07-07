from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import SCHEMA_VERSION, safe_relpath, utc_now, write_json
from .config import load_config
from .git_utils import current_sha, git_root, run_git, short_status


def detect_language(path: str, config: dict[str, Any] | None = None) -> str:
    config = config or _default_config()
    extensions = config.get("diff", {}).get("language_extensions", {})
    suffix = Path(path).suffix.lower()
    return str(extensions.get(suffix, "unknown"))


def is_test_file(path: str, config: dict[str, Any] | None = None) -> bool:
    config = config or _default_config()
    test_config = config.get("diff", {}).get("test_file", {})
    normalized = path.lower().replace("\\", "/")
    name = Path(normalized).name
    return any(marker in normalized for marker in test_config.get("path_markers", [])) or any(
        name.startswith(prefix) for prefix in test_config.get("name_prefixes", [])
    ) or any(name.endswith(suffix) for suffix in test_config.get("name_suffixes", [])) or any(
        marker in name for marker in test_config.get("name_contains", [])
    )


def _default_config() -> dict[str, Any]:
    config, _warnings, _sources = load_config(Path.cwd())
    return config


def _range_args(base: str | None, head: str, staged: bool) -> tuple[str, list[str]]:
    if base:
        return "range", [f"{base}...{head}"]
    if staged:
        return "staged", ["--cached"]
    return "working-tree", ["HEAD"]


def _parse_name_status(raw: str, exclude_prefixes: list[str]) -> dict[str, dict[str, str | None]]:
    result: dict[str, dict[str, str | None]] = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        if status.startswith(("R", "C")) and len(parts) >= 3:
            old_path = safe_relpath(parts[1])
            path = safe_relpath(parts[2])
            if not _is_internal_artifact(path, exclude_prefixes):
                result[path] = {"status": status[0], "old_path": old_path}
        elif len(parts) >= 2:
            path = safe_relpath(parts[-1])
            if not _is_internal_artifact(path, exclude_prefixes):
                result[path] = {"status": status[0], "old_path": None}
    return result


def _parse_numstat(raw: str, exclude_prefixes: list[str]) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        path = safe_relpath(parts[-1])
        if _is_internal_artifact(path, exclude_prefixes):
            continue
        additions = 0 if parts[0] == "-" else int(parts[0])
        deletions = 0 if parts[1] == "-" else int(parts[1])
        result[path] = {"additions": additions, "deletions": deletions}
    return result


def _untracked_files(root: Path, include: bool, exclude_prefixes: list[str]) -> list[str]:
    if not include:
        return []
    proc = run_git(root, ["ls-files", "--others", "--exclude-standard"], allow_error=True)
    if proc.returncode != 0:
        return []
    paths = [safe_relpath(line) for line in proc.stdout.splitlines() if line.strip()]
    return [path for path in paths if not _is_internal_artifact(path, exclude_prefixes)]


def _is_internal_artifact(path: str, exclude_prefixes: list[str]) -> bool:
    normalized = path.replace("\\", "/").lstrip("/")
    return any(normalized.startswith(prefix) for prefix in exclude_prefixes)


def _read_untracked_text(path: Path, max_bytes: int = 1_000_000) -> tuple[list[str], bool]:
    raw = path.read_bytes()
    truncated = len(raw) > max_bytes
    if truncated:
        raw = raw[:max_bytes]
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if text.endswith("\n"):
        pass
    elif text:
        lines[-1] = lines[-1]
    return lines, truncated


def _synthetic_untracked_patch(root: Path, path: str) -> tuple[str, int]:
    file_path = root / path
    if not file_path.is_file():
        return "", 0
    lines, truncated = _read_untracked_text(file_path)
    patch_lines = [
        f"diff --git a/{path} b/{path}",
        "new file mode 100644",
        "index 0000000..0000000",
        "--- /dev/null",
        f"+++ b/{path}",
        f"@@ -0,0 +1,{len(lines)} @@",
    ]
    patch_lines.extend(f"+{line}" for line in lines)
    if truncated:
        patch_lines.append("+[owndiff: file truncated at 1000000 bytes]")
    return "\n".join(patch_lines) + "\n", len(lines)


def collect_diff(
    repo: str | Path,
    out_path: str | Path,
    patch_out: str | Path,
    base: str | None = None,
    head: str = "HEAD",
    staged: bool = False,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    root = git_root(repo)
    config, warnings, config_sources = load_config(root, config_path)
    exclude_prefixes = [str(prefix) for prefix in config.get("diff", {}).get("internal_exclude_prefixes", [])]
    mode, range_args = _range_args(base, head, staged)

    name_proc = run_git(root, ["diff", "--name-status", "--find-renames", *range_args])
    num_proc = run_git(root, ["diff", "--numstat", "--find-renames", *range_args])
    patch_proc = run_git(
        root,
        ["diff", "--no-ext-diff", "--find-renames", "--src-prefix=a/", "--dst-prefix=b/", *range_args],
        timeout=30,
    )

    name_status = _parse_name_status(name_proc.stdout, exclude_prefixes)
    numstat = _parse_numstat(num_proc.stdout, exclude_prefixes)
    synthetic_patches = []
    for path in _untracked_files(root, include=mode == "working-tree", exclude_prefixes=exclude_prefixes):
        if path in name_status:
            continue
        patch_text, additions = _synthetic_untracked_patch(root, path)
        name_status[path] = {"status": "A", "old_path": None}
        numstat[path] = {"additions": additions, "deletions": 0}
        if patch_text:
            synthetic_patches.append(patch_text)
    all_paths = sorted(set(name_status) | set(numstat))

    changed_files = []
    for path in all_paths:
        status_info = name_status.get(path, {"status": "M", "old_path": None})
        stat_info = numstat.get(path, {"additions": 0, "deletions": 0})
        changed_files.append(
            {
                "path": path,
                "old_path": status_info.get("old_path"),
                "status": status_info.get("status") or "M",
                "additions": stat_info["additions"],
                "deletions": stat_info["deletions"],
                "language": detect_language(path, config),
                "is_test": is_test_file(path, config),
            }
        )

    patch_path = Path(patch_out)
    if not patch_path.is_absolute():
        patch_path = root / patch_path
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    combined_patch = patch_proc.stdout
    if synthetic_patches:
        combined_patch = combined_patch + ("\n" if combined_patch else "") + "\n".join(synthetic_patches)
    patch_path.write_text(combined_patch, encoding="utf-8")

    total_additions = sum(item["additions"] for item in changed_files)
    total_deletions = sum(item["deletions"] for item in changed_files)
    payload: dict[str, Any] = {
        "schema_version": f"{SCHEMA_VERSION}.diff",
        "created_at": utc_now(),
        "repo": str(root),
        "mode": mode,
        "base_ref": base,
        "head_ref": head,
        "commit_sha": current_sha(root),
        "config_sources": config_sources,
        "warnings": warnings,
        "patch_path": str(patch_path),
        "status": short_status(root),
        "changed_files": changed_files,
        "summary": {
            "files_changed": len(changed_files),
            "insertions": total_additions,
            "deletions": total_deletions,
            "lines_changed": total_additions + total_deletions,
        },
    }

    write_json(Path(out_path), payload)
    return payload
