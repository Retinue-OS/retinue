#!/usr/bin/env python3
"""In-container mail gateway — a mailbox mirrored into the message ledger.

E-mail gets the surface the messengers have (docs/mail-chats.md): a mail
thread is a chat, and this process is the channel gateway behind it — a
sibling of sms-gateway.py, one account per container, and the **single writer
of the mail ledger** on the ``messenger-email`` volume. It holds the mailbox
credentials (the variables ``email_client.Config`` reads, ``MAIL_ACCOUNT_NAME``
selecting a named account's suffixed set) and uses ``email_client.py`` as a
library.

    mailbox (IMAP) ──poll──▶ this gateway ──records, blobs──▶ the ledger
                                          └─events──▶ the web gateway (rail)

**This is the read-only mirror.** Folders are selected read-only and no flag
is ever stored; nothing is sent, nothing pushes, no model turn is started for
a mail, and e-mail triage keeps running beside it untouched. Writing (reply,
compose, the mailbox following the chat), the gate and the model trim are
later phases; where one of them will take over, the code says so.

**The mailbox is the source of what exists; the mirror follows it.** Three
folders are mirrored — INBOX, Sent and the archive folder:

  * a new mail in INBOX becomes a ``kb:InboundMessage``, a new mail in Sent a
    ``kb:OutboundMessage`` (the own-device echo, author ``device``, unless an
    outbound note names its author) — whatever address it is From: a copy in
    Sent is the user's own unless it says itself it was sent on (a redirect,
    ``Resent-`` fields) under someone else's From;
  * the archive folder is only *followed*: a mail that leaves INBOX is looked
    for there and is still that mail. Nothing else is read from it — but for
    the mail that came and went between two polls (read and archived on the
    phone within the minute): INBOX shows that a UID was handed out and is
    gone, and then what arrived in the archive since is read too;
  * there is ONE record per (account, Message-ID), however many folder copies
    exist — a mail to oneself, the list's copy of one's own post;
  * a mail that is in none of the three any more leaves a tombstone — once
    every folder has *answered* that it does not hold it. A folder the server
    would not open, a search it refused, is no answer: nothing is called gone
    on it, and the pass is run again — though an archive folder that keeps
    refusing holds back only what needs its answer: INBOX and Sent are still
    read, new mail and old, and /health says what waits;
  * a client's ``\\Seen`` changes are ignored. Only the backfill reads the
    flag, once, so a mailbox of ninety days does not arrive as ninety days of
    unread chats.

**A mail is text from outside**, and what one mail can do to the mirror is
bounded: its header block and the fields whose parameters are parsed, the
codecs it is decoded under (charsets, and nothing else its sender may name),
the ids it cites, its parts, the text its record carries, the chats it may
merge, the work its parsing costs — and the time its reading takes as a
whole, the bound for what none of the others foresaw: the running gateway
reads its mail in a process of its own (``Reader``), which is ended when a
mail outlasts its deadline. A mail the reader cannot make sense of, or not
in its time, does not stop the folder and does not vanish either: it is
mirrored as a placeholder that says a mail from this sender, at this time,
could not be shown (the mailbox still has it), and is read again at start
and every hour. ``/health`` counts them, and says why. What the bookkeeping
of all this must keep, whatever case comes next, is written down in one
place: the four invariants above ``class Mirror``.

**Whose fault a failure is, is decided by what failed** (``read_mail``,
``Mirror._ingest``). READING a mail — the parse, the walk of its parts, the
trim — fails on the mail's account when the same reader still reads the
mirror's own built-in test mails (the canaries, one for every path a mail
takes through it), and in their time: that mail is a placeholder at once,
and the mirror is as healthy as before. WRITING — the ledger, the indexes,
the blobs — never fails on a mail's account. A reader that cannot read the
canaries either, or is too slow for them, and any failure in writing, is a
fault of the mirror itself, and must not be absorbed mail by mail: the pass
ends, no mail is set aside over it, no mark moves past a mail that has
neither a record nor a place — so the mail is read again wherever it is by
then, also after a restart — and ``/health`` reports the mirror, not the
mailbox, as at fault. Such a fault delays only the work it was in: new mail
is followed at every poll unless following it is what fails.

How the poll stays cheap: a **mailbox index** (sqlite, on the state volume)
maps folder / UID ↔ Message-ID ↔ ledger record. New mail is the UIDs above a
folder's high-water mark; vanished mail is the difference between the indexed
UIDs and a UID SEARCH; a folder whose UIDVALIDITY changed has its mails found
again by Message-ID instead of being ingested a second time. The index is a
cache of the ledger and the mailbox: with the state volume lost it is rebuilt
from the ledger's records, and each mail's place is found again by Message-ID.

Nothing is reduced to text: the plain body, the HTML body and the header block
are kept as blobs of their own, every other part as an attachment under the
type and name its sender declared, and ``kb:text`` is the trimmed reading text
derived from them (``mail_trim``).

What it serves (behind ``MAIL_GATEWAY_TOKEN``, ``/health`` aside — and
without a token it neither mirrors nor serves: this container reads a real
mailbox from credentials the deployment already has, so "no token set" must
not mean "open"): ``GET /health`` for the gateway monitor, ``GET /media/<id>``
for the blobs,
``GET /recent-chats`` and ``GET /threads/<chat key>`` for a thread's name,
participants and counts, and ``POST /outbound-note`` so a mail an agent or the
user sent through ``email_client.py`` is not recorded as "some mail client".
The endpoints of the later phases (``/send``, ``/pending-sends``,
``/chats/delete|read|archive``) answer 501 with the reason.

What it tells the web gateway, over the chats rail: every mirrored mail
(held, no handover — so nothing rings and no turn starts), a thread merge, and
a removal. Events wait in a queue on the state volume while the web gateway
is away; the ledger is the record either way. A mail is recorded exactly
once and the rail is told at least once, wherever the process is stopped: an
index change and the event that announces it are one transaction, and what a
stop between the ledger and the index left undone is found and announced at
the next start (``Mirror._prepare``).

Everything that speaks IMAP is confined to the "mailbox adapter" section; the
rest works on the adapter's small surface and is unit-tested against a fake
mailbox in tests/test_mail_gateway.py (the adapter itself, against a scripted
IMAP server, in tests/test_mail_gateway_imap.py).
"""
import codecs
import email
import email.feedparser
import email.header
import email.message
import email.policy
import email.utils
import hashlib
import hmac
import imaplib
import io
import json
import os
import pickle
import re
import select
import signal
import sqlite3
import ssl
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NamedTuple
from urllib.parse import unquote

import email_client as _ec
import inbound_store as _ibstore
import mail_threads as _threads
import mail_trim as _trim
import triage_policy as _triage
import chat_ingest as _chats
import build_stamp as _build

CHANNEL = "email"
# Mail is the user's own correspondence; there is no control mode to run.
MAIL_GATEWAY_MODE = "inbox"
_MEDIA_REF = f"urn:retinue:media:{CHANNEL}:"


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(os.environ.get(name, "").strip() or default)
    except ValueError:
        print(f"[mail-gateway] warning: {name} is not a number; using {default}", flush=True)
        return default
    return max(minimum, value)


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        value = float(os.environ.get(name, "").strip() or default)
    except ValueError:
        print(f"[mail-gateway] warning: {name} is not a number; using {default}", flush=True)
        return default
    return max(minimum, value)


# ── The account ───────────────────────────────────────────────────────────────
# The mailbox variables are email_client's own (EMAIL_USER, EMAIL_PASS,
# IMAP_HOST, …); a named account reads the _<NAME>-suffixed set and never
# falls back to the unsuffixed one. They live ONLY in this container.
MAIL_ACCOUNT_NAME = os.environ.get("MAIL_ACCOUNT_NAME", "").strip()


def _load_config():
    """``(configured, Config | None, problem | None)`` for this container.

    Not configured means the account's address variable is absent: the
    deployment runs no mail mirror, which is a choice, not a fault. An address
    with the rest missing is a configuration error and is reported as one.

    The default account is also not configured when the deployment pinned its
    built-in channels (MESSENGER_BUILTIN_CHANNELS, read exactly as
    messenger_gateways.py reads it) and left ``email`` out: the retinue side
    then has no registry entry for this gateway — no media, no Original, no
    monitoring — and a mirror nobody can read must not run on credentials
    that happen to be in the environment. A named account is enrolled through
    MESSENGER_GATEWAYS and is not the pin's to switch off."""
    suffix = f"_{MAIL_ACCOUNT_NAME.upper()}" if MAIL_ACCOUNT_NAME else ""
    pinned = os.environ.get("MESSENGER_BUILTIN_CHANNELS")
    if not suffix and pinned is not None and CHANNEL not in {
            name.strip() for name in pinned.split(",")}:
        return False, None, (f"the {CHANNEL} channel is not in MESSENGER_BUILTIN_CHANNELS "
                             f"(add it to mirror this mailbox)")
    if not os.environ.get("EMAIL_USER" + suffix, "").strip():
        return False, None, f"EMAIL_USER{suffix} is not set"
    try:
        return True, _ec.Config(MAIL_ACCOUNT_NAME or None), None
    except (_ec.EmailError, ValueError) as exc:
        return True, None, str(exc)


CONFIGURED, MAIL_CONFIG, CONFIG_PROBLEM = _load_config()
# The account identity: the mailbox address, lowercased — what kb:account, the
# chat id and /health all carry, compared as a string everywhere.
ACCOUNT = MAIL_CONFIG.user.strip().lower() if MAIL_CONFIG else ""
# The user's own addresses: the account, plus MAIL_OWN_ADDRESSES — EVERY
# address of the user's that can appear on this mailbox's mail: its aliases
# and the user's other mirrored accounts alike (a thread that reaches two of
# their mailboxes carries the other one's address, and this container sees no
# other account's variables). A thread's participants are everyone else, the
# person a chat is with is never one of these, and a mail from one of them is
# the user's own wherever a folder copy of it turns up.
OWN_ADDRESSES = frozenset(
    a for a in [ACCOUNT, *(part.strip().lower() for part in
                           os.environ.get("MAIL_OWN_ADDRESSES", "").split(","))] if a)

MAIL_ARCHIVE_FOLDER = os.environ.get("MAIL_ARCHIVE_FOLDER", "").strip() or "Archive"
MAIL_TRASH_FOLDER = os.environ.get("MAIL_TRASH_FOLDER", "").strip() or "Trash"

MAIL_POLL_SECONDS = _env_float("MAIL_POLL_SECONDS", 60.0, 5.0)
MAIL_BACKFILL_DAYS = _env_int("MAIL_BACKFILL_DAYS", 90)
# Backfill is rate-polite: a batch of mails, a pause, the next batch — and the
# live poll keeps its own rhythm in between.
MAIL_BACKFILL_BATCH = _env_int("MAIL_BACKFILL_BATCH", 50, 1)
MAIL_BACKFILL_PAUSE_SECONDS = _env_float("MAIL_BACKFILL_PAUSE_SECONDS", 2.0, 0.1)
MAIL_IMAP_TIMEOUT = _env_float("MAIL_IMAP_TIMEOUT", 60.0, 5.0)

# Size discipline. An attachment above the part cap is not stored; a mail above
# the mail cap is fetched only up to it and keeps its text parts and no
# attachments. The text parts are always kept.
MAIL_MAX_PART_BYTES = _env_int("MAIL_MAX_PART_BYTES", 25 * 1024 * 1024, 1)
MAIL_MAX_MAIL_BYTES = _env_int("MAIL_MAX_MAIL_BYTES", 50 * 1024 * 1024, 64 * 1024)

# Everything else one mail controls is bounded too, by limits a real mail does
# not reach — a mail is its sender's to write, byte for byte, and each of
# these is something one mail could otherwise spend without end.
#
# The header block. A real one is a few kilobytes, thirty with a long chain of
# Received and signature headers; what lies beyond this is not read (the mail
# is parsed with its header block cut at the last whole line before it).
MAX_HEADER_BYTES = 256 * 1024
# One Content-Type or Content-Disposition field, the mail's own or a part's
# (a part's header block lies in the body, where the cap above does not
# reach). These are the two fields whose parameters are parsed — by the
# reader here and by the MIME parser itself, which on Python versions before
# the fix takes time with the square of the field's length: half a minute
# for 200 kB, days for the megabytes a mail may hold. A real one is a few
# hundred bytes, three kilobytes with the longest file name spelled the
# longest way; what lies beyond this is cut off before anything parses it —
# where the parser itself reads a header field (_BoundedPolicy), so a line of
# a body or of an attachment that merely begins like one is never touched.
MAX_FIELD_BYTES = 4096
# Parts kept as attachments. A part costs its sender some thirty bytes and the
# ledger two files and a line in the record, so a mail under the byte caps
# could ask for a million of them. Beyond this many, one note says how many
# more there were.
MAX_PARTS = 200
# Parts the parser may make of one mail. A part costs its sender one boundary
# line, and the parser builds an object per part before anything here can
# count them — so they are counted as it builds them (_parse_bytes): a mail
# that asks for more is not parsed to the end and is mirrored as a
# placeholder. Only what the parser takes for a part counts: the lines of a
# body or an attachment that merely begin with "--" (a diff, an SQL dump, a
# signature rule) are text, however many there are.
MAX_PARSED_PARTS = 50_000
# How deep those parts may nest. A forward of a forward nests two levels a
# time and no client writes fifty; the parser takes a level of its own stack
# for each, and beyond some thousand the interpreter ends it — which is no
# way to learn that a mail is not one. A mail nested deeper is a placeholder.
MAX_NESTING = 200
# The work parsing may cost. The parser reads a mail line by line and tests
# every line against the boundary of every multipart the line lies in, so
# its time is lines × (PARSE_LINE_COST + levels) such tests — reading a line
# at all costs what some four of them do. Measured, a test takes 0.10 µs on
# Python 3.12 and 0.05 µs on 3.13: a mail of 850 kB (900 levels around
# 800,000 empty lines) held the poll for 69 seconds, and one of the size the
# mirror fetches would for an hour. (A line that begins like a long boundary
# costs up to the boundary's length to compare: counted as one more line per
# 64 bytes.) A mail that asks for more than this many tests is a placeholder:
# the worst mail still read, as far as this bound goes, takes under six
# seconds on 3.12 and three on 3.13, flat or at any depth (the slowest mail
# of all is another: see MAIL_READ_SECONDS). No mail a client makes comes
# near — one of the largest size fetched, all of it base64, may still nest
# thirty levels.
MAX_PARSE_WORK = 50_000_000
PARSE_LINE_COST = 4
# The parameters of those two fields. MAX_FIELD_BYTES bounds one field, not
# the sum over the parts a mail may have: the parser's parameter splitter
# counts the quotes before every ";" it meets — once more from the start of
# the field for each on Python 3.12, a step of its own for each on 3.13 — and
# the reader asks it up to three times a part (file name, charset, boundary).
# So a field costs its semicolons times its length, with what a step costs
# at all counted as PARAMETER_STEP_COST characters more, summed as the parser
# takes the fields off the mail: 6,000 parts with two fields of 4,000
# semicolons each (49 MB) held the poll for 131 seconds on 3.12. A mail that
# asks for more than this is a placeholder; the worst still read takes under
# four seconds on either version. Two hundred attachments under the longest
# names, or fifty thousand parts that each name a charset, come to a
# thirtieth of it.
MAX_PARAMETER_WORK = 1_000_000_000
PARAMETER_STEP_COST = 256
# The time reading one mail may take, on the wall clock: the bound for what
# the bounds above do not know of. Each of them closes one way a mail made
# the standard library's mail code spend the square of something its sender
# chose (the parser's boundary tests, its parameter splitter, the re-folding
# of a nested mail's header, the decoder of encoded words), and each was
# found only when a mail of some hundred kilobytes had held the mirror's one
# thread for a minute. The next such path is not known yet, so the reading
# as a whole has a deadline, kept two ways (read_mail, Reader): the reader's
# own code looks at the clock wherever it is entered — for every header
# field and part the parser takes up, every part walked, before the trim —
# and the reading runs in a process of its own, which is ended when a single
# call into the library outlasts the deadline (the looks at the clock alone
# left two of the three paths found last to run their 52 and 139 seconds).
# Either way the mail is a placeholder ("took too long to read"), as for any
# other failure of its own.
#
# A mail gets MAIL_READ_SECONDS, and READ_SECONDS_PER_MB more for each
# megabyte fetched. Measured, on Python 3.12 and 3.13 alike: an ordinary
# mail reads in a millisecond; the trim's own bounds make the heaviest text
# a second, whatever its size (a megabyte of HTML); an attachment costs
# 25 ms a megabyte. The slowest mail the bounds above let through is not
# one of these: well-formed parts by the thousand, each under a file name
# in encoded words, read at some 0.9 s a megabyte (14 s for 16 MB) — which
# is what the second a megabyte is for, and why the flat thirty alone would
# not do. So the deadline is thirty times what the heaviest honest mail
# needs and ten thousand times what an ordinary one does — and it must not
# turn a slow machine into a mailbox of placeholders: after a mail ran out
# of time the canaries are read against the clock too, with CANARY_SECONDS
# for their five milliseconds. That is a far smaller multiple of what they
# need, so on a machine slow enough to time out ordinary mail the canaries
# time out first, and that is the mirror's fault, not a mail's (read_mail):
# nothing is set aside. Only the heaviest mail can run out of time while the
# canaries still read, on a machine thirty times too slow: a placeholder
# each, read again like every placeholder at start and every hour — and
# MAIL_READ_SECONDS is the setting for such a machine.
READ_SECONDS = _env_float("MAIL_READ_SECONDS", 30.0, 1.0)
READ_SECONDS_PER_MB = 1.0
CANARY_SECONDS = READ_SECONDS / 10
# How much longer than its deadline the reader process is waited for before
# it is ended (it answers for itself when its own code sees the clock), and
# how long a new one may take to start.
READER_GRACE_SECONDS = 5.0
READER_START_SECONDS = 60.0
# The text a record carries (kb:text) — one literal on one line, read by
# every reader of the record. The trim leaves a long mail long; the whole of
# it is in the body blob either way, and "Original" shows it.
MAX_RECORD_TEXT_CHARS = 256 * 1024
# A chat key is a Message-ID, and a chat id carries it whole; the web gateway
# caps a chat id's length. An id longer than this is replaced by a digest of
# itself — consistently, wherever it is cited — so its thread still holds.
# (How many ids a mail may cite, and how many chats it may merge, are
# mail_threads' MAX_CITED_IDS and MAX_MERGE_CHATS.)
MAX_ID_CHARS = 240
MAX_RECIPIENTS = 500

# Persistent state of this gateway alone (the two indexes, the outbound
# notes), per account so a volume reused for another mailbox starts clean.
MAIL_DATA_DIR = Path(os.environ.get("MAIL_DATA_DIR", "/root/.local/share/mail"))
MAIL_STATE_DIR = MAIL_DATA_DIR / (re.sub(r"[^a-z0-9._@+-]", "_", ACCOUNT) or "unconfigured")
MAIL_RECENT_CHATS_MAX = _env_int("MAIL_RECENT_CHATS_MAX", 200, 1)
MAIL_NOTES_MAX = 2000

HTTP_PORT = _env_int("MAIL_GATEWAY_HTTP_PORT", 8096, 1)
# Required, and the opt-in. Without it the gateway neither mirrors nor serves:
# nothing generates this token, the mailbox credentials are usually in the
# deployment already, and an API that is open while unset would hand every
# container on the network the user's mail. A gateway without one is
# *unconfigured* (/health says so, and why): an opt-in not taken, which the
# gateway monitor must not report as an outage.
GATEWAY_TOKEN = os.environ.get("MAIL_GATEWAY_TOKEN", "").strip()
TOKEN_PROBLEM = None if GATEWAY_TOKEN else (
    "MAIL_GATEWAY_TOKEN is not set: the mail gateway neither mirrors nor serves "
    "without it — set it in .env (compose passes it to the retinue and the "
    "mail-gateway service)")
MAX_BODY_BYTES = 64 * 1024

INBOUND_STORE_DIR = Path(os.environ.get("INBOUND_STORE_DIR", "/inbound"))
# The mirror waits for the e-mail policy file, for as long as it takes: read
# without the flags, every newsletter becomes a chat and stays one (the whole
# window's, in a first backfill). Unset or 0 is that wait. A positive number
# is the opt-out — a deployment that keeps no policy file proceeds once the
# gateway has been up that many seconds.
MAIL_POLICY_WAIT_SECONDS = _env_float("MAIL_POLICY_WAIT_SECONDS", 0.0)
# Waiting is a notice at first (the retinue container is still starting) and a
# fault after this long, so the gateway monitor says the mirror is not running
# — as what it is: a matter of the deployment's setup, with the mailbox
# reachable all the while. /health names it (``blocked: "policy"``), so that
# nobody is sent to check an app password over it.
POLICY_WAIT_GRACE_SECONDS = 600.0
MAIL_RAIL_TIMEOUT = _env_float("MAIL_RAIL_TIMEOUT", 10.0, 1.0)
# The rail is a freshness hint, the ledger is the record: an event carries at
# most this much of a mail's text, so one enormous mail cannot be refused by
# the web gateway's body limit.
MAIL_RAIL_TEXT_CHARS = 64 * 1024

# How many mails one poll looks for again (after a UIDVALIDITY change, a
# rebuilt index, or because they left their folder) before it yields.
RELOCATE_BATCH = 200
# How many mails whose record is gone from the ledger one poll looks for by
# their Message-ID (Mirror._read_lost): each is a search of three folders
# and a mail read whole, and a ledger restored from an old backup may lose
# thousands at once.
LOST_BATCH = 50
# A mail whose Message-ID the server cannot be searched for is looked for by
# reading the headers of the mails in the folder that have no place yet and
# are not older than it (a copy or a move keeps a mail's arrival time, or
# makes it later; SCAN_WINDOW_SECONDS is the slack on "not older"), newest
# first — a mail just moved somewhere has the highest UID there. At most
# SCAN_BUDGET headers are read per pass; what a pass did not
# get to is "could not tell", never "not there", and the next pass reads on.
# The ids read are remembered (up to SCAN_CACHE_MAX of them, in memory), so
# however many mails are looked for, each header is read once.
SCAN_BUDGET = 1000
SCAN_CACHE_MAX = 100_000
SCAN_WINDOW_SECONDS = 2 * 86400.0
HEADER_PEEK_BYTES = 64 * 1024
# A mail that passed through INBOX between two polls arrived after the last
# look at the folders began; the server's clock and this one may differ by
# this much.
ARRIVAL_SLACK_SECONDS = 3600.0
# Mail that was set aside (mirrored as a placeholder) is read again at start
# and then this often (Mirror._retry_set_aside).
SET_ASIDE_RETRY_SECONDS = 3600.0
# How long one such round may read (Mirror._retry_set_aside): reading a
# placeholder again can take as long as its mail may (READ_SECONDS).
SET_ASIDE_ROUND_SECONDS = 120.0
# After a fault of the mirror's own, what failed waits one poll interval, then
# twice as long after each further one in a row, up to this (_fault_wait) —
# and only what failed: new mail is followed every interval unless following
# it is what fails (_loop_step).
MIRROR_FAULT_WAIT_MAX_SECONDS = 3600.0


def _policy_path() -> Path:
    """The e-mail policy file (group flags), read raw off the ledger volume.

    The retinue side writes it into the mail ledger's own volume, which this
    container mounts at INBOUND_STORE_DIR. ``triage_policy`` is asked wherever
    the deployment tells it where the file is; with neither variable set it
    would answer the retinue-side path, which does not exist in this container,
    so the gateway's own mount is the default."""
    if os.environ.get("TRIAGE_EMAIL_WHITELIST_PATH") or os.environ.get("TRIAGE_MESSENGER_DIR"):
        return Path(_triage.email_whitelist_path())
    return INBOUND_STORE_DIR / "policy" / "email-policy.nt"


POLICY_PATH = _policy_path()


# ── Link-state tracking ───────────────────────────────────────────────────────
# Written by the mirror loop, read by /health: the probe itself never touches
# the mailbox.
_STATE_LOCK = threading.Lock()
_state: dict = {
    "connected": False,
    "error": None,      # why the last login or poll failed
    "notice": None,     # something an operator should know while it works
    "blocked": None,    # what holds the mirror when it is not the mailbox
    "last_poll": None,  # epoch of the last poll that went through
}
MIRROR = None  # the Mirror, once main() built it


def _set_state(**changes) -> None:
    with _STATE_LOCK:
        _state.update(changes)


def _health_snapshot() -> dict:
    with _STATE_LOCK:
        state = dict(_state)
    # Configured means the deployment asked for this mirror: a mailbox AND the
    # token. Either one missing is a choice (or an upgrade that changed
    # nothing in .env), reported as unconfigured with the reason — never as
    # an outage of a mirror nobody switched on.
    configured = CONFIGURED and TOKEN_PROBLEM is None
    connected = bool(configured and MAIL_CONFIG is not None and state["connected"])
    if not CONFIGURED:
        error = CONFIG_PROBLEM
    elif TOKEN_PROBLEM is not None:
        error = TOKEN_PROBLEM
    elif MAIL_CONFIG is None:
        error = CONFIG_PROBLEM
    elif not connected:
        error = state["error"] or "the mailbox has not been polled yet"
    else:
        error = state["notice"]
    backfill = (MIRROR.backfill_status() if MIRROR is not None
                else {"done": False, "pending": None, "days": MAIL_BACKFILL_DAYS})
    return {
        "status": "ok",
        "configured": configured,
        # Routing identity for the chat surface (see telegram-gateway.py).
        "mode": MAIL_GATEWAY_MODE,
        "account": (ACCOUNT or None) if configured else None,
        "connected": connected,
        # A mailbox is not paired with a QR code; a failed login is fixed in
        # the environment, and "error" says what to fix.
        "needs_repair": False,
        "error": error,
        # Set while what holds the mirror is not the mailbox — "policy": it
        # waits for the e-mail policy file, a matter of the deployment's
        # setup; "mirror": it stopped on a fault of its own (it cannot read
        # or write mail — see Mirror._ingest), in all its work or in the
        # part of it the error names. Not connected, then, is no word on
        # the IMAP login, and whoever words an alert reads this first
        # (gateway-monitor.py).
        "blocked": state["blocked"] if configured and MAIL_CONFIG is not None else None,
        "read_only": True,
        "backfill": backfill,
        # Mail mirrored as placeholders: how many stand, how many were made
        # since this start, and why — each a mail's own fault by the canary
        # rule, and a run of them is to be seen here whatever the cause
        # (the gateway monitor reads it, and says so).
        "placeholders": (MIRROR.placeholders() if MIRROR is not None
                         else {"standing": 0, "made": 0, "why": {}}),
        # Mail whose record is gone from the ledger (restored from an older
        # backup, a file lost) and that is still looked for in the mailbox
        # by its Message-ID: how many. Until one is found it is in no chat
        # (the gateway monitor reads this beside the placeholders).
        "lost": MIRROR.lost() if MIRROR is not None else 0,
        "last_poll": state["last_poll"],
        "build": _build.build_info(),
    }


