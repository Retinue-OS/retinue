#!/usr/bin/env bash
# One-time setup after cloning the health data repo: installs git hooks.
# Usage: scripts/install-hooks.sh
set -euo pipefail

# In mount-only setups a plain folder may be used instead of a git checkout.
if ! REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  echo "Not a git repository — skipping hook installation."
  exit 0
fi

cd "$REPO_ROOT"

HOOK_SRC="$REPO_ROOT/hooks/post-commit"
HOOK_DST="$REPO_ROOT/.git/hooks/post-commit"

if [[ -f "$HOOK_SRC" ]]; then
  chmod +x "$HOOK_SRC"
  ln -sf "$HOOK_SRC" "$HOOK_DST"
  echo "Git hooks installed."
else
  echo "No hooks/post-commit found in data repo — skipping."
fi
