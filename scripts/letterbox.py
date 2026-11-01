#!/usr/bin/env python3
"""The built-in letterbox: one inbox chamber every deployment has.

A letterbox is a folder files are dropped into from outside — by the phone
shell or any WebDAV client through ``/inbox/`` on the dashboard origin
(scripts/webdav_share.py), or by a sync tool such as Syncthing writing into a
host directory mounted there. It lives at the fixed container path
``LETTERBOX_DIR`` (default ``/workspace/chambers/inbox``): a named volume by
default, a host directory where the deployment mounts one
(docker-compose.override.example.yml).

It is a chamber in the one sense that matters — it carries an ``.inbox.json``
declaring its root an inbox, so the ``inbox-sweep`` job hands what lies there
to the Archivist like any other inbox — and in no other: it is no git
repository, ships no plugin, holds no data of its own. A file is taken out,
filed into a real chamber and deleted. Files whose type a chamber has declared
(``accepts`` in its ``.inbox.json``) are taken out within seconds by
scripts/letterbox-intake.py, without a model; the rest wait for the sweep.

Because the folder may be a user's bind mount, the image cannot ship the
manifest inside it: ``ensure_manifest()`` writes the default when it is
missing (the entrypoint calls it, and so does the intake watcher).

Being writable from outside, the letterbox must never be read as anything but
documents: the framework's other per-chamber hooks (``.schedule.json``,
``.refresh.json``, ``.news.json``, a ``.retinue/`` plugin, a
``requirements.txt``, an ``accepts`` list in its own manifest) are ignored
here — see ``is_letterbox()`` — and the WebDAV share refuses hidden names.
"""
from __future__ import annotations

import fnmatch
import json
import os
import sys
from pathlib import Path

LETTERBOX_DIR = Path(os.environ.get("LETTERBOX_DIR")
                     or Path(os.environ.get("CHAMBERS_DIR") or "/workspace/chambers") / "inbox")

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

# Names that are never documents: hidden files (dotfiles cover Syncthing's
# `.syncthing.*.tmp`, `.stfolder`, `.stignore`, `.stversions`, AppleDouble
# `._*`, LibreOffice's `.~lock.*#`, `.DS_Store` and the manifest itself) and
# uploads still in flight (`*.part`, as browsers and several WebDAV and sync
# clients name them).
_IN_FLIGHT = ("*.part", "*.partial", "*.crdownload", "~$*")


def ignored(name: str) -> bool:
    """Whether a file or directory name is bookkeeping, not a document."""
    if name.startswith("."):
        return True
    return any(fnmatch.fnmatchcase(name, pat) for pat in _IN_FLIGHT)


def is_letterbox(path: "str | os.PathLike[str]") -> bool:
    """Whether ``path`` is the letterbox directory."""
    try:
        return Path(path).resolve() == LETTERBOX_DIR.resolve()
    except (OSError, RuntimeError, ValueError):
        return False


def ensure_manifest(directory: Path | None = None) -> bool:
    """Write the default ``.inbox.json`` into the letterbox when it has none.
    Returns True when one was written. A manifest already there is kept: a
    deployment may have edited the description."""
    root = directory or LETTERBOX_DIR
    manifest = root / ".inbox.json"
    if manifest.exists():
        return False
    try:
        root.mkdir(parents=True, exist_ok=True)
        tmp = root / ".inbox.json.tmp"
        tmp.write_text(json.dumps(DEFAULT_MANIFEST, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, manifest)
    except OSError as exc:
        print(f"[letterbox] could not write {manifest}: {exc}", file=sys.stderr)
        return False
    return True


if __name__ == "__main__":
    # `python3 letterbox.py ensure` — the entrypoint's call.
    if sys.argv[1:] == ["ensure"]:
        if ensure_manifest():
            print(f"[letterbox] wrote the default manifest to {LETTERBOX_DIR}/.inbox.json")
        sys.exit(0)
    print("usage: letterbox.py ensure", file=sys.stderr)
    sys.exit(2)
