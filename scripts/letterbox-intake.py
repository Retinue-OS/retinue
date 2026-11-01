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
   uploads in flight are never looked at (``letterbox.ignored``). A file
   under a name the framework would read (``letterbox.denied``: ``CLAUDE.md``,
   ``requirements.txt``, …) — the share refuses those, a sync peer does not —
   is **quarantined** on sight: renamed to ``.quarantined-<name>``, so no
   session and no entrypoint ever reads it, and the user is told once.
2. **Complete.** A file is taken only once its size and mtime have held still
   for ``LETTERBOX_STABLE_SECONDS`` (3).
3. **Matched.** Against every mounted chamber's ``accepts``
   (``letterbox.load_accepts``): ``match.glob`` on the file name, and, where
   given, ``match.json`` — each top-level key of the parsed file equal to the
   declared value. No match: left for the sweep (which, for its part, leaves
   every file that *does* match alone, so the two never work on one file).
   More than one: left in place, logged and reported to the user in a
   dashboard thread — two chambers claiming one file is a configuration error
   nobody should guess around.
4. **Claimed.** Exactly one match: the file is renamed to a hidden name first
   (``.intake-<token>-<name>``). From then on nothing else sees it — not the
   sweep, not the share — and a client writing the original name again
   creates a new file, which is scanned afresh. The claimed file is opened
   once and its identity (size, mtime, inode) checked against what was
   scanned, after the copy and again before the letterbox copy is deleted: a
   mismatch means somebody replaced it under us, and the attempt is abandoned
   with the file put back under its name.
5. **Filed.** It moves to ``<chamber>/<path>/<YYYY>/<MM>/<DD>/<name>`` (the
   UTC date it was first seen). A file of that name already there with the
   same content means the drop was a resend: the letterbox copy is just
   deleted. Different content keeps both, the newcomer with a ``-2``,
   ``-3``, … before its first dot.
6. **Derived.** ``on_receive``, if declared, runs with the chamber root as
   cwd and the filed file's absolute path as its last argument, in its own
   process group under ``LETTERBOX_DERIVE_TIMEOUT`` (120 s) — the whole group
   is killed on timeout, not just the shell that started it — and the
   allowlisted session environment; a failure is logged, the file stays
   filed.
7. **Committed and pushed.** Filed and derived files are operational output
   (Tier 1): committed to the chamber and pushed at most once per
   ``LETTERBOX_COMMIT_INTERVAL`` seconds (600) per chamber. What is due is
   persisted in ``LETTERBOX_INTAKE_STATE`` *before* a file moves, and the
   loop flushes it as soon as the interval is up. A push rebases on the
   remote first (``git pull --rebase``); one that still fails stays due
   (``push_due``) and is retried every interval, with nothing new having to
   arrive. So a restart or a failure delays a commit or a push, never loses
   it. After **three consecutive failures** of the commit or the push the
   user is told once in a dashboard thread, and again only after a success
   and three new failures. Only the chamber's own repository is committed to,
   and only those paths (``git commit -- <paths>``), so other staged work in
   the chamber is never swept along. Which paths ``on_receive`` touched is
   read off ``git status`` before and after it runs.

Run by the entrypoint as ``letterbox-intake.py --watch`` (supervised: it is
restarted if it dies); ``--once`` runs a single pass (and flushes due
commits) for tests and by hand.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import signal
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
# Consecutive commit-or-push failures before the user hears of it.
ALERT_AFTER_FAILURES = 3
CLAIM_PREFIX = ".intake-"
SCRIPTS_DIR = Path(os.path.dirname(os.path.abspath(__file__)))

# Seams for the tests.
_now = time.time
_run = subprocess.run
_popen = subprocess.Popen


def log(msg: str) -> None:
    print(f"[letterbox-intake] {msg}", file=sys.stderr, flush=True)


# ── The letterbox ────────────────────────────────────────────────────────────

Sig = tuple[int, int, int]  # size, mtime_ns, inode


def _sig(st: os.stat_result) -> Sig:
    return (st.st_size, st.st_mtime_ns, st.st_ino)