# ── Reading a mail ────────────────────────────────────────────────────────────
# A mail is text from outside, every header of it. What is read here ends up
# in records, in an index and in JSON — never in a path, a header line or a
# prompt — and it is still reduced to the shape its field has: an id is an id,
# an address an address, a name one line without control characters.

_SYNTHETIC_DOMAIN = "no-message-id.mail.invalid"
_OVERLONG_DOMAIN = "overlong-message-id.mail.invalid"
# A charset is the sender's to name — on a body, on an encoded word, on a file
# name — and Python's codec registry holds more than charsets. Its own
# encodings are none: punycode costs time with the square of what it is given
# (minutes for a megabyte, under the poll), the two escape codecs and
# "undefined" exist to produce what is not text, or to raise. So a mail is
# decoded under a codec only through _charset, and the parser is never asked
# to do it (it would take any name): not for a leaf's payload (_subparts),
# not for a parameter (_parameter).
_NOT_CHARSETS = frozenset({"punycode", "idna", "unicode_escape", "raw_unicode_escape",
                           "undefined", "mbcs", "oem"})
_CHARSET_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,63}")
# A lone surrogate is not text: no file, no database and no JSON reader takes
# it — and utf-7, which is a charset, can still spell one. Every decoder here
# ends in _scrub.
_SURROGATE_RE = re.compile("[\ud800-\udfff]")
# Control characters, line and paragraph separators, and the bidirectional
# embedding / override / isolate controls, which let a name read as something
# other than what it is (and a surrogate, should one get this far).
_UNSAFE_LINE_RE = re.compile(
    "[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069\ud800-\udfff]")
# An address is shown as it is and compared as it is, so it may hold nothing
# that is not seen or that changes how the rest is seen: no C1 control, no
# soft hyphen, no zero-width or direction character of any kind, no tag
# character. An address holding one is not stripped of it \u2014 that would quietly
# make another, valid-looking address \u2014 it is not an address.
_ATOM = (r"[^\s<>()\[\]\\,;:\"@\x00-\x1f\x7f-\x9f\u00ad\u061c\u180e\u200b-\u200f"
         r"\u202a-\u202e\u2060-\u206f\ufeff\ufff9-\ufffb\ud800-\udfff"
         r"\U000e0000-\U000e007f]")
_ADDRESS_RE = re.compile(_ATOM + r"{1,64}@" + _ATOM + r"{1,255}")
_UNFOLD_RE = re.compile(r"\r?\n(?=[ \t])")
_HEADER_END_RE = re.compile(rb"\r?\n\r?\n")


# Why a mail is a placeholder, in the few words /health counts them under
# (Mirror.placeholders): one of these, or the name of what the reader raised.
OVER_A_BOUND = "asks for more than a mail may"
TOO_SLOW = "took too long to read"
READER_ENDED = "the reader process ended on it"


class UnreadableMail(Exception):
    """A mail the reader can make nothing of, by a fault that is the mail's:
    it asks for more than a mail may (MAX_PARSED_PARTS, MAX_NESTING,
    MAX_PARSE_WORK, MAX_PARAMETER_WORK), or the reader failed on it — an
    error, or its time ran out — and still reads the canaries
    (:func:`read_mail`). It is mirrored as a placeholder. ``kind`` is the
    reason in a few words, for the count on /health."""

    def __init__(self, why: str, kind: str = OVER_A_BOUND):
        super().__init__(why)
        self.kind = kind


class MirrorFault(Exception):
    """The mirror itself does not work: its reader fails on the canaries
    too, or is too slow for them, or a mail could not be written. No mail is
    set aside over it — the pass ends, and /health reports the mirror as at
    fault (``blocked: "mirror"``). The message is what /health reports, so
    it is written for the operator."""


class TookTooLong(BaseException):
    """The reading in hand is past its deadline (see READ_SECONDS). Not an
    ``Exception``: the reader catches those part by part and field by field,
    and time that is up is up for the whole mail."""


# When the reading in hand must be done, on the monotonic clock; None while
# nothing is being read against the clock. One reading at a time: the mirror
# has one thread, and the reader process reads one mail.
_READ_UNTIL: float | None = None


class _within:
    """``with _within(seconds):`` — what runs inside is a reading that must
    be done in that time. Its own code asks :func:`_tick`."""

    def __init__(self, seconds: float):
        self.seconds = seconds

    def __enter__(self):
        global _READ_UNTIL
        _READ_UNTIL = time.monotonic() + self.seconds
        return self

    def __exit__(self, *_exc):
        global _READ_UNTIL
        _READ_UNTIL = None


def _tick() -> None:
    """Called wherever the reader's own code is entered in the course of one
    reading; raises :class:`TookTooLong` once the reading is past its
    deadline. A call into the standard library that does not come back is
    not interrupted by this — that is what the reader process is for."""
    if _READ_UNTIL is not None and time.monotonic() > _READ_UNTIL:
        raise TookTooLong("its reading was not done in time")


@dataclass
class Part:
    """One part of a mail that is neither its plain nor its HTML body."""
    data: bytes
    content_type: str          # as the sender declared it; cleaned where it is stored
    file_name: str | None      # likewise
    content_id: str | None
    inline_image: bool         # an image the sender did not mark as an attachment


@dataclass
class Mail:
    """What the mirror reads out of one mail (see :func:`parse_mail`)."""
    message_id: str
    subject: str = ""
    sender: str = ""                        # the From address, lowercased; "" when unreadable
    sender_name: str | None = None
    to: list = field(default_factory=list)  # [(address, display name | None)]
    cc: list = field(default_factory=list)
    in_reply_to: str | None = None
    references: list = field(default_factory=list)
    cited: list = field(default_factory=list)
    list_id: str = ""                       # the raw List-Id header
    bulk: bool = False                      # the mail says itself it is list or bulk mail
    resent: bool = False                    # sent on as it stood (see _is_resent)
    date: float | None = None               # the record's time (see parse_mail)
    arrived: float | None = None            # when the server received it; not the sender's to write
    headers: str = ""
    plain: str | None = None
    html: str | None = None
    parts: list = field(default_factory=list)
    parts_dropped: int = 0                  # parts beyond MAX_PARTS, or that could not be read
    dropped_names: list = field(default_factory=list)   # the first few of their file names
    unreadable: str | None = None           # set on a placeholder: why (see stub_mail)
    reading: tuple | None = None            # (kb:text, kb:trim), once trimmed (see read_mail)


def _scrub(text: str) -> str:
    """Text with every lone surrogate replaced by U+FFFD."""
    return _SURROGATE_RE.sub("�", text)


def _charset(name) -> str | None:
    """The codec a declared charset is read under; None when the name is not
    one a mail may be decoded by (unknown, no text codec, or one of Python's
    own — see _NOT_CHARSETS)."""
    name = str(name or "").strip().strip("\"'")
    if not _CHARSET_NAME_RE.fullmatch(name):
        return None
    try:
        codec = codecs.lookup(name)
    except Exception:  # noqa: BLE001 - LookupError, or whatever a codec module raises
        return None
    if not getattr(codec, "_is_text_encoding", True) \
            or codec.name.replace("-", "_") in _NOT_CHARSETS:
        return None
    return codec.name


def _decode_bytes(data: bytes, charset) -> str:
    """Bytes to text under a declared charset that may be wrong or unknown."""
    codec = _charset(charset)
    if codec:
        try:
            return _scrub(data.decode(codec, errors="replace"))
        except Exception:  # noqa: BLE001 - a codec that raises all the same
            pass
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _raw_header(msg, name: str) -> str:
    """A header's value as it stands, folds and encoded words included."""
    try:
        value = msg.get(name)
        if value is None:
            return ""
        if isinstance(value, str):
            return _scrub(value)
        # The parser hands back a Header object for a value with raw 8-bit bytes.
        return _scrub("".join(
            _decode_bytes(chunk, None if str(cs or "") in ("", "unknown-8bit") else str(cs))
            if isinstance(chunk, bytes) else chunk
            for chunk, cs in email.header.decode_header(value)))
    except Exception:  # noqa: BLE001 - a header that cannot be read is an empty one
        return ""


_ENCODED_WORD_RE = re.compile(r"=\?[^?\s]+\?[bBqQ]\?[^?\s]*\?=")
_BETWEEN_WORDS_RE = re.compile(r"(\?=)[ \t]+(?==\?[^?\s]+\?[bBqQ]\?)")
# Where the standard decoder takes an encoded word to begin — it is laxer
# about a word than _ENCODED_WORD_RE: any charset, any text up to "?=".
_WORD_START_RE = re.compile(r"=\?[^?]*\?[bBqQ]\?")


def _words_end(value: str) -> bool:
    """Whether every encoded word the standard decoder would take up in
    ``value`` also ends, on the line it begins on.

    From one that does not, its pattern reads on to the end of the line
    before it gives up, and then begins again at the next: a line of such
    beginnings costs it the square of their number — 52 seconds for a
    Subject of 250 kB, and a part's file name goes through the same call."""
    for line in value.splitlines():
        if "=?" in line:
            last_end = line.rfind("?=")
            if any(word.end() > last_end for word in _WORD_START_RE.finditer(line)):
                return False
    return True


def _decode_word(match) -> str:
    try:
        ((chunk, charset),) = email.header.decode_header(match.group(0))
    except Exception:  # noqa: BLE001 - not base64 after all: the word stays as written
        return match.group(0)
    return _decode_bytes(chunk, charset) if isinstance(chunk, bytes) else chunk


def _decode_words(value: str) -> str:
    """RFC 2047 encoded words decoded; never raises on a malformed one. The
    words name their own charset, so each is read through _charset and what
    comes back is scrubbed."""
    _tick()
    try:
        # As email_client._decode reads a header, but for the codec — and
        # only where the standard decoder takes time in proportion: a value
        # with a word that never ends is read word by word, below.
        if not _words_end(value):
            raise ValueError("an encoded word that does not end")
        out = []
        for chunk, charset in email.header.decode_header(value):
            if isinstance(chunk, bytes):
                codec = _charset(charset) if charset else "utf-8"
                if codec is None:
                    raise LookupError(charset)
                chunk = chunk.decode(codec, errors="replace")
            out.append(chunk)
        return _scrub("".join(out))
    except Exception:  # noqa: BLE001 - an unknown charset, a broken base64 word
        pass
    # Word by word, so one that cannot be read does not cost the others.
    return _scrub(_ENCODED_WORD_RE.sub(_decode_word, _BETWEEN_WORDS_RE.sub(r"\1", value)))


def _clean_line(text, limit: int) -> str:
    """One line of display text: no control or direction-override characters,
    whitespace collapsed, capped."""
    text = _UNSAFE_LINE_RE.sub(" ", str(text or ""))
    return " ".join(text.split())[:limit].strip()


def _header_text(msg, name: str, limit: int = 998) -> str:
    return _clean_line(_decode_words(_UNFOLD_RE.sub("", _raw_header(msg, name))), limit)


def _addresses(msg, name: str) -> list:
    """``[(address, display name | None)]`` of an address header, in order.

    The header is split into addresses before any encoded word is decoded: a
    decoded display name may hold commas and quotes of its own. An entry whose
    address does not read as one is dropped."""
    raw = _UNFOLD_RE.sub("", _raw_header(msg, name))
    if not raw.strip():
        return []
    try:
        pairs = email.utils.getaddresses([raw])
    except Exception:  # noqa: BLE001
        return []
    out, seen = [], set()
    for display, address in pairs:
        address = (address or "").strip().lower()
        if not _ADDRESS_RE.fullmatch(address) or address in seen:
            continue
        seen.add(address)
        display = _clean_line(_decode_words(display or ""), 200)
        out.append((address, display if display and display.lower() != address else None))
        if len(out) >= MAX_RECIPIENTS:
            break
    return out


def _fit_id(message_id: str) -> str:
    """A Message-ID as the mirror keys it: itself, or a digest when overlong."""
    if len(message_id) <= MAX_ID_CHARS:
        return message_id
    digest = hashlib.sha256(message_id.encode("utf-8", "surrogatepass")).hexdigest()[:32]
    return f"{digest}@{_OVERLONG_DOMAIN}"


def _searchable(message_id: str) -> bool:
    """Whether the server can be asked for a mail by this id (HEADER search).

    Not a synthetic or digested id — no header carries those — and not one
    the search command cannot spell."""
    return (message_id.isascii() and "\\" not in message_id
            and not message_id.endswith(("@" + _SYNTHETIC_DOMAIN, "@" + _OVERLONG_DOMAIN)))


def _identify(msg, head: bytes = b"") -> str:
    """The id a mail is mirrored under: its Message-ID without brackets.

    A mail with none (or an unusable one) gets a stable synthetic id derived
    from the headers every copy of it shares, so the copy in INBOX and the
    copy in Sent are still one mail and a re-read never makes a second.
    A mail that has none of those either — never had them, or they lie
    beyond MAX_HEADER_BYTES — is told from the next such mail by ``head``,
    its header block as it stands (a move keeps it byte for byte): one id
    for all of them would make every one after the first "the same mail",
    never mirrored."""
    message_id = _threads.normalize_id(_raw_header(msg, "Message-ID"))
    if message_id:
        return _fit_id(message_id)
    shared = [_raw_header(msg, name) for name in
              ("Date", "From", "To", "Cc", "Subject", "In-Reply-To", "References")]
    basis = ("\n".join(shared).encode("utf-8", "surrogatepass")
             if any(value.strip() for value in shared) or not head else b"header block\n" + head)
    return f"{hashlib.sha256(basis).hexdigest()[:32]}@{_SYNTHETIC_DOMAIN}"


def _file_name(part) -> str | None:
    """The file name a part declares, decoded; None when it declares none.

    Read from the header values as :func:`_raw_header` gives them, because
    clients do write raw UTF-8 names and the parser would hand those back as
    replacement characters. The result is shown as a name, so no control
    character and no direction override may ride in it; store_media reduces it
    to a base name on top of that."""
    probe = email.message.Message()
    for header in ("Content-Disposition", "Content-Type"):
        value = _raw_header(part, header)
        if value:
            probe[header] = value
    name = (_parameter(probe, "filename", "content-disposition")
            or _parameter(probe, "name", "content-type"))
    return _UNSAFE_LINE_RE.sub("", _decode_words(name.strip())) or None


def _parameter(msg, name: str, header: str = "content-type") -> str:
    """One parameter of a structured header, "" when it is not there or will
    not parse. An RFC 2231 value states its own charset; it is collapsed here
    (as ``email.utils.collapse_rfc2231_value`` would, but through _charset),
    which is why ``get_filename`` and ``get_content_charset`` are not used."""
    _tick()
    try:
        value = msg.get_param(name, None, header)
        if isinstance(value, tuple) and len(value) == 3:
            charset, _language, text = value
            data = str(text).encode("raw-unicode-escape")
            codec = _charset(charset or "us-ascii")
            if codec is None:
                return email.utils.unquote(str(text))
            return _scrub(data.decode(codec, errors="replace"))
        return email.utils.unquote(str(value)) if value is not None else ""
    except Exception:  # noqa: BLE001 - malformed parameters
        return ""


def _subparts(part):
    """The parts of a multipart, or the mail inside a nested message; None for
    a leaf. (Never a leaf's own payload as text: the parser would decode that
    under whatever charset the part declares. Its bytes are _part_bytes'.)"""
    try:
        return part.get_payload() if part.is_multipart() else None
    except Exception:  # noqa: BLE001
        return None


def _part_bytes(part) -> bytes:
    """A leaf's content, its transfer encoding undone — no charset applied."""
    try:
        data = part.get_payload(decode=True)
    except Exception:  # noqa: BLE001 - a broken transfer encoding
        data = None
    return data if isinstance(data, bytes) else b""


def _is_attached(part, disposition: str) -> bool:
    """Whether a text leaf is a file its sender attached rather than the
    mail's body: email_client._is_attachment's rule, on the file name as it is
    read here."""
    if disposition == "attachment":
        return True
    if not _file_name(part):
        return False
    return disposition != "inline" or not part.get("Content-ID")


def _drop_part(mail: Mail, part=None) -> None:
    """Count a part that is not kept, and remember what the first few were
    called, for the note that stands in for them."""
    mail.parts_dropped += 1
    if part is not None and len(mail.dropped_names) < 20:
        try:
            name = _ibstore.safe_file_name(_file_name(part))
        except Exception:  # noqa: BLE001
            name = None
        if name:
            mail.dropped_names.append(name)


def _collect(part, mail: Mail, texts: dict, depth: int = 0) -> None:
    """Walk the MIME tree, keeping every leaf — up to MAX_PARTS of them.

    Unattached text/plain and text/html leaves are the mail's two bodies (more
    than one of a kind — a list's footer part, Apple Mail's HTML in pieces —
    are joined in order); every other leaf is a part of its own. A nested
    message is one part, kept whole: its own bodies are not this mail's.
    A leaf beyond the cap, below the depth any client writes, or that cannot
    be read at all is counted, not kept — and never costs the mail the rest."""
    _tick()
    if depth > 40:
        _drop_part(mail)
        return
    payload = _subparts(part)
    maintype = part.get_content_maintype()
    if maintype == "multipart" and isinstance(payload, list):
        for sub in payload:
            _collect(sub, mail, texts, depth + 1)
        return
    try:
        _collect_leaf(part, payload, maintype, mail, texts)
    except Exception as exc:  # noqa: BLE001 - one part the reader chokes on
        print(f"[mail-gateway] a part of {mail.message_id!r} could not be read: "
              f"{type(exc).__name__}: {exc}", flush=True)
        _drop_part(mail)


def _collect_leaf(part, payload, maintype: str, mail: Mail, texts: dict) -> None:
    content_type = part.get_content_type()
    try:
        disposition = (part.get_content_disposition() or "").lower()
    except Exception:  # noqa: BLE001 - malformed parameters
        disposition = ""
    if maintype == "message" and isinstance(payload, list):
        if len(mail.parts) >= MAX_PARTS:
            _drop_part(mail, part)
            return
        try:
            data = b"".join(sub.as_bytes() for sub in payload)
        except Exception:  # noqa: BLE001 - a nested mail the generator cannot write back
            data = b""
        mail.parts.append(Part(data, content_type, _file_name(part), None, False))
        return
    # A multipart with no parts in it (no boundary found, or cut off) is its
    # raw body: text, as far as anyone can tell.
    kind = "text/plain" if maintype == "multipart" else content_type
    if kind in ("text/plain", "text/html") and not _is_attached(part, disposition):
        texts[kind].append(_decode_bytes(_part_bytes(part), _parameter(part, "charset"))
                           .replace("\r\n", "\n"))
        return
    if len(mail.parts) >= MAX_PARTS:
        _drop_part(mail, part)     # before its bytes are decoded or held
        return
    # A Content-ID is what an HTML body's cid: link is matched against; one
    # too long to be an id is no use for that, and is not carried.
    content_id = _threads.normalize_id(_raw_header(part, "Content-ID"))
    mail.parts.append(Part(
        _part_bytes(part), content_type, _file_name(part),
        content_id if content_id and len(content_id) <= MAX_ID_CHARS else None,
        inline_image=maintype == "image" and disposition != "attachment"))


_STRUCTURED_FIELDS = frozenset({"content-type", "content-disposition"})
_LINE_END_RE = re.compile(r"\r\n|\r|\n")


class _BoundedPolicy(email.policy.Compat32):
    """The lenient legacy policy, with a Content-Type or Content-Disposition
    field cut to MAX_FIELD_BYTES as the parser takes it off the mail, and
    what the parameters of all those fields cost to parse counted against
    MAX_PARAMETER_WORK (``spent``: one parse's count, see _parse_bytes).

    Cut here and not in the raw bytes: only the parser knows where a header
    field is — the mail's own block, a part's, a nested mail's — and whatever
    it does not read as one is content, to be kept byte for byte."""

    spent = None

    def header_source_parse(self, sourcelines):
        _tick()
        name, value = super().header_source_parse(sourcelines)
        if name.lower() in _STRUCTURED_FIELDS:
            if len(value) > MAX_FIELD_BYTES:
                value = value[:MAX_FIELD_BYTES].rstrip("\r\n\t ")
            if self.spent is not None:
                self.spent[0] += value.count(";") * (len(value) + PARAMETER_STEP_COST)
                if self.spent[0] > MAX_PARAMETER_WORK:
                    raise UnreadableMail("the parameters of its Content-Type and "
                                         "Content-Disposition fields: more work than one "
                                         "mail may ask for")
        return name, value

    # A nested mail is written back for its part (Message.as_bytes) field by
    # field as it was read, with the line ends the rest of it is written
    # with. The legacy policy would wrap every field anew, and its wrapping
    # takes time with the square of the line breaks a value holds — to it, a
    # form feed is one: 28 seconds for a nested Subject of 2 MB, 139 for 4.
    def fold(self, name, value):
        _tick()
        return f"{name}: {_LINE_END_RE.sub(self.linesep, str(value))}{self.linesep}"

    def fold_binary(self, name, value):
        return self.fold(name, value).encode("ascii", "surrogateescape")


_BOUNDED = _BoundedPolicy()


