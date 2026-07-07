from __future__ import annotations

import subprocess
from pathlib import Path

from .common import OwnDiffError, as_path


def run_git(repo: Path, args: list[str], timeout: int = 15, allow_error: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            text=True,
            capture_output=True,
            timeout=timeout,
            shell=False,
            check=False,
        )
    except FileNotFoundError as exc:
        raise OwnDiffError("git is required but was not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise OwnDiffError(f"git {' '.join(args)} timed out after {timeout}s") from exc

    if proc.returncode != 0 and not allow_error:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit code {proc.returncode}"
        raise OwnDiffError(f"git {' '.join(args)} failed: {detail}")
    return proc


def git_root(repo: str | Path) -> Path:
    requested = as_path(repo)
    proc = run_git(requested, ["rev-parse", "--show-toplevel"])
    return Path(proc.stdout.strip()).resolve()


def current_sha(repo: Path) -> str | None:
    proc = run_git(repo, ["rev-parse", "--verify", "HEAD"], allow_error=True)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def short_status(repo: Path) -> list[str]:
    proc = run_git(repo, ["status", "--short"], allow_error=True)
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]
