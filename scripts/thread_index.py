"""Thread summaries in the life store: one N-Triples file per dashboard thread.

The dashboard's threads are JSON files the web gateway owns
(CONVERSATIONS_DIR); they stay the single source of truth. This module derives
from each one a small, deterministic summary — what a list of threads needs to
filter and order by, nothing a thread *says* — and writes it to
``<chambers>/_generated/threads/<id>.nt``, where the life store indexes it as a
named graph of its own (``<file:_generated/threads/<id>.nt>``).

Two things follow:

* The Archived list is a SPARQL ``ORDER BY … LIMIT … OFFSET`` instead of a
  parse of every thread file on every poll (``page_query``). The gateway still
  reads the page's own files to build the rows, so a thread deleted or
  restored a moment ago, which the store has not caught up with yet, is
  dropped there.
* Threads are linked data like the rest: ``urn:retinue:thread:<id>`` is the
  same subject the attention emit already uses, linked to its project and,
  for a companion, to its messenger chat.

What goes in, per thread (subject ``urn:retinue:thread:<id>``):

    rdf:type kb:Thread ; dcterms:title ; dcterms:created ; dcterms:modified ;
    kb:kind ; kb:initiator ; kb:archived ; kb:muted ; kb:starred ; kb:unread ;
    kb:messageCount ; kb:project (when linked) ; kb:companionOf (a companion)

What never goes in: message texts, previews (a preview is the start of the
last message), attachments, shell actions, context notes, the model choice or
the transient ``pending`` flag. The store is readable by every agent; a
thread's words stay in its file.

A deleted thread's file is removed, which drops its named graph: nothing about
it remains in the store. The deletion tombstone stays the gateway's own.
"""
from __future__ import annotations

import os
import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

KB = "https://w3id.org/retinue/kb#"
DCT = "http://purl.org/dc/terms/"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
XSD = "http://www.w3.org/2001/XMLSchema#"

# The gateway's thread id format (_CONV_ID_RE): also what keeps an id safe as
# a file name and inside an IRI.
_ID_RE = re.compile(r"[0-9a-f]{32}")
# A project link is spliced into an IRI position; anything N-Triples forbids
# there makes the link unusable rather than the file unloadable.
_IRI_FORBIDDEN = re.compile(r'[\x00-\x20<>"{}|^`\\]')
_IRI_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")

FLAGS = ("archived", "muted", "starred", "unread")


def subject(cid: str) -> str:
    return f"urn:retinue:thread:{cid}"


def chat_subject(chat_id: str) -> str:
    """The chat's IRI as the attention emit writes it (attention_store.subject_iri)."""
    return "urn:retinue:chat:" + urllib.parse.quote(str(chat_id), safe=":~+@=.-_")


def path_for(out_dir: Path, cid: str) -> Path:
    return Path(out_dir) / f"{cid}.nt"


def _lit(value) -> str:
    text = str(value).encode("utf-8", "replace").decode("utf-8")
    text = (text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
            .replace("\r", "\\r").replace("\t", "\\t"))
    return f'"{text}"'


