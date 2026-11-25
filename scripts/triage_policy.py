#!/usr/bin/env python3
"""Delivery-gate policy: VIP and instructed senders, group flags as N-Triples.

This is the single source of truth for *who is worth a model turn* in the
delivery gate (see `docs/triage-delivery-gate.md`, and `docs/mail-chats.md` for
mail). It is deliberately stdlib-only so the gateways can import a
byte-identical copy and read policy straight off their mounted volume, without
the ~15 s SPARQL reindex lag on their classify hot path.

Two kinds of policy live here:

  * **E-mail groups** — the e-mail counterpart of the messenger *group* flags,
    on the same three orthogonal axes (``news`` / ``quieted`` / ``ignored``).
    What plays the part of a messenger group is the **mailing list**: a mail's
    group id is its ``List-Id`` (RFC 2919) when it carries a usable one, and
    otherwise its own sender address, so a listless newsletter is simply a group
    of one. Membership takes two shapes — an exact entry, or a `*@domain` /
    `*@*.domain` wildcard — and a wildcard's domain also covers a `List-Id`
    under it: `*@substack.com` therefore matches both `no-reply@substack.com`
    and the list `sgcarney.substack.com`.

    The three flags mean:

      - ``news`` — the group is a broadcast source: its mail is filed into the
        news feed for the Herald to score, and starts no chat. Independent of
        the two below.
      - ``quieted`` / ``ignored`` — the group's mail is shown in its chat
        silently: it notifies nobody. The two differ where the group is also
        ``news``: an ``ignored`` news mail is consumed once the feed has it
        (the mail gateway marks it seen and moves it out of the INBOX), a
        ``quieted`` one stays where it arrived — a list one both reads and
        answers.

    The sender axis is independent of the group flags, as on a messenger: a
    VIP's or an instructed sender's mail is worked whatever list it arrived
    on. ``news`` membership is checked against the group id *and* the sender
    address, so address-level entries written before list detection keep
    working.

    There is no e-mail whitelist any more. It decided which mail e-mail
    triage worked at once; e-mail triage is retired, mail is gated like a
    messenger message (the sender axis below, on the ``email`` channel), and
    what the whitelist knew lives in instructions on contacts
    (docs/mail-chats.md, "What retires").
  * **Messenger policy** — per channel, on two orthogonal axes; ``email`` is
    one of the channels for the **sender** axis (its group axis is the e-mail
    groups above, in a file of their own). There is no
    sender whitelist or blacklist here: every message reaches the user through
    the chat surface, and a chat they want off their screen is archived or muted
    in the dashboard, not decided by a policy file.
      - **Sender** (a handle): VIP or not. A VIP's message is worked by a model
        the moment it arrives — filed, linked to a project, remembered, answered
        in draft — wherever it arrives, a group included. Anyone else's message
        simply shows up in the chat, which is delivery enough. Independently, a
        sender can be **instructed**: the user wrote a brief for the person,
        each message of theirs is worked by it, and the verdict of that turn
        decides whether the message rings (a VIP rings regardless).
        A handle is compared exactly — except on ``email``, where a handle
        may also be a `*@domain` / `*@*.domain` wildcard (a contact's
        domain) and a sender matches an exact address or falls under one.
        On every other channel such a string is just a handle nobody has.
      - **Group** (three independent flags): ``news`` — its messages are also
        forwarded to the news feed (Herald), a rail parallel to the chat;
        ``quieted`` — the message is stored and shown but nothing is owed to a
        model, and the record stays ``delivered: false``; ``ignored`` — the
        message is accounted for on arrival and never re-surfaced. ``news`` is
        independent of both. The legacy ``triageBlockedGroup`` predicate is read
        as ``ignored``.

Everything is emitted as sorted, deterministic N-Triples with the same
write-if-changed discipline as `discover-agents.py`, so an unchanged policy never
triggers a qlever-dir rebuild, and the very same file the gateway reads raw is
indexed in the life store for `who is a VIP?` queries.

The retinue side writes, the gateways read. There are two writers there: Ara,
through this module's CLI, and the web gateway, which projects the address
book's VIPs on every contact write and on its tick (:func:`sync_contacts`).
Every read-modify-write holds the file's :func:`policy_lock`, so neither can
drop the other's change. The `_generated` paths are framework-owned, in no
chamber's git repo.

**Contact-owned members.** A person filed in the address book
(`scripts/contacts.py`, docs/contacts.md) can be a VIP; the person, not a
handle, is then the truth, and :func:`sync_contacts` projects every handle of
every VIP person into the channels' policy files as VIP handles — messenger
accounts, and on ``email`` their addresses and domain wildcards — and every
handle of every person who carries an instruction as an instructed handle.
They are written under a subject of their own (``…:contacts``), so the
projection is rewritten wholesale without touching what was set by hand, and
a reader that does not know the split (a gateway built before it) still sees
every member: the loaders read the predicate and ignore the subject.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import os
import re
import sys
from collections import namedtuple
from pathlib import Path

KB = "https://w3id.org/retinue/kb#"

# Predicates. One flat subject per policy file, repeated predicates for members.
# E-mail group flags. The news pair keeps its original `…Address` names because
# policy files already carry them; it is a *group* predicate now like the other
# two, since a listless sender's group id is its own address. The quieted and
# ignored pairs reuse the messenger group predicates below — same relation, just
# stated about the e-mail subject — plus a wildcard twin each.
P_NEWS_ADDRESS = KB + "triageNewsAddress"
P_NEWS_WILDCARD = KB + "triageNewsWildcard"
# Retired on messenger (2026-09): triageWhitelistHandle / triageBlacklistHandle.
# The whitelist decided whose message was worth a session to *notify* about, and
# the chat surface notifies for free, so it ended up answering the same as no
# entry at all. The blacklist said "do not bother me about this person", which is
# muting their chat — a thing the user does in the interface, on the chat, where
# they are looking at it. Both are simply no longer read; existing triples in a
# policy file are inert and drop out on the next write.
# Retired on e-mail with e-mail triage: triageWhitelistAddress /
# triageWhitelistWildcard, and the address book's projection into them under
# the subject `…email-whitelist:contacts`. Not read either, and gone from the
# e-mail policy file on its next write; scripts/migrate-triage.py moves what
# they held onto contacts, reading the file raw.
# The VIP axis: a person whose message is worked by a model the moment it
# arrives, wherever it arrives. Keyed on the sender and only on the sender, so
# it holds in a group exactly as in a 1:1 — being one of forty people in a room
# does not make what this person says any less worth reading.
P_VIP_HANDLE = KB + "triageVipHandle"
# The instructed axis: a person the user wrote a standing instruction for
# (kb:instruction on the contact). Each message of theirs buys one model turn
# that works it by that brief, and the turn's verdict — not the arrival —
# decides whether it rings. Sender-only like VIP, and independent of it: a
# handle can carry both, which means "ring at once, and work it as briefed".
# Only ever projected from the address book (the words live with the person,
# so there is nothing to set by hand here); see sync_contacts.
P_INSTRUCTED_HANDLE = KB + "triageInstructedHandle"
# Group flags — three orthogonal axes (see module docstring).
P_IGNORED_GROUP = KB + "triageIgnoredGroup"
P_QUIETED_GROUP = KB + "triageQuietedGroup"
P_NEWS_GROUP = KB + "triageNewsGroup"
# The wildcard twins of the two above, only ever used on the e-mail subject (a
# messenger group id is an opaque handle, so a domain wildcard is meaningless
# there).
P_IGNORED_WILDCARD = KB + "triageIgnoredWildcard"
P_QUIETED_WILDCARD = KB + "triageQuietedWildcard"
# Legacy: the original single "blocked group" flag, now read as `ignored`. Still
# parsed so existing policy files keep working; render migrates it to
# P_IGNORED_GROUP on the next write.
P_BLOCKED_GROUP = KB + "triageBlockedGroup"

EMAIL_SUBJECT = "urn:retinue:triage:email-whitelist"
# The messenger channels the framework runs gateways for.
MESSENGER_CHANNELS = ("signal", "whatsapp", "telegram", "sms")
# Mail's channel: gated on the sender axis like a messenger (its policy file
# is messenger_policy_path("email"), read by the mail gateway), and the one
# channel whose handles may be domain wildcards.
EMAIL_CHANNEL = "email"
# The subject suffix of the contact-owned members (see the module docstring).
CONTACTS_SUFFIX = ":contacts"

# The loaded e-mail policy: one (exact, wildcard) pair per group flag. One file
# holds all three classes, so every write goes through save_email_policy() — a
# writer that renders only some of them erases the rest. Every field defaults
# to empty, so `EmailPolicy()` is the empty policy. Name the fields you set (or
# `_replace`), never pass them by position: the tuple lost its two leading
# whitelist fields, and a positional caller written before that would now put
# its sets under the wrong flags.
EmailPolicy = namedtuple(
    "EmailPolicy",
    "news news_wildcards quieted quieted_wildcards ignored ignored_wildcards",
    defaults=(frozenset(),) * 6,
)

# The loaded messenger policy: the three group-flag sets plus the sender sets
# that still decide anything — VIP, set by hand (`vip`) or projected from the
# VIP persons of the address book (`vip_contacts`), a sender being a VIP if
# either says so; and the handles of the persons who carry an instruction
# (`instructed_contacts`, projected only). On the `email` channel a member of
# a sender set may be a `*@domain` wildcard.
MessengerPolicy = namedtuple("MessengerPolicy",
                             "ignored quieted news vip vip_contacts instructed_contacts",
                             defaults=(frozenset(), frozenset()))


def _channel_subject(channel: str) -> str:
    return f"urn:retinue:triage:{channel}"


CHAMBERS_DIR = Path(os.environ.get("CHAMBERS_DIR") or "/workspace/chambers")


def _messenger_root() -> Path:
    base = os.environ.get("TRIAGE_MESSENGER_DIR")
    return Path(base) if base else (CHAMBERS_DIR / "_generated" / "messenger")


def email_whitelist_path() -> Path:
    """Where the e-mail policy `.nt` lives: the e-mail group flags.

    The function and its override variable keep the name of the whitelist the
    file once also held, because deployments and the mail gateway's compose
    service name the variable.

    On the mail gateway's ledger volume, in its ``policy/`` folder beside the
    one ``messenger_policy_path("email")`` names, because the mail gateway
    reads the group flags at ingest and mounts no other part of the retinue
    side (docs/mail-chats.md). qlever indexes it there like every messenger
    policy. ``TRIAGE_EMAIL_WHITELIST_PATH`` overrides the path, and is how the
    gateway container, which sees the volume at ``/inbound``, names the same
    file. The file used to live at :func:`legacy_email_policy_path`;
    :func:`ensure_email_policy_location` moves it.
    """
    override = os.environ.get("TRIAGE_EMAIL_WHITELIST_PATH")
    if override:
        return Path(override)
    return _messenger_root() / "email" / "policy" / "email-policy.nt"


def legacy_email_policy_path() -> Path:
    """Where the e-mail policy lived before the mail gateway had to read it."""
    return CHAMBERS_DIR / "_generated" / "triage" / "email-whitelist.nt"


def ensure_email_policy_location(path: Path | None = None,
                                 legacy: Path | None = None) -> dict:
    """Make sure the e-mail policy file exists where it now lives.

    Retinue side only: the gateway never writes policy. Holds the policy lock
    of the new file (and of the old one, so a writer still on the old path
    cannot slip a change in between) and then, in this order:

      * the new file exists — nothing to do. If the old one exists too and
        differs, the new one wins and the old is left alone and reported: it
        may hold a flag the new one lacks, which only a person can judge, and
        deleting it would lose that. The same bytes in both is a move that
        crashed after its write; the old one is then removed.
      * only the old file exists — its content moves to the new path (one
        atomic write there; the volumes differ, so it is a copy) and the old
        file is removed only after the new one is in place: a crash between
        the two leaves both, which the case above resolves.
      * neither exists — the new file is created empty, so a reader can tell
        "no flags" (an empty file) from "not set up yet" (no file); the mail
        gateway waits on the latter before its first backfill.

    The old path is moved only when ``TRIAGE_EMAIL_WHITELIST_PATH`` is unset:
    an explicit path is the operator's statement of where the file lives, and
    the default location is then not this function's to empty. Idempotent.
    Returns ``{"path", "action": "present" | "moved" | "created",
    "legacy_left": <old path> | None}``; raises OSError when the new path
    cannot be written (the caller reports it).
    """
    explicit = path is not None or bool(os.environ.get("TRIAGE_EMAIL_WHITELIST_PATH"))
    path = Path(path) if path is not None else email_whitelist_path()
    if legacy is not None:
        old = Path(legacy)
    elif explicit:
        old = None
    else:
        old = legacy_email_policy_path()
    if old is not None and old.resolve() == path.resolve():
        old = None
    with contextlib.ExitStack() as locks:
        locks.enter_context(policy_lock(path))
        if old is not None and old.exists():
            locks.enter_context(policy_lock(old))
        if path.exists():
            left = None
            if old is not None and old.exists():
                # The same bytes on both sides is a move that crashed after
                # its write: finishing it loses nothing.
                if old.read_bytes() == path.read_bytes():
                    old.unlink()
                else:
                    left = str(old)
            return {"path": str(path), "action": "present", "legacy_left": left}
        if old is not None and old.exists():
            # Bytes, not text: the file moves exactly as it is, whatever it
            # holds — this is a move, not a rewrite.
            _atomic_write_bytes(old.read_bytes(), path)
            old.unlink()
            return {"path": str(path), "action": "moved", "legacy_left": None}
        _atomic_write_bytes(b"", path)
        return {"path": str(path), "action": "created", "legacy_left": None}


def _email_policy_read_path() -> Path:
    """The file a default read of the e-mail policy reads.

    The new location, except while it does not exist yet and the old one
    still does, before :func:`ensure_email_policy_location` has run: a reader
    then sees the policy that is in force, never an empty one, and a writer
    that loads, changes and saves to the new path carries the old flags
    along instead of stranding them. An explicit path is read as given."""
    path = email_whitelist_path()
    if os.environ.get("TRIAGE_EMAIL_WHITELIST_PATH") or path.exists():
        return path
    old = legacy_email_policy_path()
    return old if old.exists() else path


def _atomic_write_bytes(data: bytes, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def messenger_policy_path(channel: str) -> Path:
    """Where a channel's policy `.nt` lives on the retinue side.

    This is the retinue writer's view. Each gateway reads the *same file* through
    its own volume mount (env `INBOUND_POLICY_PATH`), so the gateway never needs
    this path.

    The file sits in a ``policy/`` subdirectory of the channel's directory, a
    sibling of the gateway-written ``messages/``. That folder split is the
    single-writer-per-file boundary: Ara owns ``policy/``, the gateway owns
    ``messages/``, and both live in the one per-gateway volume qlever indexes
    read-only. The gateway's default ``INBOUND_POLICY_PATH`` mirrors this
    (``<store>/policy/policy.nt``), so writer and reader agree with no config.
    """
    base = os.environ.get("TRIAGE_MESSENGER_DIR")
    root = Path(base) if base else (CHAMBERS_DIR / "_generated" / "messenger")
    return root / channel / "policy" / "policy.nt"


# --------------------------------------------------------------------------- #
# N-Triples I/O                                                               #
# --------------------------------------------------------------------------- #

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


def _triple(subject: str, predicate: str, literal: str) -> str:
    return f"<{subject}> <{predicate}> {_nt_string(literal)} ."


_TRIPLE_RE = re.compile(r'^<([^>]+)>\s+<([^>]+)>\s+"(.*)"\s*\.\s*$')


def _unescape(value: str) -> str:
    out = []
    i = 0
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append(
                {"\\": "\\", '"': '"', "n": "\n", "r": "\r", "t": "\t"}.get(nxt, nxt)
            )
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _parse(path: Path, *, strict: bool = False) -> list[tuple[str, str, str]]:
    """Parse our own deterministic N-Triples into (subject, predicate, literal).

    A file that cannot be read is an empty policy — unless ``strict``, where
    only a file that is not there is: one that is there and will not open
    then raises, for a caller that has to tell "no policy" from "no answer".
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError:
        if strict:
            raise
        return []
    triples: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        m = _TRIPLE_RE.match(line.strip())
        if m:
            triples.append((m.group(1), m.group(2), _unescape(m.group(3))))
    return triples


