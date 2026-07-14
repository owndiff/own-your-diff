#!/usr/bin/env python3
from __future__ import annotations

import argparse
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a standalone OwnDiff executable with PyInstaller.")
    parser.add_argument("--name", default="owndiff", help="Executable name. Default: owndiff.")
    parser.add_argument("--dist-dir", default="dist", help="Distribution directory. Default: dist.")
    parser.add_argument("--work-dir", default="build/pyinstaller", help="PyInstaller work directory.")
    parser.add_argument("--no-clean", action="store_true", help="Keep PyInstaller's previous build cache.")
    parser.add_argument("--one-dir", action="store_true", help="Build a directory instead of a single-file executable.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    probe = subprocess.run([sys.executable, "-m", "PyInstaller", "--version"], capture_output=True, text=True, check=False)
    if probe.returncode != 0:
        print("error: pyinstaller is not installed in this Python environment", file=sys.stderr)
        print("install it with: python -m pip install pyinstaller", file=sys.stderr)
        return 2

    separator = ";" if platform.system() == "Windows" else ":"
    dist_dir = ROOT / args.dist_dir
    work_dir = ROOT / args.work_dir
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--name",
        args.name,
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(work_dir),
        "--specpath",
        str(work_dir),
        "--paths",
        str(ROOT / "scripts"),
        "--add-data",
        f"{ROOT / 'configs' / 'default_config.yaml'}{separator}configs",
        "--add-data",
        f"{ROOT / 'configs' / 'agent_install.yaml'}{separator}configs",
        "--hidden-import",
        "yaml",
    ]
    if not args.no_clean:
        command.append("--clean")
    command.append("--onedir" if args.one_dir else "--onefile")
    command.append(str(ROOT / "scripts" / "owndiff_cli.py"))

    subprocess.run(command, cwd=ROOT, check=True)
    binary = dist_dir / args.name
    if platform.system() == "Windows":
        binary = binary.with_suffix(".exe")
    print(binary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
