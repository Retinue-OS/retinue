"""What the attention model keeps, and how the gateway's documents become its
items.

The policy (scripts/attention.py) decides over plain items; this module is
the plumbing around it that the web-gateway and the boot emitter share:

- **The two small documents** under ``ATTENTION_DIR`` — ``focus.json`` (modes,
  the week of day plans, holidays, digest times, the manual override) and ``profile.json``
  (importance and sphere priors per sender or kind, lead times per kind,
  permits per mode, the learned log) — plus ``projects.json``, the delivery
  state of projects, which have no document of their own in the gateway (their
  properties live in chamber frontmatter; only what the model does with them
  is kept here). Single-writer, one lock, atomic writes: the chat-state
  precedent.
- **The clock.** Modes are a schedule over the local day, so the model needs
  a zone: ``ATTENTION_TZ``, else ``RETINUE_DISPLAY_TZ``, else ``TZ``, else
  the container's local zone.
- **The adapters** from a thread, a chat and a project row to a policy item,
  each carrying the display fields the home screen renders beside the three
  attention fields, and the way back (``block_for``) to the ``attention``
  block that is stored on the document.
- **The life-store emit**: the four properties of every open item as
  N-Triples under ``chambers/_generated/attention/``, sorted, blank-node-free
  and write-if-changed (the discover-agents discipline), so the question
  "what wants attention, at which level" is one SELECT.

Stdlib only.
"""
from __future__ import annotations

import os
import re
import threading
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import attention as policy

# Where a sender's sphere comes from when nothing has said: people who write
# to the user are friends until the profile, a triage turn or a correction
# says otherwise. Threads and projects default to admin (item_from_doc).
DEFAULT_CHAT_SPHERE = "friends"
# …except a sender nothing vouches for: not a VIP (docs/triage-delivery-
# gate.md), no contact card, no sphere the user taught the profile. A stranger
# who has the user's number is not a friend and not a customer; nobody has
# said which, and guessing "friends" would let anyone who learns the number
# ring during Social. So an unnamed sender gets a sphere of
# their own that no mode admits by default: the message is *screened* — listed
# and carried by the next digest, never rung — until the user files a contact
# card and says which sphere they belong to. Hey's Screener, in one word.
UNKNOWN_SPHERE = "unknown"
# What a message is worth before anyone has judged it. A person writing to
# the user directly is *active* (importance 4 — 3.5 is the row's edge): held
# for the next digest in every default mode, pushed at once where the sender
# holds a permit, so a human's message is never merely listed. A group is
# chatter (importance 1, passive) until the triage says otherwise — the
# fourteen messages about the street party are listed, never rung.
DEFAULT_DIRECT_IMPORTANCE = 4.0
DEFAULT_GROUP_IMPORTANCE = 1.0

# The sphere vocabulary the sheet offers. A deployment can extend it in
# focus.json (``spheres``); the modes' ``admits`` lists use the same words.
DEFAULT_SPHERES = ["customers", "admin", "health", "friends", "family", "system", UNKNOWN_SPHERE]


def zone():
    """The zone the schedule is read in (see the module docstring).

    The containers run on UTC and the retinue service takes no env_file, so a
    deployment that sets nothing here would switch modes and send digests on
    UTC. RETINUE_DISPLAY_TZ — the zone the owner lives in, which the compose
    file already passes for the approval pages — is therefore the fallback
    before TZ; ATTENTION_TZ is only for a schedule that keeps another zone."""
    name = (os.environ.get("ATTENTION_TZ") or os.environ.get("RETINUE_DISPLAY_TZ")
            or os.environ.get("TZ") or "").strip()
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001 - an unknown name falls back to local time
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc


