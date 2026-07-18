#!/usr/bin/env python3
"""
Keep the installed chamber plugins in sync with their sources.

Claude Code installs a plugin by copying it into a version-keyed cache
(~/.claude/plugins/cache/<marketplace>/<name>/<version>/).  Both
`claude plugin install` and `claude plugin update` are no-ops once that version
is present, and the version string in plugin.json rarely changes -- so an edit
to a chamber's agent definition never reaches the running subagent.  The cache
lives on the persistent /root volume, so neither a container restart nor an
image rebuild clears it.

This script compares each installed plugin against its source tree and
reinstalls the ones that drifted.  Content is compared file by file rather than
by version or git SHA: that catches uncommitted edits, and it does not reinstall
on every unrelated commit to the chamber.

Usage:
  python3 scripts/sync-plugins.py                  # reinstall drifted plugins, once
  python3 scripts/sync-plugins.py --force          # reinstall all, unconditionally
  python3 scripts/sync-plugins.py --watch          # keep syncing every --interval seconds
"""

import argparse
import filecmp
import json
import os
import subprocess
import sys
import time
from pathlib import Path

MARKETPLACE_NAME = "retinue"
MARKETPLACE = Path("/workspace/.claude-plugin/marketplace.json")
INSTALLED = Path.home() / ".claude" / "plugins" / "installed_plugins.json"


def log(msg):
    print(f"[plugin-sync] {msg}", flush=True)


def marketplace_plugins(marketplace):
    """[(name, absolute source dir)] for every plugin in the generated marketplace."""
    try:
        doc = json.loads(marketplace.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log(f"[warn] cannot read {marketplace}: {exc}")
        return []
    root = marketplace.parent.parent  # /workspace
    out = []
    for entry in doc.get("plugins", []):
        name = entry.get("name")
        source = entry.get("source")
        if name and source:
            out.append((name, (root / source).resolve()))
    return out


def install_path(name):
    """Where the CLI says this plugin's cached copy lives, or None if not installed."""
    try:
        doc = json.loads(INSTALLED.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    records = doc.get("plugins", {}).get(f"{name}@{MARKETPLACE_NAME}", [])
    for record in records:
        if record.get("scope") == "user" and record.get("installPath"):
            return Path(record["installPath"])
    return None


def trees_differ(source, cached):
    """True when the cached copy is not a byte-for-byte match of the source tree."""
    if not cached or not cached.is_dir():
        return True
    cmp = filecmp.dircmp(str(source), str(cached))

    def walk(node):
        if node.left_only or node.right_only or node.funny_files:
            return True
        # shallow=False: compare contents, not just size and mtime.
        _, mismatch, errors = filecmp.cmpfiles(
            node.left, node.right, node.common_files, shallow=False
        )
        if mismatch or errors:
            return True
        return any(walk(sub) for sub in node.subdirs.values())

    return walk(cmp)


def run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log(f"[warn] {' '.join(cmd)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.returncode == 0


def reinstall(name):
    """Force a fresh copy: the CLI refuses to overwrite an already-cached version."""
    ref = f"{name}@{MARKETPLACE_NAME}"
    run(["claude", "plugin", "uninstall", ref])  # absent is fine
    if run(["claude", "plugin", "install", ref]):
        log(f"reinstalled {ref}")
        return True
    log(f"[warn] could not reinstall {ref}")
    return False


def sync(force=False):
    plugins = marketplace_plugins(MARKETPLACE)
    if not plugins:
        return 0
    # Pick up marketplace.json edits (a chamber added or removed) before installing.
    run(["claude", "plugin", "marketplace", "update", MARKETPLACE_NAME])
    changed = 0
    for name, source in plugins:
        if not source.is_dir():
            log(f"[warn] {name}: source {source} missing, skipping")
            continue
        if force or trees_differ(source, install_path(name)):
            changed += reinstall(name)
    return changed


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true",
                    help="reinstall every plugin, even when it matches its source")
    ap.add_argument("--watch", action="store_true",
                    help="keep running, syncing every --interval seconds")
    ap.add_argument("--interval", type=int,
                    default=int(os.environ.get("PLUGIN_SYNC_INTERVAL", "60")),
                    help="seconds between passes in --watch mode (default: 60)")
    args = ap.parse_args()

    if not args.watch:
        changed = sync(force=args.force)
        log(f"{changed} plugin(s) reinstalled." if changed else "all plugins up to date.")
        return 0

    log(f"watching chamber plugins (every {args.interval}s)")
    # A drifted plugin only reaches a subagent on the next session start, which is
    # how scheduler jobs run anyway (a fresh `claude -p` each time).
    while True:
        try:
            sync(force=False)
        except Exception as exc:  # a watcher must not die on a transient failure
            log(f"[warn] sync pass failed: {exc}")
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
