#!/usr/bin/env python3
"""Scan every chamber's declared inboxes — filesystem-gated, chamber-agnostic.

A chamber that receives incoming documents declares one or more **inbox
folders** in a ``.inbox.json`` at its root (same convention family as
``.refresh.json`` / ``.schedule.json`` / ``.news.json``). This script is the
credit-free gate in front of the Archivist: it walks ``chambers/*/.inbox.json``,
counts the files actually waiting in each declared inbox, and only when
something is pending does it spawn a single ``claude -p`` session that dispatches
the **Archivist** subagent to process them. An empty set of inboxes does nothing
beyond a directory listing — no Claude credits, same shape as ``news-curate.py``.

Why a filesystem scan and not a store query
--------------------------------------------
Unlike recurring projects (whose state already lives in the life store), an
inbox is defined by *files sitting on disk that have not been filed yet*. That
is a filesystem question, so the gate reads the filesystem directly. The store
only ever sees a file *after* the Archivist has extracted it, so it cannot tell
us what is still waiting.

Manifest shape (``chambers/<name>/.inbox.json``)
------------------------------------------------
::

    {
      "inboxes": [
        {
          "id": "observations",
          "path": "observations/inbox",
          "routing": [
            {"match": "glucose_*.csv", "dest": "observations/clinical/sensors/cgm/",
             "ingest": "python3 scripts/ingest-sensors.py"},
            {"match": "*", "dest": null}
          ],
          "guide": ".retinue/archivist/extraction.md",
          "branch_tier": 1
        }
      ]
    }

Only ``path`` is required per inbox. ``routing`` (deterministic move/ingest
rules), ``guide`` (a chamber-local Markdown file holding that chamber's
ontology / extraction rules), and ``branch_tier`` (which branch-policy tier the
outputs commit under) are all read by the Archivist itself, not by this gate —
the gate only needs ``path`` to know where to look and how to name the work.

The framework carries **no** chamber-specific inbox knowledge: which folders are
inboxes, where files move, and how facts are extracted all live in the chamber.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys
from pathlib import Path

CHAMBERS_ROOT = Path(os.environ.get("CHAMBERS_ROOT", "/workspace/chambers"))
CLAUDE_MODEL = os.environ.get("RETINUE_CLAUDE_MODEL", "").strip()
PERMISSION_MODE = os.environ.get("CLAUDE_PERMISSION_MODE", "acceptEdits")

# Files that are never "pending work": the placeholder that keeps an empty inbox
# tracked in git, and editor/OS cruft. Everything else in a declared inbox counts.
IGNORE_NAMES = {".gitkeep", ".gitignore", ".DS_Store"}


def log(msg: str) -> None:
    print(f"[inbox-scan] {msg}", file=sys.stderr, flush=True)


def load_manifests() -> list[tuple[str, dict]]:
    """(chamber_name, manifest) for every readable chambers/*/.inbox.json."""
    out: list[tuple[str, dict]] = []
    if not CHAMBERS_ROOT.is_dir():
        return out
    for manifest in sorted(CHAMBERS_ROOT.glob("*/.inbox.json")):
        chamber = manifest.parent.name
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log(f"skipping {chamber}: cannot read .inbox.json ({exc})")
            continue
        out.append((chamber, data))
    return out


def pending_files(inbox_dir: Path) -> list[str]:
    """Names of files waiting in one inbox directory, ignoring placeholders and
    subdirectories (an inbox is a flat drop folder)."""
    if not inbox_dir.is_dir():
        return []
    return sorted(
        p.name for p in inbox_dir.iterdir()
        if p.is_file() and p.name not in IGNORE_NAMES
    )


def scan() -> list[dict]:
    """One record per non-empty inbox: chamber, path, and the waiting files."""
    found: list[dict] = []
    for chamber, manifest in load_manifests():
        chamber_dir = CHAMBERS_ROOT / chamber
        for inbox in manifest.get("inboxes", []):
            rel = (inbox or {}).get("path")
            if not rel:
                continue
            files = pending_files(chamber_dir / rel)
            if files:
                found.append({
                    "chamber": chamber,
                    "path": rel,
                    "files": files,
                })
    return found


def build_prompt(found: list[dict]) -> str:
    lines = [
        "One or more chamber inboxes have files waiting to be filed. Dispatch "
        "the `archivist` subagent to process them.",
        "",
        "The Archivist is generic: for each inbox below, it must read that "
        "chamber's `.inbox.json` (routing rules, extraction `guide`, "
        "`branch_tier`) and file every waiting document per the rules there — "
        "moving each file to its destination, extracting facts into a sibling "
        "`.nt`, and committing the moves, the generated `.nt` files and the "
        "inbox deletions together so the inbox is empty on the remote.",
        "",
        "Waiting now:",
    ]
    for rec in found:
        listed = ", ".join(rec["files"][:8])
        more = "" if len(rec["files"]) <= 8 else f", +{len(rec['files']) - 8} more"
        lines.append(
            f"- chamber `{rec['chamber']}`, inbox `{rec['path']}`: "
            f"{len(rec['files'])} file(s) — {listed}{more}"
        )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Gate the Archivist on pending chamber-inbox files.")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what is waiting, spawn nothing")
    args = ap.parse_args()

    found = scan()
    if not found:
        log("all chamber inboxes empty; nothing spawned")
        return 0

    total = sum(len(r["files"]) for r in found)
    if args.dry_run:
        log(f"would dispatch the Archivist for {total} file(s) "
            f"across {len(found)} inbox(es)")
        print(json.dumps(found, ensure_ascii=False, indent=1))
        return 0

    log(f"{total} file(s) across {len(found)} inbox(es); spawning session")
    cmd = ["claude", "-p", "--permission-mode", PERMISSION_MODE,
           build_prompt(found)]
    if CLAUDE_MODEL:
        cmd[2:2] = ["--model", CLAUDE_MODEL]
    result = subprocess.run(cmd, cwd="/workspace")
    if result.returncode != 0:
        log(f"archivist session exited {result.returncode}; "
            "files left for the next tick")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