def _datetime(value) -> str | None:
    """An ISO timestamp as an xsd:dateTime literal, or None when unparseable.
    Normalised to UTC, so the store orders it the way the file list did."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    iso = dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return f'{_lit(iso)}^^<{XSD}dateTime>'


def _bool(value) -> str:
    return f'"{"true" if value else "false"}"^^<{XSD}boolean>'


def _iri(value) -> str | None:
    if not isinstance(value, str) or not _IRI_SCHEME.match(value) or _IRI_FORBIDDEN.search(value):
        return None
    return value


def to_ntriples(conv: dict) -> str:
    """The thread's summary as deterministic, blank-node-free N-Triples ("" for
    a dict that is no thread). A thread whose timestamps are unreadable gets
    no dcterms:modified, and so sorts into no list query — the gateway's file
    listing still has it."""
    cid = conv.get("id") if isinstance(conv, dict) else None
    if not isinstance(cid, str) or not _ID_RE.fullmatch(cid):
        return ""
    s = f"<{subject(cid)}>"
    lines = [
        f"{s} <{RDF_TYPE}> <{KB}Thread> .",
        f"{s} <{DCT}title> {_lit(conv.get('title') or '')} .",
        f"{s} <{KB}kind> {_lit(conv.get('kind') or 'chat')} .",
        f"{s} <{KB}initiator> {_lit(conv.get('initiator') or 'user')} .",
        f"{s} <{KB}messageCount> \"{len(conv.get('messages') or [])}\"^^<{XSD}integer> .",
    ]
    for flag in FLAGS:
        lines.append(f"{s} <{KB}{flag}> {_bool(conv.get(flag))} .")
    created = _datetime(conv.get("created"))
    modified = _datetime(conv.get("updated")) or created
    if created:
        lines.append(f"{s} <{DCT}created> {created} .")
    if modified:
        lines.append(f"{s} <{DCT}modified> {modified} .")
    project = _iri(conv.get("project"))
    if project:
        lines.append(f"{s} <{KB}project> <{project}> .")
    chat = conv.get("chat")
    if isinstance(chat, str) and chat:
        lines.append(f"{s} <{KB}companionOf> <{chat_subject(chat)}> .")
    return "\n".join(sorted(lines)) + "\n"


def write(out_dir: Path, conv: dict) -> bool:
    """Write the thread's summary if it changed; True when the file changed
    (the store re-reads it only then)."""
    text = to_ntriples(conv)
    if not text:
        return False
    path = path_for(out_dir, conv["id"])
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return True


def remove(out_dir: Path, cid: str) -> bool:
    """Drop the thread's summary (its graph leaves the store with it)."""
    if not _ID_RE.fullmatch(cid or ""):
        return False
    try:
        path_for(out_dir, cid).unlink()
        return True
    except FileNotFoundError:
        return False


def stale_ids(out_dir: Path, live: set[str]) -> list[str]:
    """Summary files whose thread no longer exists — left by a crash between
    a thread's deletion and its summary's, or by a restored backup."""
    try:
        names = [p.name for p in Path(out_dir).iterdir()]
    except OSError:
        return []
    stale = []
    for name in names:
        stem, dot, ext = name.partition(".")
        if dot and _ID_RE.fullmatch(stem) and stem not in live:
            stale.append(stem)
    return sorted(set(stale))


def page_query(kind: str = "chat", limit: int = 50, offset: int = 0) -> str:
    """One page of the archived threads: starred first, then newest first —
    the order the dashboard shows. Selects ids only; the rows are built from
    the threads' own files. `kind` "all" lists every kind."""
    kind_pattern = "" if kind == "all" else f" ;\n     kb:kind {_lit(kind)}"
    return (
        f"PREFIX kb: <{KB}>\n"
        f"PREFIX dcterms: <{DCT}>\n"
        f"PREFIX xsd: <{XSD}>\n"
        "SELECT ?t ?modified ?starred WHERE {\n"
        "  ?t a kb:Thread ;\n"
        "     kb:archived \"true\"^^xsd:boolean ;\n"
        "     kb:starred ?starred ;\n"
        f"     dcterms:modified ?modified{kind_pattern} .\n"
        "}\n"
        f"ORDER BY DESC(?starred) DESC(?modified) ?t\n"
        f"LIMIT {int(limit)} OFFSET {int(offset)}\n"
    )


def page_ids(bindings: list[dict]) -> list[str]:
    """Thread ids from page_query's result bindings, in order, deduplicated
    (a thread the store briefly holds twice — mid-update — counts once)."""
    prefix = subject("")
    seen: set[str] = set()
    ids: list[str] = []
    for b in bindings:
        value = ((b or {}).get("t") or {}).get("value") or ""
        cid = value[len(prefix):] if value.startswith(prefix) else ""
        if _ID_RE.fullmatch(cid) and cid not in seen:
            seen.add(cid)
            ids.append(cid)
    return ids


__all__ = ["subject", "chat_subject", "path_for", "to_ntriples", "write", "remove",
           "stale_ids", "page_query", "page_ids"]
