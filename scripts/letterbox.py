#!/usr/bin/env python3
"""The built-in letterbox: one inbox chamber every deployment has.

A letterbox is a folder files are dropped into from outside — by the phone
shell or any WebDAV client through ``/inbox/`` on the dashboard origin
(scripts/webdav_share.py), or by a sync tool such as Syncthing writing into a
host directory mounted there. It lives at the fixed container path
``LETTERBOX_DIR`` (default ``/workspace/chambers/inbox``): a named volume by
default, a host directory where the deployment mounts one
(docker-compose.override.example.yml).

It is a chamber in the one sense that matters — the ``inbox-sweep`` job hands
what lies there to the Archivist like any other inbox — and in no other: it
is no git repository, ships no plugin, holds no data of its own. A file is
taken out, filed into a real chamber and deleted. Files whose type a chamber
has declared (``accepts`` in its ``.inbox.json``) are taken out within seconds
by scripts/letterbox-intake.py, without a model; the rest wait for the sweep.

Being writable from outside, the letterbox is never read as anything but
documents. Three rules enforce that, shared by the share, the sweep and the
intake watcher:

- **Its manifest is never read.** ``manifest()`` is the fixed
  ``DEFAULT_MANIFEST``, in process; ``ensure_manifest()`` only writes that
  same text to disk (overwriting whatever was there) so a WebDAV client
  listing the folder sees what the folder is. A sync peer that edits the file
  changes nothing.
- **Every other per-chamber hook skips it** — ``.schedule.json``,
  ``.refresh.json``, ``.news.json``, a ``.retinue/`` plugin, a
  ``requirements.txt``, ``accepted-requesters.txt``: ``mounted_chambers()`` is
  the one way the framework's Python enumerates chambers, and it leaves the
  letterbox out.
- **Names that mean something to the framework are refused** (``ignored()``,
  ``denied()``): a ``CLAUDE.md`` dropped into the folder would be loaded as
  instructions by the first session that reads a file beside it. The share
  answers 403, the sweep never lists them, and the intake watcher, which also
  sees what arrived by other routes, moves them out of the way under
  ``QUARANTINE_PREFIX`` and tells the user.
"""
from __future__ import annotations

import fnmatch
import json
import os
import sys
from pathlib import Path

CHAMBERS_DIR = Path(os.environ.get("CHAMBERS_DIR") or "/workspace/chambers")
LETTERBOX_DIR = Path(os.environ.get("LETTERBOX_DIR") or CHAMBERS_DIR / "inbox")

DEFAULT_MANIFEST = {
    "inboxes": [{
        "id": "letterbox",
        "path": ".",
        "description": (
            "The built-in letterbox: files dropped over WebDAV (/inbox/ on the "
            "dashboard) or by a sync tool. Anything here may be filed into a "
            "destination of any chamber."
        ),
    }],
    "destinations": [],
}
MANIFEST_TEXT = json.dumps(DEFAULT_MANIFEST, indent=2) + "\n"

# Names that mean something to the framework or to Claude Code and must not
# exist in a folder outsiders write to. Case-insensitive: a WebDAV client on
# a case-folding filesystem would otherwise get `claude.md` through. The
# dot-prefixed ones are hidden anyway; they are listed so the rule is in one
# place. A file arriving under one of these (Syncthing bypasses the share) is
# quarantined by the intake watcher, never filed, never handed to a model.
DENIED_NAMES = frozenset(n.lower() for n in (
    "CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", ".claude", ".claude.json",
    "accepted-requesters.txt", "requirements.txt", ".inbox.json",
    ".schedule.json", ".refresh.json", ".news.json", ".retinue", ".qlever",
    ".git", ".gitignore", ".gitattributes", ".gitmodules", ".mcp.json",
))
# A quarantined file keeps its name after this prefix, so the user can tell
# what it was; the prefix is hidden (dot) and so skipped everywhere.
QUARANTINE_PREFIX = ".quarantined-"

# Uploads still in flight, as browsers and several WebDAV and sync clients
# name them. Dotfiles (Syncthing's `.syncthing.*.tmp`, `.stfolder`,
# `.stignore`, `.stversions`, AppleDouble `._*`, LibreOffice's `.~lock.*#`,
# `.DS_Store`) are covered by the hidden rule.
_IN_FLIGHT = ("*.part", "*.partial", "*.crdownload", "~$*")


def denied(name: str) -> bool:
    """Whether a file or directory name is one the framework would read."""
    return name.lower() in DENIED_NAMES


def ignored(name: str) -> bool:
    """Whether a name is never a document: hidden, in flight, or denied."""
    if name.startswith(".") or denied(name):
        return True
    return any(fnmatch.fnmatchcase(name, pat) for pat in _IN_FLIGHT)


def quarantine_name(name: str) -> str:
    return QUARANTINE_PREFIX + name


def is_letterbox(path: "str | os.PathLike[str]") -> bool:
    """Whether ``path`` is the letterbox directory."""
    try:
        return Path(path).resolve() == LETTERBOX_DIR.resolve()
    except (OSError, RuntimeError, ValueError):
        return False


