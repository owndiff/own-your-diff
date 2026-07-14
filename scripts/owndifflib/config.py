from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

from .common import OwnDiffError

REPO_CONFIG_NAMES = (".owndiff.yml", ".owndiff.yaml", ".owndiff.json")


def bundled_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))


def default_config_path() -> Path:
    return bundled_root() / "configs" / "default_config.yaml"


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(repo: str | Path, explicit_config: str | Path | None = None) -> tuple[dict[str, Any], list[str], list[str]]:
    root = Path(repo).resolve()
    warnings: list[str] = []
    sources: list[str] = []

    default_path = default_config_path()
    config = _read_mapping(default_path)
    sources.append(str(default_path))

    for candidate in _override_candidates(root, explicit_config):
        if not candidate.exists():
            continue
        override = _read_mapping(candidate)
        config = deep_merge(config, override)
        sources.append(str(candidate))
        break

    return config, warnings, sources


def _override_candidates(root: Path, explicit_config: str | Path | None) -> list[Path]:
    if explicit_config:
        return [Path(explicit_config).expanduser().resolve()]
    return [root / name for name in REPO_CONFIG_NAMES]


def _read_mapping(path: Path) -> dict[str, Any]:
    try:
        if path.suffix == ".json":
            loaded = json.loads(path.read_text(encoding="utf-8"))
        else:
            import yaml

            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OwnDiffError(f"Config not found: {path}") from exc
    except Exception as exc:  # noqa: BLE001 - config parse errors should be user-facing.
        raise OwnDiffError(f"Failed to load config {path}: {exc}") from exc

    if not isinstance(loaded, dict):
        raise OwnDiffError(f"Config {path} must be a mapping")
    return loaded


def load_policy(repo: str | Path, explicit_policy: str | Path | None = None) -> tuple[dict[str, Any], list[str]]:
    config, warnings, _sources = load_config(repo, explicit_policy)
    return config, warnings