def _parse_bytes(raw: bytes):
    """Bytes to a message, by the lenient legacy parser — which never raises
    on a malformed header — with what it may be asked to do bounded: the two
    fields whose parameters are parsed (_BoundedPolicy), the number of parts
    it builds, how deep they nest, the work that nesting costs it and the
    work their parameters do (:class:`UnreadableMail` beyond
    MAX_PARSED_PARTS, MAX_NESTING, MAX_PARSE_WORK or MAX_PARAMETER_WORK)."""
    made = 0
    # What one level of nesting costs: a test of every line (MAX_PARSE_WORK).
    # The parser ends a line at CR, LF or CRLF.
    lines = (raw.count(b"\n") + raw.count(b"\r") - raw.count(b"\r\n")
             + len(raw) // 64 + 1)
    if PARSE_LINE_COST * lines > MAX_PARSE_WORK:
        raise UnreadableMail(f"{lines} lines: more work than one mail may ask for")

    def part(policy):
        # The mail itself, each part of a multipart, a nested mail: every
        # object the parser builds comes through here — before it reads the
        # lines of that part, and with the messages it lies inside on the
        # parser's stack. (The stack is the parser's own business; without
        # it this raises on every mail, the canary included, which is loud.)
        nonlocal made
        _tick()
        made += 1
        if made > MAX_PARSED_PARTS:
            raise UnreadableMail(f"more than {MAX_PARSED_PARTS} MIME parts")
        depth = len(parser._msgstack)
        if depth > MAX_NESTING:
            raise UnreadableMail(f"MIME parts nested more than {MAX_NESTING} deep")
        if (PARSE_LINE_COST + depth) * lines > MAX_PARSE_WORK:
            raise UnreadableMail(f"MIME parts nested {depth} deep around {lines} lines: "
                                 f"more work than one mail may ask for")
        return email.message.Message(policy=policy)

    # As email.message_from_bytes does it, but for the parser kept in hand.
    parser = email.feedparser.BytesFeedParser(
        policy=_BOUNDED.clone(message_factory=part, spent=[0]))
    for start in range(0, len(raw), 8192):
        _tick()
        parser.feed(raw[start:start + 8192])
    return parser.close()


def _head_and_rest(raw: bytes) -> tuple[bytes, bytes]:
    """A raw mail as ``(header block, everything from the empty line on)``,
    the header block cut at the last whole line within MAX_HEADER_BYTES."""
    end = _HEADER_END_RE.search(raw)
    head, rest = (raw[:end.start()], raw[end.start():]) if end else (raw, b"")
    if len(head) > MAX_HEADER_BYTES:
        head = head[:MAX_HEADER_BYTES]
        head = head[:head.rfind(b"\n") + 1].rstrip(b"\r\n")
    return head, rest


def _head_of(raw: bytes) -> bytes:
    """A raw mail's header block (:func:`_head_and_rest`). Never raises."""
    try:
        return _head_and_rest(raw)[0]
    except Exception:  # noqa: BLE001
        return bytes(raw[:MAX_HEADER_BYTES])


def _head_text(head: bytes) -> str:
    """A header block as a record keeps it (kb:headerPart)."""
    return head.decode("utf-8", errors="replace").replace("\r\n", "\n")


def _read_ids(msg, mail: Mail) -> None:
    """The ids a mail cites, as the mirror keys them (see _fit_id)."""
    def ids(references, in_reply_to):
        return list(dict.fromkeys(
            fitted for fitted in map(_fit_id, _threads.cited_ids(references, in_reply_to))
            if fitted != mail.message_id))

    raw_references = _raw_header(msg, "References")
    raw_in_reply_to = _raw_header(msg, "In-Reply-To")
    mail.in_reply_to = next(iter(ids("", raw_in_reply_to)), None)
    mail.cited = ids(raw_references, raw_in_reply_to)
    # What the record keeps beside the parent: the References ids, and any
    # further In-Reply-To id (the header may name several parents, and
    # kb:inReplyTo holds one). A thread index rebuilt from the records reads
    # exactly these two fields, and must see every id the live one saw.
    referenced = set(ids(raw_references, ""))
    mail.references = [cited for cited in mail.cited
                       if cited in referenced or cited != mail.in_reply_to]


def _read_date(msg, internal_date: float | None) -> float | None:
    """A mail's time on record: its Date header, the sender's claim, where it
    is not later than the server's arrival time — a mail cannot have been
    written after it arrived."""
    written = _threads.to_epoch(_UNFOLD_RE.sub("", _raw_header(msg, "Date")).strip())
    if written is not None and written < 0:
        written = None      # before the epoch: no mail was
    if written is not None and internal_date is not None:
        written = min(written, internal_date)
    return written if written is not None else internal_date


# What bulk senders say of their own mail (RFC 2076 and common practice). The
# value is the sender's to write; it only ever lets the trim cut a footer.
_BULK_PRECEDENCE = frozenset({"bulk", "list", "junk"})


def _is_bulk(msg, list_id: str) -> bool:
    """Whether a mail declares itself list or bulk mail: a ``List-Id``, a
    ``List-Unsubscribe``, or a bulk ``Precedence``. Evidence for the trim
    (an unsubscribe block is a footer only in such mail), nothing else."""
    try:
        if list_id.strip() or _raw_header(msg, "List-Unsubscribe").strip():
            return True
        return _raw_header(msg, "Precedence").strip().lower() in _BULK_PRECEDENCE
    except Exception:  # noqa: BLE001 - an unreadable header is no evidence
        return False


# What whoever sends a mail on as it stands adds to it (RFC 5322, 3.6.6): a
# redirect, a "bounce". A reply or a forward is a new mail and carries none.
_RESENT_FIELDS = ("Resent-From", "Resent-Sender", "Resent-Date", "Resent-To")


def _is_resent(msg) -> bool:
    """Whether a mail was sent on as it stood, under its first author's From
    and Message-ID. The one thing that says a copy in Sent is not what the
    user wrote (Mirror._mirror_one); nothing else is decided by it."""
    try:
        return any(msg.get(name) is not None for name in _RESENT_FIELDS)
    except Exception:  # noqa: BLE001 - an unreadable header is no evidence
        return False


def parse_mail(raw: bytes, internal_date: float | None = None) -> Mail:
    """Read one raw mail into what the mirror records.

    Parsed with the lenient legacy policy on purpose: it never raises on a
    malformed header, and every value is decoded here, defensively, instead.
    ``internal_date`` is when the server received the mail; the mail's own
    Date header is the sender's claim, so it is used only where it is not
    later than that. What the mail may ask of the reader is bounded before it
    is parsed (the header block, MAX_HEADER_BYTES), as it is parsed (a
    structured field, MAX_FIELD_BYTES; the number of parts, their nesting
    and the work it costs: MAX_PARSED_PARTS, MAX_NESTING, MAX_PARSE_WORK,
    MAX_PARAMETER_WORK) and while it is read (MAX_PARTS, the ids, the
    addresses) — and by the clock, where the reading has a deadline
    (:func:`_tick`). A mail that asks for more than can be parsed raises
    :class:`UnreadableMail` and is the caller's to set aside. Anything else
    it raises is not yet known to be the mail's fault: the mirror reads
    through :func:`read_mail`."""
    head, rest = _head_and_rest(raw)
    msg = _parse_bytes(head + rest)
    mail = Mail(message_id=_identify(msg, head), arrived=internal_date)
    mail.subject = _header_text(msg, "Subject")
    sender = _addresses(msg, "From") or _addresses(msg, "Sender")
    if sender:
        mail.sender, mail.sender_name = sender[0]
    mail.to = _addresses(msg, "To")
    mail.cc = _addresses(msg, "Cc")
    _read_ids(msg, mail)
    mail.list_id = _clean_line(_UNFOLD_RE.sub("", _raw_header(msg, "List-Id")), 998)
    mail.bulk = _is_bulk(msg, mail.list_id)
    mail.resent = _is_resent(msg)
    mail.date = _read_date(msg, internal_date)
    mail.headers = _head_text(head)
    texts: dict = {"text/plain": [], "text/html": []}
    _collect(msg, mail, texts)
    if texts["text/plain"]:
        mail.plain = "\n\n".join(texts["text/plain"])
    if texts["text/html"]:
        mail.html = "\n".join(texts["text/html"])
    return mail


def stub_mail(raw: bytes, internal_date: float | None, reason: str) -> Mail:
    """What is recorded of a mail that could not be read: who sent it, when,
    the ids that place it in its thread and the list it came through (a
    newsletter is one whether or not it can be read) — nothing else of it.
    Never raises.

    Its header block alone is parsed (no body, so no parts and no charset),
    each field on its own, and a field that will not read is left out. The
    id is the one the whole mail would have had, so the mail is still one
    record however many folders hold it, and is followed like any other."""
    head = _head_of(raw)
    try:
        msg = _parse_bytes(head + b"\r\n\r\n")
        mail = Mail(message_id=_identify(msg, head))
    except Exception:  # noqa: BLE001 - not even its headers: an id of its bytes
        msg = None
        mail = Mail(message_id=_bare_id(head))
    mail.unreadable = reason
    mail.arrived = mail.date = internal_date
    mail.headers = _head_text(head)

    def sender():
        found = _addresses(msg, "From") or _addresses(msg, "Sender")
        if found:
            mail.sender = found[0][0]

    def date():
        mail.date = _read_date(msg, internal_date)

    def list_id():
        # Whether it is news is decided by these two: an unreadable
        # newsletter is still no chat.
        mail.list_id = _clean_line(_UNFOLD_RE.sub("", _raw_header(msg, "List-Id")), 998)

    if msg is not None:
        mail.resent = _is_resent(msg)
        for read in (sender, date, list_id, lambda: _read_ids(msg, mail)):
            try:
                read()
            except Exception:  # noqa: BLE001 - left out
                pass
    return mail


def _bare_id(head: bytes) -> str:
    """The id of a mail of which not even the header block could be read: one
    of its bytes. It needs no reading to be had again — a move keeps a header
    block byte for byte — so a mail on record under it is found again
    whatever the reader does (Mirror._find)."""
    return f"{hashlib.sha256(head).hexdigest()[:32]}@{_SYNTHETIC_DOMAIN}"


def _bare_stub(head: bytes, internal_date: float | None, reason: str) -> Mail:
    """A placeholder for a mail of which not even the header block could be
    read (see read_stub): nothing of it is parsed. Its id is one of its
    bytes, and its header block is still what "Original" shows."""
    return Mail(message_id=_bare_id(head), unreadable=reason, arrived=internal_date,
                date=internal_date, headers=_head_text(head))


def _head_facts(head: bytes) -> dict:
    """What the mirror asks of a header block alone, to find a mail again:
    the id the mail is mirrored under, its Message-ID field as it stands,
    whether it was sent on as it stood (see read_head), and the address it
    is From (None where there is none that reads — which takes nothing of
    the rest)."""
    msg = _parse_bytes(head + b"\r\n\r\n")
    try:
        sender = (_addresses(msg, "From") or _addresses(msg, "Sender") or [(None, None)])[0][0]
    except Exception:  # noqa: BLE001 - left out, as stub_mail leaves it out
        sender = None
    return {"id": _identify(msg, head), "message_id": _raw_header(msg, "Message-ID"),
            "resent": _is_resent(msg), "sender": sender}


# What a placeholder's bubble says. The mail's sender and time are on the
# record beside it, and its header block is what "Original" shows.
UNREADABLE_TEXT = ("This mail could not be shown here: the mail gateway could not read it. "
                   "It is unchanged in the mailbox.")

# The canaries: mails known to be good, with everything in them the reader
# has a step for. When reading a mail fails, the same reading is run on
# these, and that decides whose fault it was (read_mail). Every path a mail
# can take through the reader is taken by one of them — a fault of the
# mirror's own on a path no canary takes would be laid at the door of every
# mail that takes it. They are never mirrored.
#
# The first: encoded words, a thread, two bodies in two encodings, a quoted
# reply for the trim, an attachment.
CANARY_MAIL = (
    b"From: =?utf-8?q?The_Canary?= <canary@canary.mail.invalid>\r\n"
    b"To: mirror@canary.mail.invalid\r\n"
    b"Subject: Re: =?utf-8?b?U3RpbGwgc2luZ2luZz8=?=\r\n"
    b"Date: Thu, 01 Jan 2026 12:00:00 +0000\r\n"
    b"Message-ID: <answer@canary.mail.invalid>\r\n"
    b"In-Reply-To: <question@canary.mail.invalid>\r\n"
    b"References: <question@canary.mail.invalid>\r\n"
    b"MIME-Version: 1.0\r\n"
    b"Content-Type: multipart/mixed; boundary=\"outer\"\r\n"
    b"\r\n"
    b"--outer\r\n"
    b"Content-Type: multipart/alternative; boundary=\"inner\"\r\n"
    b"\r\n"
    b"--inner\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"Content-Transfer-Encoding: quoted-printable\r\n"
    b"\r\n"
    b"Still singing =E2=80=94 the reader works.\r\n"
    b"\r\n"
    b"On Thu, 1 Jan 2026 at 11:00, the mirror wrote:\r\n"
    b"> Still singing?\r\n"
    b"--inner\r\n"
    b"Content-Type: text/html; charset=utf-8\r\n"
    b"\r\n"
    b"<p>Still singing &mdash; the reader works.</p>\r\n"
    b"--inner--\r\n"
    b"--outer\r\n"
    b"Content-Type: application/octet-stream; name=\"canary.bin\"\r\n"
    b"Content-Disposition: attachment; filename=\"canary.bin\"\r\n"
    b"Content-Transfer-Encoding: base64\r\n"
    b"\r\n"
    b"Y2FuYXJ5\r\n"
    b"--outer--\r\n")
# The second: HTML and nothing else, so its reading text is the converter's
# (beside a plain part that reads, the trim never consults the HTML one) —
# under a charset that is not UTF-8, and a Subject folded after an encoded
# word in a third.
CANARY_HTML_MAIL = (
    b"From: canary@canary.mail.invalid\r\n"
    b"To: mirror@canary.mail.invalid\r\n"
    b"Subject: =?iso-8859-1?q?Gr=FC=DFe?= from\r\n"
    b" the canary\r\n"
    b"Date: Thu, 01 Jan 2026 12:00:00 +0000\r\n"
    b"Message-ID: <page@canary.mail.invalid>\r\n"
    b"MIME-Version: 1.0\r\n"
    b"Content-Type: text/html; charset=windows-1252\r\n"
    b"Content-Transfer-Encoding: quoted-printable\r\n"
    b"\r\n"
    b"<html><head><style>p { color: red }</style></head><body>\r\n"
    b"<p>Only <b>HTML</b> here =97 the converter works.</p>\r\n"
    b"<ul><li>one</li><li>two</li></ul>\r\n"
    b"</body></html>\r\n")
# The third: an inline image beside the HTML that shows it, and a nested
# mail — written back for its part field by field (_BoundedPolicy.fold) —
# under a file name spelled the RFC 2231 way.
CANARY_FORWARD_MAIL = (
    b"From: canary@canary.mail.invalid\r\n"
    b"To: mirror@canary.mail.invalid\r\n"
    b"Subject: Fwd: Still singing?\r\n"
    b"Date: Thu, 01 Jan 2026 12:00:00 +0000\r\n"
    b"Message-ID: <forward@canary.mail.invalid>\r\n"
    b"MIME-Version: 1.0\r\n"
    b"Content-Type: multipart/mixed; boundary=\"mixed\"\r\n"
    b"\r\n"
    b"--mixed\r\n"
    b"Content-Type: multipart/related; boundary=\"related\"\r\n"
    b"\r\n"
    b"--related\r\n"
    b"Content-Type: text/html; charset=utf-8\r\n"
    b"\r\n"
    b"<p>The mail below, under our sign <img src=\"cid:sign@canary.mail.invalid\">.</p>\r\n"
    b"--related\r\n"
    b"Content-Type: image/png\r\n"
    b"Content-ID: <sign@canary.mail.invalid>\r\n"
    b"Content-Disposition: inline\r\n"
    b"Content-Transfer-Encoding: base64\r\n"
    b"\r\n"
    b"iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAYAAAA=\r\n"
    b"--related--\r\n"
    b"--mixed\r\n"
    b"Content-Type: message/rfc822\r\n"
    b"Content-Disposition: attachment; filename*=utf-8''fr%C3%BCher.eml\r\n"
    b"\r\n"
    b"From: mirror@canary.mail.invalid\r\n"
    b"Subject: Still\r\n"
    b"\tsinging?\r\n"
    b"Message-ID: <question@canary.mail.invalid>\r\n"
    b"\r\n"
    b"Still singing?\r\n"
    b"--mixed--\r\n")


def _canary_answer(mail: Mail) -> bool:
    return (mail.message_id == "answer@canary.mail.invalid" and len(mail.parts) == 1
            and "the reader works" in (mail.plain or "") and bool(mail.reading[0]))


def _canary_page(mail: Mail) -> bool:
    text = mail.reading[0]
    return (mail.subject == "Grüße from the canary" and mail.plain is None
            and "Only HTML here — the converter works." in text
            and "one" in text and "two" in text and "<" not in text and "color" not in text)


def _canary_forward(mail: Mail) -> bool:
    kinds = [(part.content_type, part.file_name, part.content_id, part.inline_image)
             for part in mail.parts]
    return (kinds == [("image/png", None, "sign@canary.mail.invalid", True),
                      ("message/rfc822", "früher.eml", None, False)]
            and mail.parts[0].data.startswith(b"\x89PNG")
            and b"Subject: Still\n\tsinging?\n" in mail.parts[1].data
            and mail.parts[1].data.endswith(b"\n\nStill singing?")
            and "The mail below, under our sign" in mail.reading[0])


_CANARIES = ((CANARY_MAIL, _canary_answer), (CANARY_HTML_MAIL, _canary_page),
             (CANARY_FORWARD_MAIL, _canary_forward))


def _reading(raw: bytes, internal_date: float | None) -> Mail:
    """All of READING one mail: the parse, the walk of its parts, and the
    trim of its text. Nothing in here writes, and nothing outside it reads."""
    mail = parse_mail(raw, internal_date)
    _tick()
    mail.reading = Mirror._reading_text(mail)
    return mail


def _read_canary() -> None:
    """Read the canaries; raises unless the reader makes of each what it is."""
    for raw, read_right in _CANARIES:
        if not read_right(_reading(raw, None)):
            raise ValueError("the reader misread the mirror's own test mail")


# ── The reader process ────────────────────────────────────────────────────────
# The deadline on reading one mail (READ_SECONDS) is kept by the reader's own
# looks at the clock wherever it can be — and those cannot end one call into
# the standard library that does not come back, which is what two of the
# three slow paths found last were: one regular expression over a Subject,
# one header written back. Nothing in a thread ends such a call. A process
# can be ended, so the running gateway reads its mail in one: this same
# script, started with READER_FLAG, taking mails on its standard input and
# answering on a pipe of its own (its standard output stays the log's, as
# whatever a module prints when it is loaded expects). It costs a second interpreter (0.2 s to
# start) and each mail's bytes across two pipes (nothing to speak of for an
# ordinary mail; 0.2 s on the second a mail of 41 MB takes to read); it buys
# a bound nothing a mail holds can get round.
#
#   gateway → reader   kind (M: a mail; C: read the canaries; S: a stub of
#                      this header block; F: the facts of this header
#                      block), the seconds it may take, a length and an
#                      arrival time; that many bytes
#   reader → gateway   H once, when it is ready; then per request a status,
#                      a length and that many bytes:
#                        M  the mail as read, or its stub; F its facts
#                           (pickled, built-in types only)
#                        K  the canaries read
#                        U  refused by a bound: (kind, why)
#                        T  its own look at the clock: out of time
#                        E  the reader raised: (what, why)
#
# The reader is given none of the gateway's secrets: it reads text from
# outside, and needs nothing but that text.

READER_FLAG = "--reader"
_REQUEST = struct.Struct("!cdQd")
_ANSWER = struct.Struct("!cQ")
_SECRET_NAME_RE = re.compile(r"PASS|TOKEN|SECRET|KEY", re.IGNORECASE)


class ReaderDown(OSError):
    """The reader process cannot be started or does not come up: like a full
    disk, the mirror's surroundings failing — no mail is at fault, and the
    pass ends (see _ENVIRONMENT)."""


class _ReadFailed(Exception):
    """What the reader process raised on a mail, by name — or that it ended."""

    def __init__(self, what: str, why: str):
        super().__init__(f"{what}: {why}" if why else what)
        self.kind = what


def _mail_from(plain: dict) -> Mail:
    return Mail(**{**plain, "parts": [Part(**part) for part in plain["parts"]]})


class _PlainUnpickler(pickle.Unpickler):
    """Reads what the reader process answers: built-in types and nothing
    else, whatever became of that process while it read text from outside."""

    def find_class(self, module, name):
        raise pickle.UnpicklingError(f"the reader's answer names {module}.{name}")


def _unpickled(answer: bytes):
    return _PlainUnpickler(io.BytesIO(answer)).load()


def _serve_reader(answer_fd: int) -> None:
    """The reader process: read each mail the gateway sends, within the time
    it names, and answer on ``answer_fd``. Ends when the gateway closes the
    pipe, or is gone."""
    requests = sys.stdin.buffer
    answers = os.fdopen(answer_fd, "wb")
    # (The alarm set below must end this process, whatever it inherited.)
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    try:
        _read_canary()      # everything the reading imports and compiles, once
    except Exception:  # noqa: BLE001 - said when the gateway asks for the canaries
        pass
    answers.write(_ANSWER.pack(b"H", 0))
    answers.flush()
    while True:
        head = requests.read(_REQUEST.size)
        if len(head) < _REQUEST.size:
            return
        kind, seconds, length, date = _REQUEST.unpack(head)
        raw = requests.read(length)
        if len(raw) < length:
            return
        # Should the gateway be gone by then, or this process never get back
        # to its own code: the alarm's default action ends it, in whatever
        # call it is. (The gateway itself stops waiting sooner.)
        signal.setitimer(signal.ITIMER_REAL, seconds + 2 * READER_GRACE_SECONDS)
        try:
            with _within(seconds):
                if kind == b"C":
                    _read_canary()
                    status, answer = b"K", b""
                elif kind == b"F":
                    status, answer = b"F", pickle.dumps(_head_facts(raw))
                else:
                    date = None if date != date else date
                    mail = stub_mail(raw, date, "") if kind == b"S" else _reading(raw, date)
                    status, answer = b"M", pickle.dumps(
                        {**vars(mail), "parts": [vars(part) for part in mail.parts]},
                        protocol=pickle.HIGHEST_PROTOCOL)
        except UnreadableMail as exc:
            status, answer = b"U", pickle.dumps((exc.kind, str(exc)))
        except TookTooLong:
            status, answer = b"T", b""
        except Exception as exc:  # noqa: BLE001 - RecursionError and MemoryError are among them
            traceback.print_exc(limit=-8, file=sys.stderr)
            status, answer = b"E", pickle.dumps((type(exc).__name__, str(exc)[:300]))
        signal.setitimer(signal.ITIMER_REAL, 0)
        answers.write(_ANSWER.pack(status, len(answer)))
        answers.write(answer)
        answers.flush()
        raw = mail = answer = None      # nothing of a mail is held while it waits


class Reader:
    """The gateway's side of the reader process: start it, hand it a mail,
    wait for the answer no longer than the mail's deadline allows — and end
    it when it does not answer. A process that was ended, or ended by
    itself, is replaced when the next reading needs one."""

    def __init__(self, command=None):
        # (The number of the pipe it answers on is added as the last argument.)
        self._command = command or [sys.executable, str(Path(__file__).resolve()), READER_FLAG]
        self._process = None
        self._answers = None    # the reading end of that pipe

    def close(self) -> None:
        process, self._process = self._process, None
        answers, self._answers = self._answers, None
        if answers is not None:
            os.close(answers)
        if process is None:
            return
        try:
            process.kill()
            process.wait(timeout=READER_GRACE_SECONDS)
        except Exception:  # noqa: BLE001 - it is being dropped either way
            pass
        try:
            process.stdin.close()
        except Exception:  # noqa: BLE001
            pass

    def _start(self) -> None:
        self.close()
        try:
            self._answers, theirs = os.pipe()
            try:
                self._process = subprocess.Popen(
                    [*self._command, str(theirs)], stdin=subprocess.PIPE, bufsize=0,
                    pass_fds=(theirs,),
                    env={name: value for name, value in os.environ.items()
                         if not _SECRET_NAME_RE.search(name)})
            finally:
                os.close(theirs)
            os.set_blocking(self._process.stdin.fileno(), False)
            status, _length = _ANSWER.unpack(self._receive(
                _ANSWER.size, time.monotonic() + READER_START_SECONDS))
        except (OSError, ValueError, struct.error, TookTooLong, _ReadFailed) as exc:
            self.close()
            raise ReaderDown("the mail reader process could not be started (" + (
                "it ended before it was ready" if isinstance(exc, _ReadFailed)
                else f"{type(exc).__name__}: {exc}") + ")") from exc
        if status != b"H":
            self.close()
            raise ReaderDown("the mail reader process did not come up as one")

    def _send(self, data: bytes, until: float) -> None:
        fd, view = self._process.stdin.fileno(), memoryview(data)
        while view:
            left = until - time.monotonic()
            if left <= 0:
                raise TookTooLong("the reader process did not take it in time")
            if not select.select([], [fd], [], left)[1]:
                continue
            try:
                view = view[os.write(fd, view[:1 << 20]):]
            except BlockingIOError:
                continue
            except OSError as exc:
                raise _ReadFailed(READER_ENDED, str(exc)) from exc

    def _receive(self, length: int, until: float) -> bytes:
        fd, got = self._answers, bytearray()
        while len(got) < length:
            left = until - time.monotonic()
            if left <= 0:
                raise TookTooLong("the reader process did not answer in time")
            if not select.select([fd], [], [], left)[0]:
                continue
            chunk = os.read(fd, min(length - len(got), 1 << 20))
            if not chunk:
                raise _ReadFailed(READER_ENDED, "")
            got += chunk
        return bytes(got)

    def _ask(self, kind: bytes, raw: bytes, seconds: float, date: float | None):
        """One request and its answer, ``(status, bytes)``. Raises
        :class:`TookTooLong` when no answer came in time and the process was
        ended for it, ``_ReadFailed`` when it ended by itself."""
        if self._process is None or self._process.poll() is not None:
            self._start()      # gone while nothing was asked of it: no mail's doing
        until = time.monotonic() + seconds + READER_GRACE_SECONDS
        try:
            self._send(_REQUEST.pack(kind, seconds, len(raw),
                                     float("nan") if date is None else date), until)
            self._send(raw, until)
            status, length = _ANSWER.unpack(self._receive(_ANSWER.size, until))
            return status, self._receive(length, until)
        except BaseException:
            # No telling where in a request the pipes are: this one is done.
            self.close()
            raise

    def read(self, raw: bytes, internal_date: float | None, seconds: float) -> Mail:
        """:func:`_reading`, in the reader process and within ``seconds``."""
        status, answer = self._ask(b"M", raw, seconds, internal_date)
        if status == b"M":
            return _mail_from(_unpickled(answer))
        self._failed(status, answer)

    def canary(self, seconds: float) -> None:
        """:func:`_read_canary`, likewise."""
        status, answer = self._ask(b"C", b"", seconds, None)
        if status != b"K":
            self._failed(status, answer)

    def stub(self, head: bytes, internal_date: float | None, seconds: float) -> Mail:
        """:func:`stub_mail` of a header block, likewise."""
        status, answer = self._ask(b"S", head, seconds, internal_date)
        if status == b"M":
            return _mail_from(_unpickled(answer))
        self._failed(status, answer)

    def facts(self, head: bytes, seconds: float) -> dict:
        """:func:`_head_facts`, likewise."""
        status, answer = self._ask(b"F", head, seconds, None)
        if status == b"F":
            return _unpickled(answer)
        self._failed(status, answer)

    def _failed(self, status: bytes, answer: bytes):
        if status == b"T":
            raise TookTooLong("its reading was not done in time")
        if status in (b"U", b"E"):
            what, why = _unpickled(answer)
            raise UnreadableMail(why, what) if status == b"U" else _ReadFailed(what, why)
        self.close()
        raise _ReadFailed(READER_ENDED, f"an answer of no known kind ({status!r})")


# The reader process of the running gateway (main() makes it). Without one —
# a Mirror built by hand, as the tests build theirs — a mail is read where
# the mirror runs, with the reader's own looks at the clock for a deadline.
READER = None


def _read_seconds(size: int) -> float:
    """How long the reading of a mail of ``size`` bytes may take."""
    return READ_SECONDS + READ_SECONDS_PER_MB * size / 1_000_000


def read_mail(raw: bytes, internal_date: float | None = None) -> Mail:
    """Read one mail for the mirror — and, when that fails, say whose fault
    it is. A reader has every way to fail on text from outside: its own
    bounds, the parser's and the codecs' errors, a recursion that does not
    end, memory that does (a mail is some megabytes; what is made of it need
    not be), time that runs out (:func:`_read_seconds`). No exception says
    whether the mail brought that out or the reader is broken — or the
    machine too slow — for every mail, so the reader is asked: the same
    reading is run on the canaries, against the clock too (CANARY_SECONDS).

      * It reads the canaries: the reader works, and the failure is this
        mail's — :class:`UnreadableMail`, a placeholder at once.
      * It fails on a canary too, or is not done with them in their time:
        the fault is the mirror's — :class:`MirrorFault`, and no mail is set
        aside over it.

    With a reader process (READER) both readings run there, and a process
    that had to be ended over a mail is replaced before the canaries are
    read; one that cannot be started is nobody's mail (:class:`ReaderDown`)."""
    seconds = _read_seconds(len(raw))
    try:
        if READER is not None:
            return READER.read(raw, internal_date, seconds)
        with _within(seconds):
            return _reading(raw, internal_date)
    except (UnreadableMail, ReaderDown):
        raise   # refused on purpose, by a bound: the mail's. No reader: nobody's.
    except TookTooLong as exc:
        failure, kind = exc, TOO_SLOW
        why = f"{TOO_SLOW}: {exc} ({seconds:.0f} s for a mail of {_human_size(len(raw))})"
    except _ReadFailed as exc:
        failure, kind, why = exc, exc.kind, str(exc)[:300]
    except Exception as exc:  # noqa: BLE001 - RecursionError and MemoryError are among them
        failure, kind, why = exc, type(exc).__name__, f"{type(exc).__name__}: {exc}"[:300]
    _ask_the_canaries(why, failure)
    raise UnreadableMail(why, kind) from failure


def _ask_the_canaries(why: str, failure: BaseException) -> None:
    """Read the canaries after a reading failed (``why``). Returns when the
    reader reads them, and in their time: the failure was not the reader's.
    Raises :class:`MirrorFault` when it does not."""
    try:
        if READER is not None:
            READER.canary(CANARY_SECONDS)
        else:
            with _within(CANARY_SECONDS):
                _read_canary()
    except ReaderDown:
        raise
    except TookTooLong as exc:
        raise MirrorFault(
            f"the mail reader is too slow: it did not read the mirror's own test mail in "
            f"{CANARY_SECONDS:.0f} s ({exc}), after a mail ({why})") from failure
    except Exception as exc:  # noqa: BLE001 - the reader is broken, whatever the mail
        said = str(exc) if isinstance(exc, _ReadFailed) else f"{type(exc).__name__}: {exc}"
        raise MirrorFault(
            f"the mail reader fails on the mirror's own test mail "
            f"({said}"[:200] + f") as it did on a mail ({why})") from failure


class NotRead(Exception):
    """The reading of a header block did not come back — it was late, or the
    reader process ended over it — and did not on a second try either, with
    the canaries read in their time in between (:func:`_read_guarded`).

    That is NO ANSWER about the mail. A header block is read to decide
    things for good — which mail lies at a UID, whether a mail is in a
    folder at all — and a reader that was stopped for some seconds (a
    process swapped out, a paused container, a thrashing host) looks just
    like this. So whoever gets it decides nothing: no id is taken from it,
    none is remembered, and no mail is called gone on it (UNSURE)."""


def _read_guarded(in_reader, here):
    """Read something off one header block as guarded as a mail is read:
    where mail is read (the reader process, when there is one) and against
    the clock — a header block is small, and gets what the canaries get.
    It ends in one of four ways, and they are kept apart:

      * the answer;
      * :class:`UnreadableMail` — the reader raised on the block, or a bound
        refused it. That is what these bytes do to the reader, at every
        reading: an answer, and the block's own;
      * :class:`MirrorFault` — it did not come back, and the canaries do
        not either: the mirror's fault, and the pass ends;
      * :class:`NotRead` — it did not come back, twice, though the reader
        read the canaries in between: no answer (see there). The second
        try is what keeps one stall of the reader from being so much as
        that."""
    for _attempt in (1, 2):
        try:
            if READER is None:
                with _within(CANARY_SECONDS):
                    return here()
            return in_reader(CANARY_SECONDS)
        except (UnreadableMail, ReaderDown):
            raise
        except TookTooLong as exc:
            failure = exc
        except _ReadFailed as exc:
            if exc.kind != READER_ENDED:
                raise UnreadableMail(str(exc)[:300], exc.kind) from exc
            failure = exc
        except Exception as exc:  # noqa: BLE001 - what these bytes bring out
            raise UnreadableMail(f"{type(exc).__name__}: {exc}"[:300],
                                 type(exc).__name__) from exc
        _ask_the_canaries(f"its header block alone: {failure}", failure)
    raise NotRead(f"a header block was not read in {CANARY_SECONDS:.0f} s, twice, by a "
                  f"reader that read the mirror's own test mail in between ({failure})")


def read_stub(raw: bytes, internal_date: float | None, reason: str, *,
              go_on: bool = True) -> Mail:
    """:func:`stub_mail` for the mirror: the placeholder of a mail that
    could not be read. Never raises, but for a fault of the mirror's own
    (:class:`MirrorFault`, :class:`ReaderDown`) — and :class:`NotRead`
    where nothing has to go on (``go_on`` false, see below).

    A stub is read off the mail's header block — the block the reading may
    just have failed on, or run out of time over. So it is read as guarded
    as the mail was (:func:`_read_guarded`). A block that cannot be read,
    or does not come back, still gets its mail a placeholder — the folder
    must go on: one that nothing was parsed for (:func:`_bare_stub`), under
    an id of the block's bytes. That id is not for good: it is found again
    without any reading, and gives way in one step when the mail is read
    under its own (Mirror._ingest). Where nothing has to go on — the mail
    read as the record of a lost one (Mirror._read_found), whose id is
    looked for again later — a block that did not come back is no answer,
    and the mail is left for another reading: under an id of its bytes, it
    would leave the id it is looked for under to the next mail found."""
    head = _head_of(raw)
    try:
        mail = _read_guarded(lambda seconds: READER.stub(head, internal_date, seconds),
                             lambda: stub_mail(head, internal_date, reason))
        mail.unreadable = reason
        return mail
    except (UnreadableMail, NotRead) as exc:
        if isinstance(exc, NotRead) and not go_on:
            raise
        print(f"[mail-gateway] not even the header block of a mail could be read "
              f"({type(exc).__name__}: {exc}); its placeholder carries none of it", flush=True)
        return _bare_stub(head, internal_date, reason)


def read_head(head: bytes) -> dict | None:
    """:func:`_head_facts` for the mirror — a header block read as guarded
    as a mail (:func:`_read_guarded`). None when the block cannot be read,
    which is an answer: it has no id but the one of its bytes. Raises
    :class:`NotRead` when the reading did not come back, which is none."""
    try:
        return _read_guarded(lambda seconds: READER.facts(head, seconds),
                             lambda: _head_facts(head))
    except UnreadableMail:
        return None


def _human_size(count: int) -> str:
    size = float(max(0, count))
    if size < 1000:
        return f"{int(size)} bytes"
    for unit in ("kB", "MB"):
        size /= 1000
        if size < 1000:
            return f"{size:.1f} {unit}"
    return f"{size / 1000:.1f} GB"


def _not_mirrored_note(part: Part) -> tuple[bytes, str]:
    """What stands in for an attachment above the part cap: a small text file
    under the attachment's own name that says so. The bubble shows it like any
    file, so the gap is visible without the reader knowing a new shape."""
    name = _ibstore.safe_file_name(part.file_name) or "attachment"
    text = ("This attachment was not mirrored: it is larger than the mirror keeps "
            f"({_human_size(MAIL_MAX_PART_BYTES)}).\n\n"
            f"Name: {name}\n"
            f"Type: {_ibstore.clean_content_type(part.content_type)}\n"
            f"Size: {_human_size(len(part.data))}\n\n"
            "The mail in the mailbox still carries it.\n")
    return text.encode("utf-8"), f"{name}.not-mirrored.txt"


def _oversized_mail_note(size: int, parts: list) -> tuple[bytes, str]:
    """The same for a mail above the mail cap, whose attachments are all left out."""
    names = [_ibstore.safe_file_name(p.file_name) for p in parts]
    listed = "".join(f"  {name}\n" for name in [n for n in names if n][:20])
    text = ("The attachments of this mail were not mirrored: the mail "
            f"({_human_size(size)}) is larger than the mirror keeps "
            f"({_human_size(MAIL_MAX_MAIL_BYTES)}). Its text is complete.\n\n"
            + (f"Attachments seen:\n{listed}\n" if listed else "")
            + "The mail in the mailbox still carries them.\n")
    return text.encode("utf-8"), "attachments.not-mirrored.txt"


def _dropped_parts_note(mail: Mail) -> tuple[bytes, str]:
    """And for the parts of a mail beyond MAX_PARTS (or that could not be read)."""
    listed = "".join(f"  {name}\n" for name in mail.dropped_names)
    text = (f"{mail.parts_dropped} further part(s) of this mail were not mirrored: "
            f"the mirror keeps at most {MAX_PARTS} attachments of one mail, and only "
            "parts it can read. Its text is complete.\n\n"
            + (f"Among them:\n{listed}\n" if listed else "")
            + "The mail in the mailbox still carries them.\n")
    return text.encode("utf-8"), "attachments.not-mirrored.txt"


# ══════════════════════════════════════════════════════════════════════════════
# Mailbox adapter — the ONLY section that speaks IMAP. Everything outside it
# works on this surface, which the tests replace with a fake mailbox:
#
#   folders()                       the mirrored folders' names, by role
#   list_uids(folder, …)            a folder's UIDVALIDITY, UIDNEXT and UIDs
#   fetch(folder, uid, limit)       one mail's raw bytes, flags and arrival time
#   find(folder, message_id)        the UID of a mail, by Message-ID
#   ensure() / close()              the connection's life
#   set_seen() / move()             the next phase's seams; refused here
#
# What an answer means is part of the surface, because the mirror calls a mail
# gone on it. ``None`` is always the server's own word — it answered, and the
# answer was "no such folder" or "no such mail". A server that would not say
# (NO to EXAMINE for any other reason, a refused SEARCH or FETCH, a dropped
# connection) raises MailboxError, and the pass that asked ends there — but
# for the archive folder's own look, whose refusal Mirror._sync sets down as
# "no answer" and goes on without. And where the server answered but the
# answer does not settle it, ``find`` says so (``Matches.whole``).
# ══════════════════════════════════════════════════════════════════════════════

class MailboxError(Exception):
    """The mailbox could not be reached or refused a command. The message is
    what /health reports, so it is written for the operator."""


class _Unsure:
    """See :data:`UNSURE`."""

    def __repr__(self):
        return "UNSURE"

    def __bool__(self):
        raise TypeError("UNSURE is neither found nor absent; compare with `is`")


# "Could not tell": neither a UID nor None. Whoever gets it does nothing with
# the mail this pass — it is not located, and above all not called gone.
UNSURE = _Unsure()
# How many mails whose Message-ID *contains* the one looked for (the server's
# search matches substrings) are compared before the answer is UNSURE.
FIND_CANDIDATES_MAX = 100


class Listing(NamedTuple):
    """One look at a folder. ``uids`` maps each UID to the mail's arrival time
    (INTERNALDATE, epoch seconds) when dates were asked for, else to None."""
    uidvalidity: int
    uidnext: int | None
    uids: dict


class Matches(NamedTuple):
    """The mails a folder holds under one Message-ID: their UIDs, newest
    first — all of them, since an id is its sender's to choose and a folder
    may hold several mails under one — and whether these are all (``whole``).
    Where they are not, "none of these" is no answer: UNSURE."""
    uids: tuple
    whole: bool


class Fetched(NamedTuple):
    """One mail. ``raw`` is cut at the fetch limit when the mail is larger;
    ``size`` is the whole mail's, so the caller can tell. ``uidvalidity`` is
    the folder's as it was opened for the fetch (None: not said): a UID names
    this mail only under that one."""
    raw: bytes
    size: int
    flags: frozenset
    date: float | None
    uidvalidity: int | None = None


_LIST_HEAD_RE = re.compile(rb"^\((?P<flags>[^)]*)\)\s+(?:\"(?:[^\"\\]|\\.)*\"|NIL|\S+)\s*(?P<name>.*)$",
                           re.DOTALL)
_UID_RE = re.compile(rb"\bUID (\d+)")
_SIZE_RE = re.compile(rb"\bRFC822\.SIZE (\d+)")
_RESPONSE_START_RE = re.compile(rb"^\d+ \(")


def _parse_list_entry(item) -> tuple[set, str] | None:
    """``(attributes, folder name)`` of one LIST response item, or None."""
    literal = None
    if isinstance(item, tuple):          # the name came as a literal
        item, literal = item[0], item[1]
    if not isinstance(item, (bytes, bytearray)):
        return None
    m = _LIST_HEAD_RE.match(bytes(item).strip())
    if not m:
        return None
    name = literal if literal is not None else m.group("name").strip()
    if literal is None and name.startswith(b'"') and name.endswith(b'"') and len(name) >= 2:
        name = re.sub(rb"\\(.)", rb"\1", name[1:-1])
    text = name.decode("utf-8", errors="replace")
    try:
        text = _ec.imap_utf7_decode(text)
    except Exception:  # noqa: BLE001 - not modified UTF-7 after all
        pass
    flags = {f.lower() for f in m.group("flags").decode("ascii", errors="replace").split()}
    return flags, text


def resolve_folders(entries, sent: str, archive: str, trash: str) -> dict:
    """Which folder plays each role: ``{inbox, sent, archive, trash}``, a name
    or None each.

    The server's own SPECIAL-USE attributes (RFC 6154) are honoured first —
    they survive a localised or renamed folder — and the configured names are
    the fallback. For the archive, ``\\All`` (Gmail's "All Mail", which is where
    an archived mail lives there) stands in when nothing is ``\\Archive``."""
    usable = [(flags, name) for flags, name in entries
              if "\\noselect" not in flags and "\\nonexistent" not in flags]

    def special(*attributes):
        for attribute in attributes:
            for flags, name in usable:
                if attribute in flags:
                    return name
        return None

    def named(wanted):
        names = [name for _flags, name in usable]
        if wanted in names:
            return wanted
        return next((n for n in names if n.casefold() == wanted.casefold()), None)

    return {
        "inbox": named("INBOX") or "INBOX",
        "sent": special("\\sent") or named(sent),
        "archive": special("\\archive", "\\all") or named(archive),
        "trash": special("\\trash") or named(trash),
    }


def _imap_date(epoch: float) -> str:
    """IMAP's DD-Mon-YYYY for SINCE, a day early: the server compares dates in
    its own zone, and a mail too many costs nothing where one too few is lost."""
    day = datetime.fromtimestamp(max(0.0, epoch - 86400), tz=timezone.utc)
    return f"{day.day:02d}-{_ec._MONTHS[day.month - 1]}-{day.year}"


def _response_text(data) -> str:
    """What the server said beside OK/NO/BAD, as one line for a message."""
    text = b" ".join(bytes(item) for item in data or [] if isinstance(item, (bytes, bytearray)))
    return _clean_line(text.decode("utf-8", errors="replace"), 200)


def _internal_date(envelope: bytes) -> float | None:
    try:
        parsed = imaplib.Internaldate2tuple(envelope)
        stamp = time.mktime(parsed) if parsed else None
    except (ValueError, OverflowError):
        return None
    # (A date before the epoch is a server's placeholder, not an arrival.)
    return stamp if stamp is not None and stamp >= 0 else None


class Mailbox:
    """The IMAP side of one account, read-only.

    email_client.py logs in once per command; this is the first long-running
    IMAP client here, so the connection's life is handled in one place: every
    command runs through :meth:`_run`, which connects on demand, and on a
    dropped or timed-out connection reconnects and tries once more — servers
    close an idle session after half an hour, and that is not an outage.
    Folders are opened with EXAMINE, never SELECT, and bodies are read with
    BODY.PEEK: this phase cannot change a flag even by accident."""

    def __init__(self, cfg, timeout: float = MAIL_IMAP_TIMEOUT):
        self._cfg = cfg
        self._timeout = timeout
        self._conn = None
        self._selected = None
        self._validity = None     # the selected folder's UIDVALIDITY
        self._lock = threading.RLock()

    # -- the connection --------------------------------------------------------

    def _connect(self):
        cfg = self._cfg
        try:
            # Like email_client.imap_connect, with the one thing a process that
            # never exits needs: a socket timeout, so a dead peer is an error
            # and not a poll thread asleep for good.
            conn = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port,
                                     ssl_context=ssl.create_default_context(),
                                     timeout=self._timeout)
        except (OSError, imaplib.IMAP4.error) as exc:
            raise MailboxError(
                f"cannot reach the IMAP server {cfg.imap_host}:{cfg.imap_port}: {exc}") from exc
        try:
            conn.login(cfg.user, cfg.password)
        except imaplib.IMAP4.error as exc:
            try:
                conn.shutdown()
            except Exception:  # noqa: BLE001
                pass
            raise MailboxError(
                f"IMAP login failed for {cfg.user}: {exc} — check the address and "
                f"EMAIL_PASS (an app password, where the provider issues them)") from exc
        except OSError as exc:
            raise MailboxError(f"the IMAP connection failed during login: {exc}") from exc
        return conn

    def close(self) -> None:
        with self._lock:
            conn, self._conn, self._selected = self._conn, None, None
        if conn is not None:
            try:
                conn.shutdown()
            except Exception:  # noqa: BLE001 - it is being dropped either way
                pass

    def _run(self, command):
        with self._lock:
            for attempt in (1, 2):
                if self._conn is None:
                    self._conn = self._connect()
                    self._selected = None
                try:
                    return command(self._conn)
                except MailboxError:
                    raise
                except (imaplib.IMAP4.abort, OSError) as exc:
                    # The connection is gone or in an unknown state (a timeout
                    # mid-answer leaves half a response behind): drop it.
                    self.close()
                    if attempt == 2:
                        raise MailboxError(f"the IMAP connection failed: {exc}") from exc
                except imaplib.IMAP4.error as exc:
                    # The server answered and said no; the connection is fine.
                    raise MailboxError(f"the IMAP server refused a command: {exc}") from exc

    def ensure(self) -> None:
        """Make sure the mailbox answers (NOOP), reconnecting when it does not."""
        self._run(lambda conn: conn.noop())

    # -- reading ---------------------------------------------------------------

    def _examine(self, conn, folder: str, fresh: bool = False) -> bool:
        """Open ``folder`` read-only. False only when the server says there is
        no such folder (the NONEXISTENT response code, RFC 5530).

        Any other NO is not an answer about the folder: servers refuse a
        folder that is there — unavailable, in use, throttled — and the name
        came from a LIST taken moments ago. Read as "absent", such a refusal
        would have every mail in the folder called gone; it raises instead."""
        if self._selected == folder and not fresh:
            return True
        self._selected = self._validity = None
        typ, data = conn.select(_ec._quote(folder), readonly=True)
        if typ == "OK":
            # (Announced on opening only: what every UID read here is under.)
            self._selected, self._validity = folder, self._number(conn, "UIDVALIDITY")
            return True
        said = _response_text(data)
        if "[NONEXISTENT]" in said.upper():
            return False
        raise MailboxError(f"the IMAP server would not open {folder!r}: {said or typ}")

    @staticmethod
    def _number(conn, name: str) -> int | None:
        _typ, data = conn.response(name)
        try:
            return int(data[0]) if data and data[0] is not None else None
        except (ValueError, TypeError):
            return None

    def folders(self) -> dict:
        def command(conn):
            typ, data = conn.list()
            if typ != "OK":
                raise MailboxError(f"the IMAP server refused LIST: {data}")
            entries = [entry for entry in map(_parse_list_entry, data or []) if entry]
            return resolve_folders(entries, self._cfg.sent_folder,
                                   MAIL_ARCHIVE_FOLDER, MAIL_TRASH_FOLDER)
        return self._run(command)

    def list_uids(self, folder: str, *, since: float | None = None,
                  first_uid: int | None = None, dates: bool = False) -> Listing | None:
        """The folder's UIDVALIDITY, UIDNEXT and UIDs — all of them, or those
        that arrived on or after ``since`` (epoch; whole days, a day early),
        or those from ``first_uid`` up. None when the server says the folder
        does not exist."""
        def command(conn):
            # Opened afresh every time: UIDVALIDITY is only announced then.
            if not self._examine(conn, folder, fresh=True):
                return None
            validity = self._validity
            uidnext = self._number(conn, "UIDNEXT")
            if validity is None:
                raise MailboxError(f"the IMAP server gave no UIDVALIDITY for {folder!r}")
            criteria = []
            if since is not None:
                criteria += ["SINCE", _imap_date(since)]
            if first_uid:
                criteria += ["UID", f"{int(first_uid)}:*"]
            typ, data = conn.uid("search", None, *(criteria or ["ALL"]))
            if typ != "OK":
                raise MailboxError(f"the IMAP server refused a search of {folder!r}: {data}")
            uids = sorted({int(token) for chunk in data or [] if chunk
                           for token in chunk.split()})
            # "n:*" always matches the newest mail, even one below n.
            uids = [uid for uid in uids if not first_uid or uid >= first_uid]
            found = dict.fromkeys(uids)
            for start in range(0, len(uids) if dates else 0, 500):
                chunk = ",".join(str(uid) for uid in uids[start:start + 500])
                typ, data = conn.uid("fetch", chunk, "(INTERNALDATE)")
                if typ != "OK":
                    # The UIDs stand; a date that is None is "not known",
                    # which no caller reads as old or as new.
                    continue
                for item in data or []:
                    line = item[0] if isinstance(item, tuple) else item
                    m = _UID_RE.search(line or b"")
                    if m and int(m.group(1)) in found:
                        found[int(m.group(1))] = _internal_date(line)
            return Listing(validity, uidnext, found)
        return self._run(command)

    def fetch(self, folder: str, uid: int, limit: int | None = None) -> Fetched | None:
        """One mail by UID, at most ``limit`` bytes of it. None when the
        server answers that it is not there (expunged since it was listed, or
        the folder with it); a fetch the server refuses raises."""
        def command(conn):
            if not self._examine(conn, folder):
                return None
            body = "BODY.PEEK[]" + (f"<0.{int(limit)}>" if limit else "")
            typ, data = conn.uid("fetch", str(int(uid)),
                                 f"(UID FLAGS INTERNALDATE RFC822.SIZE {body})")
            if typ != "OK":
                raise MailboxError(f"the IMAP server refused to fetch a mail from {folder!r}: {data}")
            raw, envelope, tail_due = None, b"", False
            for item in data or []:
                if isinstance(item, tuple):
                    m = _UID_RE.search(item[0] or b"")
                    if m and int(m.group(1)) != int(uid):
                        tail_due = False
                        continue  # an unsolicited update about another mail
                    envelope, raw, tail_due = item[0] or b"", item[1], True
                elif tail_due and isinstance(item, (bytes, bytearray)):
                    # What followed the literal: ")" or, on some servers, the
                    # FLAGS. Anything that opens a response of its own is an
                    # unsolicited update, and its flags are another mail's.
                    if not _RESPONSE_START_RE.match(item):
                        envelope += b" " + bytes(item)
                    tail_due = False
            if raw is None:
                return None
            size = _SIZE_RE.search(envelope)
            flags = frozenset(flag.decode("ascii", errors="replace")
                              for flag in imaplib.ParseFlags(envelope))
            return Fetched(bytes(raw), int(size.group(1)) if size else len(raw),
                           flags, _internal_date(envelope), self._validity)
        return self._run(command)

    def find(self, folder: str, message_id: str) -> Matches:
        """The mails with this Message-ID in ``folder``: every one of them
        (see :class:`Matches`) — none, and ``whole``, when the server answers
        that the folder holds no such mail, or is no folder.

        The server's HEADER search matches substrings, so each candidate's own
        header is read back and compared whole. A candidate whose header the
        server will not hand over is not "another mail" — that raises — and
        with more look-alikes than are compared (anyone can send mails whose
        ids contain a known one) the matches among the newest are still
        matches, and are not all there may be."""
        def command(conn):
            if not message_id or not self._examine(conn, folder):
                return Matches((), True)
            typ, data = conn.uid("search", None, "HEADER", "Message-ID",
                                 _ec._quote_mid(message_id))
            if typ != "OK":
                raise MailboxError(f"the IMAP server refused a search of {folder!r}: "
                                   f"{_response_text(data) or typ}")
            candidates = sorted({int(token) for chunk in data or [] if chunk
                                 for token in chunk.split()}, reverse=True)
            found = []
            for uid in candidates[:FIND_CANDIDATES_MAX]:
                typ, data = conn.uid("fetch", str(uid),
                                     "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
                if typ != "OK":
                    raise MailboxError(f"the IMAP server refused to read a header in "
                                       f"{folder!r}: {_response_text(data) or typ}")
                for item in data or []:
                    if not isinstance(item, tuple):
                        continue
                    header = email.message_from_bytes(item[1] or b"")
                    if _threads.normalize_id(_raw_header(header, "Message-ID")) == message_id:
                        found.append(uid)
                        break
            return Matches(tuple(found), len(candidates) <= FIND_CANDIDATES_MAX)
        return self._run(command)

    # -- the next phase's seams ------------------------------------------------
    # The mailbox follows the chat from phase 2 on: a reply or "mark as read"
    # sets \Seen, archiving moves a thread's INBOX mails to the archive folder,
    # deleting moves them to Trash. Their place is here, in the adapter, and
    # until then they refuse — nothing in this phase may write to the mailbox.

    def set_seen(self, folder: str, uids) -> None:
        raise MailboxError("the mail mirror is read-only")

    def move(self, folder: str, uid: int, to_folder: str) -> int | None:
        raise MailboxError("the mail mirror is read-only")


# ══════════════════════════════════════════════════════════════════════════════
# End mailbox adapter. Everything below is IMAP-agnostic.
# ══════════════════════════════════════════════════════════════════════════════

# ── The mailbox index ─────────────────────────────────────────────────────────
# Where each mail lives, which record mirrors it, and what is still owed to the
# rail. A cache: the ledger says what was mirrored and the mailbox what exists,
# and Mirror._prepare rebuilds this from the first and finds the second again.
#
#   folders    one row per mirrored role: its name, UIDVALIDITY, a generation
#              that is bumped whenever the UIDs stop meaning what they meant,
#              the backfill ceiling (UIDs at or below it are old mail) and the
#              live high-water mark (every UID up to it has been looked at; in
#              the archive, which is not read: up to it no mail can have
#              passed through INBOX unseen).
#   locations  (role, generation, UID) → Message-ID. `skipped` says why a UID
#              has no record of its own ("news"; "draft", in the archive;
#              "other" for a mail that came under the id of a placeholder
#              and is not the copy that was made from — Mirror._mirror_one),
#              so it is not read again. "again" is the opposite, a note
#              with no id: nothing is known of the mail at this UID, and it
#              is to be read (Mirror._read_noted) — what lay beside a
#              placeholder that is gone, what an older build marked
#              "unreadable".
#              Rows of an older generation are stale: mails to find again.
#   mails      one row per mirrored mail: its record file, direction, chat and
#              the facts a Thread answer needs. An empty `record` is a claim:
#              a ledger write begun and not confirmed (Mirror._write). It has
#              no place, and outlives no pass: a stop leaves it to the next
#              start (_prepare). `arrived` is
#              when the server received the mail (its `ts` is the sender's
#              Date where that is earlier), as the record keeps it
#              (kb:arrivedAt); NULL for a record written before it did.
#   claims     what the rail is owed for a write begun — of a claim, or of a
#              record written again in its own place — once its record
#              stands: was the mail a backfill's, was it \Seen. Gone when the
#              write is confirmed, or given up.
#   lost       the ids of rows whose record is gone from the ledger (restored
#              from an older backup, a file lost), with what the row said of
#              its sender, direction, chat and arrival — HINTS for finding
#              the mail again by its Message-ID (Mirror._read_lost), never
#              knowledge of it: nothing here is a record, a place or a claim,
#              and nothing but _read_lost reads it. `tried`, `tries` and
#              `due` are the looking's own: when it was last tried, how often
#              in vain, and when it is tried next.
#   notes      who authored a mail that will turn up in Sent (/outbound-note).
#   outbox     rail events not yet taken by the web gateway, in order.
#
# A change the rail must hear of and the event that says it are written in ONE
# transaction (MailboxIndex.atomic): there is no moment at which the index
# knows and the outbox does not. The ledger is another volume and cannot be
# in that transaction; it is always written first, and Mirror._prepare
# announces what a stop in between left unsaid.

INDEX_SCHEMA_VERSION = 1
_INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS folders (
    role TEXT PRIMARY KEY, name TEXT NOT NULL, uidvalidity INTEGER NOT NULL,
    gen INTEGER NOT NULL, ceiling INTEGER NOT NULL, hwm INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS locations (
    role TEXT NOT NULL, gen INTEGER NOT NULL, uid INTEGER NOT NULL,
    message_id TEXT, skipped TEXT,
    PRIMARY KEY (role, gen, uid)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS locations_mid ON locations (message_id);
CREATE TABLE IF NOT EXISTS mails (
    message_id TEXT PRIMARY KEY, record TEXT NOT NULL, direction TEXT NOT NULL,
    chat TEXT NOT NULL, ts REAL NOT NULL, sender TEXT, sender_name TEXT,
    recipients TEXT, cc TEXT, removed INTEGER NOT NULL DEFAULT 0, arrived REAL
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS mails_chat ON mails (chat, ts);
CREATE TABLE IF NOT EXISTS claims (
    message_id TEXT PRIMARY KEY, backfill INTEGER NOT NULL, seen INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS notes (
    message_id TEXT PRIMARY KEY, author TEXT NOT NULL, created REAL NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS lost (
    message_id TEXT PRIMARY KEY, sender TEXT, direction TEXT, chat TEXT, arrived REAL,
    noticed REAL NOT NULL, tried REAL, tries INTEGER NOT NULL DEFAULT 0,
    due REAL NOT NULL DEFAULT 0
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS outbox (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, payload TEXT NOT NULL
);
"""


class MailboxIndex:
    """The sqlite file behind the mirror: one connection, one lock, shared by
    the poll thread (the only writer of everything but notes) and the HTTP
    threads. Autocommit; writes that must stand or fall together go through
    :meth:`atomic`."""

    def __init__(self, path):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_INDEX_SCHEMA)
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                self._conn.execute(f"PRAGMA user_version = {INDEX_SCHEMA_VERSION}")
            elif version != INDEX_SCHEMA_VERSION:
                raise RuntimeError(f"mailbox index {path}: schema {version}, "
                                   f"expected {INDEX_SCHEMA_VERSION}")
            # An index made before `arrived` was kept gains the column, empty.
            if "arrived" not in {row["name"] for row in
                                 self._conn.execute("PRAGMA table_info(mails)")}:
                self._conn.execute("ALTER TABLE mails ADD COLUMN arrived REAL")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def run(self, sql: str, args=()) -> int:
        with self._lock:
            return self._conn.execute(sql, args).rowcount

    def atomic(self, statements) -> None:
        """Run ``[(sql, args), …]`` as one transaction: all of it, or none.
        (``None`` entries are skipped, so a caller can list an optional one.)"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in statements:
                    if statement is not None:
                        self._conn.execute(*statement)
                self._conn.execute("COMMIT")
            except BaseException:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass   # sqlite already rolled it back (a full disk does)
                raise

    def rows(self, sql: str, args=()) -> list:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def one(self, sql: str, args=()):
        with self._lock:
            return self._conn.execute(sql, args).fetchone()

    def get(self, key: str, default=None):
        row = self.one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else default

    def set(self, key: str, value) -> None:
        self.run("INSERT INTO meta (key, value) VALUES (?, ?) "
                 "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, str(value)))


# ── The rail ──────────────────────────────────────────────────────────────────
# Three events go to the web gateway: a mirrored mail (the existing
# /internal/chats/inbound, with mail's optional fields), a thread merge and a
# removal. They are posted here rather than through chat_ingest.notify_chat_event
# because that function's field list is fixed and its answer says nothing about
# why a call failed — and a queue that must not lose an event needs to know
# whether to retry it. The URL and the token are chat_ingest's.

def _rail_url(kind: str) -> str:
    base = _chats.CHATS_INGEST_URL
    if kind == "inbound":
        return base
    head, _slash, _last = base.rstrip("/").rpartition("/")
    return f"{head}/{kind}"


def _rail_send(kind: str, payload: dict) -> str:
    """POST one event. ``"ok"`` — taken; ``"drop"`` — refused as malformed, a
    repeat would be refused again; ``"later"`` — this event cannot be taken
    yet, others may; ``"down"`` — the web gateway is not answering."""
    request = urllib.request.Request(
        _rail_url(kind), data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json",
                 "X-Conversation-Backend-Token": _chats.CHATS_INGEST_TOKEN})
    try:
        with urllib.request.urlopen(request, timeout=MAIL_RAIL_TIMEOUT) as response:
            response.read()
        return "ok"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            print(f"[mail-gateway] the chats rail refused the token (HTTP {exc.code}); "
                  f"check CHATS_INGEST_TOKEN on both sides", flush=True)
            return "down"
        if exc.code in (404, 408, 429) or exc.code >= 500:
            # A merge or removal an older web gateway does not know yet must
            # not hold the mails behind it back.
            return "down" if kind == "inbound" else "later"
        print(f"[mail-gateway] the chats rail refused a {kind} event "
              f"(HTTP {exc.code}); dropped", flush=True)
        return "drop"
    except Exception as exc:  # noqa: BLE001 - unreachable, timed out
        print(f"[mail-gateway] the chats rail did not answer ({exc}); will retry", flush=True)
        return "down"


# The verdict every mirrored mail is offered with in this phase: held, nobody's
# VIP, nobody's instruction — so the rail pushes nothing and starts no turn.
MIRROR_GATE = {"forward": False, "vip": False, "instructed": False, "reason": "mirror"}


def _delivered_on_write(direction: str) -> bool:
    """Whether a mail's record starts out ``delivered``.

    THE read-only phase's handover decision, in one place: every record is
    written delivered, because nothing is owed for it — e-mail triage still
    owns the handling of mail, and the rail is offered each mail without a
    handover. The gate phase replaces this with the messengers' flow: persist
    ``delivered: false``, offer the rail a handover, and flip the flag only on
    its acceptance (docs/mail-chats.md, "The gate")."""
    return True


# ── The mirror ────────────────────────────────────────────────────────────────

# The mirror's surroundings failing: the mailbox, a disk, an index file that
# cannot be read or written (locked, full, corrupt). Told by their type, and
# passed on as they are, whatever mail was in hand. (Anything else that fails
# while a mail is written ends the pass just the same — as a MirrorFault that
# names the mail: Mirror._ingest. Writing is never a mail's fault.)
_ENVIRONMENT = (MailboxError, OSError, sqlite3.OperationalError, sqlite3.InternalError)


def _environment(exc: BaseException) -> bool:
    """Whether what a pass raised is the mirror's surroundings failing (see
    _ENVIRONMENT). sqlite reports an index file that is corrupt, or no
    database at all, as a plain DatabaseError; its subclasses for what one
    value can bring out (an integrity or data error, a parameter it cannot
    bind) are not that."""
    return isinstance(exc, _ENVIRONMENT) or type(exc) is sqlite3.DatabaseError


# ── What the bookkeeping must keep ────────────────────────────────────────────
# A mail is on record under an id (`mails`), lies at UIDs (`locations`), and
# its row is a record, a placeholder or a tombstone. Each rule below
# was a case first — one more mark, one more pass — and the cases kept
# coming, each at the edge of the state the last one added. These are the
# rules those cases were instances of. Whatever touches the bookkeeping keeps
# them; tests/test_mail_gateway_mime.py walks them as such, apart from the
# cases.
#
#   I1  AN ANSWER THAT WAS NOT OBTAINED IS NEVER AN ANSWER. A reading that
#       was late, a reader process that ended, a folder that could not say:
#       each reaches whoever asked as "could not tell" (NotRead, UNSURE,
#       Matches.whole) and as nothing else. It is not remembered, it calls
#       no mail gone, and it decides nothing for good — not which mail lies
#       at a UID, not a mail's direction. An answer is what the server said,
#       or what a mail's bytes do to the reader at every reading.
#       (_read_guarded, read_head, Mirror._find, Mirror._ingest)
#
#   I2  A RECORD IS REPLACED OR BURIED ONLY ON EVIDENCE ABOUT ITS OWN MAIL.
#       Never because another mail under its Message-ID is in the way: ids
#       are their senders' to choose, a folder may hold several mails under
#       one, and ALL of them are looked at (Matches, Mirror._adopt). A
#       placeholder's own mail is the one whose header block its record
#       keeps (Mirror._own_copy): only that mail takes its place, and only
#       that mail's absence from every folder buries it. What marks a UID as
#       another mail's ("other") is read off the mail there, never carried
#       over from a UID that meant it once — and a UID is that mail only
#       under the UIDVALIDITY it was recorded under (Fetched.uidvalidity,
#       Mirror._ingest). Where one mail takes the record of another id, the
#       header block is the evidence: the one that record keeps, or the
#       one whose bytes the id was made of (_bare_id).
#
#   I3  A RECORD STANDS UNTIL WHAT REPLACES IT DOES. A record written again
#       (a placeholder that reads now, a mail on record from its INBOX copy
#       whose Sent copy shows, a tombstone whose mail is back) is written in
#       its own place, in one rename (Mirror._write): wherever that stops
#       or fails, the mail is the record it was, followed and tombstoned
#       like any, and read again later. Nothing is dropped first, so no row
#       ever stands for a mail in a record's stead — and what a row knows
#       without a record is no knowledge: a row whose record is gone from
#       the ledger goes, with its places, and what it said (id, sender,
#       direction) is kept as HINTS for finding the mail again (`lost`,
#       Mirror._prepare). Its mail is looked for by its Message-ID in every
#       folder, and one mail found is read as a mail read the first time
#       where it lies — the one From the sender the lost record had, that
#       came first — the user's own again only where it is From that sender
#       and the record was the user's: evidence of the mail itself, never of
#       the list (Mirror._read_lost, Mirror._read_found). A mail that cannot
#       be told yet stays looked for, and /health counts it. Nothing else
#       is carried over to whatever mail is found under its id.
#
#   I4  A FAULT DELAYS ONLY THE WORK THAT FAILED. Following the folders —
#       new mail, vanished mail — keeps the poll interval whatever the rest
#       of a turn does; what failed is tried again less and less often, by
#       itself (_loop_step). And no mail is set aside over a fault of the
#       mirror's own (Mirror._ingest); a mail that cannot be told now holds
#       back none behind it — it is a placeholder where the folders are
#       followed, and a lost record's id that is looked for again later
#       (Mirror._read_lost).
#
# The states of a mail's row, and there are no others:
#
#   record != '', removed = 0    on record — a placeholder where the record
#                                says so (Mirror._placeholders)
#   record != '', removed = 1    a tombstone
#   either, with a `claims` row  its record being written again in its own
#                                place, inside Mirror._write (a stop there
#                                is announced by _prepare, whichever record
#                                stands)
#   record  = ''                 a claim: its first record being written,
#                                inside Mirror._write and nowhere else (a
#                                stop there is put right by _prepare)
#
# (A row whose record file is gone is none of these: it is the ledger
# restored or changed behind the mirror's back, and the next _prepare moves
# what the row said into `lost` — Mirror._write, meeting one, writes the
# record as the first time and has the index matched again.) And of a UID's:
# a place of the mail under its id (skipped NULL); a UID with no record of
# its own ("news", "draft", and "other" beside a placeholder); a UID to be
# read with nothing known of it ("again": what lay beside a placeholder that
# is gone, an older build's "unreadable"). An id in `lost` is no state of a
# row or of a UID: it is a mail to be looked for.

class Mirror:
    """One account's mailbox mirrored into the ledger.

    ``poll`` is one pass: follow the folders (new mail, vanished mail), look
    again for mails that lost their place and for those whose record the
    ledger lost, read again what is to be, backfill a batch, and hand the
    rail what it is owed. Everything it needs to resume
    is in the two indexes; a poll that fails midway is simply run again."""

    def __init__(self, mailbox, clock=time.time):
        self.mailbox = mailbox
        self._clock = clock
        MAIL_STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.index = self._open(MAIL_STATE_DIR / "mailbox.sqlite", MailboxIndex)
        self.threads = self._open(MAIL_STATE_DIR / "threads.sqlite", _threads.ThreadIndex)
        self.fault = None         # why the mailbox cannot be mirrored as configured
        self.notice = None        # for /health while the mirror works
        # One pass at a time, and no note corrected while one runs: both
        # rewrite ledger records, and a record has one writer at a time.
        self._lock = threading.RLock()
        self._folders: dict = {}
        # What the last look at the folders allows. INBOX and Sent were read:
        # old mail may be read (the backfill needs no other folder). Every
        # folder a mail could be in answered as well: a mail may be looked
        # for again, and called gone.
        self._read = False
        self._settled = False
        self._archive_said = None   # the archive's refusal, as last logged
        self._plan = None         # the backfill still to do, oldest first
        self._merges_due: set = set()
        self._reconcile = False   # an index write failed after its ledger write
        # Where the two "look again" passes stopped, so a mail that cannot be
        # settled yet does not keep the ones behind it waiting.
        self._stale_after = None
        self._settle_after = None
        # (role, generation) → {UID: the id read off its header}: a search by
        # reading headers (see _find) goes on where it stopped, and the next
        # mail looked for reads none of them again.
        self._peeked: dict = {}
        self._scan_budget = SCAN_BUDGET
        # The mails mirrored as placeholders (kept as they are written; read
        # off the ledger by _prepare), and their reading again.
        self._placeholders: set = set()
        self._retry_at = 0.0          # due at the first pass
        self._retry_queue: list = []
        self._retry_round = 0
        self._retried: dict = {}      # placeholder → (re-reads that changed nothing, next round)
        self._retry_spent = 0.0       # seconds of this round's re-reading
        self._made: dict = {}         # why → placeholders made since this start
        self._prepare()

    @staticmethod
    def _open(path: Path, kind):
        try:
            return kind(path)
        except (RuntimeError, sqlite3.DatabaseError) as exc:
            # Another schema, or not a database: both indexes are caches of the
            # ledger, so the cure is the same — start over and rebuild.
            print(f"[mail-gateway] {path.name} is unusable ({exc}); rebuilding it", flush=True)
            path.unlink(missing_ok=True)
            return kind(path)

    # -- startup: the indexes agree with the ledger ----------------------------

    def _ledger(self) -> list:
        """This account's mail records, one per Message-ID, oldest first."""
        seen, out, repeats = set(), [], []
        for path, fields in _ibstore.iter_records(INBOUND_STORE_DIR):
            message_id = fields.get("message_id")
            if (fields.get("channel") != CHANNEL or (fields.get("account") or "") != ACCOUNT
                    or not message_id or not fields.get("chat")):
                continue
            if message_id in seen:
                repeats.append(path.name)
                continue
            seen.add(message_id)
            out.append((path, fields))
        if repeats:
            # Nothing writes these any more; one an older build left, or put
            # there by hand, is not this mirror's to follow or to delete.
            print(f"[mail-gateway] {len(repeats)} ledger record(s) repeat a mail already "
                  f"on record and are left alone: {', '.join(repeats[:5])}"
                  + (" …" if len(repeats) > 5 else ""), flush=True)
        return out

    @staticmethod
    def _record_time(fields: dict) -> float:
        stamp = fields.get("sent_at") or fields.get("received_at")
        return _threads.to_epoch(stamp) or 0.0

    def _thread_time(self, fields: dict) -> float:
        """The time a record's mail is threaded by: when the server received
        it — as _write hands it to the thread index, so an index rebuilt from
        the ledger ranks the chats as the live one did, by a time no sender
        wrote. (A record from before the arrival was kept has only its own
        time, which is the sender's Date where that is earlier.)"""
        return _threads.to_epoch(fields.get("arrived_at")) or self._record_time(fields)

    def _adoption(self, path: Path, fields: dict) -> tuple:
        """The statement that makes the index say what a ledger record says."""
        outbound = fields.get("type") == _ibstore.T_OUTBOUND
        sender = fields.get("sender") or ""
        return (
            "INSERT OR REPLACE INTO mails (message_id, record, direction, chat, ts, "
            "sender, sender_name, recipients, cc, removed, arrived) VALUES "
            "(?,?,?,?,?,?,?,?,?,?, COALESCE(?, (SELECT arrived FROM mails WHERE message_id = ?)))",
            (fields["message_id"], path.name, "out" if outbound else "in", fields["chat"],
             # (An inbound record of a mail with no readable From says "unknown".)
             self._record_time(fields), sender if "@" in sender else "",
             fields.get("sender_name"),
             json.dumps([[a, None] for a in fields.get("recipients") or []]),
             json.dumps([[a, None] for a in fields.get("cc") or []]),
             1 if fields.get("removed_at") else 0,
             # (An older record does not say when its mail arrived: what the
             # index knows of it is kept.)
             _threads.to_epoch(fields.get("arrived_at")), fields["message_id"]))

    def _prepare(self) -> None:
        """Make both indexes agree with the ledger, before the mailbox is asked
        anything. The ledger is the record of what was mirrored; the indexes
        are caches of it and may be missing (a lost state volume), behind (an
        ingest a crash cut short, a volume restored from a backup) or ahead (a
        record that is gone, a ledger restored from a backup). Reading the
        whole ledger costs a start a second or two per ten thousand mails, and
        buys never recording a mail twice.

        It is also where the rail gets what a stop between a ledger write and
        the index left unsaid. The ledger is always written first, so such a
        stop leaves a row that disagrees with its record in one of three ways
        — a write whose record stands and was not confirmed (a claim, or a
        record written again in its place, with its flags in `claims`), a
        row under a chat key its record no longer has, a live row whose
        record is a tombstone — and each is put right in one transaction
        with its event. At least once: a volume restored from a backup
        leaves the same traces, and the web gateway takes a repeat of any of
        the three as a no-op."""
        ledger = self._ledger()
        by_id = {fields["message_id"]: (path, fields) for path, fields in ledger}
        # A placeholder is told by its record: the text every one carries,
        # and no body of its own (see UNREADABLE_TEXT, _retry_set_aside).
        self._placeholders = {
            mid for mid, (_path, f) in by_id.items()
            if f.get("text") == UNREADABLE_TEXT and not f.get("removed_at")
            and not f.get("body_part") and not f.get("html_part")}
        if ledger and (not self.threads.chats()
                       or any(self.threads.chat_of(mid) is None for mid in by_id)):
            # The ledger's own chat keys are honoured; keys the headers prove
            # to be one thread come back as merges due, below.
            self.threads.rebuild([
                (f["message_id"],
                 list(dict.fromkeys((f.get("references") or "").split()
                                    + [f.get("in_reply_to") or ""])),
                 f["chat"], self._thread_time(f), f.get("subject_line"))
                for _path, f in ledger])
            print(f"[mail-gateway] thread index rebuilt from {len(ledger)} ledger records",
                  flush=True)
        rows = {row["message_id"]: row for row in self.index.rows(
            "SELECT message_id, record, chat, removed FROM mails")}
        begun = {row["message_id"] for row in self.index.rows("SELECT message_id FROM claims")}
        unknown, owed, claimed, merged, removed = [], [], [], {}, {}
        for mid, (path, fields) in by_id.items():
            row = rows.get(mid)
            gone = bool(fields.get("removed_at"))
            if row is None or row["record"] not in ("", path.name):
                # Not indexed — every record, after a lost volume — or indexed
                # under a file that is not this one. The rail heard of the
                # mail long ago; its place is looked for again. (Unless a
                # write of it was begun: a record whose file was gone is
                # written under a name of its own — see _write.)
                unknown.append(self._adoption(path, fields))
                if row is not None and mid in begun:
                    claimed.append(mid)
                continue
            if not row["record"] or mid in begun:
                # Its write was begun, and the record stands: the new one,
                # or — a stop before the rename — the one it was to replace.
                # Either way the index is told what the ledger says.
                claimed.append(mid)
            elif row["chat"] == fields["chat"] and bool(row["removed"]) == gone:
                continue
            else:
                # Re-keyed in the ledger but not in the index: a merge never
                # announced. (The other way round — an older ledger — the
                # thread index disagrees with the record too, and the merge
                # is simply due again, below.)
                if row["chat"] != fields["chat"] \
                        and self.threads.chat_of(mid) == fields["chat"]:
                    merged.setdefault(fields["chat"], set()).add(row["chat"])
                # A tombstone the index holds for a live mail: a removal never
                # announced. (A live record the index holds for gone is looked
                # for again, and tombstoned again if no folder has it.)
                if gone and not row["removed"]:
                    removed.setdefault(fields["chat"], []).append(mid)
            owed.append(self._adoption(path, fields))
        self.index.atomic(unknown)
        for mid in claimed:
            flags = self.index.one("SELECT backfill, seen FROM claims WHERE message_id = ?",
                                   (mid,))
            owed.append(("DELETE FROM claims WHERE message_id = ?", (mid,)))
            if by_id[mid][1].get("removed_at"):
                continue   # a tombstone stands still, which the rail was told of
            try:
                # (A claim with no flags is an older build's: quiet is the
                # safe reading of it.)
                owed.append(self._event("inbound", self._event_of_record(
                    by_id[mid][1], backfill=bool(flags["backfill"]) if flags else True,
                    seen=bool(flags["seen"]) if flags else False)))
            except Exception as exc:  # noqa: BLE001 - the record stands either way
                print(f"[mail-gateway] could not announce {mid!r} to the rail: {exc}",
                      flush=True)
        for into, absorbed in merged.items():
            owed.append(self._event("merge", {"channel": CHANNEL, "account": ACCOUNT,
                                              "from": sorted(absorbed), "into": into}))
        for chat, message_ids in removed.items():
            live = sum(1 for _path, f in by_id.values()
                       if f["chat"] == chat and not f.get("removed_at"))
            owed.append(self._event("removed", {"channel": CHANNEL, "account": ACCOUNT,
                                                "chat": chat, "message_ids": message_ids,
                                                "live": live}))
        self.index.atomic(owed)
        # Rows whose record is not in the ledger: a claim whose write never
        # reached the ledger, a record gone from it since (restored from a
        # backup, a file lost). Such a row knows nothing that can be trusted
        # of whatever mail is found under its id: it goes, with its places.
        # A claim just goes (its mail is above its folder's mark still, or
        # in the work that wrote it). Of a record that is gone, what the row
        # said is kept in `lost` as hints for finding its mail again by its
        # Message-ID (_read_lost) — and as nothing else.
        lost = [mid for mid in rows if mid not in by_id]
        noticed = self._clock()
        hints = []
        for mid in lost:
            row = self.index.one("SELECT * FROM mails WHERE message_id = ?", (mid,))
            if row["record"]:
                hints.append((
                    "INSERT OR IGNORE INTO lost (message_id, sender, direction, chat, "
                    "arrived, noticed) VALUES (?, ?, ?, ?, ?, ?)",
                    (mid, row["sender"] or None, row["direction"], row["chat"],
                     row["arrived"] if row["arrived"] is not None else row["ts"], noticed)))
            hints.append(("DELETE FROM mails WHERE message_id = ?", (mid,)))
        # A place under an id that has no row (as an older build could
        # leave one) says no more than that id: it is a hint like these.
        hints += [
            ("INSERT OR IGNORE INTO lost (message_id, noticed) SELECT DISTINCT message_id, ? "
             "FROM locations WHERE message_id IS NOT NULL AND (skipped IS NULL OR "
             "skipped = 'other') AND message_id NOT IN (SELECT message_id FROM mails)",
             (noticed,)),
            ("DELETE FROM locations WHERE message_id IS NOT NULL AND (skipped IS NULL OR "
             "skipped = 'other') AND message_id NOT IN (SELECT message_id FROM mails)", ())]
        self.index.atomic(hints)
        # Places that say nothing true are noted to be read again, where
        # they are (_read_noted): what lay beside a placeholder that is gone
        # — buried by a pass that was stopped before the index heard of it
        # (_bury) — and the mark older builds put on a mail they could not
        # write even a placeholder for: no mail is set aside over a failed
        # write any more (_ingest).
        noted = self.index.run(
            "UPDATE locations SET message_id = NULL, skipped = 'again' WHERE "
            "skipped = 'unreadable' OR (skipped = 'other' AND message_id IN "
            "(SELECT message_id FROM mails WHERE removed = 1))")
        self.index.run("DELETE FROM claims WHERE message_id NOT IN "
                       "(SELECT message_id FROM mails)")
        if unknown or owed or lost or noted:
            print(f"[mail-gateway] mailbox index reconciled with the ledger: "
                  f"{len(unknown)} record(s) adopted, {len(lost)} row(s) with no record "
                  f"dropped (their mails looked for again: {self.lost()} in all), "
                  f"{noted} place(s) to be read again; "
                  f"announced to the rail: {len(claimed)} mail(s), {len(merged)} merge(s), "
                  f"{sum(map(len, removed.values()))} removal(s)", flush=True)
        # Where the thread index and the records disagree, a merge was begun
        # and not finished (or the rebuild just found one).
        for row in self.index.rows("SELECT message_id, chat FROM mails"):
            chat = self.threads.chat_of(row["message_id"])
            if chat and chat != row["chat"]:
                self._merges_due.add(row["chat"])

    def _retry_set_aside(self, policy, limit: int | None) -> bool:
        """Give the mails mirrored as placeholders another reading. True
        while some of this round are left for the next pass.

        At start, and every SET_ASIDE_RETRY_SECONDS after it. What made a
        mail unreadable may be this program, or a moment it was in (memory,
        for a large mail), as well as the mail: a fault that turned good mail
        into placeholders must outlive neither its fix nor its passing. A
        mail that reads now takes its placeholder's place, quietly, as old
        mail — the copy the placeholder was made from, and no other mail
        under its id (_mirror_one). One that still does not read stays
        exactly as it is — nothing is dropped in order to try — and is tried
        less and less often, since each try fetches it whole.

        And reads it whole: a mail that is a placeholder because its reading
        took too long takes that long again, with the mirror's one thread.
        So a round has SET_ASIDE_ROUND_SECONDS for all its readings, those
        tried least often first; what it does not get to waits for the next
        round. However many such mails a mailbox holds, reading them again
        costs the mirror that share of its time and no more."""
        now = self._clock()
        if not self._retry_queue and now >= self._retry_at:
            self._retry_at = now + SET_ASIDE_RETRY_SECONDS
            aside = sorted(self._placeholders)
            self._retried = {mid: self._retried[mid] for mid in aside if mid in self._retried}
            self._retry_round += 1
            self._retry_spent = 0.0
            self._retry_queue = sorted(
                (mid for mid in aside
                 if self._retried.get(mid, (0, 0))[1] <= self._retry_round),
                key=lambda mid: (self._retried.get(mid, (0, 0))[0], mid))
            if self._retry_queue:
                print(f"[mail-gateway] reading again {len(self._retry_queue)} mail(s) "
                      f"mirrored as placeholders", flush=True)
        done = 0
        while self._retry_queue and (limit is None or done < limit):
            if self._retry_spent > SET_ASIDE_ROUND_SECONDS:
                print(f"[mail-gateway] {len(self._retry_queue)} placeholder(s) wait for the "
                      f"next round: this one has read for {self._retry_spent:.0f} s",
                      flush=True)
                self._retry_queue = []
                break
            # (Taken off the queue only once it is dealt with: a pass that
            # fails reads it again.)
            began = time.monotonic()
            try:
                self._read_again(self._retry_queue[0], policy)
            finally:
                self._retry_spent += time.monotonic() - began
            self._retry_queue.pop(0)
            done += 1
        return bool(self._retry_queue)

    def _set_aside_at(self, message_id: str):
        """Where a placeholder's mail is now — ``(role, uid)`` — or None when
        it is a placeholder no longer (read since, gone, its folder
        renumbered). Never a place marked as another mail's (_mirror_one)."""
        if message_id not in self._placeholders:
            return None
        return self.index.one(
            "SELECT l.role, l.uid FROM locations l "
            "JOIN folders f ON f.role = l.role AND f.gen = l.gen "
            "JOIN mails m ON m.message_id = l.message_id "
            "WHERE l.message_id = ? AND l.skipped IS NULL AND m.removed = 0 "
            "AND m.record != '' ORDER BY l.role = 'archive', l.role, l.uid LIMIT 1",
            (message_id,))

    def _read_again(self, message_id: str, policy) -> None:
        place = self._set_aside_at(message_id)
        name = self._folders.get(place["role"]) if place is not None else None
        if name and place["role"] == "archive" and not self._settled:
            return   # the archive is not answering: another round's
        if name:
            self._ingest(place["role"], name, place["uid"], policy, backfill=True)
        if name and self._set_aside_at(message_id) is not None:
            # It changed nothing, for the n-th time: 2**n rounds until the next.
            n = self._retried.get(message_id, (0, 0))[0] + 1
            self._retried[message_id] = (n, self._retry_round + (1 << min(n, 5)))
        else:
            self._retried.pop(message_id, None)

    # -- what /health and the thread endpoints read ----------------------------

    def backfill_done(self) -> bool:
        return self.index.get("backfill_done") == "1"

    def backfill_status(self) -> dict:
        plan = self._plan
        # The window is fixed at the first run (see _sync): "days" is the one
        # the horizon was set from, not whatever the setting says today.
        try:
            days = int(self.index.get("backfill_days", MAIL_BACKFILL_DAYS))
        except ValueError:
            days = MAIL_BACKFILL_DAYS
        return {"done": self.backfill_done(),
                "pending": 0 if self.backfill_done() else (len(plan) if plan is not None else None),
                "days": days}

    def health_notice(self) -> str | None:
        """What an operator should know while the mirror works."""
        return self.notice

    def placeholders(self) -> dict:
        """What /health says of mail mirrored as placeholders: how many are
        on record now, how many were made since this start, and why — by
        the few words each reason comes under (UnreadableMail.kind)."""
        made = dict(self._made)
        return {"standing": len(self._placeholders), "made": sum(made.values()), "why": made}

    def lost(self) -> int:
        """What /health says of mail whose record is gone from the ledger:
        how many ids are still looked for (`lost`, see _read_lost)."""
        return self.index.one("SELECT COUNT(*) AS n FROM lost")["n"]

    def thread(self, chat: str, claimed: str | None = None) -> dict | None:
        """The ``Thread`` answer for one chat key, or None for no such chat.
        ``claimed`` counts one mail in whose record is being written."""
        rows = self.index.rows(
            "SELECT * FROM mails WHERE chat = ? AND (record != '' OR message_id = ?) "
            "ORDER BY ts DESC, message_id DESC", (chat, claimed or ""))
        if not rows:
            return None
        names: dict = {}

        def note(address, name):
            if not address or address in OWN_ADDRESSES:
                return
            if address not in names or (name and not names[address]):
                names[address] = name or names.get(address)

        def listed(row, column):
            try:
                return [(a, n) for a, n in json.loads(row[column] or "[]")]
            except (ValueError, TypeError):
                return []

        # Most recent sender first, then everyone only ever written to.
        for row in rows:
            note(row["sender"], row["sender_name"])
        for row in rows:
            for address, name in listed(row, "recipients") + listed(row, "cc"):
                note(address, name)
        peer = None
        # The person the chat is with: whoever wrote last — not the user, whose
        # own mail can arrive in INBOX too (a copy to oneself, a reply sent
        # from another of their accounts).
        inbound = next((r for r in rows if r["direction"] == "in" and r["sender"]
                        and r["sender"] not in OWN_ADDRESSES), None)
        if inbound is not None:
            peer = {"address": inbound["sender"],
                    "name": names.get(inbound["sender"]) or inbound["sender_name"]}
        else:
            # A thread the user started: whom they wrote to first.
            for row in reversed(rows):
                first = listed(row, "recipients") + listed(row, "cc")
                others = [(a, n) for a, n in first if a not in OWN_ADDRESSES] or first
                if others:
                    peer = {"address": others[0][0],
                            "name": names.get(others[0][0]) or others[0][1]}
                    break
        return {
            "chat": chat,
            "name": self.threads.subject_of(chat),
            "group": len(names) > 1,
            "participants": [{"address": a, "name": n} for a, n in names.items()],
            "peer": peer,
            "last_ts": rows[0]["ts"],
            "messages": len(rows),
            "live": sum(1 for r in rows if not r["removed"]),
        }

    def recent_chats(self, limit: int = MAIL_RECENT_CHATS_MAX) -> list:
        rows = self.index.rows(
            "SELECT chat, MAX(ts) AS last FROM mails WHERE record != '' "
            "GROUP BY chat ORDER BY last DESC, chat LIMIT ?", (limit,))
        return [t for t in (self.thread(row["chat"]) for row in rows) if t]

    def note_author(self, message_id: str, author: str) -> None:
        """Remember who authored a mail that is, or will be, in Sent.

        A mail already recorded as the own-device echo has its record
        corrected; the reader learns it from the ledger."""
        self.index.run(
            "INSERT INTO notes (message_id, author, created) VALUES (?, ?, ?) "
            "ON CONFLICT (message_id) DO UPDATE SET author = excluded.author, "
            "created = excluded.created", (message_id, author, self._clock()))
        self.index.run(
            "DELETE FROM notes WHERE message_id NOT IN "
            "(SELECT message_id FROM notes ORDER BY created DESC LIMIT ?)", (MAIL_NOTES_MAX,))
        with self._lock:
            row = self.index.one(
                "SELECT record FROM mails WHERE message_id = ? AND direction = 'out' "
                "AND record != '' AND removed = 0", (message_id,))
            if row is not None:
                _ibstore.update_message(self._record_path(row["record"]), author=author)

    # -- one pass --------------------------------------------------------------

    def poll(self, *, sync: bool = True, work: bool = True,
             backfill_limit: int | None = None) -> bool:
        """One pass over the mailbox. Returns whether work is waiting that the
        next pass should not wait a whole poll interval for.

        ``sync`` looks at the folders (new and vanished mail); a pass without
        it only works on what an earlier look left to do — the loop runs
        those between polls while a backfill is under way. ``work`` is that
        rest: finding mail again (and the mail of records the ledger lost),
        reading mail again, the backfill. A pass
        without it only follows the folders — the loop runs the two apart,
        so that a fault in the second never keeps new mail waiting
        (_loop_step)."""
        with self._lock:
            if self._reconcile:
                # A record was written and the index could not be told (the
                # state volume failed at that moment): match them up before
                # anything is ingested, or the mail would be written again.
                self._prepare()
                self._reconcile = False
            self._scan_budget = SCAN_BUDGET
            rail_up = self._flush_rail()
            policy = self._policy()
            self._apply_merges()
            if sync:
                self._read = self._settled = False
                self._read, self._settled = self._sync(policy)
            more = False
            if work and self._settled:
                # In this order: a mail must be found again before it can be
                # called gone, and known mail before the backfill reads the rest.
                more = (self._reidentify(RELOCATE_BATCH)
                        or self._settle_unlocated(RELOCATE_BATCH))
            if work and self._settled and not more:
                # Before the backfill, which would read such a mail in INBOX
                # or Sent as one nothing is known of.
                more = self._read_lost(policy)
            if work and self._read and not more:
                # Not held back by an archive folder that will not answer:
                # these read INBOX and Sent (the archive only when it
                # answers), and call nothing gone.
                more = (self._read_noted(policy, backfill_limit)
                        or self._retry_set_aside(policy, backfill_limit)
                        or self._backfill(policy, backfill_limit))
            if rail_up:
                self._flush_rail()
            return more

    @staticmethod
    def _policy():
        try:
            return _triage.load_email_policy(POLICY_PATH)
        except Exception as exc:  # noqa: BLE001 - no flags is the safe reading
            print(f"[mail-gateway] e-mail policy unreadable ({exc}); mirroring "
                  f"as with no flags", flush=True)
            return _triage.EmailPolicy(frozenset(), frozenset())

    def _record_path(self, name: str) -> Path:
        return _ibstore.messages_dir(INBOUND_STORE_DIR) / name

    def _folder(self, role: str):
        return self.index.one("SELECT * FROM folders WHERE role = ?", (role,))

    # -- following the folders -------------------------------------------------

    def _sync(self, policy) -> tuple[bool, bool]:
        """Look at the three folders. ``(read, settled)``: whether INBOX and
        Sent were both read, and whether every folder a mail could be in
        answered as well — only then may a mail be called gone."""
        began = self._clock()
        self._folders = self.mailbox.folders()
        if self.index.get("horizon") is None:
            # Fixed once, so a restart continues the same backfill instead of
            # sliding its window — and kept with the days it was set from,
            # which is what /health reports.
            self.index.set("horizon", began - MAIL_BACKFILL_DAYS * 86400)
            self.index.set("backfill_days", MAIL_BACKFILL_DAYS)
            self.index.set("backfill_done", "0" if MAIL_BACKFILL_DAYS else "1")
        read, missing, refusal = True, [], None
        # Sent before INBOX: a mail in both (a mail to oneself, the list's
        # copy of one's own post) is then recorded as what the user sent. The
        # archive last: by then INBOX and Sent have said whether a mail came
        # and went unseen (see _follow_archive).
        for role in ("sent", "inbox", "archive"):
            name = self._folders.get(role)
            try:
                found = bool(name) and self._sync_folder(role, name, policy)
            except MailboxError as exc:
                if role != "archive" or not read:
                    raise
                # The archive alone would not answer, and servers keep that
                # up for days (a broken mailbox, a missing permission). It is
                # no answer — nothing is looked for again or called gone on
                # it — but INBOX and Sent were read, and what needs only them
                # goes on: new mail was mirrored, old mail may be.
                refusal = str(exc)
                continue
            if not found:
                missing.append(role)
                read = read and role == "archive"
        if read:
            # INBOX and Sent were both looked at, from this moment on.
            self.index.set("looked", began)
        # Without INBOX or Sent the mirror is not the mailbox: what the other
        # folder holds is still read, but nothing is called gone and no old
        # mail is read until the configuration is put right.
        self.fault = None if read else (
            "the " + " and the ".join(r for r in missing if r != "archive")
            + " folder could not be found on the server"
            + (f" — set SENT_FOLDER (configured: {MAIL_CONFIG.sent_folder!r})"
               if "sent" in missing and MAIL_CONFIG is not None else ""))
        self.notice = (
            f"the archive folder does not answer ({refusal}): until it does, a mail "
            f"that left INBOX or Sent is not looked for there, and none is called removed"
            if refusal else
            f"no archive folder ({MAIL_ARCHIVE_FOLDER!r}) on the server: a mail moved "
            f"out of INBOX shows as removed — set MAIL_ARCHIVE_FOLDER"
            if "archive" in missing else None)
        if refusal != self._archive_said:
            print(f"[mail-gateway] {self.notice}" if refusal
                  else "[mail-gateway] the archive folder answers again", flush=True)
            self._archive_said = refusal
        return read, read and refusal is None

    def _sync_folder(self, role: str, name: str, policy) -> bool:
        """Follow one folder. False only when the server says it has no such
        folder; a folder it would not open or search raises (MailboxError)."""
        folder = self._folder(role)
        if folder is None:
            # First sight. Everything in it now is old mail, the backfill's to
            # read (or, in the archive, nobody's); what arrives later is new.
            listing = self.mailbox.list_uids(name, since=self._clock())
            if listing is None:
                return False
            newest = self._newest(name, listing)
            self.index.run(
                "INSERT INTO folders (role, name, uidvalidity, gen, ceiling, hwm) "
                "VALUES (?, ?, ?, 1, ?, ?)", (role, name, listing.uidvalidity, newest, newest))
            self.index.set(f"top:{role}", newest)
            return True
        gen, hwm = folder["gen"], folder["hwm"]
        lowest = self.index.one(
            "SELECT MIN(uid) AS uid FROM locations WHERE role = ? AND gen = ?",
            (role, gen))["uid"]
        first = min(lowest or hwm + 1, hwm + 1)
        listing = self.mailbox.list_uids(name, first_uid=first)
        if listing is None:
            return False
        if listing.uidvalidity != folder["uidvalidity"] or name != folder["name"]:
            # The UIDs no longer mean what they meant (the server renumbered
            # the folder, or the role now names another folder). The old rows
            # stay, a generation behind, as the list of mails to find again by
            # Message-ID; what is here now and unknown is the backfill's.
            newest = self._newest(name, listing)
            self.index.run(
                "UPDATE folders SET name = ?, uidvalidity = ?, gen = gen + 1, "
                "ceiling = ?, hwm = ? WHERE role = ?",
                (name, listing.uidvalidity, newest, newest, role))
            self.index.set(f"top:{role}", newest)
            if role != "archive":
                self.index.set("backfill_done", "0")
                self._plan = None
            print(f"[mail-gateway] {name!r}: the UIDs changed (UIDVALIDITY "
                  f"{folder['uidvalidity']} -> {listing.uidvalidity}); finding its "
                  f"mails again by Message-ID", flush=True)
            looked = self.index.get("looked")
            if role != "archive" and looked is not None:
                # What arrived since the last look is new mail all the same:
                # it is read now, as the folder is followed — not left to the
                # backfill, which a fault in the rest may hold back (I4).
                # (A mail of it on record is read as the follow reads it;
                # should this fail, what is left is the backfill's, as the
                # rest is.)
                since = float(looked) - ARRIVAL_SLACK_SECONDS
                recent = self.mailbox.list_uids(name, since=since, dates=True)
                if recent is not None and recent.uidvalidity == listing.uidvalidity:
                    for uid, arrived in sorted(recent.uids.items()):
                        if arrived is None or arrived >= since:
                            self._adopt_gained(role, name, uid, policy)
            return True
        present = set(listing.uids)
        for row in self.index.rows(
                "SELECT uid FROM locations WHERE role = ? AND gen = ? AND uid >= ?",
                (role, gen, first)):
            if row["uid"] not in present:
                self.index.run("DELETE FROM locations WHERE role = ? AND gen = ? AND uid = ?",
                               (role, gen, row["uid"]))
        # The highest UID the folder has handed out, as far as it says, and
        # what that was at the last look (None: an index older than the note).
        top = max(listing.uidnext - 1 if listing.uidnext else 0, max(present, default=0), hwm)
        before = self.index.get(f"top:{role}")
        if role == "archive":
            self._follow_archive(name, folder, top, before, policy)
            return True
        new = sorted(uid for uid in present if uid > hwm)
        # A UID handed out since the last look that the folder no longer
        # holds: a mail came and went between two polls. If it went to the
        # archive it is still the user's mail, and nothing else would ever
        # read it — so the archive is told to look at what it gained. Noted
        # before the high-water mark moves, so a failed pass asks again.
        # (UIDs need not be consecutive; a server that skips some has the
        # archive looked at more often than needed, which costs little.)
        # The same rule keeps the mail a failed pass left above the mark:
        # neither the mark nor the top noted below has moved past it, so if
        # it leaves the folder before a pass can mirror it — a fault of the
        # mirror may last, and so may a restart — it counts as come and gone.
        seen = max(hwm, int(before)) if before is not None else top
        if top - seen > sum(1 for uid in new if seen < uid <= top):
            if self.index.get("archive_due") is None:
                # From when such a mail can have arrived: the last time INBOX
                # and Sent were both looked at, give or take the two clocks.
                looked = float(self.index.get("looked") or self._clock())
                self.index.set("archive_due", looked - ARRIVAL_SLACK_SECONDS)
            if role == "sent":
                # It left Sent: only then can what the archive gained be
                # something the user sent (see _mirror_one).
                self.index.set("archive_own", "1")
        for uid in new:
            if self.index.one("SELECT 1 FROM locations WHERE role = ? AND gen = ? AND uid = ?",
                              (role, gen, uid)) is None:
                self._ingest(role, name, uid, policy, backfill=False)
            # Only now, when the mail has a record or a place: a pass that
            # fails — the mailbox, a disk, the mirror itself — reads it
            # again. (A mail that is the one at fault does not fail the
            # pass: it is mirrored as a placeholder by _ingest.)
            self.index.run("UPDATE folders SET hwm = ? WHERE role = ?", (uid, role))
        self.index.set(f"top:{role}", top)
        return True

    def _follow_archive(self, name: str, folder, top: int, before, policy) -> None:
        """The archive is followed, not read — but for the mail that passed
        through INBOX (or Sent) between two polls and is here now.

        While such a mail may exist (``archive_due``, set by INBOX's or Sent's
        look, holds the time from which it arrived), what the archive gained
        above its mark and nobody has placed is looked at: a mail on record is
        given its place, a mail not on record that arrived since then is
        mirrored, older mail someone filed here is left alone. Otherwise the
        mark just moves up — one look behind (``before``, the top it had at
        the last look). A mail can reach the archive after INBOX was listed
        and before the archive is; only what the archive already held at the
        previous look is certain to hold no mail that INBOX's next look will
        report as come and gone."""
        hwm = folder["hwm"]
        due = self.index.get("archive_due")
        if before is None:
            # An index older than this rule: the archive's mark stands where
            # the folder was first seen. Nothing it gained since is read.
            self.index.run("UPDATE folders SET hwm = MAX(hwm, ?) WHERE role = 'archive'", (top,))
            self.index.set("top:archive", top)
            return
        if due is not None:
            listing = self.mailbox.list_uids(name, first_uid=hwm + 1, dates=True)
            if listing is None or listing.uidvalidity != folder["uidvalidity"]:
                return   # the next look at the folders sorts it out
            placed = {row["uid"] for row in self.index.rows(
                "SELECT uid FROM locations WHERE role = 'archive' AND gen = ?",
                (folder["gen"],))}
            unseen = sorted(uid for uid in listing.uids if uid > hwm and uid not in placed)
            for uid in unseen[:RELOCATE_BATCH]:
                arrived = listing.uids[uid]
                if arrived is None or arrived >= float(due):
                    self._adopt_gained("archive", name, uid, policy)
                self.index.run("UPDATE folders SET hwm = ? WHERE role = 'archive'", (uid,))
            if len(unseen) > RELOCATE_BATCH:
                return   # still due: the next look reads on
            self.index.run("DELETE FROM meta WHERE key IN ('archive_due', 'archive_own')")
        self.index.run("UPDATE folders SET hwm = MAX(hwm, ?) WHERE role = 'archive'",
                       (int(before),))
        self.index.set("top:archive", top)

    def _adopt_gained(self, role: str, name: str, uid: int, policy) -> None:
        """One mail a folder gained that may be on record already — the
        archive's (see _follow_archive), or a renumbered folder's latest.
        In INBOX and Sent it is read as the folder is followed (_ingest):
        a mail on record gets its place there, or is written again as what
        that copy makes it — its Sent copy makes it the user's own, its
        sender's INBOX copy a mail the user redirected theirs again
        (_mirror_one). In the archive it gets a place if it is on record,
        else it is mirrored, as new mail (inbound or outbound by who sent
        it)."""
        if role != "archive":
            self._ingest(role, name, uid, policy, backfill=False)
            return
        head = self._peek_head(name, uid)
        if head is None:
            return   # the server's answer: gone again
        try:
            ids = self._ids_of(head)
        except NotRead:
            ids = ()   # not learned here: the mail is read whole, which says it too
        for message_id in ids:
            if self.index.one("SELECT 1 FROM mails WHERE message_id = ? AND removed = 0 "
                              "AND record != ''", (message_id,)) is not None:
                self._place(role, uid, message_id, head)
                return
        self._ingest(role, name, uid, policy, backfill=False)

    def _newest(self, name: str, listing: Listing) -> int:
        """The highest UID the folder has handed out so far."""
        if listing.uidnext:
            return listing.uidnext - 1
        whole = self.mailbox.list_uids(name)
        return max(whole.uids, default=0) if whole else 0

    # -- finding a mail again --------------------------------------------------

    def _peek(self, name: str, uid: int) -> Fetched | None:
        """The mail at a UID as far as its header block; None when the
        server answers that the mail is not there."""
        got = self.mailbox.fetch(name, uid, limit=HEADER_PEEK_BYTES)
        if got is not None and got.size > len(got.raw) and not _HEADER_END_RE.search(got.raw):
            # A header block longer than the peek: read as far as one is read.
            got = self.mailbox.fetch(name, uid, limit=MAX_HEADER_BYTES + 4096)
        return got

    def _peek_head(self, name: str, uid: int) -> bytes | None:
        """The header block of the mail at a UID, as it stands there; None
        when the server answers that the mail is not there."""
        got = self._peek(name, uid)
        return _head_of(got.raw) if got is not None else None

    @staticmethod
    def _ids_of(head: bytes) -> tuple:
        """The ids the mail with this header block can be on record under:
        the one it is mirrored under when the block is read, and the one of
        the block's bytes, which a placeholder made without reading it
        carries (:func:`_bare_stub`). Raises :class:`NotRead` when the
        reading did not come back — then only the second is known."""
        facts = read_head(head)
        return (facts["id"], _bare_id(head)) if facts else (_bare_id(head),)

    def _carried_whole(self, message_id: str) -> bool:
        """Whether the mail's own Message-ID header holds its id in one piece.

        An id is restored from a header some relay folded in the middle of it
        (mail_threads.normalize_id), and the server searches the header as it
        stands: asked for the restored id it answers "no such mail" about a
        mail it holds. Read off the header block the record keeps; where that
        cannot be read the answer is no, and the mail is looked for the slow
        way, which is right for every mail."""
        facts = self._recorded_facts(message_id)
        return facts is not None and message_id in facts["message_id"]

    def _recorded_head(self, message_id: str) -> bytes | None:
        """The header block a mail's record keeps, as it was stored (see
        _store_parts); None when there is no record, or none that can be
        read."""
        row = self.index.one("SELECT record FROM mails WHERE message_id = ? AND record != ''",
                             (message_id,))
        fields = _ibstore.read_record(self._record_path(row["record"])) if row else None
        media_id = _ibstore.media_id_of((fields or {}).get("header_part") or "")
        got = _ibstore.load_media(INBOUND_STORE_DIR, media_id) if media_id else None
        return got[0] if got is not None else None

    def _recorded_facts(self, message_id: str) -> dict | None:
        """What that header block says (:func:`read_head`); None when there
        is none, or it cannot be read. Raises :class:`NotRead` when its
        reading did not come back: not known, and nothing to decide on."""
        head = self._recorded_head(message_id)
        return read_head(head) if head is not None else None

    def _find(self, role: str, message_id: str, ts: float | None,
              arrived: float | None = None, every: bool = False) -> tuple:
        """Where a mail may be in one folder: ``(uids, sure)`` — the UIDs
        there that have no place yet and hold a mail under this id, newest
        first, and whether the folder has answered that these are all.
        (Without ``every`` the newest is enough, and the looking ends at
        it.) No UID and not sure is "could not tell": the caller leaves the
        mail as it is, for a later pass (see _adopt). A UID that has a place
        is spoken for — the mail's own, or marked as another's — and is
        never handed out again. ``ts`` is the mail's time on record and
        ``arrived`` when the server received it, where the index knows."""
        name = self._folders.get(role)
        folder = self._folder(role)
        if not name or folder is None:
            return [], True
        gen = folder["gen"]
        placed = {row["uid"] for row in self.index.rows(
            "SELECT uid FROM locations WHERE role = ? AND gen = ?", (role, gen))}
        # (A mail with no record — one set aside as news, whose place alone is
        # kept — has no header block on record to check, and nothing to lose.)
        if _searchable(message_id):
            try:
                carried = ts is None or self._carried_whole(message_id)
            except NotRead:
                return [], False   # its own header block, not read: not known
            if carried:
                found = self.mailbox.find(name, message_id)
                return [uid for uid in found.uids if uid not in placed], found.whole
        # No header to search for: read the headers of the mails here that
        # are not accounted for and compare ids, newest first. Not older than
        # the mail is all that bounds them — its time on record is the
        # sender's Date where that is earlier than its arrival, so the first
        # is used only where the second is not known, and no later bound
        # holds on a server that stamps a copy with the time it was made. A
        # folder that is only followed holds many such mails, so the reading
        # goes on over as many passes as it takes; "not there" is said only
        # once every one of them has been compared.
        when = arrived if arrived is not None else ts if ts is not None else self._clock()
        listing = self.mailbox.list_uids(name, since=when - SCAN_WINDOW_SECONDS)
        if listing is None:
            return [], True
        if listing.uidvalidity != folder["uidvalidity"]:
            return [], False   # renumbered since the last look at the folders
        for key in [key for key in self._peeked if key[0] == role and key[1] != gen]:
            del self._peeked[key]     # the UIDs of a folder since renumbered
        peeked = self._peeked.setdefault((role, gen), {})
        candidates = [uid for uid in sorted(listing.uids, reverse=True) if uid not in placed]
        unread = sum(1 for uid in candidates if uid not in peeked)
        if unread and unread + sum(map(len, self._peeked.values())) > SCAN_CACHE_MAX:
            # No room for what this search must read: what it has no use for
            # goes. (If that is not enough it stays unsure, at no cost.)
            wanted = set(candidates)
            for key, cache in self._peeked.items():
                for uid in [uid for uid in cache if key != (role, gen) or uid not in wanted]:
                    del cache[uid]
        room = SCAN_CACHE_MAX - sum(map(len, self._peeked.values()))
        found, sure = [], True
        for uid in candidates:
            if uid not in peeked:
                if self._scan_budget <= 0 or room <= 0:
                    sure = False
                    continue    # the ones already read are still compared
                self._scan_budget -= 1
                head = self._peek_head(name, uid)
                try:
                    ids = self._ids_of(head) if head is not None else ()
                except NotRead:
                    # No answer about this mail, so none is remembered and
                    # nothing is concluded: the folder has not said. What
                    # needs no reading is still compared — and a reader
                    # that does not answer is asked no more this pass.
                    self._scan_budget, sure = 0, False
                    ids = (_bare_id(head),)
                else:
                    room -= 1
                    peeked[uid] = ids
            else:
                ids = peeked[uid]
            if message_id in ids:
                found.append(uid)
                if not every:
                    break
        return found, sure

    def _locate(self, role: str, uid: int, message_id: str | None, skipped=None) -> None:
        folder = self._folder(role)
        self.index.run(
            "INSERT OR REPLACE INTO locations (role, gen, uid, message_id, skipped) "
            "VALUES (?, ?, ?, ?, ?)", (role, folder["gen"], uid, message_id, skipped))

    def _place(self, role: str, uid: int, message_id: str, head: bytes, skipped=None) -> bool:
        """Give the mail at a UID — under ``message_id``, by its header
        block — its place. For a mail on record whole that is one more place
        of it. Beside a placeholder only the copy it was made from is one
        (_own_copy): any other mail under its id is marked as another mail's
        ("other"), whatever it was marked before — the mark is read off the
        mail, never carried over from a UID that meant it once. Returns
        whether the place is the record's own."""
        if message_id in self._placeholders \
                and not self._own_copy(message_id, _head_text(head)):
            self._locate(role, uid, message_id, "other")
            return False
        self._locate(role, uid, message_id, None if skipped == "other" else skipped)
        return True

    def _adopt(self, role: str, message_id: str, ts: float | None,
               arrived: float | None, skipped=None):
        """Look in one folder for a mail the index knows and has no place
        for there. Returns the UID that is its place from now on; None when
        the folder has answered that it holds no such mail; UNSURE when that
        cannot be said yet — the caller then leaves the mail as it is.

        For a placeholder EVERY mail the folder holds under its id is looked
        at, and each is placed as what its header block says it is: the
        newest one may be anybody's (ids are the sender's to choose), and
        the placeholder's own copy an older UID in the same folder."""
        placeholder = message_id in self._placeholders
        uids, sure = self._find(role, message_id, ts, arrived, every=placeholder)
        for uid in uids:
            if not placeholder:
                self._locate(role, uid, message_id, None if skipped == "other" else skipped)
                return uid
            head = self._peek_head(self._folders[role], uid)
            if head is not None and self._place(role, uid, message_id, head):
                return uid
        return None if sure else UNSURE

    def _reidentify(self, limit: int) -> bool:
        """Find again, by Message-ID, the mails of a folder whose UIDs changed.
        True while some are left for the next pass."""
        after = self._stale_after or ("", 0, 0)
        stale = self.index.rows(
            "SELECT l.role, l.gen, l.uid, l.message_id, l.skipped, m.ts, m.arrived "
            "FROM locations l "
            "JOIN folders f ON f.role = l.role AND f.gen != l.gen "
            "LEFT JOIN mails m ON m.message_id = l.message_id "
            "WHERE (l.role, l.gen, l.uid) > (?, ?, ?) "
            "ORDER BY l.role, l.gen, l.uid LIMIT ?", (*after, limit + 1))
        for row in stale[:limit]:
            if row["message_id"] and self._adopt(
                    row["role"], row["message_id"], row["ts"], row["arrived"],
                    row["skipped"]) is UNSURE:
                # The row stays, a generation behind: the mail keeps a
                # place on record, so nothing calls it gone meanwhile.
                continue
            # Found, and placed under its new UID — or not found: the row
            # goes, and a mail left with no place at all is looked for in
            # every folder before it is called gone.
            self.index.run("DELETE FROM locations WHERE role = ? AND gen = ? AND uid = ?",
                           (row["role"], row["gen"], row["uid"]))
        # The rows that stayed are come back to once the others are through.
        more = len(stale) > limit
        last = stale[limit - 1] if more else None
        self._stale_after = (last["role"], last["gen"], last["uid"]) if more else None
        return more

    def _settle_unlocated(self, limit: int) -> bool:
        """Mails the index knows and has no place for: look in every
        mirrored folder — this is how a move to the archive is followed —
        and tombstone the ones that are in none. True while some are left
        for the next pass.

        "In none" is what every folder *answered*. A folder that could not
        say (UNSURE) leaves the mail as it is, and one the server would not
        open or search ends the pass (MailboxError) — after the mails already
        found gone in it have been tombstoned and announced.

        A place marked as another mail's ("other", see _place) is no place
        of the mail on record: a placeholder whose own copy is gone is in
        none, whatever else lies in the mailbox under its id — and is
        tombstoned like any mail, its reading again pending or not."""
        # (When the gateway sends mail itself, a record will exist before its
        # Sent copy is visible; that phase must keep such a mail out of here
        # until the copy has had time to appear.)
        after = self._settle_after or (-1.0, "")
        waiting = self.index.rows(
            "SELECT * FROM mails m WHERE m.removed = 0 AND m.record != '' AND NOT EXISTS "
            "(SELECT 1 FROM locations l WHERE l.message_id = m.message_id "
            "AND l.skipped IS NOT 'other') AND (m.ts, m.message_id) > (?, ?) "
            "ORDER BY m.ts, m.message_id LIMIT ?", (*after, limit + 1))
        gone, failure = [], None
        try:
            for row in waiting[:limit]:
                order = ("sent", "archive", "inbox") if row["direction"] == "out" \
                    else ("inbox", "archive", "sent")
                unsure = False
                for role in order:
                    uid = self._adopt(role, row["message_id"], row["ts"], row["arrived"])
                    if uid is UNSURE:
                        unsure = True
                    elif uid is not None:
                        break
                else:
                    if not unsure:
                        gone.append(row)
        except MailboxError as exc:
            failure = exc
        self._bury(gone)
        if failure is not None:
            raise failure
        more = len(waiting) > limit
        last = waiting[limit - 1] if more else None
        self._settle_after = (last["ts"], last["message_id"]) if more else None
        return more

    def _bury(self, rows: list) -> None:
        """Tombstone mails that are in no folder any more, and tell the rail.

        The ledger first; then, chat by chat, the index and the event that
        announces it as one transaction. A stop in between leaves tombstones
        the index holds for live mail, which the next start announces
        (_prepare) — and a failed transaction leaves them unlocated, so the
        next pass comes here again.

        What still lies in the mailbox under a buried id — a place marked
        "other" beside a placeholder — is another mail's no longer: it is
        noted to be read again (_read_noted), and is then mirrored as the
        mail it is, in the tombstone's place."""
        buried: dict = {}
        for row in rows:
            # The mail's blobs were stored for its record alone (_store_parts):
            # they go with it, with no look at the rest of the ledger.
            if not _ibstore.tombstone_message(self._record_path(row["record"]),
                                              self._clock(), own_blobs=True):
                print(f"[mail-gateway] could not tombstone {row['record']}; "
                      f"will retry", flush=True)
                continue
            self._placeholders.discard(row["message_id"])
            buried.setdefault(row["chat"], []).append(row["message_id"])
        for chat, message_ids in buried.items():
            marks = ",".join("?" * len(message_ids))
            live = self.index.one(
                f"SELECT COUNT(*) AS n FROM mails WHERE chat = ? AND removed = 0 "
                f"AND record != '' AND message_id NOT IN ({marks})",
                (chat, *message_ids))["n"]
            self.index.atomic([
                # The record lost its recipients; so does the index, which
                # must say what a rebuild from the ledger would.
                (f"UPDATE mails SET removed = 1, recipients = '[]', cc = '[]' "
                 f"WHERE message_id IN ({marks})", tuple(message_ids)),
                (f"UPDATE locations SET message_id = NULL, skipped = 'again' "
                 f"WHERE skipped = 'other' AND message_id IN ({marks})", tuple(message_ids)),
                self._event("removed", {"channel": CHANNEL, "account": ACCOUNT, "chat": chat,
                                        "message_ids": message_ids, "live": live})])
            print(f"[mail-gateway] {len(message_ids)} mail(s) of chat {chat!r} left the "
                  f"mailbox ({live} still there)", flush=True)

    def _read_noted(self, policy, limit: int | None) -> bool:
        """Read the UIDs noted "again" — mails of which nothing is known:
        what lay under the id of a placeholder that is gone (_bury), what an
        older build had marked unreadable (_prepare). Nothing else would
        come back to them — the backfill reads INBOX and Sent only, and no
        folder's mark is moved down. Each is read as any mail is the first
        time it is read where it lies (_ingest, _mirror_one): a mail that
        cannot be read is a placeholder, as where the folders are followed,
        and holds back no note behind it. Old mail, quietly. True while some
        are left for the next pass."""
        noted = self.index.rows(
            "SELECT l.role, l.gen, l.uid FROM locations l "
            "JOIN folders f ON f.role = l.role AND f.gen = l.gen "
            "WHERE l.skipped = 'again' "
            "ORDER BY l.role != 'sent', l.role != 'inbox', l.role, l.uid")
        done = 0
        for row in noted:
            name = self._folders.get(row["role"])
            if not name or (row["role"] == "archive" and not self._settled):
                continue   # the archive is not answering: another pass's
            if limit is not None and done >= limit:
                return True
            self._ingest(row["role"], name, row["uid"], policy, backfill=True)
            # The note goes: the mail there has a record or a place now —
            # or the server said it holds none at that UID any more
            # (expunged, or the folder renumbered since it was looked at,
            # which leaves every place of that generation behind).
            self.index.run("DELETE FROM locations WHERE role = ? AND gen = ? AND uid = ? "
                           "AND skipped = 'again'", (row["role"], row["gen"], row["uid"]))
            done += 1
        return False

    def _read_lost(self, policy) -> bool:
        """Look for the mails whose record is gone from the ledger, by the
        ids in `lost` (_prepare): at most LOST_BATCH of them a pass, the one
        tried least recently first, the oldest among those (noticed first,
        then arrived first, so a thread's first mail comes first). True
        while some that are due are left for the next pass.

        Each is looked for by its Message-ID in Sent, INBOX and the archive
        — every mail under it in every folder (_find) — and, found, read as
        a mail read for the first time (_read_found). Not found, with every
        folder sure of it: the id goes, with nothing to tombstone (there is
        no record). Not known (a folder that could not say, a header block
        that was not read — I1): the id stays, and is looked for again
        less and less often, as a placeholder is read again; the ids behind
        it are not held back. A UIDVALIDITY change asks nothing of this:
        nothing in `lost` names a UID."""
        now = self._clock()
        due = self.index.rows(
            "SELECT * FROM lost WHERE due <= ? "
            "ORDER BY tried IS NOT NULL, tried, noticed, arrived IS NULL, arrived, message_id "
            "LIMIT ?",
            (now, LOST_BATCH + 1))
        for hint in due[:LOST_BATCH]:
            # Tried, whatever comes of it: should it end the pass (a write
            # that fails), the next pass starts with the ids behind it.
            self.index.run("UPDATE lost SET tried = ? WHERE message_id = ?",
                           (now, hint["message_id"]))
            if not self._read_found(hint, policy):
                tries = hint["tries"] + 1
                self.index.run(
                    "UPDATE lost SET tries = ?, due = ? WHERE message_id = ?",
                    (tries, now + SET_ASIDE_RETRY_SECONDS * (1 << min(tries, 5)),
                     hint["message_id"]))
        return len(due) > LOST_BATCH

    def _read_found(self, hint, policy) -> bool:
        """One id of `lost`: find its mail and read it, or know it is in no
        folder. False when that cannot be told yet.

        The mail read as the record's is ONE of what lies under the id:
        among those From the sender the lost row named, the one the server
        received first (INTERNALDATE; Sent first on a tie, then INBOX); if
        none is From that sender, the first of all. It is read as a mail
        read for the first time where it lies (_ingest), with one thing
        more: From that sender, where the row said "out", it is recorded as
        the user's own — evidence about the mail itself, the same sender
        the lost record had (_mirror_one). The others are then read as any
        further copy is: one more place of the record, or marked as another
        mail's, by their own evidence. When the row said the mail arrived
        is the search's only bound where an id cannot be searched for
        (_find)."""
        mid = hint["message_id"]
        found, sure = [], True
        for order, role in enumerate(("sent", "inbox", "archive")):
            name = self._folders.get(role)
            if not name:
                continue   # no such folder: nothing of the mail can be there
            uids, whole = self._find(role, mid, None, hint["arrived"], every=True)
            sure = sure and whole
            found += [(order, role, name, uid) for uid in uids]
        if not sure:
            return False
        if not found:
            self.index.run("DELETE FROM lost WHERE message_id = ?", (mid,))
            print(f"[mail-gateway] the mail {mid!r}, whose record is gone from the ledger, "
                  f"is in no folder: there is no record to tombstone", flush=True)
            return True
        if len(found) > 1:
            # Which one is the record's: who each is From, and when it came.
            ranked = []
            for order, role, name, uid in found:
                got = self._peek(name, uid)
                if got is None:
                    return False   # gone since the search: where to, the next one says
                try:
                    facts = read_head(_head_of(got.raw))
                except NotRead:
                    return False
                sender = (facts or {}).get("sender")
                ranked.append((not (hint["sender"] and sender == hint["sender"]),
                               got.date is None, got.date or 0.0, order, uid, role, name))
            found = [(order, role, name, uid)
                     for *_key, order, uid, role, name in sorted(ranked)]
        for n, (_order, role, name, uid) in enumerate(found):
            try:
                self._ingest(role, name, uid, policy, backfill=True,
                             lost=hint if n == 0 else None)
            except NotRead:
                return False   # its header block did not come back: not read (I1)
            if n == 0 and self.index.one(
                    "SELECT 1 FROM locations l JOIN folders f ON f.role = l.role "
                    "AND f.gen = l.gen WHERE l.role = ? AND l.uid = ?", (role, uid)) is None:
                # Not read after all: the server holds no such mail there any
                # more (moved since the search), or not under the UIDs it was
                # found under. The next look tells where it is.
                return False
        self.index.run("DELETE FROM lost WHERE message_id = ?", (mid,))
        return True

    # -- backfill --------------------------------------------------------------

    def _backfill(self, policy, limit: int | None) -> bool:
        """Read old mail: INBOX and Sent since the horizon, merged, oldest
        first, so parents arrive before their replies. Resumable by
        construction — the plan is what the folders hold minus what the index
        already places — so a restart continues and never repeats."""
        if self.backfill_done():
            return False
        if self._plan is None:
            horizon = float(self.index.get("horizon") or 0.0)
            plan = []
            for order, role in enumerate(("sent", "inbox")):
                folder, name = self._folder(role), self._folders.get(role)
                if folder is None or not name:
                    return False
                listing = self.mailbox.list_uids(name, since=horizon, dates=True)
                if listing is None or listing.uidvalidity != folder["uidvalidity"]:
                    return False   # the next look at the folders sorts it out
                placed = {row["uid"] for row in self.index.rows(
                    "SELECT uid FROM locations WHERE role = ? AND gen = ?",
                    (role, folder["gen"]))}
                plan += [(date or 0.0, order, uid, role, folder["gen"])
                         for uid, date in listing.uids.items()
                         if uid <= folder["ceiling"] and uid not in placed]
            self._plan = sorted(plan)
        done = 0
        while self._plan and (limit is None or done < limit):
            _date, _order, uid, role, gen = self._plan[0]
            folder = self._folder(role)
            if folder is None or folder["gen"] != gen:
                self._plan = None   # the UIDs changed under the plan
                return True
            # (Popped only once the mail is dealt with: a pass that fails —
            # the mailbox, a disk, the mirror itself — reads it again. A mail
            # that cannot be read does not fail it, and holds back no other:
            # it is a placeholder at once.)
            self._ingest(role, folder["name"], uid, policy, backfill=True)
            self._plan.pop(0)
            done += 1
        if self._plan:
            return True
        self.index.set("backfill_done", "1")
        self._plan = None
        print("[mail-gateway] backfill complete", flush=True)
        return False

    # -- one mail --------------------------------------------------------------

    def _ingest(self, role: str, name: str, uid: int, policy, *, backfill: bool,
                lost=None) -> None:
        """Mirror the mail at one UID: a record, or one more place for a mail
        already recorded, or a note of why it has neither. It returns when
        the mail has one of the three and raises when it has none — so a
        caller moves its mark past the UID exactly when this returns. (Or
        when the server answers that no such mail is there: expunged, or
        its folder renumbered since it was looked at — whoever asked looks
        again: the folders' next look, the backfill's next plan, the next
        look for a lost record's mail.) ``lost``: the hints `lost` holds of
        the mail's id, for the one mail read as its record (_read_found).

        Whose fault a failure is, is decided by what failed:

          * READING the mail — the parse, the walk of its parts, the trim
            (:func:`read_mail`) — fails on the mail's account when the
            reader still reads the canaries. The mail is mirrored as a
            placeholder (:func:`read_stub`) at once, and the folder goes on:
            a mail is its sender's to write, and must stop neither the mails
            behind it nor the backfill.
          * A reader that cannot read the canaries either, or not in
            their time, is the mirror's fault; so is a reader process that
            cannot be started, and so is every failure in WRITING — the
            ledger, the indexes, the blobs — whatever mail was in hand: next
            to nothing of a mail decides whether its record can be written,
            and a mirror that set aside what it could not write would turn
            a corrupt index or a bug into a mailbox of placeholders, quietly.
            These end the pass (what the mailbox, a disk or an index file
            raised, as it is; anything else as a :class:`MirrorFault` that
            names the mail). Nothing is recorded, marked or skipped: the
            mail stays above the caller's mark, the next pass reads it
            again, and /health says the mirror is at fault meanwhile."""
        fetched = self.mailbox.fetch(name, uid, limit=MAIL_MAX_MAIL_BYTES)
        if fetched is None:
            return   # the server's answer: expunged between the listing and now
        folder = self._folder(role)
        if fetched.uidvalidity is not None and folder is not None \
                and fetched.uidvalidity != folder["uidvalidity"]:
            # The folder was renumbered (or rebuilt) since it was looked at:
            # what this UID names now is not what was listed, or placed,
            # under it — nothing about the mail here is known from the index.
            print(f"[mail-gateway] {name!r} was renumbered (UIDVALIDITY "
                  f"{folder['uidvalidity']} -> {fetched.uidvalidity}) since it was looked "
                  f"at; UID {uid} is left to the next look", flush=True)
            return
        where = f"{name!r} UID {uid}"
        why = None
        # What the index holds this very UID to be: a UID is one mail for as
        # long as its folder's UIDs hold, whatever is read off it this time.
        at = self.index.one(
            "SELECT m.* FROM locations l JOIN folders f ON f.role = l.role AND f.gen = l.gen "
            "JOIN mails m ON m.message_id = l.message_id WHERE l.role = ? AND l.uid = ? "
            "AND l.skipped IS NULL AND m.removed = 0 AND m.record != ''", (role, uid))
        try:
            try:
                mail = read_mail(fetched.raw, fetched.date)
            except UnreadableMail as exc:
                cause = exc.__cause__
                print(f"[mail-gateway] could not read the mail at {where}; mirroring a "
                      f"placeholder: {exc}" + ("\n" + "".join(traceback.format_exception(
                          type(cause), cause, cause.__traceback__, limit=-8)) if cause else ""),
                      flush=True)
                # (The mail read as a lost record's has none to go on: a
                # header block that does not come back leaves its id lost —
                # unless that id is the one of the block's bytes, the record
                # the mail had being a placeholder of the same.)
                mail = read_stub(fetched.raw, fetched.date, str(exc), go_on=lost is None
                                 or lost["message_id"] == _bare_id(_head_of(fetched.raw)))
                # (Counted for /health only where a placeholder is made of it:
                # a placeholder read again in vain is the one already on
                # record, under whichever id.)
                on_record = {mail.message_id, at["message_id"] if at is not None else None}
                why = None if on_record & self._placeholders else exc.kind
        except MirrorFault as exc:
            raise MirrorFault(f"{exc}: the mail at {where} is not set aside over it — the "
                              f"pass ends there, and nothing is skipped") from exc
        # On record under another id than it reads under now: the id was one
        # of its bytes, made when its header block did not read (_bare_stub),
        # or the block reads differently than it did. The mail is the same —
        # where its header block says so: the block whose bytes the id was
        # made of, or the one the record keeps (I2) — so what is known of it
        # goes on to the new record (its direction), and the old one gives
        # way in the same step: on what was read at its own place, not later
        # for want of one.
        head = _head_of(fetched.raw)
        renamed = at is not None and at["message_id"] != mail.message_id
        if renamed and not (at["message_id"] == _bare_id(head)
                            or self._own_copy(at["message_id"], _head_text(head))):
            # Another mail than the one on record at this UID, and the UIDs
            # hold: the index does not know what lies here. The record keeps
            # its place, and the next look at the folders sorts it out.
            print(f"[mail-gateway] the mail at {where} is not the one on record there "
                  f"({at['message_id']!r}); left as it is", flush=True)
            return
        if renamed and mail.unreadable is not None and mail.message_id == _bare_id(head):
            # Only ever towards what was read. This time nothing was, not
            # the header block either: no answer, and the record the mail
            # has is the one it keeps.
            return
        try:
            self._mirror_one(role, uid, mail, fetched, policy, backfill,
                             at["direction"] if renamed else None, lost=lost)
            if renamed and self.index.one(
                    "SELECT 1 FROM locations WHERE message_id = ? AND skipped IS NULL",
                    (at["message_id"],)) is None:
                self._bury([at])
        except NotRead as exc:
            # (The header block on record, asked whether the mail was sent
            # on: no answer is not "no".)
            raise MirrorFault(
                f"what is on record of the mail at {where} could not be read ({exc}): "
                f"nothing is decided on that — the pass ends there, and nothing is "
                f"skipped") from exc
        except Exception as exc:  # noqa: BLE001 - writing: never this mail's fault
            if _environment(exc) or isinstance(exc, MirrorFault):
                raise
            raise MirrorFault(
                f"the mail at {where} could not be written ({type(exc).__name__}: "
                f"{exc}"[:300] + "): it is not set aside over that — the pass ends "
                "there, and nothing is skipped") from exc
        if why is not None and mail.message_id in self._placeholders:
            self._made[why] = self._made.get(why, 0) + 1

    def _mirror_one(self, role: str, uid: int, mail: Mail, fetched: Fetched, policy,
                    backfill: bool, direction: str | None = None, *, lost=None) -> None:
        if role == "archive" and any(flag.lower() == "\\draft" for flag in fetched.flags):
            # Where the archive is "all mail" it holds drafts too, and a draft
            # saved in that minute is not a mail anyone sent.
            self._locate(role, uid, None, "draft")
            return
        # In Sent it is what the user sent. A mail the archive gained (see
        # _follow_archive) is told apart by who it is from — where a mail
        # left Sent unseen at all: a From is its sender's to write, and a
        # mail that came in under the user's own address and was filed away
        # within the minute is not thereby something the user wrote.
        outbound = role == "sent" or (
            role == "archive" and mail.sender in OWN_ADDRESSES
            and self.index.get("archive_own") is not None)
        # (A claim is a write under way, never seen here: see _write.)
        known = self.index.one("SELECT * FROM mails WHERE message_id = ? AND record != ''",
                               (mail.message_id,))
        if lost is not None and known is None and mail.message_id == lost["message_id"] \
                and mail.sender and mail.sender == lost["sender"] and lost["direction"] == "out":
            # The mail of a record gone from the ledger (_read_found), From the
            # very sender that record had, which was the user's own: this
            # mail is evidence of itself — the same From — and is the user's
            # again, whatever address that is (a plus-address, an alias
            # nobody listed). Any other is read as a first reading here is.
            outbound = True
        if direction is not None and known is None:
            # What the record it had under another id said (see _ingest): the
            # mail is what it was, and no second look decides its direction,
            # or whether it is news. (Only a copy in Sent still makes an
            # inbound mail the user's own, as it does for a mail on record.)
            outbound = direction == "out" or (role == "sent"
                                              and not self._sent_on(mail, mail.resent))
        revived = False
        if known is not None:
            if known["removed"]:
                # Back from wherever it had gone (out of the Trash, say): the
                # tombstone gives way to the mail.
                revived = True
            elif role == "sent" and known["direction"] == "in" \
                    and not self._sent_on(mail, mail.resent):
                # Recorded from its INBOX copy before the Sent copy was
                # visible: it is the user's own mail, and is recorded as that
                # — whatever address it is From. A copy in Sent is the user's
                # own unless something says it is not; an alias nobody listed
                # in MAIL_OWN_ADDRESSES says nothing.
                pass
            elif role == "inbox" and known["direction"] == "out" and mail.sender \
                    and mail.sender == known["sender"] \
                    and mail.sender not in OWN_ADDRESSES \
                    and self._sent_on(mail, bool(
                        (self._recorded_facts(mail.message_id) or {}).get("resent"))):
                # The other way round: recorded from a Sent copy that says
                # itself it was sent on, and here is its INBOX copy, from
                # someone else. Somebody's mail the user redirected is still
                # theirs, in either order. (The same From on both: it is the
                # copy on record that was never the user's. A mail that
                # merely arrives under the id of one the user sent — ids are
                # the sender's to choose, and whoever was sent a mail knows
                # its id — replaces nothing.)
                pass
            elif mail.message_id in self._placeholders:
                if not self._own_copy(mail.message_id, mail.headers):
                    # A mail under the id of one on record as a placeholder
                    # — and not the copy that was made from, whether it
                    # reads or not (a mail that cannot be read still has its
                    # header block). Ids are the sender's to choose, and
                    # whoever was sent a mail knows its id: this one
                    # replaces nothing either. It is one more place, marked
                    # so that the placeholder's reading again does not come
                    # back to it — or stay with it, for good, in place of
                    # the copy that may read by now.
                    self._locate(role, uid, mail.message_id, "other")
                    return
                if mail.unreadable is not None or not backfill:
                    # The copy itself, and it still does not read: the
                    # record it has is the one it keeps. Or it reads, where
                    # the folders are followed (moved into INBOX, say): the
                    # placeholder's reading again writes it, quietly, as old
                    # mail (_retry_set_aside) — following the folders waits
                    # on no record written again, should that fail (I4).
                    self._locate(role, uid, mail.message_id)
                    return
                # On record as a placeholder, and here is the very copy it
                # was made from, read: the mail takes the placeholder's
                # place, as what that was recorded as.
                outbound = known["direction"] == "out"
            else:
                self._locate(role, uid, mail.message_id)
                return
        if not outbound and known is None and direction is None \
                and self._is_news(mail, policy):
            self._locate(role, uid, mail.message_id, "news")
            return
        # (A record written again replaces the one on record in its place,
        # and only once it stands: should this fail, the mail is still the
        # record it was, and is read again from wherever it is — _write.)
        self._write(mail, fetched, outbound=outbound,
                    backfill=backfill and not revived, replace=known)
        self._locate(role, uid, mail.message_id)
        if mail.unreadable is None:
            # No placeholder (any more): what else lies under its id is a
            # second copy like any mail's — one more place of the mail on
            # record, not set apart from it.
            self.index.run("UPDATE locations SET skipped = NULL WHERE message_id = ? "
                           "AND skipped = 'other'", (mail.message_id,))

    def _own_copy(self, message_id: str, headers: str) -> bool:
        """Whether a mail with this header block is the copy the placeholder
        under ``message_id`` was made from: its header block is the one the
        placeholder's record keeps, byte for byte. A move keeps a mail's
        header block as it stands, so the copy is known wherever it is by
        now, and after a restart; no other mail has it — what arrives is
        given trace fields of its own by the server that takes it in."""
        return self._recorded_head(message_id) == headers.encode("utf-8", "replace")

    def _sent_on(self, mail: Mail, resent: bool) -> bool:
        """Whether the copy of ``mail`` in Sent is positively someone else's
        mail: sent on as it stood (``resent``, read off the Sent copy), not
        From an address known to be the user's, and claimed by no outbound
        note. A From alone is no such evidence — the user's own mail goes
        out under addresses MAIL_OWN_ADDRESSES does not list (a plus-address,
        a send-as alias), and its INBOX copy comes back under the same."""
        return (resent and mail.sender not in OWN_ADDRESSES
                and self._author_noted(mail) is None)

    def _author_noted(self, mail: Mail):
        """The author an outbound note names for this mail, or None."""
        noted = self.index.one("SELECT author FROM notes WHERE message_id = ?",
                               (mail.message_id,))
        return noted["author"] if noted else None

    def _is_news(self, mail: Mail, policy) -> bool:
        """A news sender or list starts no chat (the news rail has the mail) —
        unless the mail continues a thread that is already mirrored, normally
        because the user wrote in it: then it is the answer to their post."""
        try:
            news = _triage.email_gate_decision(mail.sender, mail.list_id, pol=policy)["news"]
        except Exception as exc:  # noqa: BLE001 - when in doubt, mirror
            print(f"[mail-gateway] e-mail gate failed ({exc}); mirroring the mail", flush=True)
            return False
        return bool(news) and not any(self.threads.chat_of(cited) for cited in mail.cited)

    def _store_parts(self, mail: Mail, fetched: Fetched, stored: list) -> dict:
        """Store a mail's parts as blobs; returns the record's references.
        Every blob id written is appended to ``stored`` as it is, so the
        caller can undo them if the record itself cannot be written.

        Every blob here is minted for this one record and referenced by no
        other — which is what lets a tombstone unlink a mail's blobs without
        reading the ledger (``own_blobs``). A later phase that cites a mail's
        blob from a second record must give that up first."""
        def keep(data: bytes, content_type: str, file_name=None) -> str:
            media_id = _ibstore.store_media(INBOUND_STORE_DIR, data, content_type,
                                            file_name=file_name)
            stored.append(media_id)
            return _MEDIA_REF + media_id

        refs: dict = {"attachments": [], "content_ids": {}}
        # The three parts of the original, each normalised to UTF-8 text so a
        # reader needs no charset but the one stated.
        if mail.plain is not None:
            refs["body_part"] = keep(mail.plain.encode("utf-8", "replace"),
                                     "text/plain; charset=utf-8")
        if mail.html is not None:
            refs["html_part"] = keep(mail.html.encode("utf-8", "replace"),
                                     "text/html; charset=utf-8")
        refs["header_part"] = keep(mail.headers.encode("utf-8", "replace"),
                                   "text/plain; charset=utf-8")
        if fetched.size > MAIL_MAX_MAIL_BYTES:
            # Only the head of the mail was fetched: its text parts come
            # first and are whole, its attachments are cut off or missing.
            data, file_name = _oversized_mail_note(fetched.size, mail.parts)
            refs["attachments"].append(keep(data, "text/plain; charset=utf-8", file_name))
            return refs
        for part in mail.parts:
            if len(part.data) > MAIL_MAX_PART_BYTES:
                data, file_name = _not_mirrored_note(part)
                refs["attachments"].append(keep(data, "text/plain; charset=utf-8", file_name))
                continue
            # The type and the name are the sender's claims; store_media keeps
            # each as one clean line and a bare base name, never as a path.
            ref = keep(part.data, part.content_type, part.file_name)
            if part.inline_image:
                meta = _ibstore.media_meta(INBOUND_STORE_DIR, stored[-1]) or {}
                if _trim.is_tracking_pixel(meta.get("width"), meta.get("height")):
                    _ibstore.discard_media(INBOUND_STORE_DIR, stored.pop())
                    continue   # a pixel is not an attachment
            refs["attachments"].append(ref)
            if part.content_id:
                refs["content_ids"][ref] = part.content_id
        if mail.parts_dropped:
            data, file_name = _dropped_parts_note(mail)
            refs["attachments"].append(keep(data, "text/plain; charset=utf-8", file_name))
        return refs

    @staticmethod
    def _reading_text(mail: Mail) -> tuple[str, str]:
        """``(kb:text, kb:trim)`` of a mail: its trimmed text, capped."""
        if mail.unreadable is not None:
            return UNREADABLE_TEXT, "deterministic"
        # The trim cuts on evidence only (scripts/mail_trim.py): history that
        # carries no ">" goes only from a mail that cites a parent, an
        # unsubscribe block or a list footer only from mail that says itself
        # it is bulk. Without the evidence the text stays and the state says
        # a better trim is wanted.
        trimmed = _trim.trim(mail.plain, mail.html, subject=mail.subject,
                             is_reply=bool(mail.cited), bulk=mail.bulk)
        if len(trimmed.text) <= MAX_RECORD_TEXT_CHARS:
            return trimmed.text, trimmed.state
        # Cut, and said to be: the mark for a reader, the state for whoever
        # trims better later. The whole text is the body blob.
        return trimmed.text[:MAX_RECORD_TEXT_CHARS].rstrip() + "\n…", "wanting"

    def _write(self, mail: Mail, fetched: Fetched, *, outbound: bool, backfill: bool,
               replace=None) -> None:
        """Thread one mail, store its parts, write its record, tell the rail.

        In an order that leaves, wherever it stops, exactly one record and a
        rail that is told at least once: the claim (index), the record
        (ledger), then — one transaction — the claim confirmed and the event
        queued. A stop before the record leaves a claim that is dropped; a
        stop after it leaves a claim whose record stands, which _prepare
        confirms and announces.

        ``replace`` is the row of the record on file for this mail, when it
        is written again (I3): then there is no claim. The new record takes
        the old one's place in one rename, and until then — a write that
        fails, the process killed — the old one stands as it was, with its
        row and its places; only its flags in `claims` say that a write was
        begun, so that _prepare tells the rail whichever record stands. The
        old record's blobs go once the new one stands. Where the old record's
        file is gone (lost, or a ledger restored under the running gateway),
        nothing stands to be replaced: the record is written as the first
        time, under a name of its own, with the row standing as it was
        until then — and the index is matched with the ledger again before
        the next pass (_prepare), which also says what is to become of the
        row should this write fail."""
        now = self._clock()
        ts = min(mail.date, now) if mail.date is not None else now
        # Threaded by when the server received the mail, not by its Date
        # header: which chat survives a merge, and which mail names a thread,
        # goes by age, and a sender must not be able to write their mail
        # older than everyone else's. The record keeps it (kb:arrivedAt), so
        # a thread index rebuilt from the ledger goes by the same time.
        arrived = min(mail.arrived, now) if mail.arrived is not None else ts
        # (A placeholder has no subject to give: none, rather than an empty
        # one that would stand in the way of the real one after a re-read.)
        resolution = self.threads.resolve(mail.message_id, mail.cited, arrived,
                                          None if mail.unreadable else mail.subject)
        chat = resolution.chat
        # The thread index has committed the merge: it is due from here on,
        # whatever becomes of this mail below (a retry is a repeat, and a
        # repeat reports no merge).
        self._merges_due.update(resolution.merged)
        if resolution.unmerged:
            print(f"[mail-gateway] the mail {mail.message_id!r} from {mail.sender or '?'} "
                  f"cites {len(resolution.unmerged) + 1} chats, more than one mail may join "
                  f"({_threads.MAX_MERGE_CHATS}): none was merged, it is in {chat!r}",
                  flush=True)
        text, state = mail.reading or self._reading_text(mail)
        direction = "out" if outbound else "in"
        seen = "\\Seen" in fetched.flags
        old = self._record_path(replace["record"]) if replace is not None else None
        if old is not None and not old.is_file():
            old, self._reconcile = None, True
        was = _ibstore.read_record(old) if old is not None else None
        # The claim goes in before the ledger write, so a crash between that
        # write and the index update leaves a row for the next start to match
        # to the record (_prepare) instead of a second record of the same
        # mail — with what the rail is owed for it then. (A record written
        # again has its row: only the flags go in.)
        self.index.atomic([
            None if replace is not None else
            ("INSERT OR REPLACE INTO mails (message_id, record, direction, chat, ts, sender, "
             "sender_name, recipients, cc, removed, arrived) "
             "VALUES (?, '', ?, ?, ?, ?, ?, ?, ?, 0, ?)",
             (mail.message_id, direction, chat, ts, mail.sender, mail.sender_name,
              json.dumps(mail.to), json.dumps(mail.cc), arrived)),
            ("INSERT OR REPLACE INTO claims (message_id, backfill, seen) VALUES (?, ?, ?)",
             (mail.message_id, int(backfill), int(seen)))])
        stored: list = []
        author = None
        try:
            refs = self._store_parts(mail, fetched, stored)
            common = dict(
                channel=CHANNEL, chat=chat, account=ACCOUNT, text=text,
                message_id=mail.message_id, timestamp=ts,
                attachment_urls=refs["attachments"], sender_name=mail.sender_name,
                subject=mail.subject, recipients=[a for a, _n in mail.to],
                cc=[a for a, _n in mail.cc], in_reply_to=mail.in_reply_to,
                references=mail.references,
                list_id=_triage.email_list_id(mail.list_id) or None,
                body_part=refs.get("body_part"), html_part=refs.get("html_part"),
                header_part=refs["header_part"], trim=state,
                content_ids=refs["content_ids"], arrived_at=arrived)
            if outbound:
                # Whichever client sent it: the own-device echo, unless
                # someone said who wrote it.
                author = self._author_noted(mail) or "device"
                _urn, path = _ibstore.write_outbound(
                    INBOUND_STORE_DIR, author=author, sender=mail.sender or None,
                    replace=old, **common)
            else:
                _urn, path = _ibstore.write_message(
                    INBOUND_STORE_DIR, sender=mail.sender,
                    delivered=_delivered_on_write(direction), replace=old, **common)
        except Exception:
            for media_id in stored:
                _ibstore.discard_media(INBOUND_STORE_DIR, media_id)
            try:
                self.index.atomic([
                    # (A claim goes: its mail is not yet mirrored, and its
                    # caller's mark comes back to it. A record written again
                    # is the one it was.)
                    ("DELETE FROM mails WHERE message_id = ? AND record = ''",
                     (mail.message_id,)),
                    ("DELETE FROM claims WHERE message_id = ?", (mail.message_id,))])
            except sqlite3.Error:
                self._reconcile = True   # _prepare drops it, before anything is ingested
            raise
        if was is not None:
            # What the record it replaced named is named by none now (each
            # mail's blobs are its record's alone: _store_parts).
            for ref in [was.get(key) for key in ("body_part", "html_part", "header_part")] \
                    + list(was.get("attachments") or ()):
                media_id = _ibstore.media_id_of(ref or "")
                if media_id and media_id not in stored:
                    _ibstore.discard_media(INBOUND_STORE_DIR, media_id)
        try:
            if replace is not None:
                # The row says what the record in its place now does — before
                # the event is made of it. (Its flags in `claims` stand until
                # the event is queued: a stop in between is announced at the
                # next start.)
                self.index.run(
                    "UPDATE mails SET direction = ?, chat = ?, ts = ?, sender = ?, "
                    "sender_name = ?, recipients = ?, cc = ?, removed = 0, arrived = ? "
                    "WHERE message_id = ?",
                    (direction, chat, ts, mail.sender, mail.sender_name,
                     json.dumps(mail.to), json.dumps(mail.cc), arrived, mail.message_id))
            if resolution.merged:
                # Before the mail itself is announced, where it can be.
                self._apply_merges()
            event = self._inbound_event(
                direction=direction, chat=chat, message_id=mail.message_id, ts=ts,
                sender=mail.sender or None, sender_name=mail.sender_name, text=text,
                attachments=refs["attachments"], author=author,
                subject=mail.subject or None, backfill=backfill, seen=seen)
            self.index.atomic([
                ("UPDATE mails SET record = ? WHERE message_id = ?",
                 (path.name, mail.message_id)),
                ("DELETE FROM claims WHERE message_id = ?", (mail.message_id,)),
                self._event("inbound", event)])
        except Exception:
            # The record stands and the index does not know it. Nothing may be
            # ingested before the two are matched up (poll does that first).
            self._reconcile = True
            raise
        if mail.unreadable is not None:
            self._placeholders.add(mail.message_id)
        else:
            self._placeholders.discard(mail.message_id)

    def _inbound_event(self, *, direction: str, chat: str, message_id: str, ts: float,
                       sender, sender_name, text: str, attachments: list, author,
                       subject, backfill: bool, seen: bool) -> dict:
        """What the rail is told of one mirrored mail."""
        thread = self.thread(chat, claimed=message_id) or {}
        event = {
            "direction": direction, "channel": CHANNEL, "chat": chat, "account": ACCOUNT,
            "sender": sender, "sender_name": sender_name,
            "chat_name": thread.get("name") or None, "group": bool(thread.get("group")),
            "message_id": message_id, "ts": ts, "text": text[:MAIL_RAIL_TEXT_CHARS],
            "attachments": attachments, "author": author,
            "gate": dict(MIRROR_GATE), "subject": subject,
            "participants": thread.get("participants") or [], "peer": thread.get("peer"),
        }
        if backfill:
            # Old mail: the rail must not treat it as an arrival, and may
            # start the chat's read mark from what the mailbox says was read.
            event["backfill"] = True
            event["seen"] = seen
        return event

    def _event_of_record(self, fields: dict, *, backfill: bool, seen: bool) -> dict:
        """The same, for a mail whose record stands and whose event was never
        queued: read off the record."""
        outbound = fields.get("type") == _ibstore.T_OUTBOUND
        sender = fields.get("sender") or ""
        return self._inbound_event(
            direction="out" if outbound else "in", chat=fields["chat"],
            message_id=fields["message_id"], ts=self._record_time(fields),
            sender=sender if "@" in sender else None, sender_name=fields.get("sender_name"),
            text=fields.get("text") or "", attachments=list(fields.get("attachments") or []),
            author=fields.get("author") if outbound else None,
            subject=fields.get("subject_line") or None, backfill=backfill, seen=seen)

    # -- merges ----------------------------------------------------------------

    def _apply_merges(self) -> None:
        """Re-key the records of chats the thread index merged away, then tell
        the web gateway — only once the ledger is clean (rekey_chat fails
        closed), so a merge a full disk interrupts is finished by a later poll.
        The index and the event are one transaction; a stop after the ledger
        and before it is announced by the next start (_prepare)."""
        merged: dict = {}
        for old in sorted(self._merges_due):
            member = self.index.one("SELECT message_id FROM mails WHERE chat = ? LIMIT 1", (old,))
            into = self.threads.chat_of(member["message_id"]) if member else None
            if not into or into == old:
                self._merges_due.discard(old)
                continue
            # The chat's records are the ones the index names — every record
            # of this account is indexed (_prepare) — so only they are read.
            records = [self._record_path(row["record"]) for row in self.index.rows(
                "SELECT record FROM mails WHERE chat = ? AND record != ''", (old,))]
            try:
                result = _ibstore.rekey_chat(INBOUND_STORE_DIR, old, ACCOUNT, into,
                                             paths=records)
            except Exception as exc:  # noqa: BLE001 - one merge, not every pass after it
                if _environment(exc):
                    raise
                print(f"[mail-gateway] merging chat {old!r} into {into!r} failed "
                      f"({type(exc).__name__}: {exc}); will retry\n{traceback.format_exc()}",
                      flush=True)
                continue
            if result["errors"]:
                print(f"[mail-gateway] merging chat {old!r} into {into!r}: "
                      f"{result['errors']} record(s) could not be re-keyed; will retry",
                      flush=True)
                continue
            merged.setdefault(into, []).append(old)
        for into, absorbed in merged.items():
            self.index.atomic(
                [("UPDATE mails SET chat = ? WHERE chat = ?", (into, old)) for old in absorbed]
                + [self._event("merge", {"channel": CHANNEL, "account": ACCOUNT,
                                         "from": absorbed, "into": into})])
            self._merges_due.difference_update(absorbed)
            print(f"[mail-gateway] merged chat(s) {absorbed!r} into {into!r}", flush=True)

    # -- the rail's queue ------------------------------------------------------

    @staticmethod
    def _event(kind: str, payload: dict) -> tuple | None:
        """The statement that queues one rail event — for a transaction with
        the change it announces. None with no rail configured: the reader has
        the ledger."""
        if not _chats.chats_enabled():
            return None
        # Everything a mail put into the event was scrubbed where it was
        # decoded; this is the door, and nothing that is not text leaves by
        # it: a lone surrogate would be passed on as an escape the receiver
        # turns back into one, and it writes what it gets into files.
        text = json.dumps(payload, ensure_ascii=False).encode("utf-8", "replace").decode("utf-8")
        return ("INSERT INTO outbox (kind, payload) VALUES (?, ?)", (kind, text))

    def _flush_rail(self) -> bool:
        """Offer the web gateway everything it has not taken, in order. An
        event it cannot take now stays queued for the next poll. False when
        the web gateway is not answering at all."""
        if not _chats.chats_enabled():
            return True
        after = 0
        while True:
            batch = self.index.rows(
                "SELECT seq, kind, payload FROM outbox WHERE seq > ? ORDER BY seq LIMIT 200",
                (after,))
            if not batch:
                return True
            for row in batch:
                after = row["seq"]
                try:
                    payload = json.loads(row["payload"])
                except ValueError:
                    outcome = "drop"
                else:
                    outcome = _rail_send(row["kind"], payload)
                if outcome in ("ok", "drop"):
                    self.index.run("DELETE FROM outbox WHERE seq = ?", (row["seq"],))
                elif outcome == "down":
                    return False


# ── The mirror loop ───────────────────────────────────────────────────────────

def _policy_problem() -> str | None:
    """Why the e-mail policy cannot be read right now; None when it can."""
    if not POLICY_PATH.exists():
        return (f"there is no e-mail policy file at {POLICY_PATH} yet "
                f"(the retinue container writes it when it starts)")
    try:
        # Read here as well: the loader takes a file it cannot open for an
        # empty policy, and "no flags" is exactly what must not be assumed.
        POLICY_PATH.read_text(encoding="utf-8")
        _triage.load_email_policy(POLICY_PATH)
    except Exception as exc:  # noqa: BLE001
        return f"the e-mail policy file {POLICY_PATH} cannot be read ({exc})"
    return None


def _policy_wait(loop: dict, now: float) -> str | None:
    """The mirror does not run without the e-mail policy: which mail is news
    is decided once, when a mail is first read, and read without the flags
    every newsletter — of the ninety days of a first backfill, or of today —
    becomes a chat and stays one. The retinue side creates the file (empty if
    it has nothing to say) at its own start, and until it has there is no
    telling how long that takes — so the wait has no end of its own, before
    the first backfill or after it. MAIL_POLICY_WAIT_SECONDS is the opt-out: a
    deployment that sets it proceeds, as with no flags, once the gateway has
    been up that long."""
    problem = _policy_problem()
    if problem is None:
        return None
    if 0 < MAIL_POLICY_WAIT_SECONDS <= now - loop["started"]:
        if not loop.get("gave_up"):
            loop["gave_up"] = True
            print(f"[mail-gateway] {problem}; MAIL_POLICY_WAIT_SECONDS "
                  f"({int(MAIL_POLICY_WAIT_SECONDS)}s) is over: mirroring as with no flags",
                  flush=True)
        return None
    return f"the mirror is waiting for the e-mail policy: {problem}"


# Said once the wait is a fault: where to look, since it is not the mailbox.
POLICY_WAIT_REMEDY = (
    " — the mailbox is reachable, this is the deployment's setup: check the retinue "
    "container's start log, and that both containers name the same file (a pinned "
    "TRIAGE_EMAIL_WHITELIST_PATH must reach the gateway's own mount)")


def _fault_wait(faults: int) -> float:
    """How long what failed on a fault of the mirror's own waits for its
    next try, after ``faults`` such failures in a row: one poll interval,
    then twice as long each time, up to MIRROR_FAULT_WAIT_MAX_SECONDS (and
    never less than the interval a deployment set, however long)."""
    return float(min(max(MIRROR_FAULT_WAIT_MAX_SECONDS, MAIL_POLL_SECONDS),
                     MAIL_POLL_SECONDS * 2 ** min(faults - 1, 16)))


def _loop_step(mirror: Mirror, loop: dict) -> float:
    """One turn of the mirror loop; returns how long to sleep before the next.
    Never raises: a failure is a health state, and the next turn tries again.

    A turn is two passes, and a fault of the mirror's own delays only the
    one it was in. FOLLOWING the folders — new mail, vanished mail — comes
    first and by itself, every poll interval, whatever became of the rest
    last time. THE REST — finding mail again, reading mail again, the
    backfill — is what an earlier look left to do: when it fails, it is
    tried again with a later look at the folders, after one, then two, then
    four of them … while new mail goes on being mirrored in between."""
    now = time.time()
    loop.setdefault("started", now)
    try:
        mirror.mailbox.ensure()
        waiting = _policy_wait(loop, now)
        if waiting:
            if now - loop["started"] < POLICY_WAIT_GRACE_SECONDS:
                _set_state(connected=True, error=None, notice=waiting, blocked="policy")
            else:
                # Not a start any more: nothing is being mirrored, and the
                # gateway monitor should say so — and say what, which is not
                # the mailbox (it answered just now).
                _set_state(connected=False, error=waiting + POLICY_WAIT_REMEDY,
                           notice=None, blocked="policy")
            return min(MAIL_POLL_SECONDS, 5.0)
        sync = now - loop.get("synced", 0.0) >= MAIL_POLL_SECONDS
        if sync:
            mirror.poll(sync=True, work=False)
            loop["synced"] = now
            loop["faults"] = 0
        more = False
        held = loop.get("rest_faults", 0) > 0
        if held and sync and loop.get("rest_skip", 0) > 0:
            loop["rest_skip"] -= 1      # its wait, counted in looks at the folders
        elif sync or not held:
            try:
                more = mirror.poll(sync=False, backfill_limit=MAIL_BACKFILL_BATCH)
                loop["rest_faults"] = 0
            except MailboxError:
                raise
            except Exception as exc:  # noqa: BLE001 - reported, and tried again later
                faults = loop["rest_faults"] = loop.get("rest_faults", 0) + 1
                wait = _fault_wait(faults)
                loop["rest_skip"] = int(wait // MAIL_POLL_SECONDS) - 1
                loop["rest_error"] = (
                    f"the mirror stopped on an internal error: {exc}"[:440]
                    + " (new mail is still mirrored meanwhile)")
                print(f"[mail-gateway] the work an earlier look left to do failed ({faults} "
                      f"time(s) in a row; next try in {wait:.0f} s, new mail is followed "
                      f"meanwhile): {exc}\n{traceback.format_exc()}", flush=True)
        if loop.get("rest_faults", 0) > 0:
            # New mail is mirrored, and still the mirror is at fault: what it
            # cannot do must not go unseen behind what it can.
            _set_state(connected=False, blocked="mirror", error=loop["rest_error"],
                       last_poll=time.time())
        else:
            _set_state(connected=mirror.fault is None, error=mirror.fault,
                       notice=mirror.health_notice(), blocked=None, last_poll=time.time())
        if more:
            return MAIL_BACKFILL_PAUSE_SECONDS
        return max(1.0, MAIL_POLL_SECONDS - (time.time() - loop.get("synced", now)))
    except MailboxError as exc:
        mirror.mailbox.close()
        _set_state(connected=False, error=str(exc)[:500], blocked=None)
        print(f"[mail-gateway] poll failed: {exc}", flush=True)
    except Exception as exc:  # noqa: BLE001 - reported, and retried next turn
        # The mailbox answered (a MailboxError is caught above): what failed
        # is the mirror itself — a MirrorFault, a disk, an index, a bug —
        # and the gateway monitor must not word that as a refused login.
        _set_state(connected=False, blocked="mirror",
                   error=f"the mirror stopped on an internal error: {exc}"[:500])
        # It failed while following the folders: the mail in hand is new
        # mail, and none behind it can be mirrored before it. Nothing
        # changes between two tries that fail the same way, and each fetches
        # that mail whole again before it gets to where it failed (a full
        # disk and one mail of 50 MB: 72 GB a day off a server that counts).
        # So the wait doubles with every such failure in a row, up to an
        # hour, and a look at the folders that goes through resets it.
        loop["faults"] = faults = loop.get("faults", 0) + 1
        wait = _fault_wait(faults)
        print(f"[mail-gateway] poll failed ({faults} time(s) in a row; next try in "
              f"{wait:.0f} s): {exc}\n{traceback.format_exc()}", flush=True)
        return wait
    # A whole interval after a failure: a refused login retried every few
    # seconds is how a mailbox gets locked.
    return float(MAIL_POLL_SECONDS)


def _mirror_loop(mirror: Mirror) -> None:
    loop: dict = {}
    while True:
        time.sleep(_loop_step(mirror, loop))


# ── HTTP API ──────────────────────────────────────────────────────────────────

READ_ONLY_ERROR = {"error": "the mail mirror is read-only"}
# Present so a caller gets a reason, not a 404: each is a later phase's.
_READ_ONLY_POSTS = ("/send", "/chats/delete", "/chats/read", "/chats/archive")
_NOTE_AUTHORS = ("agent", "user")


def _is_pending_sends(path: str) -> bool:
    return path == "/pending-sends" or path.startswith("/pending-sends/")


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _reply(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _reply_raw(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        # One clean line, whatever the sender declared (load_media). Whether a
        # browser may show the blob as that type is the web gateway's call.
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def _authorized(self) -> bool:
        if not GATEWAY_TOKEN:
            return False   # fail closed: no token set is not "open" (TOKEN_PROBLEM)
        auth = self.headers.get("Authorization", "")
        token = auth[len("Bearer "):].strip() if auth.startswith("Bearer ") else ""
        # Compared as bytes: compare_digest refuses text that is not ASCII, and
        # a header is whatever bytes the peer sent (read here as Latin-1) — a
        # token with one such byte is a wrong token, not a dropped connection.
        return bool(token) and hmac.compare_digest(
            token.encode("latin-1", "replace"), GATEWAY_TOKEN.encode("utf-8", "surrogateescape"))

    def _read_body(self, limit: int) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            return None
        if length <= 0 or length > limit:
            return None
        return self.rfile.read(length)

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in ("", "/health"):
            self._reply(200, _health_snapshot())
            return
        if not self._authorized():
            self._reply(401, {"error": "unauthorized"})
            return
        media = re.match(r"^/media/([^/?]+)$", path)
        if media:
            # Attachments and the three part blobs; load_media validates the
            # id before it touches the disk.
            got = _ibstore.load_media(INBOUND_STORE_DIR, media.group(1))
            if got is None:
                self._reply(404, {"error": "not found"})
            else:
                self._reply_raw(200, got[0], got[1])
            return
        if _is_pending_sends(path):
            self._reply(501, READ_ONLY_ERROR)
            return
        if path == "/recent-chats":
            self._reply(200, {"recent_chats": MIRROR.recent_chats() if MIRROR else []})
            return
        if path.startswith("/threads/"):
            chat = unquote(path[len("/threads/"):])
            thread = MIRROR.thread(chat) if MIRROR and chat else None
            if thread is None:
                self._reply(404, {"error": "not found"})
            else:
                self._reply(200, thread)
            return
        self._reply(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if not self._authorized():
            self._reply(401, {"error": "unauthorized"})
            return
        if path in _READ_ONLY_POSTS or _is_pending_sends(path):
            self._reply(501, READ_ONLY_ERROR)
            return
        if path != "/outbound-note":
            self._reply(404, {"error": "not found"})
            return
        raw = self._read_body(MAX_BODY_BYTES)
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except (ValueError, UnicodeDecodeError):
            body = None
        if not isinstance(body, dict):
            self._reply(400, {"error": "body must be a JSON object"})
            return
        message_id = _threads.normalize_id(str(body.get("message_id") or ""))
        author = str(body.get("author") or "").strip().lower()
        if not message_id:
            self._reply(400, {"error": "message_id (a Message-ID) is required"})
            return
        if author not in _NOTE_AUTHORS:
            self._reply(400, {"error": "'author' must be one of " + "|".join(_NOTE_AUTHORS)})
            return
        if MIRROR is None:
            self._reply(503, {"error": "the mail gateway is not configured"})
            return
        try:
            MIRROR.note_author(_fit_id(message_id), author)
        except (sqlite3.Error, OSError) as exc:
            print(f"[mail-gateway] could not keep an outbound note: {exc}", flush=True)
            self._reply(503, {"error": "could not keep the note; retry later"})
            return
        self._reply(200, {"ok": True})


def _serve_http() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), _Handler)
    print(f"[mail-gateway] HTTP API listening on port {HTTP_PORT} "
          + ("(token required)" if GATEWAY_TOKEN else "(/health only: no token is set)"),
          flush=True)
    server.serve_forever()


def main() -> None:
    global MIRROR, READER
    if not CONFIGURED:
        # Stay up with /health reporting configured: false, like the other
        # gateways: an unconfigured channel is a deployment choice, not a fault.
        print(f"[mail-gateway] {CONFIG_PROBLEM} — idling (health reports unconfigured)",
              flush=True)
    elif TOKEN_PROBLEM is not None:
        # Not mirrored either: a ledger nothing may read is no use, and the
        # mailbox must not be read into a container whose API is not guarded.
        print(f"[mail-gateway] {TOKEN_PROBLEM} — idling (health reports unconfigured)",
              flush=True)
    elif MAIL_CONFIG is None:
        print(f"[mail-gateway] the mailbox is not usable: {CONFIG_PROBLEM}", flush=True)
    else:
        # Started with the first mail it is to read, and again whenever one
        # had to be ended.
        READER = Reader()
        MIRROR = Mirror(Mailbox(MAIL_CONFIG))
        print(f"[mail-gateway] mirroring {ACCOUNT} (read-only; backfill "
              f"{MAIL_BACKFILL_DAYS} days)", flush=True)
        threading.Thread(target=_mirror_loop, args=(MIRROR,), name="mail-mirror",
                         daemon=True).start()
    _serve_http()


if __name__ == "__main__":
    if sys.argv[1:2] == [READER_FLAG]:
        _serve_reader(int(sys.argv[2]))
    else:
        main()
