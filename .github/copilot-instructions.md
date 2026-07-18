# Copilot instructions for interactive VS Code sessions

These instructions apply to interactive GitHub Copilot sessions in VS Code working
on this repository.

## Commit and push policy

The branch/commit rules in [`CLAUDE.md`](../CLAUDE.md) (the tiered "push directly to
main" permissions, the PR workflow, etc.) describe how the **deployed Retinue
runtime** (Ara / Claude Code inside the container) operates. **They do not apply to
interactive Copilot sessions.**

In an interactive session:

- Make and edit files freely in the working tree.
- **Do not commit and do not push.** The user commits and pushes themselves.
- Only run `git commit` / `git push` when the user explicitly asks for it in the
  current session.

When work is complete, summarize what changed and leave staging, committing, and
pushing to the user.
