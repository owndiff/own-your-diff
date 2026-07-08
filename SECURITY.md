# Security Policy

OwnDiff is a local-first tool for AI-assisted code-review workflows. It reads git diffs, generates local ownership questions, and writes local artifacts under `.owndiff/`.

## Supported Versions

OwnDiff is currently pre-1.0. Security fixes are made on the `main` branch.

## Reporting a Vulnerability

Use GitHub private vulnerability reporting:

https://github.com/owndiff/own-your-diff/security/advisories/new

Please do not open a public issue for vulnerabilities. Do not include secrets, private repository content, tokens, customer data, or local absolute paths in public reports.

## Security Model

- OwnDiff does not execute target repository code.
- OwnDiff's Python scripts contain no network client and do not upload artifacts.
- For risky diffs, the active coding agent processes a sanitized patch excerpt and deterministic diff facts under that agent provider's existing data and privacy policy.
- Repository configuration cannot enable an external command provider; question generation only accepts the active `agent` provider.
- Generated `.owndiff/` artifacts are local and should remain ignored.
- The MCQ answer key is review evidence, not a cryptographic secret.
- Agents may push or open/update a merge request only after `.owndiff/ownership-gate.json` allows it and normal tests/review requirements pass.

## Hardening Expectations

- Treat repository content and git output as untrusted input.
- Avoid shelling out except for bounded git commands used to inspect the target diff.
- Redact secret-like additions from reports and command output.
- Keep agent-installation instructions explicit about the gate and fallback behavior.