@contextlib.contextmanager
def policy_lock(path: Path):
    """Hold an exclusive lock for a read-modify-write of one policy file.

    Advisory, on a ``<file>.lock`` sibling, so the gateways — readers only —
    are never blocked, and a write stays one atomic rename they may read at
    any moment."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def write_if_changed(content: str, path: Path) -> bool:
    """Atomically write only when bytes differ. Returns True if it wrote."""
    data = content.encode("utf-8")
    try:
        if path.read_bytes() == data:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return True


# --------------------------------------------------------------------------- #
# E-mail policy: the group flags                                             #
# --------------------------------------------------------------------------- #

def load_email_policy(path: Path | None = None) -> EmailPolicy:
    """Return the whole e-mail policy: one (exact, wildcard) pair per group
    flag. Triples of the retired whitelist that a file still holds are not
    read."""
    path = path or _email_policy_read_path()
    buckets: dict[str, set[str]] = {key: set() for key in EmailPolicy._fields}
    by_predicate = {
        P_NEWS_ADDRESS: "news",
        P_NEWS_WILDCARD: "news_wildcards",
        P_QUIETED_GROUP: "quieted",
        P_QUIETED_WILDCARD: "quieted_wildcards",
        P_IGNORED_GROUP: "ignored",
        P_IGNORED_WILDCARD: "ignored_wildcards",
        # A policy file written before the ignored/quieted split spelled its
        # one "blocked" flag this way; read it as `ignored` so it keeps its
        # meaning.
        P_BLOCKED_GROUP: "ignored",
    }
    for _subj, pred, lit in _parse(path):
        low = lit.strip().translate(_ASCII_LOWER)
        key = by_predicate.get(pred)
        if low and key:
            buckets[key].add(low)
    return EmailPolicy(**buckets)


def render_email_policy(pol: EmailPolicy) -> str:
    pairs = (
        (P_NEWS_ADDRESS, pol.news),
        (P_NEWS_WILDCARD, pol.news_wildcards),
        (P_QUIETED_GROUP, pol.quieted),
        (P_QUIETED_WILDCARD, pol.quieted_wildcards),
        (P_IGNORED_GROUP, pol.ignored),
        (P_IGNORED_WILDCARD, pol.ignored_wildcards),
    )
    lines = [_triple(EMAIL_SUBJECT, pred, v) for pred, values in pairs for v in values]
    lines.sort()
    return "".join(line + "\n" for line in lines)


def save_email_policy(pol: EmailPolicy, path: Path | None = None) -> bool:
    """Write the whole policy, write-if-changed. The only supported writer."""
    return write_if_changed(render_email_policy(pol), path or email_whitelist_path())


def _domain_matches_wildcard(domain: str, wildcard: str) -> bool:
    """Match a domain against a `*@domain` or `*@*.domain` wildcard."""
    try:
        pattern = wildcard.split("@", 1)[1]
    except IndexError:
        return False
    if pattern.startswith("*."):
        base = pattern[2:]
        # A subdomain wildcard also covers the apex, so `*@*.epfl.ch` trusts both
        # alice@epfl.ch and bob@cs.epfl.ch — the friendlier reading, and the
        # one contacts.py gives a person's `*.domain` (its _domain_rank): the
        # two must agree on who a wildcard covers.
        return domain == base or domain.endswith("." + base)
    return domain == pattern


def email_news_sender(addr: str, news: set[str], news_wildcards: set[str]) -> bool:
    """True if `addr` is a declared news sender (exact entry or wildcard).

    The address-keyed special case of :func:`email_group_member`, kept because
    that is all a caller holding only a sender address needs.
    """
    low = (addr or "").strip().translate(_ASCII_LOWER)
    if not low or "@" not in low:
        return False
    return email_group_member(low, news, news_wildcards)


# RFC 2919 puts the identifier in angle brackets; everything before them is a
# human-readable label. `Retinue-OS/retinue <retinue.Retinue-OS.github.com>` and
# a bare `<sgcarney.substack.com>` both yield the bracketed token.
_LIST_ID_BRACKETED = re.compile(r"<([^<>]+)>\s*$")


def email_list_id(raw: str) -> str:
    """Normalise a ``List-Id`` header to a usable group id, or "" if unusable.

    Real headers are messier than the RFC: alongside well-formed ids we see
    opaque hashes (Google) and outright malformed values whose bracketed part is
    a display name with a space in it (`799706515 <Brack News>`). An id has to
    look like a domain — no whitespace, at least one dot — to be worth keying
    policy on; anything else is rejected here so the caller falls back to the
    sender address rather than inventing a group nobody can flag.
    """
    value = (raw or "").strip()
    if not value:
        return ""
    match = _LIST_ID_BRACKETED.search(value)
    candidate = (match.group(1) if match else value).strip().translate(_ASCII_LOWER)
    if not candidate or len(candidate) > 200:
        return ""
    if any(c.isspace() for c in candidate) or "." not in candidate:
        return ""
    return candidate


def email_group_id(list_id: str, sender: str) -> str:
    """The group a message belongs to: its mailing list, else its own sender.

    Making a listless sender its own group is what keeps the two axes uniform —
    every mail has exactly one group, so ``news``/``quieted``/``ignored`` can be
    stated about a newsletter that carries no ``List-Id`` just as about a real
    list, with no second mechanism.
    """
    return email_list_id(list_id) or (sender or "").strip().translate(_ASCII_LOWER)


def email_group_member(group: str, entries: set[str], wildcards: set[str]) -> bool:
    """True if `group` is in a class, by exact entry or by domain wildcard.

    For an address-shaped group the wildcard is matched against the part
    after the `@` (:func:`_domain_matches_wildcard`). For a `List-Id` group the id
    *is* the domain, and a wildcard also covers ids **beneath** it: a List-Id is
    a namespace a platform hands out per publication, so `*@substack.com` reads
    as "that platform" and has to catch `sgcarney.substack.com` too, without
    thereby trusting a mailbox at that subdomain.
    """
    low = (group or "").strip().translate(_ASCII_LOWER)
    if not low:
        return False
    if low in entries:
        return True
    if "@" in low:
        domain = low.split("@", 1)[1]
        return any(_domain_matches_wildcard(domain, w) for w in wildcards)
    return any(
        _domain_matches_wildcard(low, w) or low.endswith("." + w.split("@", 1)[-1])
        for w in wildcards
    )


def email_gate_decision(
    sender: str,
    list_id: str = "",
    *,
    pol: EmailPolicy | None = None,
    path: Path | None = None,
) -> dict:
    """Place one inbound e-mail on the **group axis** of the policy.

    The sender axis — VIP, instructed — is :func:`gate_decision` on the
    ``email`` channel; the mail gateway asks both (docs/mail-chats.md, "The
    gate"). Returns a dict with:

    - ``group`` — the group id the mail was keyed on: its ``List-Id``, else
      its sender address.
    - ``news`` — the group is a news source: file the mail into the feed for
      the Herald. Independent of the two below.
    - ``ignored`` / ``quieted`` — the group's attention flag: the chat shows
      the mail silently. At most one is true; a group a file puts in both
      classes (an exact entry in one, a wildcard in the other) reads as
      ``ignored``, the order this function has always decided in.

    Nothing here says whether the mail is worth a model turn: that is the
    sender's doing alone, whatever group the mail arrived on.
    """
    pol = pol if pol is not None else load_email_policy(path)
    addr = (sender or "").strip().translate(_ASCII_LOWER)
    group = email_group_id(list_id, addr)
    # News is checked against the group *and* the bare sender address so an
    # address-level entry written before list detection keeps matching mail that
    # turns out to carry a List-Id.
    news = (email_group_member(group, pol.news, pol.news_wildcards)
            or email_news_sender(addr, pol.news, pol.news_wildcards))

    ignored = email_group_member(group, pol.ignored, pol.ignored_wildcards)
    quieted = not ignored and email_group_member(group, pol.quieted, pol.quieted_wildcards)
    return {"group": group, "news": news, "quieted": quieted, "ignored": ignored}


# --------------------------------------------------------------------------- #
# Messenger policy                                                            #
# --------------------------------------------------------------------------- #

_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _norm_handle(handle: str) -> str:
    # Phone numbers/JIDs are case-insensitive digits; usernames case-insensitive.
    # Only ASCII letters are folded: a full Unicode lowercasing would turn a
    # sender's KELVIN SIGN (U+212A) into "k", so that a mail from "<U+212A>ate@…"
    # matched the VIP kate@… — a class is never reached by a look-alike.
    return (handle or "").strip().translate(_ASCII_LOWER)


# A mail sender a domain wildcard may be matched against: one "@", and nothing
# an address cannot hold — the shape contacts.py files an address in, so the
# address book finds the person behind every sender the gate matched by domain.
_EMAIL_SENDER_RE = re.compile(r'^[^@\s<>"]+@([^@\s<>"]+)$')


def _sender_member(channel: str, sender: str, handles) -> bool:
    """Whether a sender is in a sender class (``handles``) of a channel.

    Exact, case-insensitive membership — and on the ``email`` channel also by
    domain: a member spelled `*@domain` / `*@*.domain` covers every address
    under it (:func:`_domain_matches_wildcard`). Only there: a messenger
    handle is opaque, and the same string in a messenger's policy is a handle
    like any other, which no sender has.
    """
    low = _norm_handle(sender)
    if not low:
        return False
    if low in handles:
        return True
    if channel != EMAIL_CHANNEL:
        return False
    match = _EMAIL_SENDER_RE.match(low)
    if not match:
        return False
    domain = match.group(1)
    return any(h.startswith("*@") and _domain_matches_wildcard(domain, h) for h in handles)


def load_messenger_policy(
    channel: str, path: Path | None = None, *, strict: bool = False
) -> MessengerPolicy:
    """Return a :class:`MessengerPolicy` for one channel.

    The legacy ``triageBlockedGroup`` predicate is folded into ``ignored`` so a
    policy file written before the news/quieted/ignored split keeps its old
    behaviour (blocked == never reaches triage). ``strict`` raises for a file
    that exists and cannot be read (:func:`_parse`).
    """
    path = path or messenger_policy_path(channel)
    ignored: set[str] = set()
    quieted: set[str] = set()
    news: set[str] = set()
    vip: set[str] = set()
    vip_contacts: set[str] = set()
    instructed: set[str] = set()
    for subj, pred, lit in _parse(path, strict=strict):
        val = lit.strip()
        if not val:
            continue
        if pred == P_VIP_HANDLE:
            (vip_contacts if subj.endswith(CONTACTS_SUFFIX) else vip).add(_norm_handle(val))
        elif pred == P_INSTRUCTED_HANDLE:
            instructed.add(_norm_handle(val))
        elif pred in (P_IGNORED_GROUP, P_BLOCKED_GROUP):
            ignored.add(val)
        elif pred == P_QUIETED_GROUP:
            quieted.add(val)
        elif pred == P_NEWS_GROUP:
            news.add(val)
    return MessengerPolicy(ignored, quieted, news, vip, vip_contacts, instructed)


def render_messenger_policy(channel: str, pol: MessengerPolicy) -> str:
    subj = _channel_subject(channel)
    lines = [_triple(subj, P_VIP_HANDLE, h) for h in pol.vip]
    lines += [_triple(subj, P_IGNORED_GROUP, g) for g in pol.ignored]
    lines += [_triple(subj, P_QUIETED_GROUP, g) for g in pol.quieted]
    lines += [_triple(subj, P_NEWS_GROUP, g) for g in pol.news]
    lines += [_triple(subj + CONTACTS_SUFFIX, P_VIP_HANDLE, h) for h in pol.vip_contacts]
    lines += [_triple(subj + CONTACTS_SUFFIX, P_INSTRUCTED_HANDLE, h)
              for h in pol.instructed_contacts]
    lines.sort()
    return "".join(line + "\n" for line in lines)


def gate_decision(
    channel: str,
    sender: str,
    group_id: str | None = None,
    *,
    path: Path | None = None,
    enabled: bool = True,
) -> dict:
    """Route one inbound messenger message against the policy.

    This is the single source of truth for the delivery-gate routing table (see
    docs/triage-delivery-gate.md); all three gateways call it so they can never
    drift. Returns a dict with:

    - ``forward`` — worth the user's attention: whether the arrival notifies, or
      updates the chat silently. Read off the **group** alone now, since the
      sender-level whitelist and blacklist are retired — a person one does not
      want to hear from is a chat one mutes, which the user does in the
      interface. On the gateways' fallback path — the rail refusing — `forward`
      still also decides the triage forward, as it always did.
    - ``delivered_if_held`` — the ``delivered`` flag to persist when NOT
      forwarding *and* the chat did not take the message either: ``True`` means
      "accounted for, never drained" (ignored group), ``False`` means "held,
      a recovery sweep picks it up" (quieted group). On the normal path the
      chat's acceptance sets the flag and this is unused.
    - ``news`` — the message's group is a news source: forward it to the news
      feed (Herald) too. This rail is *independent* of the triage decision above
      (a message can be both, either, or neither).
    - ``vip`` — this **sender** is one the user wants worked by a model the
      moment they write: read in context, filed to a project, remembered where
      it matters, and answered with a staged draft. Sender-only and
      group-independent by design — a VIP writing in a group of forty is still
      the person the user wanted to hear from — and independent of every other
      flag here, which govern attention, not handling.
    - ``instructed`` — this **sender** carries the user's standing instruction
      (a person in the address book with ``kb:instruction``): each message of
      theirs is worked by one model turn with that instruction as its brief,
      and for a non-VIP the turn's verdict is what decides whether it rings.
      Sender-only and independent of ``vip``; a handle carrying both rings at
      once, as a VIP, and is worked as instructed in the same single turn.
    - ``reason`` — a short label for the gateway log.

    Attention rail (news and vip are orthogonal — news is driven only by
    group ∈ news, vip only by sender ∈ vip):

    | class          | forward | held-flag |
    |----------------|---------|-----------|
    | quieted group  | no      | false     |
    | ignored group  | no      | true      |
    | anything else  | yes     | —         |

    One chat's messages can be quieted without muting the chat and the other way
    round; the group flags are the policy's way of saying it for a room, `muted`
    is the user's way of saying it for a chat, and both end at the same place —
    the arrival updates the mirror without interrupting anybody.

    ``enabled=False`` forwards everything (the gate turned off). May raise if the
    policy file is present but unreadable; the caller decides fail-open.

    **Mail** (``channel="email"``) is gated on the sender axis here and on the
    group axis by :func:`email_gate_decision`, whose flags live in a file of
    their own; the caller reads ``vip`` and ``instructed`` off this answer.
    A sender matches an exact address, then a domain wildcard
    (:func:`_sender_member`). And an unreadable policy does not raise there:
    the answer is nobody special — ``vip`` and ``instructed`` false, reason
    ``policy-unreadable`` for the caller to log. A messenger gateway fails
    open because a message it cannot classify must still reach the user,
    which costs one turn; a mail reaches the chat either way, and must not
    buy a model turn because a file could not be read.
    """
    if not enabled:
        # The gate off has always meant "forward everything", i.e. every message
        # gets a model turn. Since the chat rail reads `vip` and not `forward`
        # to decide that, the translation is that the switch treats everyone as
        # a VIP — otherwise turning the gate off would quietly turn *off* the
        # handling it exists to force on.
        # Not `instructed`, which nobody is for want of a policy to read: as a
        # VIP everyone already gets the turn, and the web gateway reads a
        # VIP's instruction off the address book itself.
        return {"forward": True, "delivered_if_held": True, "news": False,
                "vip": True, "instructed": False, "reason": "gate-disabled"}
    try:
        pol = load_messenger_policy(channel, path=path, strict=channel == EMAIL_CHANNEL)
    except Exception:  # noqa: BLE001 - whatever made the file unreadable
        if channel != EMAIL_CHANNEL:
            raise
        return {"forward": True, "delivered_if_held": True, "news": False,
                "vip": False, "instructed": False, "reason": "policy-unreadable"}
    grp = group_id.strip() if group_id else None
    news = bool(grp and grp in pol.news)
    vip = _sender_member(channel, sender, set(pol.vip) | set(pol.vip_contacts))
    instructed = _sender_member(channel, sender, pol.instructed_contacts)

    if grp and grp in pol.ignored:
        dec = {"forward": False, "delivered_if_held": True,
               "reason": "group-ignored"}
    elif grp and grp in pol.quieted:
        dec = {"forward": False, "delivered_if_held": False,
               "reason": "group-quieted"}
    else:
        dec = {"forward": True, "delivered_if_held": True, "reason": "open"}

    dec["news"] = news
    dec["vip"] = vip
    dec["instructed"] = instructed
    return dec


def _ensure_before_write() -> None:
    """Place the e-mail policy file before a retinue-side write of it.

    Outside the lock the write then takes (flock does not nest across two
    opens, even in one process). Best effort: the write reports its own
    failure."""
    try:
        ensure_email_policy_location()
    except OSError:
        pass


def _mutate_email(**kwargs) -> None:
    _ensure_before_write()
    with policy_lock(email_whitelist_path()):
        _mutate_email_locked(**kwargs)


def _mutate_email_locked(*, news_add=(), news_remove=(),
                         quiet_add=(), quiet_remove=(),
                         ignore_add=(), ignore_remove=()) -> None:
    """Add/remove e-mail group flags, preserving every class not touched.

    Every argument sorts each entry by shape — a leading `*@` makes it a
    wildcard, anything else an exact entry (an address or a `List-Id`) — so the
    caller passes what the user said ("*@substack.com", "sgcarney.substack.com")
    without picking a subcommand per shape. Keyword-only: the three leading
    positional arguments were the whitelist's mutations, and a caller still
    passing them must fail loudly rather than flag a group.
    """
    pol = load_email_policy()
    sets = {name: set(getattr(pol, name)) for name in EmailPolicy._fields}

    def _add(entries, exact_key, wildcard_key):
        for entry in entries:
            e = entry.strip().translate(_ASCII_LOWER)
            if e:
                sets[wildcard_key if e.startswith("*@") else exact_key].add(e)

    def _drop(entries, *keys):
        for entry in entries:
            e = entry.strip().translate(_ASCII_LOWER)
            for key in keys:
                sets[key].discard(e)

    _add(news_add, "news", "news_wildcards")
    _add(quiet_add, "quieted", "quieted_wildcards")
    _add(ignore_add, "ignored", "ignored_wildcards")
    _drop(news_remove, "news", "news_wildcards")
    _drop(quiet_remove, "quieted", "quieted_wildcards")
    _drop(ignore_remove, "ignored", "ignored_wildcards")
    # A group carries at most one of quieted/ignored at a time: adding one clears
    # the other, so `email-quiet-add` on an ignored group moves it rather than
    # leaving a contradiction for the decision order to resolve silently.
    _drop(quiet_add, "ignored", "ignored_wildcards")
    _drop(ignore_add, "quieted", "quieted_wildcards")
    save_email_policy(EmailPolicy(**sets))


def _mutate_messenger(channel, **kwargs) -> None:
    with policy_lock(messenger_policy_path(channel)):
        _mutate_messenger_locked(channel, **kwargs)


def _mutate_messenger_locked(channel, *, ig_add=(), ig_del=(), q_add=(), q_del=(),
                             news_add=(), news_del=(), vip_add=(), vip_del=()) -> None:
    pol = load_messenger_policy(channel)
    ignored = set(pol.ignored)
    quieted = set(pol.quieted)
    news = set(pol.news)
    vip = set(pol.vip)
    vip |= {_norm_handle(h) for h in vip_add if h.strip()}
    vip -= {_norm_handle(h) for h in vip_del}
    ignored |= {g.strip() for g in ig_add if g.strip()}
    ignored -= {g.strip() for g in ig_del}
    quieted |= {g.strip() for g in q_add if g.strip()}
    quieted -= {g.strip() for g in q_del}
    news |= {g.strip() for g in news_add if g.strip()}
    news -= {g.strip() for g in news_del}
    # A group carries at most one of ignored/quieted at a time: adding one clears
    # the other, so `quiet-add` on an ignored group moves it rather than doubling.
    ignored -= {g.strip() for g in q_add if g.strip()}
    quieted -= {g.strip() for g in ig_add if g.strip()}
    write_if_changed(
        render_messenger_policy(
            channel, MessengerPolicy(ignored, quieted, news, vip, pol.vip_contacts,
                                     pol.instructed_contacts)
        ),
        messenger_policy_path(channel),
    )


def messenger_policy_channels() -> list[str]:
    """Every channel with a policy directory on this side — the gateways'
    mounted volumes, plus any channel a policy was already written for."""
    base = os.environ.get("TRIAGE_MESSENGER_DIR")
    root = Path(base) if base else (CHAMBERS_DIR / "_generated" / "messenger")
    try:
        return sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return []


def sync_contacts(vip_handles: dict[str, set[str]], *,
                  instructed_handles: dict[str, set[str]] | None = None) -> list[str]:
    """Project the address book's VIP persons into the gate: ``vip_handles``
    maps a channel to the handles of every VIP person on it, and
    ``instructed_handles`` maps a channel to the handles of every person who
    carries an instruction (None: nobody does). ``email`` is a channel like
    the others — its handles are addresses and `*@domain` wildcards, written
    to the mail gateway's policy file in the messenger shape. Replaces the
    contact-owned members wholesale — hand-set VIPs stay as they are — and
    writes a file only when its bytes change. A channel with no policy
    directory yet is written only when it has members and is one the
    framework runs a gateway for (MESSENGER_CHANNELS, and ``email``).
    Returns the files written.

    ``instructed_handles`` is keyword-only: the second positional argument
    used to be the e-mail addresses projected into the whitelist, and a
    caller still passing them must fail loudly rather than instruct them."""
    instructed_handles = instructed_handles or {}
    written = []
    # The file of the e-mail *group* flags sits in the same folder as the
    # `email` channel's policy, and the mail gateway waits for it before its
    # first backfill: this is one of the retinue-side writers that puts it
    # there (and moves an old one), though it no longer writes into it.
    _ensure_before_write()
    # A channel no gateway reads (a person's Matrix account, say) gets no
    # policy directory of its own: only the ones already there, and the
    # services the framework runs gateways for.
    channels = set(messenger_policy_channels()) | (
        {c for table in (vip_handles, instructed_handles) for c, hs in table.items() if hs}
        & set(MESSENGER_CHANNELS + (EMAIL_CHANNEL,)))
    for channel in sorted(channels):
        want = {_norm_handle(h) for h in vip_handles.get(channel, ()) if h.strip()}
        briefed = {_norm_handle(h) for h in instructed_handles.get(channel, ()) if h.strip()}
        path = messenger_policy_path(channel)
        with policy_lock(path):
            pol = load_messenger_policy(channel, path=path)
            if (set(pol.vip_contacts), set(pol.instructed_contacts)) != (want, briefed) and write_if_changed(
                    render_messenger_policy(channel, pol._replace(vip_contacts=want,
                                                                  instructed_contacts=briefed)), path):
                written.append(str(path))
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Triage delivery-gate policy editor")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("show-email")
    # Only the placement every command does first (below), and its result on
    # stdout; the entrypoint runs it before anything slow at start.
    sub.add_parser("place-email-policy")
    # The e-mail commands edit the group flags only. A sender whose mail is to
    # be worked is a contact with an instruction or the VIP flag
    # (contacts.py update <id> --instruction … / --vip; --add-domain for a
    # whole domain): the address book projects it, nothing is set here.
    p = sub.add_parser("email-news-add"); p.add_argument("entry", nargs="+")
    p = sub.add_parser("email-news-remove"); p.add_argument("entry", nargs="+")
    p = sub.add_parser("email-quiet-add"); p.add_argument("entry", nargs="+")
    p = sub.add_parser("email-quiet-remove"); p.add_argument("entry", nargs="+")
    p = sub.add_parser("email-ignore-add"); p.add_argument("entry", nargs="+")
    p = sub.add_parser("email-ignore-remove"); p.add_argument("entry", nargs="+")
    p = sub.add_parser("check-email")
    p.add_argument("address")
    p.add_argument("--list-id", default="",
                   help="the message's List-Id header, if it carries one")

    # Handle commands take --handle; group commands take --group. `groupblock-*`
    # is kept as a legacy alias of `ignore-*`.
    handle_cmds = ("vip-add", "vip-remove")
    group_cmds = ("ignore-add", "ignore-remove", "quiet-add", "quiet-remove",
                  "news-add", "news-remove", "groupblock-add", "groupblock-remove")
    for name in ("show",) + handle_cmds + group_cmds:
        p = sub.add_parser(name)
        p.add_argument("--channel", required=True)
        if name in handle_cmds:
            p.add_argument("--handle", action="append", default=[])
        if name in group_cmds:
            p.add_argument("--group", action="append", default=[])
    p = sub.add_parser("check-handle")
    p.add_argument("--channel", required=True)
    p.add_argument("handle")

    args = parser.parse_args(argv)

    # This CLI runs on the retinue side only, so it is one of the places that
    # puts the e-mail policy file where the mail gateway reads it. A failure
    # is reported and the command still runs: reading or editing a messenger
    # policy does not depend on it, and an e-mail edit fails on its own write.
    try:
        placed = ensure_email_policy_location()
    except OSError as exc:
        print(f"triage_policy: could not place the e-mail policy file: {exc}", file=sys.stderr)
        placed = None
    if placed is not None:
        note = (f"left the old file at {placed['legacy_left']} (it differs; the new "
                f"one is used)" if placed["legacy_left"] else placed["action"])
        if args.cmd == "place-email-policy":
            print(f"{placed['path']}: {note}")
        elif placed["action"] != "present" or placed["legacy_left"]:
            print(f"triage_policy: e-mail policy {placed['path']}: {note}", file=sys.stderr)

    if args.cmd == "place-email-policy":
        return 0 if placed is not None else 1
    elif args.cmd == "show-email":
        # The group flags; the sender classes of mail are `show --channel email`.
        pol = load_email_policy()
        for a in sorted(pol.news | pol.news_wildcards):
            print(f"news\t{a}")
        for a in sorted(pol.quieted | pol.quieted_wildcards):
            print(f"quiet\t{a}")
        for a in sorted(pol.ignored | pol.ignored_wildcards):
            print(f"ignore\t{a}")
    elif args.cmd == "email-news-add":
        _mutate_email(news_add=args.entry)
    elif args.cmd == "email-news-remove":
        _mutate_email(news_remove=args.entry)
    elif args.cmd == "email-quiet-add":
        _mutate_email(quiet_add=args.entry)
    elif args.cmd == "email-quiet-remove":
        _mutate_email(quiet_remove=args.entry)
    elif args.cmd == "email-ignore-add":
        _mutate_email(ignore_add=args.entry)
    elif args.cmd == "email-ignore-remove":
        _mutate_email(ignore_remove=args.entry)
    elif args.cmd == "check-email":
        dec = email_gate_decision(args.address, args.list_id)
        sender = gate_decision(EMAIL_CHANNEL, args.address)
        # One line per question, so the axes stay visibly independent: the
        # group and its flags, then who the sender is to the gate.
        print(f"group\t{dec['group']}")
        print(f"news\t{'yes' if dec['news'] else 'no'}")
        print(f"attention\t{'ignored' if dec['ignored'] else 'quieted' if dec['quieted'] else 'open'}")
        classes = [name for name in ("vip", "instructed") if sender[name]]
        print(f"sender\t{'+'.join(classes) or 'neither'}"
              + ("\t(the policy file could not be read)" if sender["reason"] == "policy-unreadable" else ""))
        # As check-handle answers: 0 for a sender whose mail buys a turn.
        return 0 if classes else 3
    elif args.cmd == "show":
        pol = load_messenger_policy(args.channel)
        for h in sorted(pol.vip):
            print(f"vip\t{h}")
        for h in sorted(set(pol.vip_contacts) - set(pol.vip)):
            print(f"vip\t{h}\t(address book)")
        for h in sorted(pol.instructed_contacts):
            print(f"instructed\t{h}\t(address book)")
        for g in sorted(pol.ignored):
            print(f"ignore\t{g}")
        for g in sorted(pol.quieted):
            print(f"quiet\t{g}")
        for g in sorted(pol.news):
            print(f"news\t{g}")
    elif args.cmd == "vip-add":
        _mutate_messenger(args.channel, vip_add=args.handle)
    elif args.cmd == "vip-remove":
        _mutate_messenger(args.channel, vip_del=args.handle)
    elif args.cmd in ("ignore-add", "groupblock-add"):
        _mutate_messenger(args.channel, ig_add=args.group)
    elif args.cmd in ("ignore-remove", "groupblock-remove"):
        _mutate_messenger(args.channel, ig_del=args.group)
    elif args.cmd == "quiet-add":
        _mutate_messenger(args.channel, q_add=args.group)
    elif args.cmd == "quiet-remove":
        _mutate_messenger(args.channel, q_del=args.group)
    elif args.cmd == "news-add":
        _mutate_messenger(args.channel, news_add=args.group)
    elif args.cmd == "news-remove":
        _mutate_messenger(args.channel, news_del=args.group)
    elif args.cmd == "check-handle":
        # One question left about a sender: does a model work their messages?
        # Yes for a VIP and for an instructed sender alike (exit 0), as
        # check-email answers; the lines say which.
        if args.channel == EMAIL_CHANNEL:
            # Mail reads the policy as its gate does: unreadable is nobody special.
            sender = gate_decision(EMAIL_CHANNEL, args.handle)
            vip, instructed = sender["vip"], sender["instructed"]
        else:
            pol = load_messenger_policy(args.channel)
            vip = _sender_member(args.channel, args.handle, set(pol.vip) | set(pol.vip_contacts))
            instructed = _sender_member(args.channel, args.handle, pol.instructed_contacts)
        print("vip" if vip else "not-vip")
        # The second question: is there a brief the turn works their messages by?
        if instructed:
            print("instructed")
        if args.channel == EMAIL_CHANNEL and sender["reason"] == "policy-unreadable":
            print("(the policy file could not be read)")
        return 0 if (vip or instructed) else 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
