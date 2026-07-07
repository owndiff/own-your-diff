from __future__ import annotations

import fnmatch


def matches_pattern(path: str, pattern: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("/")
    pat = pattern.replace("\\", "/").lstrip("/")
    variants = {pat}
    if pat.startswith("**/"):
        variants.add(pat[3:])
    if "/" not in pat:
        variants.add(f"**/{pat}")
    return any(fnmatch.fnmatchcase(normalized, variant) for variant in variants)


def matches_any(path: str, patterns: list[str]) -> bool:
    return any(matches_pattern(path, pattern) for pattern in patterns)