class AttentionStore:
    """focus.json, profile.json and projects.json under one directory."""

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.lock = threading.RLock()
        self.tz = zone()

    def now(self) -> datetime:
        return datetime.now(self.tz)

    # -- the documents --------------------------------------------------------

    def focus(self) -> dict:
        with self.lock:
            # A document from before the week is read as one (upgrade_focus)
            # before the shipped defaults fill in what it does not say.
            stored = policy.upgrade_focus(policy.load_json(self.dir / "focus.json", {}))
            focus = policy.default_focus()
            focus.update(stored)
            # Every mode the document names exists, whatever wrote it — or
            # every reading of the mode would fail on the missing one.
            policy.heal_focus(focus)
            focus.setdefault("spheres", list(DEFAULT_SPHERES))
            if UNKNOWN_SPHERE not in focus["spheres"]:
                # Structural, not a matter of taste: the model itself puts a
                # sender nobody has named into this sphere, so a document
                # written before it existed (or a deployment that pruned the
                # vocabulary) still has to be able to name it in a rule.
                focus["spheres"].append(UNKNOWN_SPHERE)
            for mid, mode in focus["modes"].items():
                # A document from before the flag: the shipped default for
                # the mode of that name, off for one the deployment named.
                mode.setdefault("only_admitted", bool(policy.DEFAULT_MODES.get(mid, {}).get("only_admitted")))
                mode.setdefault("admit_tags", [])
            return focus

    def save_focus(self, focus: dict) -> None:
        with self.lock:
            policy.save_json(self.dir / "focus.json", focus)

    def profile(self) -> dict:
        with self.lock:
            profile = policy.load_json(self.dir / "profile.json", policy.default_profile())
            profile.setdefault("spheres", {})
            profile.setdefault("tags", {})
            profile.setdefault("priors", {})
            profile.setdefault("permits", {})
            profile.setdefault("learned", [])
            return profile

    def save_profile(self, profile: dict) -> None:
        with self.lock:
            # The learned log is a diary, not an archive: keep the last 200.
            profile["learned"] = (profile.get("learned") or [])[-200:]
            policy.save_json(self.dir / "profile.json", profile)

    def projects(self) -> dict:
        """Delivery state per project URI (an ``attention`` block each)."""
        with self.lock:
            return policy.load_json(self.dir / "projects.json", {})

    def save_project(self, uri: str, block: dict | None) -> None:
        with self.lock:
            states = self.projects()
            if block is None:
                states.pop(uri, None)
            else:
                states[uri] = block
            policy.save_json(self.dir / "projects.json", states)

    def last_tick(self) -> datetime | None:
        """The last minute the gateway's tick acted on, as it left it — so a
        restart can make up a digest or a mode change it slept through."""
        with self.lock:
            try:
                return policy.parse_dt(policy.load_json(self.dir / "tick.json", {}).get("minute"))
            except (TypeError, ValueError):
                return None

    def save_last_tick(self, minute: datetime) -> None:
        with self.lock:
            policy.save_json(self.dir / "tick.json", {"minute": minute.isoformat()})


# -- adapters ---------------------------------------------------------------------

def _first_message(conv: dict) -> dict:
    msgs = conv.get("messages") or []
    return msgs[0] if msgs else {}


def _last_message(conv: dict) -> dict:
    msgs = conv.get("messages") or []
    return msgs[-1] if msgs else {}


# A thread's reply chips belong to the thread; a one-line preview — the row,
# the push, the digest line — shows the words ([[chip: Send it]] · [[chip: …]]).
_CHIP_RE = re.compile(r"\[\[chip:[^\]]*\]\]\s*(?:·\s*)?")


def _one_line(text: str, limit: int = 160) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[:limit - 1].rstrip() + "…"


def thread_wants_attention(conv: dict) -> bool:
    """Whether a thread is an item at all.

    A thread an agent opened is a request for a decision: it stays an item
    until the user marks it done or archives it. A thread the user opened is
    theirs; it appears only while Ara's reply lies unread. Edit, companion and
    cowork threads never appear — they belong to a project page, a chat, or
    the audit trail."""
    if conv.get("archived") or (conv.get("kind") or "chat") != "chat":
        return False
    block = conv.get("attention") or {}
    if block.get("state") == "done":
        return False
    if conv.get("initiator") == "agent":
        return True
    return bool(conv.get("unread"))


def thread_item(conv: dict, profile: dict) -> dict:
    """A conversation thread as a policy item, display fields included."""
    first = _first_message(conv)
    block = dict(conv.get("attention") or {})
    agent = first.get("agent") or ("Ara" if first.get("role") == "assistant" else None)
    if conv.get("initiator") == "agent":
        agent = agent or "Retinue"
        preview_msg = first
    else:
        preview_msg = _last_message(conv)
    doc = {"id": f"thread:{conv['id']}", "title": conv.get("title") or "Conversation",
           "attention": block, "archived": bool(conv.get("archived"))}
    item = policy.item_from_doc(doc, "thread", profile)
    if conv.get("initiator") != "agent":
        # Ara's reply to the user's own question is never an interruption to
        # rule on; it is listed while unread and pushed the way it always was.
        item["released"] = True
        item["importance_from"] = block.get("importance_from") or "your thread"
    project = conv.get("project") or None
    item.update({
        "source_id": conv["id"],
        # The user's own thread stays visible in every mode (sections' fold).
        "own": conv.get("initiator") != "agent",
        # The project the thread is about, if any: the list shows the thread
        # in the project's place (fold_projects), the row links to both.
        "project": project,
        "project_title": conv.get("project_title") if project else None,
        "project_href": ("/project.html?" + urllib.parse.urlencode({"id": project})) if project else None,
        "preview": _one_line(_CHIP_RE.sub("", preview_msg.get("text") or "")
                             or ("Sent you a file" if preview_msg.get("attachments") else "")),
        "agent": agent,
        "unread": bool(conv.get("unread")),
        "pending": bool(conv.get("pending")),
        "count": 1,
        "href": f"/#conversation-{conv['id']}",
        "channel": None,
    })
    return item


