#!/usr/bin/env python3
"""Session memory — store, recall and compact durable log entries via the life store.

Every Claude session in Retinue is a fresh `claude -p`; whatever it learned
dies with it unless written down. This CLI is the writing-down: `store` appends
a memory entry as N-Triples into the framework-owned `_generated`
pseudo-chamber (`chambers/_generated/memory/`), which qlever-life indexes like
any chamber data, and `recall` queries the entries back by tag, time range,
actor, or minimum relevance — formatted for pasting straight into a dispatch
prompt, so an agent that cannot query the store itself still gets the memories
that matter. The design document is docs/memory.md; this docstring is the
reference for the vocabulary and the command line.

A memory entry is a resource, not a bare fact:

    <urn:retinue:memory:ID> a               kb:Memory .
    <urn:retinue:memory:ID> kb:content      "what to remember" .
    <urn:retinue:memory:ID> kb:tag          "insurance" .          # repeatable
    <urn:retinue:memory:ID> kb:recordedAt   "…"^^xsd:dateTime .
    <urn:retinue:memory:ID> kb:actor        <urn:retinue:actor:ara> .
    <urn:retinue:memory:ID> kb:relevance    "0.7"^^xsd:decimal .   # optional
    <urn:retinue:memory:ID> kb:expires      "…"^^xsd:dateTime .    # optional
    <urn:retinue:memory:ID> kb:session      "…" .                  # optional
    <urn:retinue:memory:ID> kb:model        "…" .                  # optional
    <urn:retinue:memory:ID> kb:reiteratedAt "…"^^xsd:dateTime .    # per reinforce
    <urn:retinue:memory:ID> kb:correctedBy  <urn:retinue:memory:NEW> .  # was false
    <urn:retinue:memory:ID> kb:supersededBy <urn:retinue:memory:NEW> .  # world changed
    <urn:retinue:memory:ID> kb:questionedBy <urn:retinue:memory:NEW> .  # in doubt
    <urn:retinue:memory:ID> kb:compactedInto <urn:retinue:memory:SUM> . # see compact

The actor URI is the same convention the agent registry types
(`discover-agents.py`), so memories join with the `kb:AiAgent` roster.

`kb:expires` marks an entry about a pending state, a deadline or an in-flight
process ("mail queued for approval", "waiting for the insurer's answer"): after
that time the entry is no longer relevant whatever happens later, and recall
leaves it out. A rule, preference, lesson or decision never expires — it is
challenged when it stops holding.

`kb:model` records which model wrote the memory — the judgement-attribution
stamp (docs/model-routing.md): the actor stays the household (`ara`), while
the model tells a reader how much to trust a recorded decision. A session
cannot introspect its own `--model` flag, so the spawner advertises it via
RETINUE_SESSION_MODEL (set by the scheduler, the gate scripts, and the
entrypoint alongside the flag they build); `--model` overrides, and with
neither the stamp is simply absent. The stamp is normalized before it is
written — lowercased, any provider prefix up to the first "/" dropped — so
`anthropic/claude-opus-5` and `Claude-Opus-5` are one model, not two.

`reinforce` strengthens an existing memory instead of duplicating it: when the
user restates a rule or preference already on record, one `kb:reiteratedAt`
timestamp is appended to the *same subject* — written into the current
session's file, since RDF merges triples by subject across named graphs, so
the original file is never touched. Alongside the creation-time relevance,
recall then reports how often and how recently an entry was repeated
(`COUNT`/`MAX` over the reiterations), which is the query-side signal for
"the user keeps saying this".

The inverse is challenging: a memory can turn out plainly false, be overtaken
by events, or become doubtful on new evidence. The old entry is never edited
or deleted — the *new* memory carries what is known now (the correction, the
new state, the doubt and its reason), and `store --corrects/--supersedes/
--questions <id>` links old to new (`kb:correctedBy` / `kb:supersededBy` /
`kb:questionedBy`, object = the new entry) in the new session's file, again by
subject-merge.

Store guards. Production showed the log filling with near-duplicates of
standing rules (re-stored instead of reinforced) and with one-off tags, so
`store` checks two things before it writes, and how strictly depends on the
session's tier (derived from RETINUE_SESSION_MODEL against
RETINUE_FRONTIER_MODEL; an untiered deployment counts as frontier, an unknown
stamp does not):

  - New tags: once the store holds at least 20 distinct tags, a tag no memory
    uses yet is refused, with the closest existing tags as a hint. A frontier
    session may coin it with `--new-tag`. Tags with an identifier prefix
    (`sender:…`, slugged `sender-…`) name one sender each and are exempt.
  - Duplicates: a live entry sharing a tag whose word overlap with the new
    content is >= 0.6 refuses the store (reinforce it, challenge it, or —
    frontier only — pass `--duplicate-ok`); an overlap >= 0.3 only warns. An
    entry this very call challenges is an intended neighbour, not a duplicate.

Both guards also read the files written to the memory directory in the
last minutes, which the store, indexing seconds behind, cannot return yet;
guards and write run under a lock per directory, so two sessions storing
at once cannot both pass. A store that cannot be reached never blocks a
write: the guards then only warn, from those recent files, and the
existence check of challenged ids proceeds unverified.

Compaction. `compact --plan` writes summaries that carry the substance of many
older entries forward and hide them from recall:

    <urn:retinue:memory:SUM> a kb:Memory , kb:MemorySummary .
    <urn:retinue:memory:SUM> kb:generation "1"^^xsd:integer .  # 1 + max member gen
    <urn:retinue:memory:SUM> kb:coversFrom "…"^^xsd:dateTime .
    <urn:retinue:memory:SUM> kb:coversTo   "…"^^xsd:dateTime .
    <urn:retinue:memory:SUM> kb:summarizes <urn:retinue:memory:A> .  # kept
    <urn:retinue:memory:SUM> kb:retires    <urn:retinue:memory:B> .  # dropped
    <urn:retinue:memory:A>   kb:compactedInto <urn:retinue:memory:SUM> .
    <urn:retinue:memory:B>   kb:compactedInto <urn:retinue:memory:SUM> .

plus the usual content, tags, recordedAt, actor, relevance and model. Nothing
is deleted: a summary that is later corrected (`store --corrects SUM`) stops
hiding its members, which is the undo. The plan is JSON, one object or a list:

    {"topic": "ludmila", "content": "…", "tags": ["ludmila", "signal"],
     "relevance": 0.8, "summarizes": ["<id>", …], "retires": ["<id>", …],
     "covers_from": "…optional…", "covers_to": "…optional…"}

Recall leaves out, by default: corrected and superseded entries
(`--include-superseded`), expired ones (`--include-expired`, labeled
EXPIRED), and entries compacted into a summary that stands
(`--include-compacted`, labeled COMPACTED). Summaries come first, highest
generation first, then everything newest first; `--expand SUM` lists a
summary's members, kept and retired. Questioned entries always stay in,
flagged — doubt is a signal, not a verdict.

File layout: one flat directory. Entries from the same session share a file
when a session label is known (`--session` or RETINUE_MEMORY_SESSION —
appending to an `.nt` is an ordinary incremental store update); without a
label each entry gets its own file, and one compaction run writes one
`compaction-*.nt`. The directory is deliberately flat: a subdirectory created
at runtime is invisible to the store's inotify watches until a rebuild
(qlever-dir#10), so per-month folders would silently delay a whole month of
memories.

Usage:

    memory.py store --tag insurance --tag deadline --relevance 0.3 \
        --expires 3w \
        "IV filing submitted; response expected within three weeks."
    memory.py tags --prefix sender
    memory.py recall --tag insurance --since 2026-06-01 --limit 10
    memory.py recall --tag health --json
    memory.py recall --expand 20261005T120000Z-a1b2c3
    memory.py reinforce 20260829T193141Z-575748
    memory.py store --tag insurance --supersedes 20260829T193141Z-575748 \
        "The filing deadline moved to the 15th; the August arrangement is void."
    memory.py compact --plan plan.json

Environment:
  RETINUE_MEMORY           set to 0/false/off/no to disable: `store`,
                           `reinforce` and `compact` become successful no-ops
                           (recall still works — old entries may exist)
  RETINUE_MEMORY_DIR       where entries are written
                           (default $CHAMBERS_DIR/_generated/memory)
  RETINUE_MEMORY_SESSION   default session label for `store`
  RETINUE_MEMORY_ACTOR     default actor name (default: ara)
  RETINUE_SESSION_MODEL    the model this session runs on, advertised by the
                           spawner; stamped as kb:model and compared with the
                           tier variables for the store guards
  RETINUE_FRONTIER_MODEL   the frontier tier's model (docs/model-routing.md);
  RETINUE_ROUTER_MODEL     with neither set the deployment is untiered and
  RETINUE_CLAUDE_MODEL     every session counts as frontier; the frontier tier
                           falls back to RETINUE_CLAUDE_MODEL as everywhere
  SPARQL_ENDPOINT_LIFE     the life store endpoint for queries
"""

