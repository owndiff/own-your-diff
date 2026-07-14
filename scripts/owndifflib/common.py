from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "owndiff.v1"


class OwnDiffError(RuntimeError):
    """Expected error with a user-actionable message."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()  # noqa: UP017


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OwnDiffError(f"Required input not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise OwnDiffError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise OwnDiffError(f"Expected JSON object in {path}")
    return data


def write_json(path: Path, payload: dict[str, Any]) -> None:
    ensure_parent(path)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def safe_relpath(path: str) -> str:
    cleaned = path.replace("\\", "/").lstrip("/")
    parts = [part for part in cleaned.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise OwnDiffError(f"Unsafe repository path in git output: {path!r}")
    return "/".join(parts)


def as_path(raw: str | Path) -> Path:
    return Path(raw).expanduser().resolve()