def chat_wants_attention(chat: dict, state: dict) -> bool:
    """A chat is an item while its attention state is open — set when an
    inbound message arrives and cleared by a reply or *Mark handled* — or,
    for a chat from before the model existed, while it holds unread
    messages."""
    if chat.get("archived"):
        return False
    block = (state or {}).get("attention") or {}
    if block:
        return block.get("state", "open") == "open"
    return int(chat.get("unread") or 0) > 0


def chat_is_unknown(state: dict) -> bool:
    """Is this chat's peer a stranger? — nothing on the rail vouched for the
    last arrival's sender (they are not a VIP; see the web-gateway's
    _rail_unknown_sender) and no contact card has since named them. A sphere
    the user taught the profile for them still wins in `chat_item`."""
    state = state or {}
    return bool(state.get("unknown_sender")) and not state.get("contact")


def chat_screened(item: dict) -> bool:
    """Is this chat item screened — nothing vouches for its sender *and*
    nothing has placed them either, so its sphere is still ``unknown``?
    Read off the item's current sphere, so a correction that moves it answers
    at once."""
    return bool(item.get("stranger")) and item.get("sphere") == UNKNOWN_SPHERE


def chat_item(chat: dict, state: dict, profile: dict) -> dict:
    """A messenger chat (a /chats row plus its state document) as an item.

    The sender is the chat's name — a person or a group — which is also the
    key the profile's priors and permits use, since that is how the user
    thinks of them."""
    block = dict((state or {}).get("attention") or {})
    # Read before the defaults below fill the block in.
    predates_model = not block
    name = chat.get("name") or chat.get("key") or chat.get("id") or "Chat"
    stranger = chat_is_unknown(state)
    priors = profile.get("priors") or {}
    if "importance" not in block:
        if name in priors:
            block["importance"] = priors[name]
            block["importance_from"] = "prior"
        else:
            block["importance"] = DEFAULT_GROUP_IMPORTANCE if chat.get("group") else DEFAULT_DIRECT_IMPORTANCE
            block["importance_from"] = "default"
    # The spheres: a triage judgement of the latest message where there is
    # one (a customer who is also a friend, asking about Saturday's
    # barbecue, is friends for that message), otherwise the sender's own —
    # every sphere they are in, as the profile learned them from the contact
    # card and the corrections, the card itself for one filed before the
    # profile kept further spheres. A stranger keeps the importance of a
    # human writing to a human — someone took the trouble — and loses only
    # the guess about *where they belong*.
    card = (state or {}).get("contact") or {}
    sphere = ((profile.get("spheres") or {}).get(name) or card.get("sphere")
              or (UNKNOWN_SPHERE if stranger else DEFAULT_CHAT_SPHERE))
    known = profile.get("tags") or {}
    tags = list(known[name] if name in known else card.get("tags") or [])
    if block.get("sphere_from") == "message":
        # A judged sphere is the message's alone (with the tags judged
        # beside it); judged tags alone sit beside the sender's main sphere.
        if block.get("sphere"):
            sphere = block["sphere"]
        tags = list(block.get("tags") or [])
    judged = dict(block, sphere=sphere, tags=[t for t in dict.fromkeys(tags) if t != sphere])
    doc = {"id": f"chat:{chat['id']}", "title": name, "attention": judged, "sphere": sphere,
           "sender": name, "archived": bool(chat.get("archived"))}
    item = policy.item_from_doc(doc, "chat", profile)
    last = chat.get("last") or {}
    if predates_model:
        # No block yet (the chat predates the model): it is on the list, not
        # held — nobody decided to hold it, and hiding it would lose it.
        item["released"] = True
    item.update({
        "source_id": chat["id"],
        "preview": _one_line(last.get("text") or ("Photo" if last.get("kind") == "image" else "")),
        "channel": chat.get("channel"),
        # The chat's current name, not the copy the block was persisted with:
        # naming a number renames its peer, and the profile's priors, permits
        # and sphere are keyed on the name the user sees.
        "sender": name,
        "handle": chat.get("key") or "",
        # Screened is where that lands (see chat_screened): a sender the
        # profile or an earlier judgement on this chat already placed is not a
        # stranger, whatever the rail said.
        "stranger": stranger,
        "unknown_sender": chat_screened({"stranger": stranger, "sphere": sphere}),
        "contact": (state or {}).get("contact") or None,
        "group": bool(chat.get("group")),
        "count": int(chat.get("unread") or 0),
        "unread": int(chat.get("unread") or 0) > 0,
        "muted": bool(chat.get("muted")),
        "draft": chat.get("draft"),
        "href": "/chat.html?" + urllib.parse.urlencode({"id": chat["id"]}),
        "agent": None,
    })
    return item


