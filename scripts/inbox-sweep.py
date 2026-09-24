#!/usr/bin/env python3
"""Zero-credit gate for chamber inbox ingestion.

The problem this closes: the Archivist's "Processing an inbox" rules and each
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
the inbox and reports it (archivist.md, "Processing an inbox", step 4). Without a
guard that one file would spawn a session on every tick forever. So the sweep
records the listing it last spawned for and stays quiet while the inbox is
unchanged -- any added, removed or modified file makes it due again. The
guard only settles after a session that exited cleanly; a failed one is
retried with exponential backoff instead of being written off.

The manifest is untrusted input: an inbox path must stay inside its chamber,
and symlinks are never handed on as documents.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
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

# Backoff after a failed session: the first retry comes a tick later, then the
# wait doubles up to a day, so a persistent failure costs a few sessions a day
# rather than one an hour.
RETRY_BASE_SECONDS = 3600
RETRY_MAX_SECONDS = 24 * 3600


def resolve_inbox(chamber: Path, rel) -> Path | None:
    """The inbox directory for a manifest path, or None if it leaves the chamber.

    The contract is a path relative to the chamber. An absolute path or a `..`
    would make `chamber / rel` point anywhere, and a symlinked directory could
    do the same after resolution -- so both the syntax and the resolved
    location are checked.
    """
    if not isinstance(rel, str) or not rel.strip():
        return None
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        return None
    root = chamber.resolve()
    target = (chamber / p).resolve()
    if target != root and root not in target.parents:
        return None
    return chamber / p


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
        # Valid JSON is not yet a valid manifest; a wrong shape skips this
        # chamber like a parse error would, instead of aborting the sweep.
        inboxes = data.get("inboxes", []) if isinstance(data, dict) else None
        if not isinstance(inboxes, list):
            print(f"[inbox-sweep] {chamber.name}: .inbox.json has no "
                  "'inboxes' list; skipping chamber", file=sys.stderr)
            continue
        for entry in inboxes:
            if not isinstance(entry, dict):
                print(f"[inbox-sweep] {chamber.name}: ignoring non-object "
                      "inbox entry", file=sys.stderr)
                continue
            rel = entry.get("path")
            path = resolve_inbox(chamber, rel)
            if path is None:
                if rel:
                    print(f"[inbox-sweep] {chamber.name}: inbox path {rel!r} "
                          "is not inside the chamber; ignoring",
                          file=sys.stderr)
                continue
            found.append({
                "chamber": chamber.name,
                "id": str(entry.get("id") or rel),
                "path": path,
                "rel": rel,
                "description": str(entry.get("description") or ""),
            })
    return found


def pending_files(inbox_path: Path) -> list[Path]:
    """Files awaiting filing, in a stable order.

    Symlinks are skipped, file or directory: whatever they point at is not
    something the user dropped into the letterbox, and following one would
    hand the Archivist a file from outside the inbox.
    """
    if inbox_path.is_symlink() or not inbox_path.is_dir():
        return []
    found = []
    for dirpath, dirnames, filenames in os.walk(inbox_path, followlinks=False):
        dirnames[:] = [d for d in dirnames
                       if not os.path.islink(os.path.join(dirpath, d))]
        for name in filenames:
            if name in IGNORED_NAMES or name.startswith("."):
                continue
            p = Path(dirpath) / name
            if p.is_symlink() or not p.is_file():
                continue
            found.append(p)
    return sorted(found)


def signature(scan: list[dict]) -> dict:
    """What the sweep last acted on: path -> (size, mtime), per inbox.

    Size and mtime, not just the name, so a file that was edited in place
    (a corrected export dropped over the old one) counts as new work. The
    mtime is in nanoseconds: whole seconds would miss a same-size overwrite
    within the same second.
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
                                                       st.st_mtime_ns]
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


def retry_due(state: dict, now: float) -> bool:
    """Whether a failed run's backoff has elapsed."""
    failures = int(state.get("failures") or 0)
    wait = min(RETRY_BASE_SECONDS * 2 ** max(failures - 1, 0),
               RETRY_MAX_SECONDS)
    # A little slack so an hourly tick that lands a few seconds early still
    # counts as a full interval.
    return now - float(state.get("attempted_at") or 0) >= wait - 60


def build_prompt(scan: list[dict]) -> str:
    """Hand the agent the scanned listing so it need not re-scan."""
    lines = [
        "You are running the scheduled chamber inbox sweep. Files are sitting "
        "in chamber inboxes waiting to be filed; nothing else will surface "
        "them.",
        "",
        "For each chamber below, dispatch the `archivist` subagent to process "
        "that chamber's inbox: file each document to a destination declared "
        "in the chamber's `.inbox.json`, get its facts into the store "
        "(converter first, per-file extraction only for one-offs), and "
        "commit the destination files together with the inbox deletions, per "
        "\"Processing an inbox\" in the Archivist's definition.",
        "",
        "The Archivist starts cold: include the chamber path, the file "
        "listing, and any relevant memories in the dispatch prompt.",
        "",
        "A file the Archivist cannot classify stays in the inbox and is "
        "flagged -- that is correct, not a failure. Report what was filed and "
        "what was left behind; only open a dashboard conversation if "
        "something needs the user's decision.",
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
    state = load_state()
    now = time.time()
    if state.get("signature") == current:
        if not state.get("failures"):
            print("[inbox-sweep] inbox contents unchanged since last sweep "
                  "(likely files the Archivist could not classify); "
                  "nothing spawned", file=sys.stderr)
            return 0
        if not retry_due(state, now):
            print(f"[inbox-sweep] last session failed "
                  f"({state['failures']}x); backing off", file=sys.stderr)
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

    if result.returncode == 0:
        # Settled: whatever is still lying there was left on purpose.
        save_state({"signature": current})
    else:
        # A failed session (API or auth hiccup) has not looked at the files;
        # retry the same listing later, backing off so it cannot burn a
        # session every tick.
        failures = (int(state.get("failures") or 0) + 1
                    if state.get("signature") == current else 1)
        save_state({"signature": current, "failures": failures,
                    "attempted_at": now})
        print(f"[inbox-sweep] session exited {result.returncode}; will retry "
              f"with backoff (failure {failures})", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