from __future__ import annotations

import argparse
import calendar
import contextlib
import datetime
import difflib
import json
import os
import re
import secrets
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Mapping

try:
    import fcntl
except ImportError:  # Windows development hosts; the container is Linux
    fcntl = None

KB = "https://w3id.org/retinue/kb#"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
XSD = "http://www.w3.org/2001/XMLSchema#"
ACTOR_PREFIX = "urn:retinue:actor:"
MEMORY_PREFIX = "urn:retinue:memory:"

CHAMBERS_DIR = Path(os.environ.get("CHAMBERS_DIR") or "/workspace/chambers")
MEMORY_DIR = Path(
    os.environ.get("RETINUE_MEMORY_DIR") or (CHAMBERS_DIR / "_generated" / "memory")
)
ENDPOINT = os.environ.get("SPARQL_ENDPOINT_LIFE", "http://qlever-life:7001")

SLUG_RE = re.compile(r"[^a-z0-9-]+")

FRONTIER = "frontier"
LOWER = "lower"

# Word-overlap thresholds of the duplicate guard. 0.6 catches the production
# cases — the same rule restated with a word or two changed, the same incident
# stored twice in one second — while two entries that merely share a topic
# rarely clear 0.3.
DUPLICATE_OVERLAP = 0.6
SIMILAR_OVERLAP = 0.3

# Below this many distinct tags the new-tag guard is off: a fresh deployment
# has no vocabulary to reuse yet, and refusing its first tags would deadlock it.
NEW_TAG_BOOTSTRAP = 20

# Tags that name an identifier rather than a topic — one per sender, by design
# (CLAUDE.md: standing sender instructions are tagged `sender:<address>`, which
# slugs to `sender-…`). Nearly every such tag is new when first stored, and
# that is not vocabulary drift, so the new-tag guard lets them through.
IDENTIFIER_TAG_PREFIXES = ("sender-",)

EXCERPT_CHARS = 120


def memory_enabled() -> bool:
    return os.environ.get("RETINUE_MEMORY", "").strip().lower() not in {
        "0", "false", "off", "no",
    }


def _slug(value: str) -> str:
    return SLUG_RE.sub("-", value.strip().lower()).strip("-")


def _nt_string(value: str) -> str:
    """Escape a Python string as an N-Triples literal (RDF 1.1 §7.2)."""
    out = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{out}"'


def _sparql_string(value: str) -> str:
    """Escape a string for embedding in a SPARQL query (same escapes work)."""
    return _nt_string(value)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def _xsd_datetime(dt: datetime.datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _nt_datetime(dt: datetime.datetime) -> str:
    return f"{_nt_string(_xsd_datetime(dt))}^^<{XSD}dateTime>"


def _parse_datetime(value: str) -> datetime.datetime | None:
    """An ISO dateTime as the store returns it (or a person types it), as an
    aware UTC datetime; a missing offset means UTC. None when unreadable."""
    try:
        dt = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc).replace(microsecond=0)


def _target_file(fallback_stem: str, session: str) -> Path:
    """Entries sharing a session label share a file; otherwise one per entry.
    A label that slugs to nothing falls back to the per-entry stem — never a
    hidden ".nt" that would silently pool unrelated writes."""
    stem = _slug(session)
    if session and not stem:
        print(f"[memory] unusable session label {session!r}; "
              "using a per-entry file", file=sys.stderr)
    return MEMORY_DIR / f"{stem or fallback_stem}.nt"


def _append(path: Path, lines: list[str]) -> None:
    """Append under an exclusive lock: session files are shared between
    concurrent sessions, and interleaved writes would corrupt N-Triples.
    Without fcntl (a Windows development host) there is no lock — nothing
    there runs concurrent sessions."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        if fcntl is not None:
            fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.write("".join(line + "\n" for line in lines))
            fh.flush()
        finally:
            if fcntl is not None:
                fcntl.flock(fh, fcntl.LOCK_UN)


def _new_id(now: datetime.datetime) -> str:
    return f"{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"


ENTRY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")

# Challenge links: store-flag -> predicate localname. Subject is the OLD
# memory, object the NEW one, so the link reads as history on the old
# resource — same subject-merge trick as kb:reiteratedAt.
CHALLENGE_PREDICATES = {
    "corrects": "correctedBy",       # the old memory was plainly false
    "supersedes": "supersededBy",    # the world changed; old no longer holds
    "questions": "questionedBy",     # new evidence puts the old one in doubt
}


def _bare_id(ref: str) -> str:
    return ref.strip().removeprefix(MEMORY_PREFIX)


# ---------------------------------------------------------------- pure helpers


_DURATION_RE = re.compile(r"^(\d+)\s*([dwm])?$", re.IGNORECASE)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _add_months(dt: datetime.datetime, months: int) -> datetime.datetime:
    """Calendar months, clamped to the last valid day (31 January plus one
    month is the end of February, not an error)."""
    index = dt.year * 12 + dt.month - 1 + months
    year, month0 = divmod(index, 12)
    last = calendar.monthrange(year, month0 + 1)[1]
    return dt.replace(year=year, month=month0 + 1, day=min(dt.day, last))


def parse_expires(spec: str, now: datetime.datetime) -> datetime.datetime | None:
    """When an entry stops being relevant, as an aware UTC datetime.

    Accepts a date (`2026-10-20`, meaning the end of that day UTC — an entry
    "relevant until the 20th" is still relevant on the 20th), a dateTime, or a
    duration from now in the syntax projects use for `remind_before`: a bare
    number or `Nd` for days, `Nw` for weeks, `Nm` for calendar months. None
    for anything else, so the caller can say so instead of guessing.
    """
    s = (spec or "").strip()
    if not s:
        return None
    m = _DURATION_RE.match(s)
    if m:
        count, unit = int(m.group(1)), (m.group(2) or "d").lower()
        try:
            if unit == "d":
                return now + datetime.timedelta(days=count)
            if unit == "w":
                return now + datetime.timedelta(weeks=count)
            return _add_months(now, count)
        except (OverflowError, ValueError):
            # Syntactically a duration, arithmetically nonsense (a year past
            # 9999): unreadable, so the caller refuses instead of crashing.
            return None
    if _DATE_RE.match(s):
        try:
            d = datetime.date.fromisoformat(s)
        except ValueError:
            return None
        return datetime.datetime(d.year, d.month, d.day, 23, 59, 59,
                                 tzinfo=datetime.timezone.utc)
    if "T" in s:
        return _parse_datetime(s)
    return None


def normalize_model(stamp: str | None) -> str:
    """One spelling per model: lowercase, provider prefix dropped.

    Production stamps came in as `anthropic/claude-opus-5`, `claude-opus-5`
    and `Claude-Opus-5` for one model, depending on which spawner set
    RETINUE_SESSION_MODEL. Aliases (`opus`) cannot be resolved here and stay
    as they are.
    """
    s = (stamp or "").strip().lower()
    if "/" in s:
        s = s.split("/", 1)[1].strip()
    return s


def session_tier(env: Mapping[str, str]) -> str:
    """FRONTIER or LOWER for the session described by `env`.

    A deployment that declares no tiers is one model doing everything, so it
    is trusted as frontier. Otherwise only a session whose stamp matches the
    frontier model is frontier — the router, a scheduled job pinned to its own
    model, and a session with no stamp at all are LOWER: unknown is not
    trusted. The frontier tier falls back to RETINUE_CLAUDE_MODEL exactly as
    the spawners resolve it (docs/model-routing.md). Only the environment's
    RETINUE_SESSION_MODEL counts, which the spawner sets and the session
    cannot change; `--model` adjusts the recorded stamp and nothing else, or
    a lower-tier session could name the frontier model and pass its own
    guards.
    """
    frontier_var = env.get("RETINUE_FRONTIER_MODEL", "").strip()
    router_var = env.get("RETINUE_ROUTER_MODEL", "").strip()
    if not frontier_var and not router_var:
        return FRONTIER
    frontier = normalize_model(frontier_var or env.get("RETINUE_CLAUDE_MODEL", ""))
    stamp = normalize_model(env.get("RETINUE_SESSION_MODEL", ""))
    return FRONTIER if stamp and stamp == frontier else LOWER


_TOKEN_RE = re.compile(r"\w+")


def _tokens(text: str) -> set[str]:
    # \w is Unicode-aware, so this works the same in any language; words
    # shorter than three letters are mostly function words in every language
    # and would inflate the overlap of unrelated entries.
    return {t for t in _TOKEN_RE.findall(text.lower()) if len(t) >= 3}


def word_overlap(a: str, b: str) -> float:
    """Jaccard similarity of the two texts' words (length >= 3, lowercased)."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def classify_duplicates(
    content: str, hits: list[tuple[str, str]], linked: set[str],
) -> tuple[list[tuple[str, str, float]], list[tuple[str, str, float]]]:
    """Split (id, content) hits into near-duplicates and merely similar ones,
    each as (id, content, overlap), highest overlap first. Entries the store
    call itself challenges are left out of both: superseding an entry is
    supposed to resemble it."""
    near, similar = [], []
    for entry_id, other in hits:
        if entry_id in linked:
            continue
        score = word_overlap(content, other)
        if score >= DUPLICATE_OVERLAP:
            near.append((entry_id, other, score))
        elif score >= SIMILAR_OVERLAP:
            similar.append((entry_id, other, score))
    near.sort(key=lambda h: -h[2])
    similar.sort(key=lambda h: -h[2])
    return near, similar