_FRONTMATTER_LEAD_RE = re.compile(r"^\s*(\d+)\s*([dwm]?)\s*$", re.IGNORECASE)
_FRONTMATTER_LEAD_DAYS = {"": 1, "d": 1, "w": 7, "m": 30}


def frontmatter_lead(text) -> float | None:
    """A project's ``remind_before`` in minutes. The frontmatter speaks the
    grammar of recurring-projects.py — a bare number or ``Nd`` days, ``Nw``
    weeks, ``Nm`` calendar months (thirty days here) — not the agents' one,
    where a bare number and ``m`` are minutes. None when it is not one."""
    m = _FRONTMATTER_LEAD_RE.match(str(text or ""))
    if not m:
        return None
    return float(int(m.group(1)) * _FRONTMATTER_LEAD_DAYS[m.group(2).lower()] * policy.DAY)


def _frontmatter_said(row: dict) -> dict:
    """What a project's frontmatter says about the four properties, as plain
    comparable values — the snapshot project_item keeps in the block."""
    tags = sorted({t for t in (policy.sphere_id(_humanize_sphere(x)) for x in row.get("tags") or []) if t})
    return {
        "importance": row.get("importance"),
        "sphere": policy.sphere_id(_humanize_sphere(row["sphere"])) if row.get("sphere") else None,
        "tags": tags or None,
        "kind": row.get("kind") or None,
        "deadline": row.get("expected") or row.get("next_due") or None,
        "remind_before": row.get("remind_before") or None,
    }


def _apply_frontmatter(block: dict, key: str, value, now: datetime) -> None:
    """Put one frontmatter value into the block — or, where the author took
    it out, the field back to its default."""
    if key == "importance":
        block.pop("importance", None)
        block.pop("importance_from", None)
        try:
            block["importance"] = max(0.0, min(5.0, float(value)))
            block["importance_from"] = "frontmatter"
        except (TypeError, ValueError):
            pass
    elif key == "sphere":
        if value:
            block["sphere"] = value
        else:
            block.pop("sphere", None)
    elif key == "tags":
        block["tags"] = list(value or [])
    elif key == "kind":
        if value:
            block["kind"] = value
        else:
            block.pop("kind", None)
    elif key == "deadline":
        due = policy.parse_due(value, now) if value else None
        block["due"] = due.isoformat() if due else None
    elif key == "remind_before":
        block.pop("lead", None)
        block.pop("lead_from", None)
        lead = frontmatter_lead(value) if value else None
        if lead is not None:
            block["lead"] = lead
            block["lead_from"] = "remind_before"


