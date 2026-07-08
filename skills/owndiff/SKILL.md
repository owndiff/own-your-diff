---
name: owndiff
description: Verify human ownership of AI-assisted code changes by analyzing git diffs, generating easy diff-grounded MCQs with the active agent model, and blocking risky pushes or merge requests until the human passes the terminal gate.
---

# OwnDiff

Read and follow the complete OwnDiff instructions in `${CLAUDE_PLUGIN_ROOT}/SKILL.md`.

Resolve bundled commands and configuration from `${CLAUDE_PLUGIN_ROOT}`. Never substitute another checkout, use web search for question generation, print the answer key, or allow a push or merge request while the OwnDiff gate is blocked.
