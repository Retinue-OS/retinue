#!/usr/bin/env bash
# git wrapper that serializes index/ref/remote-mutating operations across the
# parallel agents that share the working trees under /workspace/chambers/*.
#
# The web gateway can run several Claude sessions at once (different users in
# parallel), and the scheduler, refresh dispatcher and main session also touch
# the same checkouts. Without coordination two concurrent `git commit`s race on
# `.git/index.lock` and two `git push`es race on the remote ref. This wrapper
# takes an exclusive flock per repository for the write subcommands so they run
# one at a time, while read-only commands and operations in different repos stay
# fully parallel.
#
# Install by putting a `git` symlink to this script ahead of the real git on
# PATH (see scripts/entrypoint.sh). The per-repo lock files live under
# GIT_SERIALIZE_LOCK_DIR (default /tmp/retinue-git-locks).
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

# Resolve the real git binary, skipping this wrapper's directory (whether this
# script is invoked directly or via a `git` symlink placed on PATH).
REAL_GIT=""
IFS=':' read -ra _path_dirs <<< "$PATH"
for _d in "${_path_dirs[@]}"; do
  [[ -z "$_d" ]] && continue
  _resolved="$(cd "$_d" 2>/dev/null && pwd -P || true)"
  [[ "$_resolved" == "$SELF_DIR" ]] && continue
  if [[ -x "$_d/git" && ! -L "$_d/git" ]]; then
    REAL_GIT="$_d/git"
    break
  fi
done
[[ -n "$REAL_GIT" ]] || REAL_GIT="/usr/bin/git"

LOCK_DIR="${GIT_SERIALIZE_LOCK_DIR:-/tmp/retinue-git-locks}"

# Only write operations that can take index.lock or mutate refs/remotes need
# serialization; read-only commands run unlocked for full parallelism.
case "${1:-}" in
  commit|push|pull|merge|rebase|am|cherry-pick|revert|add|rm|mv|reset|restore|checkout|switch|stash|tag|fetch)
    mkdir -p "$LOCK_DIR"
    # Lock per repository top level so different repos never block each other.
    repo_root="$("$REAL_GIT" rev-parse --show-toplevel 2>/dev/null || echo "_global")"
    lock_name="$(printf '%s' "$repo_root" | tr -c 'A-Za-z0-9._-' '_')"
    exec 9>"$LOCK_DIR/$lock_name.lock"
    flock 9
    "$REAL_GIT" "$@"
    exit $?
    ;;
  *)
    exec "$REAL_GIT" "$@"
    ;;
esac