def scan(root: Path) -> tuple[dict[str, Sig], list[Path]]:
    """Documents in the letterbox (relative path -> signature), and every
    entry under a denied name that must be quarantined."""
    found: dict[str, Sig] = {}
    denied: list[Path] = []
    if not root.is_dir():
        return found, denied
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        keep = []
        for d in dirnames:
            if os.path.islink(os.path.join(dirpath, d)):
                continue
            if letterbox.denied(d):
                denied.append(here / d)
            elif not letterbox.ignored(d):
                keep.append(d)
        dirnames[:] = keep
        for name in filenames:
            p = here / name
            if letterbox.denied(name):
                if name != ".inbox.json" or here != root:  # ours, at the root
                    denied.append(p)
                continue
            if letterbox.ignored(name):
                continue
            try:
                if p.is_symlink() or not p.is_file():
                    continue
                st = p.stat()
            except OSError:
                continue
            found[p.relative_to(root).as_posix()] = _sig(st)
    return found, denied


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
    entry.setdefault("push_due", False)
    entry.setdefault("failures", 0)
    entry.setdefault("alerted", False)
    return entry


def add_pending(chamber: Path, paths: set[str]) -> None:
    state = load_state()
    entry = _chamber_state(state, chamber)
    entry["pending"] = sorted(set(entry["pending"]) | paths)
    save_state(state)


# ── Filing ───────────────────────────────────────────────────────────────────

