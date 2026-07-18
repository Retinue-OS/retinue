#!/usr/bin/env python3
"""
Generic refresh dispatcher for external data sources.

Reads a manifest from <data-dir>/.refresh.json, checks staleness per source,
and runs stale commands.  Handles git coordination to avoid redundant fetches
across parallel container instances.

Manifest format  (<chamber>/.refresh.json):
  {
    "sources": [
      {
        "id": "garmin",
        "command": "python3 /workspace/scripts/sync-garmin.py",
        "max_age_seconds": 86400,
        "lock_path": "/tmp/refresh-garmin.lock"
      }
    ]
  }

Per-source state files  (<chamber>/.refresh/<source-id>.json):
  {
    "last_run": "2025-01-01T10:00:00+00:00",
    "status": "success"
  }

Usage:
  python3 scripts/refresh.py --data-dir /workspace/chambers/<name>            # run all stale sources
  python3 scripts/refresh.py --data-dir /workspace/chambers/<name> --ensure garmin
"""

import argparse
import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Manifest & state helpers ─────────────────────────────────────────────────


def load_manifest(manifest_path: Path) -> dict:
    """Load the manifest JSON file. Returns an empty manifest if not found."""
    if not manifest_path.exists():
        return {"sources": []}
    with open(manifest_path) as f:
        return json.load(f)


def _state_path(data_dir: Path, source_id: str) -> Path:
    return data_dir / ".refresh" / f"{source_id}.json"


def load_state(data_dir: Path, source_id: str) -> dict:
    """Load the last-run state for a source. Returns an empty dict if absent."""
    p = _state_path(data_dir, source_id)
    if not p.exists():
        return {}
    with open(p) as f:
        return json.load(f)


def save_state(data_dir: Path, source_id: str, status: str) -> None:
    """Write the current timestamp and status to the source's state file."""
    p = _state_path(data_dir, source_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f:
        json.dump(
            {
                "last_run": datetime.now(timezone.utc).isoformat(),
                "status": status,
            },
            f,
            indent=2,
        )
        f.write("\n")


# After a failed run we still record last_run, but we must NOT then treat the
# source as "fresh" for the full max_age_seconds window — that silently
# suppressed retries for a day (e.g. a Garmin login that needed MFA failed at
# 09:00 and nothing tried again until the next morning). Back off only briefly
# so a transient failure is retried soon; override per-source with
# "error_retry_seconds".
ERROR_RETRY_SECONDS_DEFAULT = 900


def is_stale(source: dict, state: dict) -> bool:
    """Return True if the source needs refreshing.

    Fresh runs use max_age_seconds; a previous *error* uses the much shorter
    error_retry_seconds so a failure does not suppress retries for a full day.
    """
    last_run_str = state.get("last_run")
    if not last_run_str:
        return True
    last_run = datetime.fromisoformat(last_run_str)
    if last_run.tzinfo is None:
        last_run = last_run.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - last_run).total_seconds()

    max_age = source.get("max_age_seconds", 86400)
    if state.get("status") not in (None, "success"):
        # Don't wait longer than the normal window even if a custom retry is big.
        retry_after = source.get("error_retry_seconds", ERROR_RETRY_SECONDS_DEFAULT)
        return age >= min(retry_after, max_age)
    return age >= max_age


# ── Git helpers ───────────────────────────────────────────────────────────────


def _git(data_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=data_dir,
        capture_output=True,
        text=True,
    )


def is_git_repo(data_dir: Path) -> bool:
    """Return True when data_dir is a git working tree."""
    result = _git(data_dir, "rev-parse", "--is-inside-work-tree")
    return result.returncode == 0 and result.stdout.strip() == "true"


def git_pull_rebase(data_dir: Path) -> bool:
    result = _git(data_dir, "pull", "--rebase")
    if result.returncode != 0:
        print(
            f"[refresh] git pull --rebase failed: {result.stderr.strip()}",
            file=sys.stderr,
        )
    return result.returncode == 0


