#!/usr/bin/env python3
"""The notice board — portal notifications that can wait, kept out of the way.

Many mails announce rather than say: "a new document is in your portal",
"3 hours were logged", "your statement is ready". Each one alone is noise in
the user's chats; together they are a to-do list the user clears now and then
("go and read the portal", "check this month's hours"). The board is that
list: every notice becomes a small record here, notices from one source and
account form a **group** shown as one line, and the user — or a session on
their word — **handles** a group, which closes everything in it up to now.
The mail itself can then be let go (its arrival turn gives it the triage word
*delete*, docs/chat-triage.md).

    notices.py add --source "Example Insurance" --account 12345 \\
        --text "1 new document: general correspondence" \\
        --ref "email:~owner@owner.example:<message-id>"

    notices.py list                 # open groups, one line each
    notices.py list --group example-insurance/12345 --json
    notices.py handle example-insurance/12345   # closes the group up to now
    notices.py handle --id 20261127T081500Z-3fa2c1

**Urgent notices.** A notice that can wait goes on the board and rings
nothing. One that cannot gets a thread as well: a notice is urgent only when
the session can quote from the message itself both a **deadline** (`--due`)
and a **consequence** (`--consequence`: "otherwise the account is locked",
"a reminder fee", "the code expires"). With both, `add` also opens a dashboard
thread of importance 4.5 with that deadline (critical when it is less than a
day away), whose context names the notice so the turn that settles it can
close it (`handle --id`). A deadline without a consequence is just a date on
the board. When even the frontier tier cannot tell, `--unsure` keeps the
deadline but opens the thread at importance 3, so it is held for the next
digest instead of ringing, and the board marks it "deadline?".

Ringing is the frontier tier's decision (scripts/ring_guard.py): the thread is
opened through conversation-push.py, whose guard refuses a lower tier with
exit 3, which `add` passes on — escalate and the turn is re-run. `add` is
idempotent per source and `--ref`, so the re-run finds the notice already
filed rather than filing it twice.

Storage: one N-Triples file per notice, and one per handling, in
``$CHAMBERS_DIR/_generated/notices/`` (override: RETINUE_NOTICES_DIR) — the
framework's own pseudo-chamber, indexed into the life store like memory and
never pushed to any repository. A group is open while it holds a notice that
arrived after the group's latest handling and is not handled by id.

    <urn:retinue:notice:ID> a kb:Notice ;
        kb:source "…" ; kb:account "…" ; kb:group "source-slug/account-slug" ;
        kb:content "…" ; kb:arrivedAt "…"^^xsd:dateTime ; kb:ref "…" ;
        kb:due "…"^^xsd:dateTime ; kb:consequence "…" ; kb:urgency "urgent"|"unsure" ;
        kb:thread "…" .
    <urn:retinue:notice-handling:ID> a kb:NoticeHandling ;
        kb:group "…" | kb:handles <urn:retinue:notice:ID> ;
        kb:handledAt "…"^^xsd:dateTime ; kb:actor <urn:retinue:actor:…> .

Design and the rules sessions follow: docs/notices.md.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
KB = "https://w3id.org/retinue/kb#"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
XSD_DT = "http://www.w3.org/2001/XMLSchema#dateTime"
URGENT_IMPORTANCE = 4.5
UNSURE_IMPORTANCE = 3.0
CRITICAL_WITHIN = timedelta(hours=24)


def notices_dir() -> Path:
    explicit = os.environ.get("RETINUE_NOTICES_DIR", "").strip()
    if explicit:
        return Path(explicit)
    return Path(os.environ.get("CHAMBERS_DIR") or "/workspace/chambers") / "_generated" / "notices"


# ── small helpers ────────────────────────────────────────────────────────────

def slug(text: str) -> str:
    """Lowercase ASCII-ish slug, any script kept as letters (\\w is Unicode)."""
    norm = unicodedata.normalize("NFKD", text or "")
    norm = "".join(c for c in norm if not unicodedata.combining(c)).lower()
    return re.sub(r"[^\w]+", "-", norm).strip("-_") or "-"


def group_key(source: str, account: str | None) -> str:
    return f"{slug(source)}/{slug(account)}" if account else slug(source)


def now_utc() -> datetime:
    fixed = os.environ.get("RETINUE_NOTICES_NOW", "").strip()  # tests
    return parse_when(fixed) if fixed else datetime.now(timezone.utc).replace(microsecond=0)


def parse_when(text: str) -> datetime:
    """ISO date-time (zone optional: local time then), or YYYY-MM-DD (17:00)."""
    s = text.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        s += "T17:00"
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _lit(value: str) -> str:
    return '"' + (value.replace("\\", "\\\\").replace('"', '\\"')
                  .replace("\n", "\\n").replace("\r", "\\r")) + '"'


def _unlit(value: str) -> str:
    out, i = [], 0
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value):
            out.append({"n": "\n", "r": "\r", '"': '"', "\\": "\\"}.get(value[i + 1], value[i + 1]))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


_TRIPLE = re.compile(r'^<([^>]+)> <([^>]+)> (?:<([^>]+)>|"((?:[^"\\]|\\.)*)"(?:\^\^<[^>]+>)?) \.$')


def read_file(path: Path) -> dict:
    """Our own files back into {"uri": …, property: value | [values]}."""
    rec: dict = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _TRIPLE.match(line.strip())
        if not m:
            continue
        subj, pred, obj_uri, obj_lit = m.groups()
        rec.setdefault("uri", subj)
        key = pred.rsplit("#", 1)[-1]
        rec[key] = obj_uri if obj_uri is not None else _unlit(obj_lit)
    return rec


def write_record(kind: str, rec_id: str, fields: list[tuple[str, str, str]]) -> Path:
    """fields: (property, value, 'lit'|'dt'|'uri')."""
    subj = f"<urn:retinue:{kind}:{rec_id}>"
    lines = [f"{subj} <{RDF_TYPE}> <{KB}{'Notice' if kind == 'notice' else 'NoticeHandling'}> ."]
    for prop, value, typ in fields:
        if typ == "uri":
            obj = f"<{value}>"
        elif typ == "dt":
            obj = f"{_lit(value)}^^<{XSD_DT}>"
        else:
            obj = _lit(value)
        lines.append(f"{subj} <{KB}{prop}> {obj} .")
    d = notices_dir()
    d.mkdir(parents=True, exist_ok=True)
    prefix = "" if kind == "notice" else "handled-"
    path = d / f"{prefix}{rec_id}.nt"
    tmp = path.with_suffix(".nt.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def new_id(at: datetime) -> str:
    return at.strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)


# ── the board ────────────────────────────────────────────────────────────────

def load() -> tuple[list[dict], list[dict]]:
    d = notices_dir()
    if not d.is_dir():
        return [], []
    notices, handlings = [], []
    for path in sorted(d.glob("*.nt")):
        rec = read_file(path)
        if not rec:
            continue
        rec["id"] = rec["uri"].rsplit(":", 1)[-1]
        (handlings if path.name.startswith("handled-") else notices).append(rec)
    return notices, handlings


def open_notices(notices: list[dict], handlings: list[dict]) -> list[dict]:
    upto: dict[str, str] = {}
    by_id: set[str] = set()
    for h in handlings:
        if h.get("handles"):
            by_id.add(h["handles"])
        elif h.get("group"):
            g = h["group"]
            upto[g] = max(upto.get(g, ""), h.get("handledAt", ""))
    # ISO-Z strings of one format compare as times.
    return [n for n in notices
            if n["uri"] not in by_id and n.get("arrivedAt", "") > upto.get(n.get("group", ""), "")]


def groups(open_list: list[dict]) -> list[dict]:
    out: dict[str, dict] = {}
    for n in sorted(open_list, key=lambda n: n.get("arrivedAt", "")):
        g = out.setdefault(n["group"], {"group": n["group"], "source": n.get("source", ""),
                                         "account": n.get("account"), "notices": []})
        g["notices"].append(n)
    result = []
    for g in out.values():
        ns = g["notices"]
        dues = sorted(n["due"] for n in ns if n.get("due"))
        g.update(count=len(ns), since=ns[0].get("arrivedAt"), latest=ns[-1].get("arrivedAt"),
                 next_due=dues[0] if dues else None,
                 urgent=any(n.get("urgency") == "urgent" for n in ns),
                 unsure=any(n.get("urgency") == "unsure" for n in ns))
        result.append(g)
    # Deadlines first (nearest first), then the oldest waiting.
    result.sort(key=lambda g: (g["next_due"] is None, g["next_due"] or "", g["since"] or ""))
    return result


def board(group: str | None = None) -> list[dict]:
    notices, handlings = load()
    gs = groups(open_notices(notices, handlings))
    return [g for g in gs if g["group"] == group] if group else gs


def _short(ts: str | None) -> str:
    return (ts or "")[:16].replace("T", " ")


def render(gs: list[dict], detail: bool = False) -> str:
    if not gs:
        return "The board is empty."
    lines = []
    for g in gs:
        who = g["source"] + (f" ({g['account']})" if g.get("account") else "")
        mark = " — URGENT" if g["urgent"] else " — deadline?" if g["unsure"] else ""
        due = f", next deadline {_short(g['next_due'])}" if g["next_due"] else ""
        lines.append(f"[{g['group']}] {who}: {g['count']} waiting since {_short(g['since'])}{due}{mark}")
        shown = g["notices"] if detail else g["notices"][-3:]
        if not detail and g["count"] > 3:
            lines.append(f"    … {g['count'] - 3} earlier")
        for n in shown:
            extra = f" (due {_short(n['due'])})" if n.get("due") else ""
            lines.append(f"    {_short(n.get('arrivedAt'))}  {n.get('content', '')}{extra}  [{n['id']}]")
    return "\n".join(lines)


def handle(group: str | None = None, notice_id: str | None = None, actor: str = "ara",
           at: datetime | None = None) -> dict:
    """Close a group up to ``at`` (now), or one notice by id."""
    at = at or now_utc()
    notices, handlings = load()
    open_list = open_notices(notices, handlings)
    fields = [("handledAt", iso(at), "dt"), ("actor", f"urn:retinue:actor:{slug(actor)}", "uri")]
    if notice_id:
        match = [n for n in open_list if n["id"] == notice_id]
        if not match:
            raise LookupError(f"no open notice with id {notice_id}")
        fields.insert(0, ("handles", match[0]["uri"], "uri"))
        closed = match
    else:
        closed = [n for n in open_list if n["group"] == group and n.get("arrivedAt", "") <= iso(at)]
        if not closed:
            known = sorted({n["group"] for n in open_list})
            raise LookupError(f"no open group {group!r}" + (f"; open: {', '.join(known)}" if known else ""))
        fields.insert(0, ("group", group, "lit"))
    write_record("notice-handling", new_id(at), fields)
    return {"handled": len(closed), "group": group, "id": notice_id,
            "notices": [n["id"] for n in closed]}


def add(source: str, text: str, account: str | None = None, ref: str | None = None,
        at: datetime | None = None, due: datetime | None = None,
        consequence: str | None = None, unsure: bool = False) -> tuple[dict, bool]:
    """File one notice; returns (record, created). Idempotent per source+ref."""
    at = at or now_utc()
    key = group_key(source, account)
    if ref:
        notices, _ = load()
        for n in notices:
            if n.get("group") == key and n.get("ref") == ref:
                return n, False
    rec_id = new_id(at)
    urgency = None
    if due is not None and unsure:
        urgency = "unsure"
    elif due is not None and consequence:
        urgency = "urgent"
    fields = [("source", source, "lit"), ("group", key, "lit"), ("content", text, "lit"),
              ("arrivedAt", iso(at), "dt")]
    if account:
        fields.append(("account", account, "lit"))
    if ref:
        fields.append(("ref", ref, "lit"))
    if due is not None:
        fields.append(("due", iso(due), "dt"))
    if consequence:
        fields.append(("consequence", consequence, "lit"))
    if urgency:
        fields.append(("urgency", urgency, "lit"))
    write_record("notice", rec_id, fields)
    rec = {"id": rec_id, "uri": f"urn:retinue:notice:{rec_id}", "group": key, "source": source,
           "account": account, "content": text, "arrivedAt": iso(at), "ref": ref,
           "due": iso(due) if due else None, "consequence": consequence, "urgency": urgency}
    return rec, True


def set_thread(rec: dict, thread_id: str) -> None:
    """Record the thread an urgent notice opened, rewriting its own file."""
    path = notices_dir() / f"{rec['id']}.nt"
    text = path.read_text(encoding="utf-8")
    line = f"<{rec['uri']}> <{KB}thread> {_lit(thread_id)} .\n"
    tmp = path.with_suffix(".nt.tmp")
    tmp.write_text(text + line, encoding="utf-8")
    os.replace(tmp, path)


def open_thread(rec: dict, sphere: str | None, now: datetime) -> tuple[int, str | None]:
    """The urgent (or unsure) notice's own thread, via conversation-push.py —
    whose ring guard decides for a lower tier. Returns (exit, thread id)."""
    due = datetime.fromisoformat(rec["due"].replace("Z", "+00:00"))
    urgent = rec["urgency"] == "urgent"
    who = rec["source"] + (f" ({rec['account']})" if rec.get("account") else "")
    body = [f"**{who}**: {rec['content']}", "",
            f"**Deadline:** {_short(rec['due'])} UTC"]
    if rec.get("consequence"):
        body.append(f"**Otherwise:** {rec['consequence']}")
    if not urgent:
        body.append("")
        body.append("Whether this deadline binds could not be told from the message, so it waits "
                    "for the digest instead of ringing.")
    cmd = [sys.executable, str(SCRIPTS_DIR / "conversation-push.py"),
           "--title", f"{rec['source']}: {rec['content']}"[:120],
           "--key", f"notice:{rec['id']}",
           "--importance", str(URGENT_IMPORTANCE if urgent else UNSURE_IMPORTANCE),
           "--due", rec["due"], "--kind", "deadline notice",
           "--context", (f"Notice board entry {rec['id']} (group {rec['group']}). Once the user "
                         f"has dealt with it, close it: python3 /workspace/scripts/notices.py "
                         f"handle --id {rec['id']}")]
    if urgent and due - now <= CRITICAL_WITHIN:
        cmd.append("--critical")
    if sphere:
        cmd += ["--sphere", sphere]
    cmd.append("\n".join(body))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        return proc.returncode, None
    try:
        return 0, json.loads(proc.stdout.strip().splitlines()[0]).get("id")
    except (ValueError, IndexError, AttributeError):
        return 0, None


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The notice board (docs/notices.md).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="file one notice")
    a.add_argument("--source", required=True, help="who sends it: the portal or organisation")
    a.add_argument("--account", help="which account or person of that source it concerns")
    a.add_argument("--text", required=True, help="what it announces, in one line")
    a.add_argument("--ref", help="where it came from (a chat id, a URL); makes add idempotent")
    a.add_argument("--at", help="when it arrived (default now)")
    a.add_argument("--due", help="a deadline quoted from the message (ISO, or YYYY-MM-DD for 17:00)")
    a.add_argument("--consequence", help="what happens otherwise, quoted from the message")
    a.add_argument("--unsure", action="store_true",
                   help="the frontier tier cannot tell whether the deadline binds: digest, not ring")
    a.add_argument("--sphere", help="sphere for the urgent notice's thread")
    a.add_argument("--json", action="store_true")

    ls = sub.add_parser("list", help="the open groups")
    ls.add_argument("--group", help="only this group (as list shows it)")
    ls.add_argument("--detail", action="store_true", help="every notice, not the last three")
    ls.add_argument("--json", action="store_true")

    h = sub.add_parser("handle", help="close a group up to now, or one notice")
    h.add_argument("group", nargs="?")
    h.add_argument("--id", dest="notice_id")
    h.add_argument("--actor", default="ara")
    h.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)

    if args.cmd == "list":
        gs = board(args.group)
        if args.json:
            print(json.dumps(gs, ensure_ascii=False, indent=2))
        else:
            print(render(gs, args.detail))
        return 0

    if args.cmd == "handle":
        if bool(args.group) == bool(args.notice_id):
            print("notices: handle takes a group or --id, not both", file=sys.stderr)
            return 2
        try:
            result = handle(args.group, args.notice_id, args.actor)
        except LookupError as exc:
            print(f"notices: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result) if args.json else
              f"Handled {result['handled']} notice(s) of {args.group or args.notice_id}.")
        return 0

    # add
    if args.consequence and not args.due:
        print("notices: --consequence without --due is no deadline; give the deadline the "
              "message names, or leave both out", file=sys.stderr)
        return 2
    if args.unsure and not args.due:
        print("notices: --unsure qualifies a deadline; give --due", file=sys.stderr)
        return 2
    try:
        at = parse_when(args.at) if args.at else None
        due = parse_when(args.due) if args.due else None
    except ValueError as exc:
        print(f"notices: not a date-time: {exc}", file=sys.stderr)
        return 2
    rec, created = add(args.source, args.text, args.account, args.ref, at, due,
                       args.consequence, args.unsure)
    code = 0
    if rec.get("urgency") and not rec.get("thread"):
        # Also on a re-run (escalation): the first attempt may have been refused.
        code, thread_id = open_thread(rec, args.sphere, now_utc())
        if thread_id:
            set_thread(rec, thread_id)
            rec["thread"] = thread_id
    if args.json:
        print(json.dumps({"created": created, **rec}, ensure_ascii=False))
    else:
        state = "filed" if created else "already on the board"
        extra = f"; thread {rec['thread']}" if rec.get("thread") else ""
        print(f"{rec['id']} {state} in [{rec['group']}]{extra}")
    return code


if __name__ == "__main__":
    sys.exit(main())
