#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from owndifflib.config import bundled_root

BEGIN_MARKER = "<!-- BEGIN OWNDIFF AGENT RULE -->"
END_MARKER = "<!-- END OWNDIFF AGENT RULE -->"
SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = bundled_root() / "configs" / "agent_install.yaml"


class InstallError(RuntimeError):
    pass


def is_bundled() -> bool:
    return bool(getattr(sys, "_MEIPASS", None))


def default_owndiff_command(python_command: str) -> str:
    return "owndiff"


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise InstallError(f"Config must be a YAML object: {path}")
    return data


def render(value: str, context: dict[str, str]) -> str:
    rendered = value
    for key, replacement in context.items():
        rendered = rendered.replace("{{" + key + "}}", replacement)
    return rendered


def render_action(action: dict[str, Any], context: dict[str, str]) -> dict[str, str]:
    rendered: dict[str, str] = {}
    for key, value in action.items():
        if isinstance(value, str):
            rendered[key] = render(value, context)
    return rendered


def replace_marked_block(existing: str, block: str) -> str:
    start = existing.find(BEGIN_MARKER)
    end = existing.find(END_MARKER)
    if start != -1 and end != -1 and end > start:
        end += len(END_MARKER)
        prefix = existing[:start].rstrip()
        suffix = existing[end:].lstrip()
        parts = [part for part in [prefix, block.rstrip(), suffix] if part]
        return "\n\n".join(parts) + "\n"

    if existing.strip():
        return existing.rstrip() + "\n\n" + block.rstrip() + "\n"
    return block.rstrip() + "\n"


def write_file(path: Path, content: str, dry_run: bool) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def append_marked(path: Path, content: str, dry_run: bool) -> None:
    block = f"{BEGIN_MARKER}\n{content.rstrip()}\n{END_MARKER}\n"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    updated = replace_marked_block(existing, block)
    write_file(path, updated, dry_run)


def create_symlink(path: Path, target: Path, force: bool, dry_run: bool) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink() and path.resolve() == target.resolve():
            return
        if not force:
            raise InstallError(f"Refusing to replace existing path without --force: {path}")
        if path.is_dir() and not path.is_symlink():
            raise InstallError(f"Refusing to replace existing directory: {path}")
        if not dry_run:
            path.unlink()

    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target, target_is_directory=True)


def verify_action(repo: Path, action: dict[str, str]) -> dict[str, Any]:
    action_type = action["type"]
    relative_path = action["path"]
    path = repo / relative_path
    result: dict[str, Any] = {"type": action_type, "path": relative_path, "ok": False}
    if action.get("skipped"):
        result["ok"] = True
        result["skipped"] = action["skipped"]
        return result

    if action_type == "symlink":
        target = Path(action["target"])
        result["target"] = str(target)
        result["ok"] = path.is_symlink() and path.resolve() == target.resolve() and (path / "SKILL.md").exists()
        return result

    if action_type in {"write_file", "append_marked"}:
        if not path.exists():
            result["ok"] = False
            return result
        text = path.read_text(encoding="utf-8")
        required = [
            "run --repo",
            "agent_may_push_merge_request",
        ]
        if action_type == "append_marked":
            required.extend([BEGIN_MARKER, END_MARKER])
        result["ok"] = all(item in text for item in required)
        return result

    result["error"] = f"Unknown action type: {action_type}"
    return result


def run_command_check(owndiff_command: str) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    base_command = shlex.split(owndiff_command)
    commands = [
        [*base_command, "--help"],
        [*base_command, "run", "--help"],
        [*base_command, "install-agent-rules", "--help"],
    ]
    for command in commands:
        proc = subprocess.run(command, cwd=SKILL_DIR, text=True, capture_output=True, check=False)
        checks.append(
            {
                "command": command,
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "stderr": proc.stderr.strip(),
            }
        )
    return checks


def select_agents(config: dict[str, Any], requested: str) -> list[str]:
    agents = config.get("agents")
    if not isinstance(agents, dict):
        raise InstallError("Config missing agents map")

    if requested == "all":
        selected = config.get("default_agents", [])
    else:
        selected = [item.strip() for item in requested.split(",") if item.strip()]

    unknown = [agent for agent in selected if agent not in agents]
    if unknown:
        raise InstallError(f"Unknown agent(s): {', '.join(unknown)}")
    return selected