def git_commit_push(data_dir: Path, source_id: str) -> None:
    """Stage all changes, commit, and push with one rebase-retry on conflict."""
    _git(data_dir, "add", "-A")

    # Nothing staged → nothing to do
    if _git(data_dir, "diff", "--cached", "--quiet").returncode == 0:
        return

    _git(data_dir, "commit", "-m", f"refresh: auto-update {source_id} [skip ci]")

    push = _git(data_dir, "push")
    if push.returncode == 0:
        return

    # Conflict: rebase and retry once
    print(
        f"[refresh] push failed for {source_id} — rebasing and retrying ...",
        file=sys.stderr,
    )
    git_pull_rebase(data_dir)
    push2 = _git(data_dir, "push")
    if push2.returncode != 0:
        print(
            f"[refresh] Warning: push still failed for {source_id}. "
            "Changes are committed locally; next run will retry the push.",
            file=sys.stderr,
        )


# ── Core refresh logic ────────────────────────────────────────────────────────


def run_source(source: dict, data_dir: Path) -> bool:
    """
    Refresh a single source.  Returns True on success.

    Flow:
      1. Acquire a local flock (if lock_path is set) — skip if already locked
         by another process in the same container.
      2. git pull --rebase to pick up updates from other containers.
      3. Re-check staleness — skip if another container was faster.
      4. Run the command, record status.
      5. Commit + push result.
    """
    source_id = source["id"]
    command = source["command"]
    lock_path_str = source.get("lock_path")
    git_available = is_git_repo(data_dir)

    lock_file = None
    if lock_path_str:
        try:
            lock_file = open(lock_path_str, "a")
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            print(
                f"[refresh] {source_id}: locked by another process in this "
                "container — skipping."
            )
            if lock_file:
                lock_file.close()
            return False

    try:
        if git_available:
            # Pull latest so we see any update committed by a parallel container.
            git_pull_rebase(data_dir)

            # Re-check after pull: another container may have beaten us.
            state = load_state(data_dir, source_id)
            if not is_stale(source, state):
                print(f"[refresh] {source_id}: already fresh after git pull — skipping.")
                return True

        print(f"[refresh] {source_id}: running '{command}' ...")
        env = {**os.environ, "CHAMBER_DIR": str(data_dir)}
        result = subprocess.run(command, shell=True, cwd=data_dir, env=env)

        status = "success" if result.returncode == 0 else "error"
        save_state(data_dir, source_id, status)

        if result.returncode != 0:
            print(
                f"[refresh] {source_id}: command exited with {result.returncode}.",
                file=sys.stderr,
            )

        if git_available:
            git_commit_push(data_dir, source_id)
        else:
            print(
                f"[refresh] {source_id}: data dir is not a git repo; "
                "skipping pull/commit/push."
            )
        return result.returncode == 0

    finally:
        if lock_file:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
            except OSError:
                pass
            lock_file.close()


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generic refresh dispatcher for external data sources."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Path to manifest JSON file (default: <data-dir>/.refresh.json)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        required=True,
        dest="data_dir",
        help="Path to the chamber directory holding .refresh.json",
    )
    parser.add_argument(
        "--ensure",
        metavar="SOURCE_ID",
        help=(
            "Ensure a specific source is fresh (blocking); "
            "exits 0 immediately if the source is already within max_age_seconds"
        ),
    )
    args = parser.parse_args()

    manifest_path = args.manifest or (args.data_dir / ".refresh.json")
    manifest = load_manifest(manifest_path)
    sources = manifest.get("sources", [])

    if not sources:
        print(f"[refresh] No sources defined in {manifest_path} — nothing to do.")
        return

    if args.ensure:
        source = next((s for s in sources if s["id"] == args.ensure), None)
        if source is None:
            print(
                f"[refresh] Source '{args.ensure}' not found in {manifest_path}.",
                file=sys.stderr,
            )
            sys.exit(1)

        state = load_state(args.data_dir, args.ensure)
        if not is_stale(source, state):
            print(f"[refresh] {args.ensure}: fresh, no update needed.")
            return

        run_source(source, args.data_dir)
    else:
        # Run all stale sources in declaration order
        for source in sources:
            state = load_state(args.data_dir, source["id"])
            if is_stale(source, state):
                run_source(source, args.data_dir)
            else:
                print(f"[refresh] {source['id']}: fresh, skipping.")


if __name__ == "__main__":
    main()
