#!/usr/bin/env python3
"""Zero-credit gate for chamber inbox ingestion.

The problem this closes: the Archivist's `inbox/ processing` rules and each
chamber's `.inbox.json` describe *how* an incoming file is filed and *where* it
goes -- but nothing ever says *when*. No watcher, no job. The rule "never leave
the inbox non-empty after a push" was written down and then executed only when
a human happened to ask, so a chamber inbox silently accumulated months of
files.

The mechanism is generic, not chamber-specific: any chamber may declare inboxes
in a `.inbox.json` at its root, so the sweep belongs in the framework base
manifest rather than in whichever chamber noticed first.

This runs as a scheduler `command` job, so the scheduler spends no Claude
credits to invoke it. The gate is a filesystem scan -- also free. Only when a
declared inbox actually holds files does it spawn a single `claude -p` session,
handed the already-scanned listing so the agent does not re-scan.

Re-spawn guard: the Archivist deliberately leaves a file it cannot classify in
the inbox and flags it (archivist.md, `inbox/ processing`, step 4). Without a
guard that one file would spawn a session on every tick forever. So the sweep
records the listing it last spawned for and stays quiet while the inbox is
unchanged -- any added, removed or modified file makes it due again.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_auth  # noqa: E402
import session_env  # noqa: E402

CHAMBERS_DIR = Path(os.environ.get("CHAMBERS_DIR", "/workspace/chambers"))
# State lives outside the chambers, like the scheduler's own, so a sweep
# creates no git noise in the repos it is watching.
STATE_PATH = Path(
    os.environ.get("INBOX_SWEEP_STATE",
                   "/root/.retinue/inbox-sweep/state.json"))
# Ingestion is routing -- hand the files to the Archivist -- which is squarely
# inside the router tier's whitelist (docs/model-routing.md), so junior wins
# here where self-review takes the frontier tier.
CLAUDE_MODEL = (
    os.environ.get("RETINUE_ROUTER_MODEL", "").strip()
    or os.environ.get("RETINUE_CLAUDE_MODEL", "").strip()
)
PERMISSION_MODE = os.environ.get("CLAUDE_PERMISSION_MODE", "acceptEdits")

# A chamber's own bookkeeping, not mail: never work the agent should be doing.
IGNORED_NAMES = {".gitkeep", ".DS_Store"}


def declared_inboxes() -> list[dict]:
    """Every inbox declared by every mounted chamber, in a stable order."""
    found: list[dict] = []
    if not CHAMBERS_DIR.is_dir():
        return found
    for chamber in sorted(CHAMBERS_DIR.iterdir()):
        manifest = chamber / ".inbox.json"
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            # A malformed manifest must not take the whole sweep down with it:
            # the other chambers' inboxes are still worth draining.
            print(f"[inbox-sweep] {chamber.name}: unreadable .inbox.json "
                  f"({e}); skipping chamber", file=sys.stderr)
            continue
        for entry in data.get("inboxes", []):
            rel = (entry or {}).get("path")
            if not rel:
                continue
            found.append({
                "chamber": chamber.name,
                "id": entry.get("id") or rel,
                "path": chamber / rel,
                "rel": rel,
                "description": entry.get("description") or "",
            })
    return found


def pending_files(inbox_path: Path) -> list[Path]:
    """Files awaiting filing, deepest-first order irrelevant -- just stable."""
    if not inbox_path.is_dir():
        return []
    return sorted(
        p for p in inbox_path.rglob("*")
        if p.is_file() and p.name not in IGNORED_NAMES
        and not p.name.startswith(".")
    )


def signature(scan: list[dict]) -> dict:
    """What the sweep last acted on: path -> (size, mtime), per inbox.

    Size and mtime, not just the name, so a file that was edited in place
    (a corrected export dropped over the old one) counts as new work.
    """
    sig: dict[str, dict[str, list]] = {}
    for item in scan:
        key = f"{item['chamber']}:{item['id']}"
        entry: dict[str, list] = {}
        for f in item["files"]:
            try:
                st = f.stat()
            except OSError:
                continue
            entry[str(f.relative_to(item["path"]))] = [st.st_size,
                                                       int(st.st_mtime)]
        sig[key] = entry
    return sig


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True),
                              encoding="utf-8")
    except OSError as e:
        # Losing the guard costs a duplicate session next tick, not correctness.
        print(f"[inbox-sweep] could not write state ({e})", file=sys.stderr)


def build_prompt(scan: list[dict]) -> str:
    """Hand the agent the scanned listing so it need not re-scan."""
    lines = [
        "You are running the scheduled chamber inbox sweep. Files are sitting "
        "in chamber inboxes waiting to be filed; nothing else will surface "
        "them.",
        "",
        "For each chamber below, dispatch the `archivist` subagent to process "
        "that chamber's inbox: file each document to its declared destination, "
        "extract triples into the sibling .nt, and commit the destination "
        "files together with the inbox deletions, per the Archivist's "
        "`inbox/ processing` rules and the chamber's `.inbox.json`.",
        "",
        "The Archivist starts cold: include the chamber path, the file "
        "listing, and any relevant memories in the dispatch prompt.",
        "",
        "A file the Archivist cannot classify stays in the inbox and is "
        "flagged -- that is correct, not a failure. Report what was filed and "
        "what was left behind; only open a dashboard conversation if "
        "something needs Reto's decision.",
        "",
        "Pending inboxes:",
    ]
    for item in scan:
        lines.append(f"\n## {item['chamber']} -- {item['rel']} "
                     f"({len(item['files'])} file(s))")
        if item["description"]:
            lines.append(f"  {item['description']}")
        lines.append(f"  path: {item['path']}")
        for f in item["files"]:
            lines.append(f"  - {f.relative_to(item['path'])}")
    return "\n".join(lines)


def main() -> int:
    scan = []
    for inbox in declared_inboxes():
        files = pending_files(inbox["path"])
        if files:
            scan.append({**inbox, "files": files})

    if not scan:
        # Nothing pending anywhere: forget the guard so the next arrival, even
        # of a file that was stuck before, spawns a session again.
        if STATE_PATH.exists():
            save_state({})
        print("[inbox-sweep] all declared inboxes empty; nothing spawned",
              file=sys.stderr)
        return 0

    current = signature(scan)
    previous = load_state().get("signature")
    if previous == current:
        print("[inbox-sweep] inbox contents unchanged since last sweep "
              "(likely files the Archivist could not classify); "
              "nothing spawned", file=sys.stderr)
        return 0

    total = sum(len(i["files"]) for i in scan)
    print(f"[inbox-sweep] {total} file(s) pending across {len(scan)} inbox(es); "
          "spawning session", file=sys.stderr)

    cmd = ["claude", "-p", "--output-format=json",
           "--permission-mode", PERMISSION_MODE, build_prompt(scan)]
    if CLAUDE_MODEL:
        cmd[2:2] = ["--model", CLAUDE_MODEL]
    env = session_env.build(model=CLAUDE_MODEL)
    claude_auth.ensure_fresh_credentials(
        log=lambda msg: print(f"[inbox-sweep] {msg}", file=sys.stderr))
    result = subprocess.run(cmd, cwd="/workspace", env=env)

    # Record what this run was handed, whatever the session made of it: a
    # session that failed outright should not re-spawn every tick either.
    save_state({"signature": current})
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
