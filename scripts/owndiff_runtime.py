#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - OwnDiff requires Python 3.11+.
    tomllib = None  # type: ignore[assignment]


SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_VENV_PREFIX = "owndiff-runtime"
REQUIRED_IMPORTS = ("yaml",)
COMMANDS = {
    "run": "run_owndiff.py",
    "run-owndiff": "run_owndiff.py",
    "run_owndiff": "run_owndiff.py",
    "install-agent-rules": "install_agent_rules.py",
    "install_agent_rules": "install_agent_rules.py",
    "quiz-web": "quiz_web.py",
    "quiz_web": "quiz_web.py",
}


class RuntimeErrorMessage(RuntimeError):
    pass


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print_help()
        return 0

    command_name = args.pop(0)
    script_name = COMMANDS.get(command_name)
    if script_name is None:
        print(f"error: unknown OwnDiff command: {command_name}", file=sys.stderr)
        print_help(file=sys.stderr)
        return 2

    bootstrap_python = Path(sys.executable)
    if script_name == "install_agent_rules.py" and not has_python_command(args):
        args.extend(["--python-command", str(bootstrap_python)])

    script_path = SKILL_DIR / "scripts" / script_name
    if not script_path.exists():
        print(f"error: OwnDiff command script is missing: {script_name}", file=sys.stderr)
        return 2

    try:
        python = ensure_python()
    except RuntimeErrorMessage as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    os.execv(str(python), [str(python), str(script_path), *args])
    return 2


def print_help(file: object = sys.stdout) -> None:
    commands = ", ".join(sorted(COMMANDS))
    print(
        "usage: owndiff_runtime.py <command> [args...]\n\n"
        "Runs OwnDiff scripts with a Python runtime that has the required dependencies.\n"
        "Uses the current interpreter when possible; otherwise creates or reuses a venv\n"
        f"under {runtime_root()}/{DEFAULT_VENV_PREFIX}-<hash>.\n\n"
        f"commands: {commands}\n\n"
        "environment:\n"
        "  OWNDIFF_RUNTIME_VENV=/tmp/custom-owndiff-venv  override the venv path\n"
        "  OWNDIFF_RUNTIME_FORCE_VENV=1                  always use the venv\n",
        file=file,
    )


def has_python_command(args: list[str]) -> bool:
    return any(arg == "--python-command" or arg.startswith("--python-command=") for arg in args)


def ensure_python() -> Path:
    if os.environ.get("OWNDIFF_RUNTIME_FORCE_VENV") != "1" and interpreter_is_ready(Path(sys.executable)):
        return Path(sys.executable)

    venv_dir = runtime_venv_path()
    python = venv_python(venv_dir)
    if not python.exists():
        print(f"OwnDiff runtime: creating virtualenv at {venv_dir}", file=sys.stderr, flush=True)
        run_checked([sys.executable, "-m", "venv", str(venv_dir)], "create runtime virtualenv")
    else:
        print(f"OwnDiff runtime: reusing virtualenv at {venv_dir}", file=sys.stderr, flush=True)

    if not interpreter_is_ready(python):
        install_dependencies(python, venv_dir)

    if not interpreter_is_ready(python):
        raise RuntimeErrorMessage(f"runtime virtualenv is missing required dependencies: {venv_dir}")
    return python


def runtime_venv_path() -> Path:
    override = os.environ.get("OWNDIFF_RUNTIME_VENV")
    if override:
        return Path(override).expanduser()
    digest = hashlib.sha256(str(SKILL_DIR.resolve()).encode("utf-8")).hexdigest()[:12]
    return runtime_root() / f"{DEFAULT_VENV_PREFIX}-{digest}"


def runtime_root() -> Path:
    if os.name == "nt":
        return Path(tempfile.gettempdir())
    return Path("/tmp")


def venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def interpreter_is_ready(python: Path) -> bool:
    if not python.exists():
        return False
    probe = "; ".join(f"import {module}" for module in REQUIRED_IMPORTS)
    proc = subprocess.run([str(python), "-c", probe], text=True, capture_output=True, check=False)
    return proc.returncode == 0


def install_dependencies(python: Path, venv_dir: Path) -> None:
    dependencies = project_dependencies()
    if not dependencies:
        raise RuntimeErrorMessage("pyproject.toml does not list runtime dependencies")
    print(f"OwnDiff runtime: installing dependencies into {venv_dir}", file=sys.stderr, flush=True)
    run_checked(
        [str(python), "-m", "pip", "install", "--disable-pip-version-check", *dependencies],
        "install runtime dependencies",
    )


def project_dependencies() -> list[str]:
    if tomllib is None:
        raise RuntimeErrorMessage("Python 3.11+ is required to read OwnDiff runtime dependencies")
    pyproject = SKILL_DIR / "pyproject.toml"
    with pyproject.open("rb") as handle:
        data = tomllib.load(handle)
    dependencies = data.get("project", {}).get("dependencies", [])
    if not isinstance(dependencies, list) or not all(isinstance(item, str) for item in dependencies):
        raise RuntimeErrorMessage("pyproject.toml project.dependencies must be a list of strings")
    return dependencies


def run_checked(command: list[str], action: str) -> None:
    try:
        subprocess.run(command, cwd=SKILL_DIR, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeErrorMessage(f"could not {action}: {exc}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
