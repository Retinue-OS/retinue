#!/usr/bin/env python3
"""Typed intake: files appearing in the letterbox, filed by declaration.

The letterbox (scripts/letterbox.py) takes files from anywhere — a WebDAV PUT
to ``/inbox/``, a sync tool writing into a mounted host folder. Most of them
want reading and judgement, and wait for the ``inbox-sweep`` job to hand them
to the Archivist. Some are machine output of a known type, arriving often and
needing none: a chamber declares those in its ``.inbox.json``::

    "accepts": [
      { "name": "sensor-batch",
        "match": { "glob": "*.sensor.json", "json": { "schema": "example/1" } },
        "path": "data/sensor/raw/",
        "on_receive": "python3 .retinue/scripts/derive.py" }
    ]

and this watcher files them within seconds, without a model:

1. **Seen.** The letterbox is polled every ``LETTERBOX_POLL_SECONDS`` (2) —
   polling rather than inotify, because a sync tool on the host writes into a
   bind mount and the scheduler's ticks are minutes apart. Hidden names and
   uploads in flight are never looked at (``letterbox.ignored``).
2. **Complete.** A file is taken only once its size and mtime have held still
   for ``LETTERBOX_STABLE_SECONDS`` (3).
3. **Matched.** Against every mounted chamber's ``accepts``: ``match.glob``
   on the file name, and, where given, ``match.json`` — each top-level key of
   the parsed file equal to the declared value (only for files up to
   ``LETTERBOX_JSON_MATCH_MAX`` bytes; a larger or unparsable file does not
   match). No match: left for the sweep. More than one: left in place, logged
   and reported to the user in a dashboard thread — two chambers claiming one
   file is a configuration error nobody should guess around.
4. **Filed.** Exactly one match: the file moves to
   ``<chamber>/<path>/<YYYY>/<MM>/<DD>/<name>`` (the UTC date it was first
   seen). A file of that name already there with the same content means the
   drop was a resend: the letterbox copy is just deleted. Different content
   keeps both, the newcomer with a ``-2``, ``-3``, … before its first dot.
5. **Derived.** ``on_receive``, if declared, runs with the chamber root as cwd
   and the filed file's absolute path as its last argument, under
   ``LETTERBOX_DERIVE_TIMEOUT`` (120 s) and the allowlisted session
   environment; a failure is logged, the file stays filed.
6. **Committed.** Filed and derived files are operational output (Tier 1):
   committed to the chamber and pushed at most once per
   ``LETTERBOX_COMMIT_INTERVAL`` seconds (600) per chamber. What is due is
   persisted in ``LETTERBOX_INTAKE_STATE`` *before* a file moves, and the
   loop flushes it as soon as the interval is up — so a commit is never
   lost to a restart, only delayed. Only the chamber's own repository is
   committed to, and only those paths (``git commit -- <paths>``), so other
   staged work in the chamber is never swept along. Which paths
   ``on_receive`` touched is read off ``git status`` before and after it runs.

Run by the entrypoint as ``letterbox-intake.py --watch``; ``--once`` runs a
single pass (and flushes due commits) for tests and by hand.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import letterbox  # noqa: E402
import session_env  # noqa: E402

CHAMBERS_DIR = Path(os.environ.get("CHAMBERS_DIR", "/workspace/chambers"))
STATE_PATH = Path(os.environ.get("LETTERBOX_INTAKE_STATE",
                                 "/root/.retinue/letterbox-intake/state.json"))
POLL_SECONDS = float(os.environ.get("LETTERBOX_POLL_SECONDS", "2"))
STABLE_SECONDS = float(os.environ.get("LETTERBOX_STABLE_SECONDS", "3"))
COMMIT_INTERVAL = float(os.environ.get("LETTERBOX_COMMIT_INTERVAL", "600"))
DERIVE_TIMEOUT = float(os.environ.get("LETTERBOX_DERIVE_TIMEOUT", "120"))
JSON_MATCH_MAX = int(os.environ.get("LETTERBOX_JSON_MATCH_MAX", str(32 * 1024 * 1024)))
SCRIPTS_DIR = Path(os.path.dirname(os.path.abspath(__file__)))

# Seams for the tests.
_now = time.time
_run = subprocess.run


def log(msg: str) -> None:
    print(f"[letterbox-intake] {msg}", file=sys.stderr, flush=True)


# ── Declarations ─────────────────────────────────────────────────────────────

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


def load_accepts() -> list[dict]:
    """Every valid ``accepts`` entry of every mounted chamber but the letterbox.
    An invalid entry is skipped with a warning; the others still count."""
    found: list[dict] = []
    if not CHAMBERS_DIR.is_dir():
        return found
    for chamber in sorted(CHAMBERS_DIR.iterdir()):
        if letterbox.is_letterbox(chamber) or not chamber.is_dir():
            continue
        manifest = chamber / ".inbox.json"
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # the inbox sweep reports unreadable manifests
        accepts = data.get("accepts") if isinstance(data, dict) else None
        if accepts is None:
            continue
        if not isinstance(accepts, list):
            log(f"{chamber.name}: .inbox.json 'accepts' is not a list; ignored")
            continue
        for i, entry in enumerate(accepts):
            label = f"{chamber.name}: accepts[{i}]"
            if not isinstance(entry, dict):
                log(f"{label} is not an object; ignored")
                continue
            match = entry.get("match")
            glob = match.get("glob") if isinstance(match, dict) else None
            if not isinstance(glob, str) or not glob.strip():
                log(f"{label} has no match.glob; ignored")
                continue
            want_json = match.get("json")
            if want_json is not None and not isinstance(want_json, dict):
                log(f"{label}: match.json is not an object; ignored")
                continue
            if not _inside(chamber, entry.get("path")):
                log(f"{label}: path {entry.get('path')!r} is not inside the chamber; ignored")
                continue
            cmd = entry.get("on_receive")
            argv = None
            if cmd is not None:
                try:
                    argv = shlex.split(cmd) if isinstance(cmd, str) else None
                except ValueError:
                    argv = None
                if not argv:
                    log(f"{label}: on_receive {cmd!r} is not a command line; ignored")
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


# ── The letterbox ────────────────────────────────────────────────────────────

def scan(root: Path) -> dict[str, tuple[int, int]]:
    """Documents in the letterbox: relative path -> (size, mtime_ns)."""
    found: dict[str, tuple[int, int]] = {}
    if not root.is_dir():
        return found
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not letterbox.ignored(d)
                       and not os.path.islink(os.path.join(dirpath, d))]
        for name in filenames:
            if letterbox.ignored(name):
                continue
            p = Path(dirpath) / name
            try:
                if p.is_symlink() or not p.is_file():
                    continue
                st = p.stat()
            except OSError:
                continue
            found[p.relative_to(root).as_posix()] = (st.st_size, st.st_mtime_ns)
    return found


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _suffixed(target: Path, n: int) -> Path:
    """``a.b.c`` -> ``a-<n>.b.c``: the counter goes before the first dot, so
    a compound type suffix survives."""
    stem, dot, rest = target.name.partition(".")
    return target.with_name(f"{stem}-{n}{dot}{rest}")


def destination(accept: dict, name: str, arrived: float) -> Path:
    day = datetime.fromtimestamp(arrived, tz=timezone.utc)
    return (accept["chamber"] / accept["path"] / f"{day:%Y}" / f"{day:%m}"
            / f"{day:%d}" / name)


# ── Git ──────────────────────────────────────────────────────────────────────

def _git(chamber: Path, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return _run(["git", "-C", str(chamber), *args],
                capture_output=True, text=True, timeout=timeout)


def own_repo(chamber: Path) -> bool:
    try:
        top = _git(chamber, "rev-parse", "--show-toplevel")
    except (subprocess.SubprocessError, OSError):
        return False
    return top.returncode == 0 and Path(top.stdout.strip()).resolve() == chamber.resolve()


def dirty(chamber: Path) -> set[str]:
    """Paths (relative to the chamber) git reports as changed or untracked."""
    try:
        out = _git(chamber, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    except (subprocess.SubprocessError, OSError):
        return set()
    if out.returncode != 0:
        return set()
    paths: set[str] = set()
    fields = out.stdout.split("\0")
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        paths.add(entry[3:])
        if entry[0] in "RC":
            i += 1  # a rename's or copy's source path follows
    return paths


# ── State ────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, STATE_PATH)
    except OSError as exc:
        log(f"could not save state: {exc}")


def _chamber_state(state: dict, chamber: Path) -> dict:
    chambers = state.setdefault("chambers", {})
    entry = chambers.setdefault(str(chamber), {})
    if not isinstance(entry.get("pending"), list):
        entry["pending"] = []
    entry.setdefault("last_commit_at", 0)
    return entry


def add_pending(chamber: Path, paths: set[str]) -> None:
    state = load_state()
    entry = _chamber_state(state, chamber)
    entry["pending"] = sorted(set(entry["pending"]) | paths)
    save_state(state)


# ── Filing ───────────────────────────────────────────────────────────────────

def _derive(accept: dict, target: Path) -> bool:
    chamber = accept["chamber"]
    try:
        res = _run([*accept["on_receive"], str(target)], cwd=str(chamber),
                   env=session_env.build(), capture_output=True, text=True,
                   timeout=DERIVE_TIMEOUT)
    except subprocess.TimeoutExpired:
        log(f"{chamber.name}: on_receive timed out after {DERIVE_TIMEOUT:.0f}s on {target.name}")
        return False
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"{chamber.name}: on_receive could not run: {exc}")
        return False
    if res.returncode != 0:
        tail = (res.stderr or res.stdout or "").strip()[-2000:]
        log(f"{chamber.name}: on_receive exited {res.returncode} on {target.name}: {tail}")
        return False
    return True


def file_one(accept: dict, src: Path, arrived: float) -> str:
    """Move ``src`` into the accepting chamber, run its on_receive. Returns
    "filed", "duplicate" (the letterbox copy was dropped) or "failed" (the
    file stays in the letterbox)."""
    chamber = accept["chamber"]
    target = destination(accept, src.name, arrived)
    n = 1
    while target.exists():
        try:
            if target.is_file() and _sha256(target) == _sha256(src):
                src.unlink()
                log(f"{src.name}: already filed in {chamber.name} with the same content; "
                    "letterbox copy deleted")
                return "duplicate"
        except OSError as exc:
            log(f"{src.name}: could not compare with {target}: {exc}")
            return "failed"
        n += 1
        target = _suffixed(destination(accept, src.name, arrived), n)
    rel = target.relative_to(chamber).as_posix()
    # Recorded before the move: a crash right after it still leaves the file
    # due for a commit.
    add_pending(chamber, {rel})
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # The letterbox is usually another filesystem than the chamber, so this
        # is a copy: into a hidden sibling first, renamed into place whole.
        tmp = target.parent / f".{target.name}.letterbox-tmp"
        shutil.copy2(src, tmp)
        os.replace(tmp, target)
        src.unlink()
    except OSError as exc:
        log(f"{src.name}: could not file into {chamber.name}: {exc}")
        return "failed"
    log(f"{src.name} -> {chamber.name}/{rel} ({accept['name']})")
    if accept["on_receive"]:
        before = dirty(chamber)
        _derive(accept, target)
        touched = dirty(chamber) - before
        if touched:
            add_pending(chamber, touched)
    return "filed"


def commit_due(now: float, force: bool = False) -> None:
    """Commit and push every chamber whose interval is up and that has
    something pending."""
    state = load_state()
    for key in sorted((state.get("chambers") or {}).keys()):
        chamber = Path(key)
        entry = _chamber_state(state, chamber)
        if not entry["pending"]:
            continue
        if not force and now < float(entry["last_commit_at"] or 0) + COMMIT_INTERVAL:
            continue
        commit(chamber)


def commit(chamber: Path) -> bool:
    state = load_state()
    entry = _chamber_state(state, chamber)
    pending = set(entry["pending"])
    if not chamber.is_dir() or not own_repo(chamber):
        log(f"{chamber} is no git repository of its own; its filed files stay uncommitted")
        entry["pending"] = []
        save_state(state)
        return False
    now_dirty = dirty(chamber)
    paths = sorted(p for p in pending if p in now_dirty)
    if not paths:
        entry["pending"] = []
        save_state(state)
        return False
    n_filed = len(paths)
    msg = (f"letterbox: file {n_filed} path{'s' if n_filed != 1 else ''} "
           "taken in from the letterbox")
    try:
        res = _git(chamber, "add", "-A", "--", *paths)
        if res.returncode != 0:
            raise RuntimeError(f"git add: {res.stderr.strip()}")
        res = _git(chamber, "commit", "-m", msg, "--", *paths)
        if res.returncode != 0:
            raise RuntimeError(f"git commit: {(res.stderr or res.stdout).strip()}")
    except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
        # Everything stays pending; the next pass after the interval retries.
        log(f"{chamber.name}: commit failed ({exc}); retrying in {COMMIT_INTERVAL:.0f}s")
        entry["last_commit_at"] = _now()
        save_state(state)
        return False
    state = load_state()
    entry = _chamber_state(state, chamber)
    entry["pending"] = sorted(set(entry["pending"]) - pending)
    entry["last_commit_at"] = _now()
    save_state(state)
    try:
        res = _git(chamber, "push", timeout=120)
        if res.returncode != 0:
            log(f"{chamber.name}: push failed (goes out with the next one): {res.stderr.strip()}")
    except (subprocess.SubprocessError, OSError) as exc:
        log(f"{chamber.name}: push failed (goes out with the next one): {exc}")
    return True


def alert(title: str, text: str) -> None:
    """A dashboard thread for the user; best-effort."""
    try:
        _run([sys.executable, str(SCRIPTS_DIR / "conversation-push.py"),
              "--title", title, "--importance", "3", text],
             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"could not open a dashboard thread: {exc}")


# ── The loop ─────────────────────────────────────────────────────────────────

class Watcher:
    def __init__(self, root: Path | None = None):
        self.root = root or letterbox.LETTERBOX_DIR
        # relative path -> {"sig", "first_seen", "since", "done"}; `done` holds
        # the signature a file was last decided on (no match, ambiguous), so it
        # is not matched again every poll until it changes.
        self.files: dict[str, dict] = {}
        self.accepts_key = None

    def tick(self) -> None:
        now = _now()
        letterbox.ensure_manifest(self.root)
        current = scan(self.root)
        for rel in list(self.files):
            if rel not in current:
                del self.files[rel]
        accepts = None
        for rel, sig in sorted(current.items()):
            seen = self.files.get(rel)
            if seen is None or seen["sig"] != sig:
                self.files[rel] = {"sig": sig, "first_seen": seen["first_seen"] if seen else now,
                                   "since": now, "done": None}
                continue
            if now - seen["since"] < STABLE_SECONDS:
                continue
            if accepts is None:
                accepts = load_accepts()
                # A changed declaration re-opens every file it was decided on.
                key = json.dumps([[str(a["chamber"]), a["name"], a["glob"], a["json"], a["path"]]
                                  for a in accepts], sort_keys=True, default=str)
                if key != self.accepts_key:
                    self.accepts_key = key
                    for f in self.files.values():
                        f["done"] = None
            if seen["done"] == sig:
                continue
            self.decide(rel, seen, accepts)
        commit_due(now)

    def decide(self, rel: str, seen: dict, accepts: list[dict]) -> None:
        path = self.root / rel
        hits = [a for a in accepts if matches(a, path)]
        if not hits:
            seen["done"] = seen["sig"]  # the inbox sweep's, not ours
            return
        if len(hits) > 1:
            seen["done"] = seen["sig"]
            claim = ", ".join(f"{a['chamber'].name} ({a['name']})" for a in hits)
            log(f"AMBIGUOUS: {rel} is accepted by more than one declaration: {claim}; "
                "left in the letterbox")
            alert("Letterbox: a file two chambers claim",
                  f"The file `{rel}` in the letterbox matches the `accepts` declarations "
                  f"of more than one chamber: {claim}. It stays in the letterbox until "
                  "the declarations are fixed so that exactly one matches.")
            return
        if file_one(hits[0], path, seen["first_seen"]) == "failed":
            seen["done"] = seen["sig"]  # tried again once the file changes
        else:
            self.files.pop(rel, None)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--watch", action="store_true", help="poll the letterbox forever")
    mode.add_argument("--once", action="store_true",
                      help="one pass (files must already be stable), then flush due commits")
    args = ap.parse_args(argv)
    watcher = Watcher()
    if args.once:
        global STABLE_SECONDS
        STABLE_SECONDS = 0
        watcher.tick()
        watcher.tick()
        return 0
    log(f"watching {watcher.root} every {POLL_SECONDS:g}s")
    while True:
        try:
            watcher.tick()
        except Exception as exc:  # noqa: BLE001 — the watcher must outlive one bad pass
            log(f"pass failed: {exc}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