def closest_tags(tag: str, counts: Mapping[str, int], limit: int = 8) -> list[str]:
    """Existing tags a new tag probably meant: fuzzy matches first (typos,
    plural/singular), then tags containing it or contained in it (a more or
    less specific spelling of the same topic), the commoner ones first."""
    existing = sorted(counts)
    close = difflib.get_close_matches(tag, existing, n=5, cutoff=0.6)
    substr = sorted(
        (t for t in existing
         if t != tag and min(len(t), len(tag)) >= 3 and (tag in t or t in tag)),
        key=lambda t: (-counts[t], t),
    )
    out: list[str] = []
    for t in close + substr:
        if t not in out:
            out.append(t)
    return out[:limit]


def sort_tag_counts(counts: Mapping[str, int], prefix: str = "",
                    contains: str = "") -> list[tuple[str, int]]:
    """Tags by count descending, then name; optionally narrowed. The filters
    are slugged like tags are, so `--prefix sender:` finds `sender-…`."""
    p, c = _slug(prefix), _slug(contains)
    rows = [(t, n) for t, n in counts.items()
            if (not p or t.startswith(p)) and (not c or c in t)]
    rows.sort(key=lambda r: (-r[1], r[0]))
    return rows


def liveness_patterns(var: str = "?m", *, include_superseded: bool = False,
                      include_expired: bool = False,
                      include_compacted: bool = False,
                      now: datetime.datetime | None = None) -> list[str]:
    """SPARQL FILTER lines that keep only live entries bound to `var`.

    Live means: not corrected, not superseded (no longer true), not past its
    kb:expires (no longer relevant), and not compacted into a summary that
    stands — compaction into a summary that was later corrected is undone, so
    the member counts as live again. Each include_* flag lifts one exclusion.
    The current time is injected as a literal rather than NOW(), so a query is
    reproducible and testable. The query must declare the kb: and xsd:
    prefixes.
    """
    v = var.lstrip("?")
    out: list[str] = []
    if not include_superseded:
        out.append(f"FILTER NOT EXISTS {{ {var} kb:correctedBy ?{v}_corrected }}")
        out.append(f"FILTER NOT EXISTS {{ {var} kb:supersededBy ?{v}_superseded }}")
    if not include_expired:
        stamp = _xsd_datetime(now or _now())
        out.append(
            f"FILTER NOT EXISTS {{ {var} kb:expires ?{v}_expires . "
            f'FILTER(?{v}_expires < "{stamp}"^^xsd:dateTime) }}'
        )
    if not include_compacted:
        out.append(
            f"FILTER NOT EXISTS {{ {var} kb:compactedInto ?{v}_summary . "
            f"FILTER NOT EXISTS {{ ?{v}_summary kb:correctedBy ?{v}_undone }} }}"
        )
    return out


