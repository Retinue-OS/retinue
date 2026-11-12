#!/usr/bin/env python3
"""What happened lately about a person or subject — one lookup across sources.

Memory holds what a session chose to store; the recent course of events lives
elsewhere: messenger chats, dashboard threads, project logs. A session about
to ask the user something, or to write to someone autonomously, should know
what was said there in the last days. This gathers it on demand, so nothing
is added to every session's prompt:

    python3 /workspace/scripts/recent.py Roger            # last 7 days
    python3 /workspace/scripts/recent.py "Eignungsabklärung" --days 14
    python3 /workspace/scripts/recent.py Ari --source threads --source memory

A term that names a contact (contacts.py find --name) also matches that
person's messenger handles, so a chat whose text never repeats the name is
still found. Matching is case- and accent-insensitive. Read-only; prints
compact, prompt-ready lines, newest first per source.

Sources: chats (gateway message stores), threads (dashboard conversations,
including autonomous ones), memory (entries recorded in the window, read from
the files so a fresh store is not needed), projects (project files changed in
the window — the matching lines). E-mail is not covered: use
email_client.py search --text for that.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHAMBERS_DIR = Path(os.environ.get("CHAMBERS_DIR") or "/workspace/chambers")
CONVERSATIONS_DIR = Path(os.environ.get("CONVERSATIONS_DIR") or "/root/.retinue/conversations")
SOURCES = ("chats", "threads", "memory", "projects")
EXCERPT = 220


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fold(text: str) -> str:
    """Lower-case, accents stripped: 'Brändle' matches 'brandle' and 'BRÄNDLE'."""
    norm = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in norm if not unicodedata.combining(c)).casefold()


def excerpt(text: str, needles: list[str], width: int = EXCERPT) -> str:
    """One line around the first hit, so long messages stay short."""
    flat = " ".join((text or "").split())
    if len(flat) <= width:
        return flat
    low = fold(flat)
    pos = min((low.find(n) for n in needles if n and low.find(n) >= 0), default=0)
    start = max(0, pos - width // 3)
    cut = flat[start:start + width]
    return ("…" if start else "") + cut + ("…" if start + width < len(flat) else "")


def _iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        when = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)


def contact_handles(term: str, chambers_dir: Path) -> tuple[list[str], set[str]]:
    """Names and bare handles of the contacts the term names (best effort)."""
    try:
        contacts = _load("contacts")
        book = contacts.ContactBook(str(chambers_dir), None)
        hits = book.search(term)
    except Exception:  # an unreadable address book must not break the lookup
        return [], set()
    names, handles = [], set()
    for rec in hits or []:
        names.append(rec.get("name") or "")
        for h in contacts.handles_of(rec):
            handles.add(fold(str(h.get("handle", "")).split("@")[0]))
        for v in rec.get("emails") or []:
            handles.add(fold(v))
        for v in rec.get("phones") or []:
            handles.add(fold(str(v)))
    return [n for n in names if n], {h for h in handles if h}


def _bare(value: str | None) -> str:
    return fold((value or "").split("@")[0])


def scan_chats(root: Path, since: dt.datetime, needles: list[str], handles: set[str]):
    store = _load("inbound_store")
    floor = since.timestamp()
    for channel_dir in sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []:
        msgs = channel_dir / "messages"
        if not msgs.is_dir():
            continue
        for path in msgs.glob("*.nt"):
            try:
                if path.stat().st_mtime < floor:
                    continue
                fields = store._parse(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
            if not fields:
                continue
            outbound = "Outbound" in str(fields.get("type", ""))
            when = _iso(fields.get("sent_at") if outbound else fields.get("received_at"))
            if not when or when < since:
                continue
            text = fields.get("text") or ""
            who = {_bare(fields.get("chat")), _bare(fields.get("sender"))} - {""}
            if not (who & handles or any(n in fold(text) for n in needles)):
                continue
            arrow = "→" if outbound else "←"
            party = fields.get("chat") or fields.get("sender") or "?"
            yield when, f"{channel_dir.name} {arrow} {party}: {excerpt(text, needles) or '[media]'}"


def scan_threads(root: Path, since: dt.datetime, needles: list[str], names: list[str]):
    keys = needles + [fold(n) for n in names]
    for path in root.glob("*.json") if root.is_dir() else []:
        try:
            if path.stat().st_mtime < since.timestamp():
                continue
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        title = doc.get("title") or ""
        titled = any(k in fold(title) for k in keys)
        for msg in doc.get("messages") or []:
            when = _iso(msg.get("ts"))
            text = str(msg.get("text") or "")
            if not when or when < since:
                continue
            if titled or any(k in fold(text) for k in keys):
                yield when, (f"thread {doc.get('id', path.stem)} «{title}» "
                             f"[{msg.get('role', '?')}]: {excerpt(text, keys)}")


_MEM_ID = re.compile(r"^(\d{8}T\d{6}Z)-")
_CONTENT = re.compile(r'<https://w3id\.org/retinue/kb#content> "((?:[^"\\]|\\.)*)"')


def scan_memory(root: Path, since: dt.datetime, needles: list[str], names: list[str]):
    keys = needles + [fold(n) for n in names]
    for path in root.glob("*.nt") if root.is_dir() else []:
        m = _MEM_ID.match(path.name)
        if not m:
            continue
        when = dt.datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
        if when < since:
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for content in _CONTENT.findall(raw):
            text = content.encode().decode("unicode_escape", errors="replace") \
                if "\\u" in content else content.replace('\\"', '"').replace("\\n", " ")
            if any(k in fold(text) for k in keys):
                yield when, f"memory {path.stem}: {excerpt(text, keys)}"


def scan_projects(root: Path, since: dt.datetime, needles: list[str], names: list[str]):
    keys = needles + [fold(n) for n in names]
    floor = since.timestamp()
    for path in root.glob("*/**/projects/**/*.md") if root.is_dir() else []:
        try:
            st = path.stat()
            if st.st_mtime < floor:
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        hits = [l.strip() for l in lines if any(k in fold(l) for k in keys)]
        if hits:
            when = dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc)
            rel = path.relative_to(root)
            shown = " | ".join(excerpt(h, keys, 160) for h in hits[:3])
            more = f" (+{len(hits) - 3} more)" if len(hits) > 3 else ""
            yield when, f"project {rel}: {shown}{more}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("term", help="a person's name, a handle or a subject word")
    ap.add_argument("--days", type=float, default=7, help="look-back window (default 7)")
    ap.add_argument("--source", action="append", choices=SOURCES,
                    help="restrict to these sources (repeatable; default all)")
    ap.add_argument("--limit", type=int, default=12, help="lines per source (default 12)")
    args = ap.parse_args(argv)

    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)
    needles = [fold(args.term.strip())]
    names, handles = contact_handles(args.term, CHAMBERS_DIR)
    handles.add(_bare(args.term))
    messenger = Path(os.environ.get("TRIAGE_MESSENGER_DIR") or CHAMBERS_DIR / "_generated" / "messenger")
    scanners = {
        "chats": lambda: scan_chats(messenger, since, needles, handles),
        "threads": lambda: scan_threads(CONVERSATIONS_DIR, since, needles, names),
        "memory": lambda: scan_memory(CHAMBERS_DIR / "_generated" / "memory", since, needles, names),
        "projects": lambda: scan_projects(CHAMBERS_DIR, since, needles, names),
    }
    print(f"recent: «{args.term}» since {since:%Y-%m-%d %H:%M}Z"
          + (f" — contact: {', '.join(names)}" if names else ""))
    for src in args.source or SOURCES:
        rows = sorted(scanners[src](), key=lambda r: r[0], reverse=True)
        print(f"\n## {src} ({len(rows)})")
        for when, line in rows[:args.limit]:
            print(f"- {when:%m-%d %H:%M}Z {line}")
        if len(rows) > args.limit:
            print(f"- … {len(rows) - args.limit} older; narrow the term or --days")
    return 0


if __name__ == "__main__":
    sys.exit(main())