def mounted_chambers(chambers_dir: Path | None = None) -> list[Path]:
    """Every chamber directory under ``chambers_dir`` (default CHAMBERS_DIR),
    sorted by name — hidden entries and the letterbox left out. The one way
    the framework enumerates chambers for anything it *reads*: manifests,
    plugins, allowlists."""
    root = chambers_dir or CHAMBERS_DIR
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    return [p for p in entries
            if p.is_dir() and not p.name.startswith(".") and not is_letterbox(p)]


def manifest() -> dict:
    """The letterbox's manifest — fixed, never read from disk."""
    return json.loads(MANIFEST_TEXT)


def ensure_manifest(directory: Path | None = None) -> bool:
    """Write the fixed ``.inbox.json`` into the letterbox, replacing whatever
    is there, so a client listing the folder sees what it is. Returns True
    when the file was (re)written. Nothing reads it back."""
    root = directory or LETTERBOX_DIR
    target = root / ".inbox.json"
    try:
        if target.is_file() and target.read_text(encoding="utf-8") == MANIFEST_TEXT:
            return False
    except OSError:
        pass
    try:
        root.mkdir(parents=True, exist_ok=True)
        tmp = root / ".inbox.json.tmp"
        tmp.write_text(MANIFEST_TEXT, encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        print(f"[letterbox] could not write {target}: {exc}", file=sys.stderr)
        return False
    return True


# ── Typed intake declarations (`accepts`) ────────────────────────────────────
# Read by the intake watcher, which files matching letterbox files, and by the
# inbox sweep, which leaves them alone so the two never work on one file.

JSON_MATCH_MAX = int(os.environ.get("LETTERBOX_JSON_MATCH_MAX", str(32 * 1024 * 1024)))


def _inside(chamber: Path, rel) -> bool:
    """A manifest path strictly inside the chamber (the inbox sweep's rule)."""
    if not isinstance(rel, str) or not rel.strip():
        return False
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        return False
    try:
        return chamber.resolve() in (chamber / p).resolve().parents
    except (OSError, RuntimeError, ValueError):
        return False


def load_accepts(chambers_dir: Path | None = None, log=None) -> list[dict]:
    """Every valid ``accepts`` entry of every mounted chamber (never the
    letterbox's). An invalid entry is skipped with a warning through ``log``;
    the others still count."""
    import shlex
    say = log or (lambda msg: print(f"[letterbox] {msg}", file=sys.stderr))
    found: list[dict] = []
    for chamber in mounted_chambers(chambers_dir):
        path = chamber / ".inbox.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # the inbox sweep reports unreadable manifests
        accepts = data.get("accepts") if isinstance(data, dict) else None
        if accepts is None:
            continue
        if not isinstance(accepts, list):
            say(f"{chamber.name}: .inbox.json 'accepts' is not a list; ignored")
            continue
        for i, entry in enumerate(accepts):
            label = f"{chamber.name}: accepts[{i}]"
            if not isinstance(entry, dict):
                say(f"{label} is not an object; ignored")
                continue
            match = entry.get("match")
            glob = match.get("glob") if isinstance(match, dict) else None
            if not isinstance(glob, str) or not glob.strip():
                say(f"{label} has no match.glob; ignored")
                continue
            want_json = match.get("json")
            if want_json is not None and not isinstance(want_json, dict):
                say(f"{label}: match.json is not an object; ignored")
                continue
            if not _inside(chamber, entry.get("path")):
                say(f"{label}: path {entry.get('path')!r} is not inside the chamber; ignored")
                continue
            cmd = entry.get("on_receive")
            argv = None
            if cmd is not None:
                try:
                    argv = shlex.split(cmd) if isinstance(cmd, str) else None
                except ValueError:
                    argv = None
                if not argv:
                    say(f"{label}: on_receive {cmd!r} is not a command line; ignored")
                    continue
            found.append({
                "chamber": chamber,
                "name": str(entry.get("name") or f"accepts[{i}]"),
                "glob": glob,
                "json": want_json,
                "path": Path(entry["path"]).as_posix().strip("/"),
                "on_receive": argv,
            })
    return found


def matches(accept: dict, path: Path) -> bool:
    """Whether one declaration takes the file at ``path``."""
    if not fnmatch.fnmatchcase(path.name, accept["glob"]):
        return False
    want = accept["json"]
    if not want:
        return True
    try:
        if path.stat().st_size > JSON_MATCH_MAX:
            return False
        doc = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return False
    return isinstance(doc, dict) and all(k in doc and doc[k] == v for k, v in want.items())


def accepted_by(accepts: list[dict], path: Path) -> list[dict]:
    return [a for a in accepts if matches(a, path)]


if __name__ == "__main__":
    # `python3 letterbox.py ensure` — the entrypoint's call.
    if sys.argv[1:] == ["ensure"]:
        if ensure_manifest():
            print(f"[letterbox] wrote the manifest to {LETTERBOX_DIR}/.inbox.json")
        sys.exit(0)
    print("usage: letterbox.py ensure", file=sys.stderr)
    sys.exit(2)