def install_agents(
    repo: Path,
    config: dict[str, Any],
    selected: list[str],
    python_command: str,
    owndiff_command: str,
    force: bool,
    dry_run: bool,
    skip_skill_links: bool = False,
) -> dict[str, Any]:
    rule_body_template = config.get("rule_body")
    if not isinstance(rule_body_template, str):
        raise InstallError("Config missing rule_body")

    context = {
        "skill_dir": str(SKILL_DIR),
        "repo": str(repo),
        "python_command": python_command,
        "owndiff_command": owndiff_command,
    }
    context["rule_body"] = render(rule_body_template, context)

    summary: dict[str, Any] = {
        "repo": str(repo),
        "skill_dir": str(SKILL_DIR),
        "agents": [],
        "dry_run": dry_run,
    }

    agents = config["agents"]
    for agent_name in selected:
        agent = agents[agent_name]
        actions = agent.get("actions", [])
        if not isinstance(actions, list):
            raise InstallError(f"Agent actions must be a list: {agent_name}")

        agent_summary = {
            "name": agent_name,
            "description": agent.get("description", ""),
            "docs_url": agent.get("docs_url", ""),
            "actions": [],
        }

        for raw_action in actions:
            if not isinstance(raw_action, dict):
                raise InstallError(f"Invalid action for {agent_name}")
            action = render_action(raw_action, context)
            action_type = action.get("type")
            if action_type == "symlink" and skip_skill_links:
                action.pop("target", None)
                action["skipped"] = "standalone executable mode"
                agent_summary["actions"].append(action)
                continue
            if action_type == "write_file":
                write_file(repo / action["path"], action["content"], dry_run)
            elif action_type == "append_marked":
                append_marked(repo / action["path"], action["content"], dry_run)
            elif action_type == "symlink":
                create_symlink(repo / action["path"], Path(action["target"]), force, dry_run)
            else:
                raise InstallError(f"Unknown action type for {agent_name}: {action_type}")
            agent_summary["actions"].append(action)

        summary["agents"].append(agent_summary)

    return summary


def verify_install(repo: Path, summary: dict[str, Any], owndiff_command: str, skip_command_check: bool) -> dict[str, Any]:
    verification = {"agents": [], "command_checks": []}
    for agent in summary["agents"]:
        agent_result = {"name": agent["name"], "actions": []}
        for action in agent["actions"]:
            agent_result["actions"].append(verify_action(repo, action))
        verification["agents"].append(agent_result)

    if not skip_command_check:
        verification["command_checks"] = run_command_check(owndiff_command)

    return verification


def all_verified(verification: dict[str, Any]) -> bool:
    for agent in verification["agents"]:
        if not all(action.get("ok") for action in agent["actions"]):
            return False
    return all(check.get("ok") for check in verification.get("command_checks", []))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install and verify OwnDiff rule files for coding agents.")
    parser.add_argument("--repo", default=".", help="Target repository. Default: current directory.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Agent install config YAML.")
    parser.add_argument("--agents", default="all", help="Comma-separated agent names or 'all'.")
    parser.add_argument(
        "--python-command",
        default=sys.executable,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--owndiff-command",
        help="OwnDiff command to put in generated rules. Defaults to 'owndiff' in the standalone executable.",
    )
    parser.add_argument(
        "--skip-skill-links",
        action="store_true",
        help="Skip project skill symlinks; useful when installing rules from the standalone executable.",
    )
    parser.add_argument("--force", action="store_true", help="Replace existing non-matching symlinks or files when safe.")
    parser.add_argument("--dry-run", action="store_true", help="Preview actions without writing files.")
    parser.add_argument("--verify", action="store_true", help="Verify installed files and OwnDiff command entry points.")
    parser.add_argument("--verify-only", action="store_true", help="Verify files without installing first.")
    parser.add_argument("--skip-command-check", action="store_true", help="Skip OwnDiff --help command checks.")
    parser.add_argument("--list-agents", action="store_true", help="List available configured agents.")
    parser.add_argument("--json", action="store_true", help="Write machine-readable JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        repo = Path(args.repo).resolve()
        config = load_yaml(Path(args.config).resolve())
        agents = config.get("agents", {})

        if args.list_agents:
            payload = {"agents": sorted(agents)}
            print(json.dumps(payload, indent=2) if args.json else "\n".join(payload["agents"]))
            return 0

        selected = select_agents(config, args.agents)
        owndiff_command = args.owndiff_command or default_owndiff_command(args.python_command)
        skip_skill_links = args.skip_skill_links or is_bundled()
        if args.verify_only:
            summary = install_agents(
                repo,
                config,
                selected,
                args.python_command,
                owndiff_command,
                args.force,
                dry_run=True,
                skip_skill_links=skip_skill_links,
            )
        else:
            summary = install_agents(
                repo,
                config,
                selected,
                args.python_command,
                owndiff_command,
                args.force,
                args.dry_run,
                skip_skill_links=skip_skill_links,
            )

        if args.verify or args.verify_only:
            summary["verification"] = verify_install(repo, summary, owndiff_command, args.skip_command_check)
            summary["verified"] = all_verified(summary["verification"])
            exit_code = 0 if summary["verified"] else 3
        else:
            exit_code = 0

        if args.json:
            print(json.dumps(summary, indent=2, sort_keys=True))
        else:
            for agent in summary["agents"]:
                print(f"{agent['name']}:")
                for action in agent["actions"]:
                    print(f"  {action['type']} {action['path']}")
            if "verified" in summary:
                print(f"verified: {str(summary['verified']).lower()}")
        return exit_code
    except (OSError, InstallError, yaml.YAMLError, subprocess.SubprocessError) as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
