#!/usr/bin/env python3
from __future__ import annotations

import sys
from collections.abc import Callable

from owndifflib import __version__

COMMANDS: dict[str, tuple[str, Callable[[list[str] | None], int]]] = {}


def _load_commands() -> dict[str, tuple[str, Callable[[list[str] | None], int]]]:
    if COMMANDS:
        return COMMANDS

    from collect_diff import main as collect_diff_main
    from generate_mcq import main as generate_mcq_main
    from generate_questions import main as generate_questions_main
    from generate_report import main as generate_report_main
    from install_agent_rules import main as install_agent_rules_main
    from risk_score import main as risk_score_main
    from run_owndiff import main as run_main
    from test_gap_scan import main as test_gap_main

    COMMANDS.update(
        {
            "run": ("Run the complete ownership-check pipeline.", run_main),
            "collect-diff": ("Collect git diff facts.", collect_diff_main),
            "test-gap": ("Detect test coverage signals.", test_gap_main),
            "risk-score": ("Score ownership risk.", risk_score_main),
            "generate-questions": ("Prepare or validate agent-written ownership questions.", generate_questions_main),
            "generate-mcq": ("Generate MCQ artifacts and initial gate.", generate_mcq_main),
            "generate-report": ("Write the ownership report.", generate_report_main),
            "install-agent-rules": ("Install OwnDiff project rules for coding agents.", install_agent_rules_main),
        }
    )
    return COMMANDS


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print_help()
        return 0
    if args[0] in {"--version", "-V"}:
        print(f"owndiff {__version__}")
        return 0

    command = args.pop(0)
    commands = _load_commands()
    if command not in commands:
        print(f"error: unknown OwnDiff command: {command}", file=sys.stderr)
        print_help(file=sys.stderr)
        return 2
    original_argv = sys.argv[:]
    try:
        sys.argv = [f"owndiff {command}", *args]
        return commands[command][1](args)
    finally:
        sys.argv = original_argv


def print_help(file: object = sys.stdout) -> None:
    print(
        "usage: owndiff <command> [args...]\n\n"
        "Local human ownership gate for AI-assisted source-code diffs.\n\n"
        "common commands:\n"
        "  run                run diff analysis and browser review when needed\n"
        "  install-agent-rules install project rules for supported coding agents\n\n"
        "other commands:",
        file=file,
    )
    for name, (description, _main) in sorted(_load_commands().items()):
        print(f"  {name:<18} {description}", file=file)
    print("\nUse 'owndiff <command> --help' for command options.", file=file)


if __name__ == "__main__":
    raise SystemExit(main())