def _derive(accept: dict, target: Path) -> bool:
    """Run on_receive in its own process group, so a timeout kills whatever
    it started (a `sh -c '… &'` would otherwise outlive the kill)."""
    chamber = accept["chamber"]
    argv = [*accept["on_receive"], str(target)]
    try:
        proc = _popen(argv, cwd=str(chamber), env=session_env.build(),
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                      start_new_session=True)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"{chamber.name}: on_receive could not run: {exc}")
        return False
    try:
        out, err = proc.communicate(timeout=DERIVE_TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.communicate()
        log(f"{chamber.name}: on_receive timed out after {DERIVE_TIMEOUT:.0f}s on "
            f"{target.name}; its process group was killed")
        return False
    if proc.returncode != 0:
        tail = (err or out or "").strip()[-2000:]
        log(f"{chamber.name}: on_receive exited {proc.returncode} on {target.name}: {tail}")
        return False
    return True


class Replaced(Exception):
    """The letterbox file is not the one that was scanned any more."""


def _check_identity(fd: int, sig: Sig, what: str) -> None:
    if _sig(os.fstat(fd)) != sig:
        raise Replaced(what)


def file_one(accept: dict, claimed: Path, name: str, sig: Sig, arrived: float) -> str:
    """Copy the claimed letterbox file into the accepting chamber, run its
    on_receive, delete the letterbox copy. Returns "filed", "duplicate" (the
    letterbox copy was dropped) or "failed" (the caller puts the file back).
    ``sig`` is the identity the file had when it was scanned; a file that
    does not match it any more is left alone."""
    chamber = accept["chamber"]
    target = destination(accept, name, arrived)
    tmp = None
    try:
        with open(claimed, "rb") as src:
            _check_identity(src.fileno(), sig, "before the copy")
            src_hash = None
            n = 1
            while target.exists():
                if src_hash is None:
                    src_hash = hashlib.sha256(src.read()).hexdigest()
                    src.seek(0)
                if target.is_file() and _sha256(target) == src_hash:
                    _check_identity(src.fileno(), sig, "before the delete")
                    claimed.unlink()
                    log(f"{name}: already filed in {chamber.name} with the same content; "
                        "letterbox copy deleted")
                    return "duplicate"
                n += 1
                target = _suffixed(destination(accept, name, arrived), n)
            rel = target.relative_to(chamber).as_posix()
            # Recorded before the move: a crash right after it still leaves
            # the file due for a commit.
            add_pending(chamber, {rel})
            target.parent.mkdir(parents=True, exist_ok=True)
            # The letterbox is usually another filesystem than the chamber,
            # so this is a copy: into a hidden sibling first, renamed into
            # place whole.
            tmp = target.parent / f".{target.name}.letterbox-tmp"
            with open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            _check_identity(src.fileno(), sig, "after the copy")
            os.replace(tmp, target)
            tmp = None
            _check_identity(src.fileno(), sig, "before the delete")
            claimed.unlink()
    except Replaced as why:
        log(f"{name}: replaced under us ({why}); not filed, will be looked at again")
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass
        return "failed"
    except OSError as exc:
        log(f"{name}: could not file into {chamber.name}: {exc}")
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass
        return "failed"
    log(f"{name} -> {chamber.name}/{rel} ({accept['name']})")
    if accept["on_receive"]:
        before = dirty(chamber)
        _derive(accept, target)
        touched = dirty(chamber) - before
        if touched:
            add_pending(chamber, touched)
    return "filed"


# ── Commit and push ──────────────────────────────────────────────────────────

def commit_due(now: float) -> None:
    """Every chamber whose interval is up: commit what is pending, push what
    is due."""
    state = load_state()
    for key in sorted((state.get("chambers") or {}).keys()):
        chamber = Path(key)
        entry = _chamber_state(state, chamber)
        if not entry["pending"] and not entry["push_due"]:
            continue
        if now < float(entry["last_commit_at"] or 0) + COMMIT_INTERVAL:
            continue
        if entry["pending"]:
            commit(chamber)
        elif entry["push_due"]:
            push(chamber)


def _failed(chamber: Path, what: str, detail: str) -> None:
    """One more consecutive failure; the user hears of the third."""
    state = load_state()
    entry = _chamber_state(state, chamber)
    entry["failures"] = int(entry["failures"]) + 1
    entry["last_commit_at"] = _now()
    n = entry["failures"]
    log(f"{chamber.name}: {what} failed ({detail}); {n} in a row, retrying in "
        f"{COMMIT_INTERVAL:.0f}s")
    if n >= ALERT_AFTER_FAILURES and not entry["alerted"]:
        entry["alerted"] = True
        save_state(state)
        alert(f"Letterbox: {chamber.name} cannot be {what}ed",
              f"Files taken in from the letterbox for the chamber `{chamber.name}` have "
              f"been filed, but the {what} has failed {n} times in a row: {detail}. "
              "The files are on disk in the chamber and the attempt repeats every "
              f"{COMMIT_INTERVAL / 60:.0f} minutes; the repository needs a look.")
        return
    save_state(state)


def _succeeded(chamber: Path, **changes) -> None:
    state = load_state()
    entry = _chamber_state(state, chamber)
    entry.update(changes)
    entry["failures"] = 0
    entry["alerted"] = False
    entry["last_commit_at"] = _now()
    save_state(state)


def commit(chamber: Path) -> bool:
    state = load_state()
    entry = _chamber_state(state, chamber)
    pending = set(entry["pending"])
    if not chamber.is_dir() or not own_repo(chamber):
        log(f"{chamber} is no git repository of its own; its filed files stay uncommitted")
        entry["pending"] = []
        entry["push_due"] = False
        save_state(state)
        return False
    now_dirty = dirty(chamber)
    paths = sorted(p for p in pending if p in now_dirty)
    if not paths:
        entry["pending"] = []
        save_state(state)
        return push(chamber) if entry["push_due"] else False
    n = len(paths)
    msg = f"letterbox: file {n} path{'s' if n != 1 else ''} taken in from the letterbox"
    try:
        res = _git(chamber, "add", "-A", "--", *paths)
        if res.returncode != 0:
            raise RuntimeError(f"git add: {res.stderr.strip()}")
        res = _git(chamber, "commit", "-m", msg, "--", *paths)
        if res.returncode != 0:
            raise RuntimeError(f"git commit: {(res.stderr or res.stdout).strip()}")
    except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
        _failed(chamber, "commit", str(exc))  # everything stays pending
        return False
    state = load_state()
    entry = _chamber_state(state, chamber)
    entry["pending"] = sorted(set(entry["pending"]) - pending)
    entry["push_due"] = True
    save_state(state)
    return push(chamber)


def push(chamber: Path) -> bool:
    """Rebase on the remote and push. Stays due until it succeeds."""
    try:
        res = _git(chamber, "pull", "--rebase", "--autostash", timeout=120)
        if res.returncode != 0:
            raise RuntimeError(f"git pull --rebase: {(res.stderr or res.stdout).strip()}")
        res = _git(chamber, "push", timeout=120)
        if res.returncode != 0:
            raise RuntimeError(f"git push: {(res.stderr or res.stdout).strip()}")
    except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
        _run(["git", "-C", str(chamber), "rebase", "--abort"], capture_output=True, timeout=60)
        _failed(chamber, "push", str(exc))
        return False
    _succeeded(chamber, push_due=False)
    return True


def alert(title: str, text: str) -> None:
    """A dashboard thread for the user; best-effort."""
    try:
        _run([sys.executable, str(SCRIPTS_DIR / "conversation-push.py"),
              "--title", title, "--importance", "3", text],
             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"could not open a dashboard thread: {exc}")


# ── Quarantine ───────────────────────────────────────────────────────────────

def quarantine(root: Path, paths: list[Path]) -> None:
    """Rename every entry under a denied name to its quarantine name, and
    tell the user once per name."""
    if not paths:
        return
    state = load_state()
    told = set(state.get("quarantined") or [])
    new = []
    for p in paths:
        q = p.with_name(letterbox.quarantine_name(p.name))
        rel = p.relative_to(root).as_posix()
        try:
            if q.exists():
                q = p.with_name(letterbox.quarantine_name(f"{secrets.token_hex(3)}-{p.name}"))
            os.rename(p, q)
        except OSError as exc:
            log(f"QUARANTINE: could not rename {rel}: {exc}")
            continue
        log(f"QUARANTINE: {rel} -> {q.name} (a name the framework would read; never filed)")
        if rel not in told:
            new.append(rel)
            told.add(rel)
    if new:
        state["quarantined"] = sorted(told)
        save_state(state)
        names = ", ".join(f"`{n}`" for n in new)
        alert("Letterbox: a file with a reserved name was quarantined",
              f"Something put {names} into the letterbox. Files of that name are read by "
              "the framework or by Claude Code as instructions or configuration, so it was "
              f"renamed with the prefix `{letterbox.QUARANTINE_PREFIX}` and left there, "
              "unread. If it was yours, rename it to what it is; if not, check what writes "
              "into the letterbox folder.")


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
        current, denied = scan(self.root)
        quarantine(self.root, denied)
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
                accepts = letterbox.load_accepts(CHAMBERS_DIR, log=log)
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
        hits = letterbox.accepted_by(accepts, path)
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
        claimed = self.claim(path, seen["sig"])
        if claimed is None:
            return  # changed since the scan: seen again next poll
        result = file_one(hits[0], claimed, path.name, seen["sig"], seen["first_seen"])
        if result == "failed":
            self.release(claimed, path)
            seen["done"] = seen["sig"]  # tried again once the file changes
        else:
            self.files.pop(rel, None)

    def claim(self, path: Path, sig: Sig) -> Path | None:
        """Rename the file to a hidden name, so nothing else sees it while
        it is filed; None when it is no longer the file that was scanned."""
        claimed = path.with_name(f"{CLAIM_PREFIX}{secrets.token_hex(4)}-{path.name}")
        try:
            if _sig(path.stat()) != sig:
                return None
            os.rename(path, claimed)
            if _sig(claimed.stat()) != sig:
                self.release(claimed, path)
                return None
        except OSError as exc:
            log(f"{path.name}: could not claim: {exc}")
            return None
        return claimed

    def release(self, claimed: Path, path: Path) -> None:
        """Put a claimed file back under a visible name."""
        back = path if not path.exists() else path.with_name(f"recovered-{path.name}")
        try:
            os.rename(claimed, back)
        except OSError as exc:
            log(f"{claimed.name}: could not put back as {back.name}: {exc}")


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
