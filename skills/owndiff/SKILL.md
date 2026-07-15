---
name: owndiff
description: Verify human ownership of AI-assisted source-code changes by analyzing git diffs, generating easy diff-grounded multiple choice questions with the active agent model, and blocking risky pushes or merge requests until the human passes the ownership gate. Documentation and other non-source-only changes receive a report without multiple choice questions or gate artifacts.
---

# OwnDiff

Read and follow the complete OwnDiff instructions in `${CLAUDE_PLUGIN_ROOT}/SKILL.md`.

Use the installed `owndiff` executable as the normal review flow. If `owndiff` is not available on `PATH`, tell the user to install the OwnDiff CLI before enforcing this skill. Run `owndiff run --repo . --out-dir .owndiff`; each run starts a fresh review for the current diff and clears old local answers, gates, prompts, reports, and stale canonical LLM responses before writing current artifacts. When questions are pending, it starts a localhost server and opens the human's default browser. The browser review shows hints by default, lets the human retry before submitting, attempts to close itself after submission, then exits back to the same terminal session and can best-effort refocus known macOS terminal apps. Never launch the quiz in a detached/background terminal or second agent console. Never route the human to a separate multiple choice question command. Never substitute another checkout, use web search for question generation, print the answer key, or allow a push or merge request while the OwnDiff gate is blocked.
