# Contributing to OwnDiff

Thanks for helping improve OwnDiff. Keep contributions focused, local-first, and safe for repositories that may contain private code.

Please follow the [Code of Conduct](CODE_OF_CONDUCT.md) in issues, pull requests, discussions, and support conversations connected to the project.

## Development Setup

```bash
git clone https://github.com/owndiff/own-your-diff.git
cd owndiff
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e . pytest ruff
```

## Before Opening a Pull Request

Run:

```bash
ruff check .
pytest
```

For behavior changes, add or update tests. For agent-installation changes, verify the generated instructions with:

```bash
python scripts/install_agent_rules.py --repo ../test-repo --agents all --verify --python-command "$(command -v python)"
```

## Security and Privacy

- Do not commit generated `.owndiff/` artifacts.
- Do not paste private source code, secrets, tokens, customer data, or local absolute paths into issues, PRs, tests, or docs.
- Treat target repository files and diffs as untrusted input.
- Keep OwnDiff local-first; do not add network calls unless the feature explicitly requires them and the README documents the behavior.

## Design Principles

- Prefer configuration-driven rules over hardcoded project assumptions.
- Keep scripts small, deterministic, and composable.
- Avoid executing target repository code.
- Keep agent-facing instructions simple enough to follow under pressure.
