# Copilot instructions

These instructions apply to GitHub Copilot sessions working on this repository.
This project uses two modes: interactive editing sessions in VS Code, and an
automated coding agent that works on assigned issues or pull requests.

## Interactive VS Code sessions

In an interactive session you are editing with the user present:

- Make and edit files freely in the working tree.
- **Do not commit and do not push.** The user commits and pushes themselves.
- Only run `git commit` / `git push` when the user explicitly asks for it in the
  current session.

When work is complete, summarize what changed and leave staging, committing, and
pushing to the user.

## Coding agent

When assigned an issue or asked to fix a pull request, you are a coding agent
whose work product is a branch and a pull request:

- Commits to your own feature branch are expected and correct.
- Pushing to your branch (to open or update a PR) is expected and correct.
- **Do not push to `main`.** Your changes go to a feature branch and a PR.

## Repository rules

Before making changes, read the conventions and process that govern this project.

**Conventions** (from [`CONTRIBUTING.md`](../CONTRIBUTING.md)):
- All non-user-facing natural language — code comments, commit messages, PR titles
  and bodies, documentation — is English.
- Comments and commits explain *why*, not *what*. Match the surrounding code's
  style and comment density.

**Change tiers and process** (from [`CONTRIBUTING.md`](../CONTRIBUTING.md)):

Your contributions as a coding agent are **external contributions and always
Tier 3**, regardless of what tier the touched paths would be for a maintainer
in the deployed runtime. Branch and PR are required; expect review.

For context, the tiers that govern maintainer changes are:
- **Tier 1** (operational output): direct to `main`.
- **Tier 2** (sensitive content changes): in-conversation consent, then direct to
  `main`.
- **Tier 3** (system changes): `scripts/`, `Dockerfile`, `docker-compose.yml`,
  `CLAUDE.md`, `agents/`, `.claude/`, `webapp/` — feature branch + PR.

**Before opening a PR**, run the test suite:
```bash
pip install markdown-it-py requests pywebpush
( failed=0
  for t in tests/test_*.py; do python3 "$t" || { echo "FAILED: $t"; failed=1; }; done
  exit $failed )
```

The subshell is deliberate: it yields a real non-zero exit status when any test
failed, the way the CI workflow does, without a bare `exit` closing the shell of
whoever pasted the block. Each failure is named as it happens, so a long run
does not have to be scrolled back to find out which test broke.

**Architecture and roadmap**: [`review.md`](../review.md) is an honest assessment
of the codebase, its strengths and weaknesses, and the priorities for future work.
Skim it to understand the context.

---

**Note:** The branch/commit rules in [`CLAUDE.md`](../CLAUDE.md) describe how the
**deployed Retinue runtime** (Ara / Claude Code inside the container) operates.
**They do not apply to Copilot in either mode.**