def _excerpt(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= EXCERPT_CHARS else flat[:EXCERPT_CHARS - 1] + "…"


# ---------------------------------------------------------------- local files

# The store indexes a new or appended file a few seconds after it is written.
# Anything the guards or compact decide inside that lag has to come from the
# files themselves; this window bounds how far back they look.
LOCAL_WINDOW = datetime.timedelta(minutes=10)

_ENTRY_LINE_RE = re.compile(
    r'^<' + re.escape(MEMORY_PREFIX) + r'([A-Za-z0-9-]+)> <' + re.escape(KB)
    + r'(content|tag)> "((?:[^"\\]|\\.)*)" \.$')
_CLAIM_LINE_RE = re.compile(
    r'^<' + re.escape(MEMORY_PREFIX) + r'([A-Za-z0-9-]+)> <' + re.escape(KB)
    + r'compactedInto> <' + re.escape(MEMORY_PREFIX) + r'([A-Za-z0-9-]+)> \.$')


def _unescape_nt(raw: str) -> str:
    """Undo _nt_string for the literals this script wrote itself."""
    return re.sub(r'\\(.)', lambda mt: {"n": "\n", "r": "\r", "t": "\t"}.get(
        mt.group(1), mt.group(1)), raw)


def _recent_files(pattern: str) -> list[Path]:
    if not MEMORY_DIR.is_dir():
        return []
    cutoff = time.time() - LOCAL_WINDOW.total_seconds()
    out = []
    for path in sorted(MEMORY_DIR.glob(pattern)):
        try:
            if path.stat().st_mtime >= cutoff:
                out.append(path)
        except OSError:
            continue
    return out


def _recent_local_entries(tags: list[str]) -> list[tuple[str, str]]:
    """(id, content) of entries written here in the last few minutes that
    share a tag — what the store has not indexed yet. The production
    duplicates were stored seconds apart by one session; a guard that only
    asks a store lagging a few seconds behind never sees them."""
    wanted = set(tags)
    contents: dict[str, str] = {}
    entry_tags: dict[str, set[str]] = {}
    for path in _recent_files("*.nt"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            mt = _ENTRY_LINE_RE.match(line)
            if not mt:
                continue
            entry_id, pred, raw = mt.groups()
            if pred == "content":
                contents[entry_id] = _unescape_nt(raw)
            else:
                entry_tags.setdefault(entry_id, set()).add(raw)
    return [(i, contents[i]) for i in sorted(contents)
            if entry_tags.get(i, set()) & wanted]


def _recent_local_claims(ids: list[str]) -> dict[str, list[str]]:
    """id -> summaries that compaction files written in the last few minutes
    already claim it for. A compaction that starts inside the index lag of
    the previous one would not see those claims through SPARQL."""
    wanted = set(ids)
    out: dict[str, list[str]] = {}
    for path in _recent_files("compaction-*.nt"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            mt = _CLAIM_LINE_RE.match(line)
            if mt and mt.group(1) in wanted:
                out.setdefault(mt.group(1), []).append(mt.group(2))
    return out


@contextlib.contextmanager
def _dir_lock(name: str):
    """One writer at a time per memory directory for a check-then-write: the
    store guards and the already-compacted check read a store that indexes
    seconds behind, and two processes inside that window would both pass.
    The lock file is not `.nt`, so the store ignores it. Without fcntl (a
    development host) there is no lock."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    with (MEMORY_DIR / name).open("a", encoding="utf-8") as fh:
        if fcntl is not None:
            fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fh, fcntl.LOCK_UN)


# ---------------------------------------------------------------- network


def _query(sparql: str) -> list[dict]:
    data = urllib.parse.urlencode({"query": sparql}).encode()
    req = urllib.request.Request(
        ENDPOINT, data=data,
        headers={"Accept": "application/sparql-results+json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.load(resp)
    return body.get("results", {}).get("bindings", [])


def _ask(sparql: str) -> bool:
    data = urllib.parse.urlencode({"query": sparql}).encode()
    req = urllib.request.Request(
        ENDPOINT, data=data,
        headers={"Accept": "application/sparql-results+json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return bool(json.load(resp).get("boolean"))


def _val(row: dict, key: str) -> str:
    return row.get(key, {}).get("value", "")


def _ids(row: dict, key: str) -> list[str]:
    return [x.strip().removeprefix(MEMORY_PREFIX)
            for x in _val(row, key).split(",") if x.strip()]


def _verify_exists(entry_id: str, force: bool) -> bool:
    """May entry_id be referenced? Guards against a typo silently attaching
    history to a subject recall never surfaces. Store lag (seconds) or
    downtime must not block a legitimate write: --force skips the check, an
    unreachable store warns and proceeds."""
    if force:
        return True
    uri = f"{MEMORY_PREFIX}{entry_id}"
    try:
        if not _ask(f"ASK {{ <{uri}> a <{KB}Memory> }}"):
            print(f"[memory] no such memory in the life store: {entry_id} "
                  "(just stored? the index lags a few seconds — use --force)",
                  file=sys.stderr)
            return False
    except Exception as exc:  # noqa: BLE001 — one endpoint, one failure mode
        print(f"[memory] store unreachable ({exc}); proceeding unverified",
              file=sys.stderr)
    return True


def _tag_counts(include_all: bool, now: datetime.datetime) -> dict[str, int]:
    """Distinct tags -> number of entries carrying them (live ones unless
    include_all). Raises when the store cannot be reached."""
    live = [] if include_all else liveness_patterns(now=now)
    body = "\n".join("  " + p for p in live)
    sparql = f"""
PREFIX kb: <{KB}>
PREFIX xsd: <{XSD}>
SELECT ?tag (COUNT(DISTINCT ?m) AS ?n) WHERE {{
  ?m a kb:Memory ;
     kb:tag ?tag .
{body}
}}
GROUP BY ?tag
"""
    return {_val(r, "tag"): int(_val(r, "n") or 0) for r in _query(sparql)
            if _val(r, "tag")}


def _live_entries_sharing(tags: list[str], now: datetime.datetime) -> list[tuple[str, str]]:
    """(id, content) of every live entry sharing at least one tag. No time
    cutoff: the entries most worth reinforcing are the oldest standing rules."""
    wanted = " ".join(_sparql_string(t) for t in tags)
    body = "\n".join("  " + p for p in liveness_patterns(now=now))
    sparql = f"""
PREFIX kb: <{KB}>
PREFIX xsd: <{XSD}>
SELECT DISTINCT ?m ?content WHERE {{
  ?m a kb:Memory ;
     kb:content ?content ;
     kb:tag ?want .
  VALUES ?want {{ {wanted} }}
{body}
}}
"""
    return [(_bare_id(_val(r, "m")), _val(r, "content")) for r in _query(sparql)]


# ---------------------------------------------------------------- store guards


def _tag_guard(tags: list[str], tier: str, allow_new: bool,
               now: datetime.datetime) -> bool:
    """True when the store may proceed as far as its tags are concerned.

    Tags are what recall finds entries by; production had 443 tags after five
    weeks, 40% of them used once — entries filed under a tag nobody will ever
    ask for again. A lower-tier session has to reuse the vocabulary; a
    frontier session may extend it, but has to say so.
    """
    try:
        counts = _tag_counts(include_all=True, now=now)
    except Exception as exc:  # noqa: BLE001 — one endpoint, one failure mode
        print(f"[memory] store unreachable ({exc}); tags not checked",
              file=sys.stderr)
        return True
    if len(counts) < NEW_TAG_BOOTSTRAP:
        return True
    unknown = [t for t in tags
               if t not in counts and not t.startswith(IDENTIFIER_TAG_PREFIXES)]
    if not unknown:
        return True

    def hint(tag: str) -> str:
        near = closest_tags(tag, counts)
        return (f"closest existing: {', '.join(near)}" if near
                else "no existing tag is close")

    if tier == FRONTIER and allow_new:
        for t in unknown:
            print(f"[memory] warning: new tag {t!r} ({hint(t)})", file=sys.stderr)
        return True
    for t in unknown:
        print(f"[memory] refused: no memory uses the tag {t!r} yet ({hint(t)})",
              file=sys.stderr)
    if tier == FRONTIER:
        print("[memory] reuse an existing tag (`memory.py tags` lists them), "
              "or pass --new-tag if the topic is genuinely new", file=sys.stderr)
    else:
        print("[memory] reuse an existing tag (`memory.py tags` lists them); "
              "only Ara senior can coin a new one — escalate if none fits",
              file=sys.stderr)
    return False


def _duplicate_guard(content: str, tags: list[str], linked: set[str], tier: str,
                     duplicate_ok: bool, now: datetime.datetime) -> bool:
    """True when the store may proceed as far as near-duplicates are concerned.

    Production: four reinforcements in five weeks while standing rules were
    re-stored as fresh entries, each copy then competing with the original for
    recall's limit. The way out of a refusal is the operation that was meant.
    """
    store_down = False
    try:
        hits = _live_entries_sharing(tags, now)
    except Exception as exc:  # noqa: BLE001 — one endpoint, one failure mode
        print(f"[memory] store unreachable ({exc}); duplicates not checked "
              "against the store, only against the last minutes' files",
              file=sys.stderr)
        hits, store_down = [], True
    # What the store has not indexed yet: the entries this or another session
    # wrote moments ago, which is where the production duplicates came from.
    seen = {entry_id for entry_id, _ in hits}
    hits += [h for h in _recent_local_entries(tags) if h[0] not in seen]
    near, similar = classify_duplicates(content, hits, linked)
    for entry_id, other, score in similar:
        print(f"[memory] warning: similar to {entry_id} (overlap {score:.2f}): "
              f"{_excerpt(other)}", file=sys.stderr)
    if not near:
        return True
    if store_down:
        # The store being down never blocks a write: what the local files
        # show is said, not enforced — the ways out of a refusal cannot be
        # verified either while it is down.
        for entry_id, _, score in near:
            print(f"[memory] warning: near-duplicate of {entry_id} "
                  f"(overlap {score:.2f}) in a recent local file; stored anyway "
                  "because the store is unreachable", file=sys.stderr)
        return True
    if tier == FRONTIER and duplicate_ok:
        for entry_id, _, score in near:
            print(f"[memory] warning: near-duplicate of {entry_id} "
                  f"(overlap {score:.2f}); stored anyway (--duplicate-ok)",
                  file=sys.stderr)
        return True
    print("[memory] refused: near-duplicate of live memories:", file=sys.stderr)
    for entry_id, other, score in near:
        print(f"  - {entry_id} (overlap {score:.2f}): {_excerpt(other)}",
              file=sys.stderr)
    first = near[0][0]
    print("[memory] ways out:", file=sys.stderr)
    print(f"  - the same thing said again: memory.py reinforce {first}",
          file=sys.stderr)
    print(f"  - it changed, was wrong, or is now in doubt: store again with "
          f"--supersedes / --corrects / --questions {first}", file=sys.stderr)
    if tier == FRONTIER:
        print("  - genuinely a different memory: store again with --duplicate-ok",
              file=sys.stderr)
    else:
        print("  - neither fits: escalate to Ara senior", file=sys.stderr)
    return False


# ---------------------------------------------------------------- store


def store(args: argparse.Namespace) -> int:
    if not memory_enabled():
        print("[memory] disabled (RETINUE_MEMORY=0) — nothing stored", file=sys.stderr)
        return 0

    content = args.content.strip()
    if not content:
        print("[memory] refusing to store an empty memory", file=sys.stderr)
        return 1

    actor = _slug(args.actor or os.environ.get("RETINUE_MEMORY_ACTOR", "") or "ara")
    if not actor:
        print(f"[memory] invalid actor name: {args.actor!r}", file=sys.stderr)
        return 1

    tags = sorted({_slug(t) for t in (args.tag or []) if _slug(t)})
    if not tags:
        print("[memory] at least one --tag is required (recall is tag-driven)",
              file=sys.stderr)
        return 1

    if args.relevance is not None and not (0.0 <= args.relevance <= 1.0):
        print("[memory] --relevance must be between 0 and 1", file=sys.stderr)
        return 1

    now = _now()
    expires = None
    if args.expires:
        expires = parse_expires(args.expires, now)
        if expires is None:
            print(f"[memory] unreadable --expires {args.expires!r} (a date, a "
                  "dateTime, or a duration like 10, 10d, 2w, 3m)", file=sys.stderr)
            return 1
        if expires <= now:
            # Recall would never show it; storing it is a mistake, not a record.
            print(f"[memory] --expires {args.expires!r} is already past",
                  file=sys.stderr)
            return 1

    # Validate every challenged reference before anything is written.
    challenges: list[tuple[str, str]] = []  # (predicate localname, old id)
    for flag, pred in CHALLENGE_PREDICATES.items():
        for ref in getattr(args, flag) or []:
            old_id = _bare_id(ref)
            if not ENTRY_ID_RE.match(old_id):
                print(f"[memory] not a memory id: {ref!r}", file=sys.stderr)
                return 1
            if not _verify_exists(old_id, args.force):
                return 1
            challenges.append((pred, old_id))

    model = normalize_model(args.model or os.environ.get("RETINUE_SESSION_MODEL", ""))
    tier = session_tier(os.environ)

    # Guards and write under one lock per memory directory: two sessions
    # storing the same thing at once would otherwise both pass the guards
    # before either file exists, and the store cannot arbitrate — it indexes
    # seconds later. The lock turns the second one into the refusal it earns.
    with _dir_lock(".store.lock"):
        # Guards: tags first (cheap to fix, and a refused tag changes which
        # entries the duplicate check would compare against), then duplicates.
        if not _tag_guard(tags, tier, args.new_tag, now):
            return 1
        linked = {old_id for _, old_id in challenges}
        if not _duplicate_guard(content, tags, linked, tier, args.duplicate_ok, now):
            return 1

        entry_id = _new_id(now)
        session = (args.session or os.environ.get("RETINUE_MEMORY_SESSION", "")).strip()
        path = _target_file(entry_id, session)

        subj = f"<{MEMORY_PREFIX}{entry_id}>"
        lines = [
            f"{subj} <{RDF_TYPE}> <{KB}Memory> .",
            f"{subj} <{KB}content> {_nt_string(content)} .",
            f"{subj} <{KB}recordedAt> {_nt_datetime(now)} .",
            f"{subj} <{KB}actor> <{ACTOR_PREFIX}{actor}> .",
        ]
        lines += [f"{subj} <{KB}tag> {_nt_string(t)} ." for t in tags]
        if args.relevance is not None:
            lines.append(
                f"{subj} <{KB}relevance> \"{args.relevance:g}\"^^<{XSD}decimal> ."
            )
        if expires is not None:
            lines.append(f"{subj} <{KB}expires> {_nt_datetime(expires)} .")
        if session:
            lines.append(f"{subj} <{KB}session> {_nt_string(session)} .")
        if model:
            lines.append(f"{subj} <{KB}model> {_nt_string(model)} .")
        lines += [f"<{MEMORY_PREFIX}{old_id}> <{KB}{pred}> {subj} ."
                  for pred, old_id in challenges]

        _append(path, lines)
        print(f"[memory] stored {entry_id} -> {path}", file=sys.stderr)
        for pred, old_id in challenges:
            print(f"[memory] {old_id} {pred} {entry_id}", file=sys.stderr)
        return 0


# ---------------------------------------------------------------- reinforce


def reinforce(args: argparse.Namespace) -> int:
    if not memory_enabled():
        print("[memory] disabled (RETINUE_MEMORY=0) — nothing reinforced",
              file=sys.stderr)
        return 0

    entry_id = _bare_id(args.id)
    if not ENTRY_ID_RE.match(entry_id):
        print(f"[memory] not a memory id: {args.id!r}", file=sys.stderr)
        return 1
    if not _verify_exists(entry_id, args.force):
        return 1
    uri = f"{MEMORY_PREFIX}{entry_id}"

    now = _now()
    session = (args.session or os.environ.get("RETINUE_MEMORY_SESSION", "")).strip()
    path = _target_file(_new_id(now), session)
    _append(path, [f"<{uri}> <{KB}reiteratedAt> {_nt_datetime(now)} ."])
    print(f"[memory] reinforced {entry_id} -> {path}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------- tags


def tags(args: argparse.Namespace) -> int:
    try:
        counts = _tag_counts(include_all=args.all, now=_now())
    except Exception as exc:  # noqa: BLE001 — one endpoint, one failure mode
        print(f"[memory] life store query failed: {exc}", file=sys.stderr)
        return 1
    rows = sort_tag_counts(counts, args.prefix, args.contains)
    if args.json:
        print(json.dumps([{"tag": t, "count": n} for t, n in rows],
                         ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("(no matching tags)")
        return 0
    width = len(str(rows[0][1]))
    for t, n in rows:
        print(f"{n:>{width}}  {t}")
    return 0


# ---------------------------------------------------------------- recall


def _date_bound(value: str, end_of_day: bool) -> str:
    """Accept a date or a full dateTime; widen a bare date to the day's edge."""
    if "T" in value:
        return value if value.endswith("Z") or "+" in value else value + "Z"
    return f"{value}T23:59:59Z" if end_of_day else f"{value}T00:00:00Z"


def _recall_sparql(patterns: list[str], limit: int | None,
                   member_of: str | None = None) -> str:
    """The recall SELECT. With member_of (a summary id) the entries are that
    summary's members, each with ?role "kept" or "retired".

    Every GROUP_CONCAT over IRIs wraps them in STR(): the life store leaves a
    GROUP_CONCAT over IRI values unbound — the cell is absent, not "" — while
    the same aggregate over literals (the tags) works. Verified on the live
    QLever; see docs/triple-stores.md.

    Only a compaction that stands is reported: a member of a summary that was
    later corrected is live again, and labelling it COMPACTED would send the
    reader to a summary recall itself hides. Verified on the live QLever: an
    OPTIONAL with an inner FILTER NOT EXISTS executes there.

    Ordering: DESC(?gen) puts summaries first, highest generation first
    (an unbound ?gen sorts last under DESC), then newest first — so a broad
    recall's limit cuts old episodic entries, not the consolidated rules.
    """
    role_select, role_group, members = "", "", ""
    if member_of:
        uri = f"<{MEMORY_PREFIX}{member_of}>"
        role_select = " ?role"
        role_group = " ?role"
        members = (f'  {{ {uri} kb:summarizes ?m . BIND("kept" AS ?role) }}\n'
                   f'  UNION {{ {uri} kb:retires ?m . BIND("retired" AS ?role) }}\n')
    body = "\n".join("  " + p for p in patterns)
    limit_clause = f"LIMIT {limit}" if limit is not None else ""
    return f"""
PREFIX kb: <{KB}>
PREFIX xsd: <{XSD}>
SELECT ?m ?content ?t ?actor ?relevance ?model ?gen ?expires ?coversFrom ?coversTo{role_select}
       (GROUP_CONCAT(DISTINCT ?tag; SEPARATOR=", ") AS ?tags)
       (COUNT(DISTINCT ?r) AS ?reiterations) (MAX(?r) AS ?lastReiterated)
       (GROUP_CONCAT(DISTINCT STR(?cb); SEPARATOR=", ") AS ?correctedBy)
       (GROUP_CONCAT(DISTINCT STR(?sb); SEPARATOR=", ") AS ?supersededBy)
       (GROUP_CONCAT(DISTINCT STR(?qb); SEPARATOR=", ") AS ?questionedBy)
       (GROUP_CONCAT(DISTINCT STR(?sm); SEPARATOR=", ") AS ?summarizes)
       (GROUP_CONCAT(DISTINCT STR(?rt); SEPARATOR=", ") AS ?retires)
       (COUNT(DISTINCT ?sm) AS ?summarizedCount) (COUNT(DISTINCT ?rt) AS ?retiredCount)
       (GROUP_CONCAT(DISTINCT STR(?ci); SEPARATOR=", ") AS ?compactedInto) WHERE {{
{members}  ?m a kb:Memory ;
     kb:content ?content ;
     kb:recordedAt ?t .
  OPTIONAL {{ ?m kb:tag ?tag }}
  OPTIONAL {{ ?m kb:actor ?actor }}
  OPTIONAL {{ ?m kb:relevance ?relevance }}
  OPTIONAL {{ ?m kb:model ?model }}
  OPTIONAL {{ ?m kb:generation ?gen }}
  OPTIONAL {{ ?m kb:expires ?expires }}
  OPTIONAL {{ ?m kb:coversFrom ?coversFrom }}
  OPTIONAL {{ ?m kb:coversTo ?coversTo }}
  OPTIONAL {{ ?m kb:reiteratedAt ?r }}
  OPTIONAL {{ ?m kb:correctedBy ?cb }}
  OPTIONAL {{ ?m kb:supersededBy ?sb }}
  OPTIONAL {{ ?m kb:questionedBy ?qb }}
  OPTIONAL {{ ?m kb:summarizes ?sm }}
  OPTIONAL {{ ?m kb:retires ?rt }}
  OPTIONAL {{ ?m kb:compactedInto ?ci .
             FILTER NOT EXISTS {{ ?ci kb:correctedBy ?ci_undone }} }}
{body}
}}
GROUP BY ?m ?content ?t ?actor ?relevance ?model ?gen ?expires ?coversFrom ?coversTo{role_group}
ORDER BY DESC(?gen) DESC(?t)
{limit_clause}
"""


def _row_json(r: dict) -> dict:
    gen = _val(r, "gen")
    out = {
        "id": _val(r, "m"),
        "content": _val(r, "content"),
        "recorded_at": _val(r, "t"),
        "actor": _val(r, "actor").removeprefix(ACTOR_PREFIX),
        "relevance": _val(r, "relevance") or None,
        "model": _val(r, "model") or None,
        "expires": _val(r, "expires") or None,
        "reiterations": int(_val(r, "reiterations") or 0),
        "last_reiterated": _val(r, "lastReiterated") or None,
        "corrected_by": _ids(r, "correctedBy"),
        "superseded_by": _ids(r, "supersededBy"),
        "questioned_by": _ids(r, "questionedBy"),
        # Every summary carries kb:generation (compact writes it), so a bound
        # ?gen is the summary test without a second type pattern.
        "summary": bool(gen),
        "generation": int(gen) if gen else None,
        "covers_from": _val(r, "coversFrom") or None,
        "covers_to": _val(r, "coversTo") or None,
        "summarizes": _ids(r, "summarizes"),
        "retires": _ids(r, "retires"),
        "compacted_into": _ids(r, "compactedInto"),  # standing summaries only
        "tags": [t for t in _val(r, "tags").split(", ") if t],
    }
    if _val(r, "role"):
        out["role"] = _val(r, "role")
    return out


def _render_row(r: dict, now: datetime.datetime) -> list[str]:
    when = _val(r, "t").replace("T", " ").removesuffix("Z") + "Z"
    actor = _val(r, "actor").removeprefix(ACTOR_PREFIX)
    meta = [m for m in (_val(r, "role"), actor, _val(r, "tags")) if m]
    rel = _val(r, "relevance")
    if rel:
        meta.append(f"relevance {rel}")
    gen = _val(r, "gen")
    if gen:
        kept = int(_val(r, "summarizedCount") or 0)
        retired = int(_val(r, "retiredCount") or 0)
        label = f"SUMMARY gen {gen} of {kept + retired} entries"
        if retired:
            label += f", {retired} retired"
        cf, ct = _val(r, "coversFrom"), _val(r, "coversTo")
        if cf and ct:
            label += f", covers {cf.split('T')[0]}..{ct.split('T')[0]}"
        meta.append(label)
    model = _val(r, "model")
    if model:
        meta.append(f"via {model}")
    times = int(_val(r, "reiterations") or 0)
    if times:
        last = _val(r, "lastReiterated").split("T")[0]
        meta.append(f"reiterated {times}x, last {last}")
    expires = _val(r, "expires")
    if expires:
        exp = _parse_datetime(expires)
        day = expires.split("T")[0]
        meta.append(f"EXPIRED {day}" if exp is not None and exp < now
                    else f"expires {day}")
    for key, label in (("correctedBy", "CORRECTED by"),
                       ("supersededBy", "SUPERSEDED by"),
                       ("questionedBy", "questioned by"),
                       ("compactedInto", "COMPACTED into")):
        linked = _ids(r, key)
        if linked:
            meta.append(f"{label} {', '.join(linked)}")
    entry_id = _val(r, "m").removeprefix(MEMORY_PREFIX)
    return [f"- {when} ({'; '.join(meta)}) [{entry_id}]",
            f"  {_val(r, 'content')}"]


def recall(args: argparse.Namespace) -> int:
    now = _now()
    patterns: list[str] = []
    member_of = None
    limit = args.limit

    if args.expand:
        # A summary's members are wanted exactly because they are hidden, so
        # no exclusion applies and no filter makes sense next to it.
        clashing = [flag for flag, on in (
            ("--tag", bool(args.tag)), ("--actor", bool(args.actor)),
            ("--since", bool(args.since)), ("--until", bool(args.until)),
            ("--min-relevance", args.min_relevance is not None),
            ("--include-superseded", args.include_superseded),
            ("--include-expired", args.include_expired),
            ("--include-compacted", args.include_compacted),
        ) if on]
        if clashing:
            print(f"[memory] --expand lists one summary's members; it does not "
                  f"combine with {', '.join(clashing)}", file=sys.stderr)
            return 1
        member_of = _bare_id(args.expand)
        if not ENTRY_ID_RE.match(member_of):
            print(f"[memory] not a memory id: {args.expand!r}", file=sys.stderr)
            return 1
    else:
        if limit is None:
            limit = 20
        if args.tag:
            slugged = [s for s in (_slug(t) for t in args.tag) if s]
            if not slugged:
                print(f"[memory] no usable --tag values in {args.tag!r}",
                      file=sys.stderr)
                return 1
            wanted = " ".join(_sparql_string(s) for s in slugged)
            patterns.append(f"?m kb:tag ?want . VALUES ?want {{ {wanted} }}")
        if args.actor:
            patterns.append(f"?m kb:actor <{ACTOR_PREFIX}{_slug(args.actor)}> .")
        if args.since:
            patterns.append(
                f'FILTER(?t >= "{_date_bound(args.since, False)}"^^xsd:dateTime)'
            )
        if args.until:
            patterns.append(
                f'FILTER(?t <= "{_date_bound(args.until, True)}"^^xsd:dateTime)'
            )
        if args.min_relevance is not None:
            # Filtering on the OPTIONAL drops entries that declare no
            # relevance — asking for a minimum means asking for entries that
            # state one.
            patterns.append(f"FILTER(?relevance >= {args.min_relevance:g})")
        # Nothing stale may leak into a dispatch prompt by default: no longer
        # true (corrected/superseded), no longer relevant (expired), or
        # carried by a summary that is shown instead (compacted).
        patterns += liveness_patterns(
            include_superseded=args.include_superseded,
            include_expired=args.include_expired,
            include_compacted=args.include_compacted,
            now=now,
        )

    try:
        rows = _query(_recall_sparql(patterns, limit, member_of))
    except Exception as exc:  # noqa: BLE001 — one endpoint, one failure mode
        print(f"[memory] life store query failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps([_row_json(r) for r in rows], ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print(f"(no members of {member_of})" if member_of
              else "(no matching memories)")
        return 0
    for r in rows:
        for line in _render_row(r, now):
            print(line)
    return 0


# ---------------------------------------------------------------- compact


PLAN_KEYS = {"topic", "content", "tags", "relevance", "summarizes", "retires",
             "covers_from", "covers_to"}


def _plan_bound(value, end_of_day: bool) -> datetime.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    if _DATE_RE.match(v):
        try:
            d = datetime.date.fromisoformat(v)
        except ValueError:
            return None
        t = datetime.time(23, 59, 59) if end_of_day else datetime.time(0, 0, 0)
        return datetime.datetime.combine(d, t, tzinfo=datetime.timezone.utc)
    return _parse_datetime(v)


def validate_plan(plan) -> tuple[list[dict], list[str]]:
    """Check a compaction plan without touching the store.

    Returns (summaries, errors); the summaries are normalized (tags slugged,
    the topic among them, ids bare, covers as datetimes or None). Every error
    is collected, so one run tells the planner everything that is wrong
    before anything is written. An entry may appear in only one summary: it
    is compacted once.
    """
    if isinstance(plan, dict):
        items = [plan]
    elif isinstance(plan, list):
        items = plan
    else:
        return [], ["the plan must be a JSON object or a list of objects"]
    errors: list[str] = []
    if not items:
        errors.append("the plan holds no summaries")
    out: list[dict] = []
    owner: dict[str, int] = {}
    for n, item in enumerate(items, 1):
        where = f"summary {n}"
        if not isinstance(item, dict):
            errors.append(f"{where}: not an object")
            continue
        unknown = sorted(set(item) - PLAN_KEYS)
        if unknown:
            errors.append(f"{where}: unknown keys {', '.join(unknown)}")

        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            errors.append(f"{where}: content is empty")
            content = ""

        topic = item.get("topic")
        if topic is not None and not isinstance(topic, str):
            errors.append(f"{where}: topic must be a string")
            topic = None
        raw_tags = item.get("tags") or []
        if not isinstance(raw_tags, list) or not all(isinstance(t, str) for t in raw_tags):
            errors.append(f"{where}: tags must be a list of strings")
            raw_tags = []
        # The topic is the cluster tag the summary was built from; recall
        # finds the summary by it, so it is always among the tags.
        tags = sorted({s for s in (_slug(t) for t in raw_tags + [topic or ""]) if s})
        if not tags:
            errors.append(f"{where}: at least one tag is required")

        relevance = item.get("relevance")
        if relevance is not None and (
                isinstance(relevance, bool) or not isinstance(relevance, (int, float))
                or not 0.0 <= relevance <= 1.0):
            errors.append(f"{where}: relevance must be a number between 0 and 1")
            relevance = None

        lists: dict[str, list[str]] = {}
        for key in ("summarizes", "retires"):
            raw = item.get(key) or []
            if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
                errors.append(f"{where}: {key} must be a list of ids")
                raw = []
            ids: list[str] = []
            for ref in raw:
                entry_id = _bare_id(ref)
                if not ENTRY_ID_RE.match(entry_id):
                    errors.append(f"{where}: not a memory id in {key}: {ref!r}")
                elif entry_id not in ids:
                    ids.append(entry_id)
            lists[key] = ids
        both = sorted(set(lists["summarizes"]) & set(lists["retires"]))
        if both:
            errors.append(f"{where}: both summarized and retired: {', '.join(both)}")
        if not lists["summarizes"] and not lists["retires"]:
            errors.append(f"{where}: summarizes and retires are both empty")
        for entry_id in lists["summarizes"] + lists["retires"]:
            if entry_id in owner and owner[entry_id] != n:
                errors.append(f"{where}: {entry_id} is already a member of "
                              f"summary {owner[entry_id]}")
            owner.setdefault(entry_id, n)

        covers = {}
        for key, end in (("covers_from", False), ("covers_to", True)):
            value = item.get(key)
            covers[key] = None
            if value is not None:
                covers[key] = _plan_bound(value, end)
                if covers[key] is None:
                    errors.append(f"{where}: unreadable {key} {value!r}")
        if covers["covers_from"] and covers["covers_to"] and \
                covers["covers_from"] > covers["covers_to"]:
            errors.append(f"{where}: covers_from is after covers_to")

        out.append({
            "content": content.strip(),
            "tags": tags,
            "relevance": relevance,
            "summarizes": lists["summarizes"],
            "retires": lists["retires"],
            "covers_from": covers["covers_from"],
            "covers_to": covers["covers_to"],
        })
    return out, errors


def _values_clause(ids: list[str]) -> str:
    return " ".join(f"<{MEMORY_PREFIX}{i}>" for i in ids)


def _member_facts(ids: list[str]) -> dict[str, dict]:
    """id -> {"t", "gen", "from", "to"} for the members the store knows."""
    sparql = f"""
PREFIX kb: <{KB}>
SELECT ?m ?t ?gen ?from ?to WHERE {{
  VALUES ?m {{ {_values_clause(ids)} }}
  ?m kb:recordedAt ?t .
  OPTIONAL {{ ?m kb:generation ?gen }}
  OPTIONAL {{ ?m kb:coversFrom ?from }}
  OPTIONAL {{ ?m kb:coversTo ?to }}
}}
"""
    facts: dict[str, dict] = {}
    for r in _query(sparql):
        facts[_bare_id(_val(r, "m"))] = {
            "t": _val(r, "t"), "gen": _val(r, "gen"),
            "from": _val(r, "from"), "to": _val(r, "to"),
        }
    return facts


def _already_compacted(ids: list[str]) -> dict[str, list[str]]:
    """id -> the uncorrected summaries it is already compacted into."""
    sparql = f"""
PREFIX kb: <{KB}>
SELECT ?m ?summary WHERE {{
  VALUES ?m {{ {_values_clause(ids)} }}
  ?m kb:compactedInto ?summary .
  FILTER NOT EXISTS {{ ?summary kb:correctedBy ?undone }}
}}
"""
    out: dict[str, list[str]] = {}
    for r in _query(sparql):
        out.setdefault(_bare_id(_val(r, "m")), []).append(_bare_id(_val(r, "summary")))
    return out


def summary_generation(member_ids: list[str], facts: Mapping[str, dict]) -> int:
    """1 over raw entries only; otherwise one above the highest member."""
    gens = [int(facts[i]["gen"]) for i in member_ids
            if i in facts and str(facts[i].get("gen") or "").isdigit()]
    return 1 + max(gens, default=0)


def summary_covers(member_ids: list[str], facts: Mapping[str, dict]
                   ) -> tuple[datetime.datetime | None, datetime.datetime | None]:
    """The span of the members' substance. A member that is itself a summary
    contributes the span it covers, not the moment it was written — otherwise
    every consolidation would claim to start at the previous compaction run."""
    starts, ends = [], []
    for i in member_ids:
        f = facts.get(i)
        if not f:
            continue
        start = _parse_datetime(f.get("from") or f.get("t") or "")
        end = _parse_datetime(f.get("to") or f.get("t") or "")
        if start:
            starts.append(start)
        if end:
            ends.append(end)
    return (min(starts) if starts else None, max(ends) if ends else None)


def compaction_lines(summary_id: str, item: dict, generation: int,
                     covers_from: datetime.datetime | None,
                     covers_to: datetime.datetime | None,
                     actor: str, model: str, now: datetime.datetime) -> list[str]:
    subj = f"<{MEMORY_PREFIX}{summary_id}>"
    lines = [
        f"{subj} <{RDF_TYPE}> <{KB}Memory> .",
        f"{subj} <{RDF_TYPE}> <{KB}MemorySummary> .",
        f"{subj} <{KB}content> {_nt_string(item['content'])} .",
        f"{subj} <{KB}recordedAt> {_nt_datetime(now)} .",
        f"{subj} <{KB}actor> <{ACTOR_PREFIX}{actor}> .",
        f"{subj} <{KB}generation> \"{generation}\"^^<{XSD}integer> .",
    ]
    lines += [f"{subj} <{KB}tag> {_nt_string(t)} ." for t in item["tags"]]
    if item["relevance"] is not None:
        lines.append(f"{subj} <{KB}relevance> \"{item['relevance']:g}\"^^<{XSD}decimal> .")
    if model:
        lines.append(f"{subj} <{KB}model> {_nt_string(model)} .")
    if covers_from is not None:
        lines.append(f"{subj} <{KB}coversFrom> {_nt_datetime(covers_from)} .")
    if covers_to is not None:
        lines.append(f"{subj} <{KB}coversTo> {_nt_datetime(covers_to)} .")
    for pred, key in (("summarizes", "summarizes"), ("retires", "retires")):
        lines += [f"{subj} <{KB}{pred}> <{MEMORY_PREFIX}{m}> ." for m in item[key]]
    # The member side, by subject-merge: what recall's exclusion reads.
    lines += [f"<{MEMORY_PREFIX}{m}> <{KB}compactedInto> {subj} ."
              for m in item["summarizes"] + item["retires"]]
    return lines


def _write_atomically(path: Path, lines: list[str]) -> None:
    """Write under a name the store ignores, then rename into place: the
    store indexes every .nt it sees, and a half-written file would be a
    summary hiding members it does not yet carry."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write("".join(line + "\n" for line in lines))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def compact(args: argparse.Namespace) -> int:
    if not memory_enabled():
        print("[memory] disabled (RETINUE_MEMORY=0) — nothing compacted",
              file=sys.stderr)
        return 0

    # Compaction hides members from every recall; a plan written by a session
    # that may not even coin a tag has no business doing that. The scheduled
    # job runs as frontier by construction; this is for the hand-run case.
    model = normalize_model(args.model or os.environ.get("RETINUE_SESSION_MODEL", ""))
    if session_tier(os.environ) != FRONTIER:
        print("[memory] refused: compaction is frontier work — run it from Ara "
              "senior or let the scheduled job do it", file=sys.stderr)
        return 1

    try:
        raw = sys.stdin.read() if args.plan == "-" else \
            Path(args.plan).read_text(encoding="utf-8")
        plan = json.loads(raw)
    except (OSError, ValueError) as exc:
        print(f"[memory] cannot read the plan: {exc}", file=sys.stderr)
        return 1

    items, errors = validate_plan(plan)
    actor = _slug(args.actor or os.environ.get("RETINUE_MEMORY_ACTOR", "") or "ara")
    if not actor:
        errors.append(f"invalid actor name: {args.actor!r}")
    if errors:
        for e in errors:
            print(f"[memory] plan error: {e}", file=sys.stderr)
        return 1

    all_ids = [m for item in items for m in item["summarizes"] + item["retires"]]

    # Existence, like _verify_exists — but one warning, not one per member,
    # when the store is down.
    if not args.force:
        for entry_id in all_ids:
            try:
                if not _ask(f"ASK {{ <{MEMORY_PREFIX}{entry_id}> a <{KB}Memory> }}"):
                    errors.append(f"no such memory in the life store: {entry_id}")
            except Exception as exc:  # noqa: BLE001
                print(f"[memory] store unreachable ({exc}); members unverified",
                      file=sys.stderr)
                break

    with _dir_lock(".compact.lock"):
        return _compact_locked(items, all_ids, errors, args.actor, model)


def _compact_locked(items: list[dict], all_ids: list[str], errors: list[str],
                    actor_arg: str, model: str) -> int:
    # Generation and coverage are facts about the members that only the store
    # knows; a summary with an invented generation would break the ordering
    # and the consolidation chain, so here an unreachable store refuses.
    try:
        compacted = _already_compacted(all_ids)
        facts = _member_facts(all_ids)
    except Exception as exc:  # noqa: BLE001
        print(f"[memory] store unreachable ({exc}); cannot compute generations "
              "and coverage — nothing written", file=sys.stderr)
        return 1
    for entry_id, summaries in _recent_local_claims(all_ids).items():
        known = compacted.setdefault(entry_id, [])
        known += [x for x in summaries if x not in known]
    # --force skips only the existence check. A member the store does not
    # return has no recordedAt and no generation, and a summary written over
    # it would carry a guessed generation and coverage while hiding it.
    for entry_id in all_ids:
        if entry_id not in facts:
            errors.append(f"{entry_id} is not indexed yet (the store lags a "
                          "few seconds after a store); nothing written")
    for entry_id in all_ids:
        if compacted.get(entry_id):
            errors.append(f"{entry_id} is already compacted into "
                          f"{', '.join(compacted[entry_id])}")
    if errors:
        for e in errors:
            print(f"[memory] plan error: {e}", file=sys.stderr)
        return 1

    actor = _slug(actor_arg or os.environ.get("RETINUE_MEMORY_ACTOR", "") or "ara")
    now = _now()
    lines: list[str] = []
    written: list[tuple[str, dict, int]] = []
    used: set[str] = set()
    for item in items:
        summary_id = _new_id(now)
        while summary_id in used:
            summary_id = _new_id(now)
        used.add(summary_id)
        members = item["summarizes"] + item["retires"]
        generation = summary_generation(members, facts)
        auto_from, auto_to = summary_covers(members, facts)
        covers_from = item["covers_from"] or auto_from
        covers_to = item["covers_to"] or auto_to
        lines += compaction_lines(summary_id, item, generation, covers_from,
                                  covers_to, actor, model, now)
        written.append((summary_id, item, generation))

    path = MEMORY_DIR / f"compaction-{_new_id(now)}.nt"
    _write_atomically(path, lines)
    for summary_id, item, generation in written:
        print(f"[memory] stored summary {summary_id} (gen {generation}, "
              f"{len(item['summarizes'])} kept, {len(item['retires'])} retired) "
              f"-> {path}", file=sys.stderr)
        print(summary_id)
    return 0


# ---------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_store = sub.add_parser("store", help="store one memory entry")
    p_store.add_argument("content", help="the memory text")
    p_store.add_argument("--tag", action="append",
                         help="topic tag (repeatable, at least one)")
    p_store.add_argument("--actor", default="",
                         help="recording actor basename (default: ara)")
    p_store.add_argument("--relevance", type=float, default=None,
                         help="expected durability, 0..1 (1.0 standing rule, "
                              "0.7 decision/lesson, 0.3 incident/status)")
    p_store.add_argument("--expires", default="",
                         help="when the entry stops being relevant: a date, a "
                              "dateTime, or 10 / 10d / 2w / 3m from now — for "
                              "pending states and deadlines, never for rules")
    p_store.add_argument("--session", default="",
                         help="session label — entries sharing it share a file")
    p_store.add_argument("--model", default="",
                         help="model that wrote this memory "
                              "(default: RETINUE_SESSION_MODEL)")
    p_store.add_argument("--corrects", action="append", metavar="ID",
                         help="this entry corrects that plainly false memory")
    p_store.add_argument("--supersedes", action="append", metavar="ID",
                         help="the world changed; this entry replaces that one")
    p_store.add_argument("--questions", action="append", metavar="ID",
                         help="this entry puts that memory's veracity in doubt")
    p_store.add_argument("--force", action="store_true",
                         help="skip the existence check for challenged ids")
    p_store.add_argument("--duplicate-ok", action="store_true",
                         help="frontier only: store despite a near-duplicate")
    p_store.add_argument("--new-tag", action="store_true",
                         help="frontier only: allow a tag no memory uses yet")
    p_store.set_defaults(func=store)

    p_reinforce = sub.add_parser(
        "reinforce",
        help="strengthen an existing memory (the user restated it)")
    p_reinforce.add_argument("id", help="memory id or full urn:retinue:memory: URI")
    p_reinforce.add_argument("--session", default="",
                             help="session label for the file the "
                                  "reiteration is written to")
    p_reinforce.add_argument("--force", action="store_true",
                             help="skip the existence check in the life store")
    p_reinforce.set_defaults(func=reinforce)

    p_tags = sub.add_parser("tags", help="list the tags in use, with counts")
    p_tags.add_argument("--prefix", default="", help="only tags starting with this")
    p_tags.add_argument("--contains", default="", help="only tags containing this")
    p_tags.add_argument("--all", action="store_true",
                        help="count every entry, not only live ones")
    p_tags.add_argument("--json", action="store_true", help="JSON instead of text")
    p_tags.set_defaults(func=tags)

    p_recall = sub.add_parser("recall", help="query memories from the life store")
    p_recall.add_argument("--tag", action="append",
                          help="match any of these tags (repeatable)")
    p_recall.add_argument("--actor", default="", help="filter by recording actor")
    p_recall.add_argument("--since", default="", help="date or dateTime lower bound")
    p_recall.add_argument("--until", default="", help="date or dateTime upper bound")
    p_recall.add_argument("--min-relevance", type=float, default=None,
                          help="only entries declaring at least this relevance")
    p_recall.add_argument("--limit", type=int, default=None,
                          help="maximum rows (default 20; --expand: all members)")
    p_recall.add_argument("--include-superseded", action="store_true",
                          help="also return corrected/superseded entries "
                               "(labeled; hidden by default)")
    p_recall.add_argument("--include-expired", action="store_true",
                          help="also return entries past their expiry "
                               "(labeled EXPIRED; hidden by default)")
    p_recall.add_argument("--include-compacted", action="store_true",
                          help="also return entries a standing summary carries "
                               "(labeled COMPACTED; hidden by default)")
    p_recall.add_argument("--expand", default="", metavar="SUMMARY_ID",
                          help="list that summary's members, kept and retired")
    p_recall.add_argument("--json", action="store_true",
                          help="raw rows instead of prompt-ready text")
    p_recall.set_defaults(func=recall)

    p_compact = sub.add_parser(
        "compact", help="write summaries from a compaction plan (JSON)")
    p_compact.add_argument("--plan", required=True,
                           help="path of the plan, or - for stdin")
    p_compact.add_argument("--actor", default="",
                           help="recording actor basename (default: ara)")
    p_compact.add_argument("--model", default="",
                           help="model that wrote the summaries "
                                "(default: RETINUE_SESSION_MODEL)")
    p_compact.add_argument("--force", action="store_true",
                           help="skip the existence check of the members")
    p_compact.set_defaults(func=compact)
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