def project_item(row: dict, state_block: dict | None, profile: dict, now: datetime,
                 you: str) -> dict:
    """A project (one SPARQL row of the projects query) as an item.

    ``row`` carries what the frontmatter said (``title``, ``actor``, ``next``,
    ``expected``, ``next_due``, ``since``, ``importance``, ``sphere``, ``tags``,
    ``kind``, ``remind_before``); ``state_block`` is what the model keeps
    about it. A correction on a project changes the state block, never the
    chamber file — the author's frontmatter stays the author's.

    The two meet by the latest word: the block keeps a snapshot of what the
    frontmatter said when it was last stored (``frontmatter``), and a value
    the author has changed since — a deadline moved, recurring-projects.py
    advancing ``next_due``, an importance raised — replaces whatever the block
    held, while one the author left alone lets a correction stand. A block
    from before the snapshot takes what the frontmatter states."""
    block = dict(state_block or {})
    seen = block.pop("frontmatter", None)
    seen = seen if isinstance(seen, dict) else {}
    said = _frontmatter_said(row)
    for key, value in said.items():
        changed = seen[key] != value if key in seen else value is not None
        if changed:
            _apply_frontmatter(block, key, value, now)
    if block.get("lead_from") == "kind default":
        # A stored copy of the kind's lead is no decision about this project:
        # its kind's lead as the profile has it now (a correction on another
        # item of the kind teaches it), for the kind the frontmatter names now.
        block.pop("lead", None)
        block.pop("lead_from", None)
    actor_uri = row.get("actor") or ""
    actor = "you" if (not actor_uri or actor_uri == you) else _humanize(actor_uri)
    block["actor"] = actor
    if row.get("since") and not block.get("waiting_since"):
        since = policy.parse_due(row["since"], now)
        if since is not None:
            block["waiting_since"] = since.replace(hour=0).isoformat()
    doc = {"id": row["id"], "title": row.get("title") or _humanize(row["id"]), "attention": block,
           "sphere": block.get("sphere") or "admin"}
    item = policy.item_from_doc(doc, "project", profile)
    if not state_block:
        # First sight of a running project: it is listed at its level from
        # the start. Projects surface by their deadlines through the sweep and
        # the recurring-projects wake threads, never by arriving.
        item["released"] = True
    item.update({
        "source_id": row["id"],
        # Stored with the block (block_for), so the next read can tell the
        # author's changes from what the block has held since.
        "frontmatter": said,
        "preview": _one_line(row.get("next") or ""),
        "href": "/project.html?" + urllib.parse.urlencode({"id": row["id"]}),
        "count": 1,
        "unread": False,
        "channel": None,
        "agent": None,
    })
    return item


def fold_projects(items: list[dict]) -> list[dict]:
    """One row per thing: a project that an open thread is about — the
    wake-up recurring-projects opened, a question an agent asked about it —
    shows as that thread, not beside it. The thread carries the news and
    the decision; the project page is one link away on its row. A project
    nobody has a thread open about stays its own row. A thread the user
    opened about a project is theirs, not the project's: the project stays
    the item, the thread shows while Ara's reply lies unread, as any of
    their threads does."""
    claimed = {i["project"] for i in items
               if i.get("kind") == "thread" and i.get("project") and not i.get("own")
               and i.get("state", "open") == "open"}
    if not claimed:
        return items
    return [i for i in items if not (i.get("kind") == "project" and i["id"] in claimed)]


def _humanize(uri: str) -> str:
    tail = uri.rsplit(":", 1)[-1] if uri else ""
    for pfx in ("project-", "goal-"):
        if tail.startswith(pfx):
            tail = tail[len(pfx):]
    return " ".join(w.capitalize() for w in tail.replace("_", "-").split("-") if w) or uri


def _humanize_sphere(value: str) -> str:
    """A sphere as frontmatter or the store spells it — a bare word, or a
    ``urn:retinue:sphere:<word>`` URI — as the bare word."""
    v = str(value or "").strip()
    return v.rsplit(":", 1)[-1] if v.startswith("urn:") else v


def block_for(item: dict) -> dict:
    """The ``attention`` block to store back on the item's document — for a
    project with the snapshot of its frontmatter (see project_item)."""
    block = policy.item_to_attention(item)
    if item.get("frontmatter") is not None:
        block["frontmatter"] = item["frontmatter"]
    return block


# -- the life-store emit --------------------------------------------------------

def subject_iri(item: dict) -> str:
    kind = item.get("kind")
    if kind == "thread":
        return f"urn:retinue:thread:{item['source_id']}"
    if kind == "chat":
        return "urn:retinue:chat:" + urllib.parse.quote(str(item["source_id"]), safe=":~+@=.-_")
    return str(item["source_id"])


def emit(items: list[dict], path: Path) -> bool:
    """Write the open items' four properties as N-Triples; True when the file
    changed (a qlever-dir rebuild follows only then)."""
    open_items = [i for i in items if i.get("state", "open") == "open"]
    return policy.write_if_changed(path, policy.to_ntriples(open_items, subject_iri))


def default_emit_path(chambers_dir: Path) -> Path:
    return Path(chambers_dir) / "_generated" / "attention" / "items.nt"


__all__ = [
    "AttentionStore", "DEFAULT_CHAT_SPHERE", "DEFAULT_SPHERES", "DEFAULT_DIRECT_IMPORTANCE",
    "DEFAULT_GROUP_IMPORTANCE", "zone",
    "thread_wants_attention", "thread_item", "chat_wants_attention", "chat_is_unknown",
    "chat_screened", "chat_item",
    "project_item", "fold_projects", "block_for", "subject_iri", "emit", "default_emit_path",
]
