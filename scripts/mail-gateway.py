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
                                          ├─events, offers──▶ the web gateway (chats rail)
                                          └─news mail──▶ the web gateway (news rail)

**It mirrors, and it writes** (docs/mail-chats.md, "Writing"): replies and
new mail leave through ``POST /send`` under the e-mail send policy, and what
the user does to a chat reaches the mailbox — read, archive, delete.

**And it is mail's delivery gate** (docs/mail-chats.md, "The gate") — the
messengers' inbound flow, with a mailbox behind it. A mail that arrives is
persisted ``delivered: false``, gated on two axes — its group (``news`` /
``quieted`` / ``ignored``, on the ``List-Id`` or the sender address) and its
sender (VIP / instructed / everyone else, from the address book's projection),
where the sender is the one address of the one ``From`` header or nobody
(:func:`_gate_sender`) — and offered to the chats rail with a handover. The
record turns
``delivered`` only when the rail accepts it, and where a model turn works the
mail (a VIP's, an instructed sender's), only when that turn's job is done.
There is **no triage forward and no daily sweep**: a mail the rail did not
take stays ``delivered: false`` and is offered again on later polls — the
ledger's flag is the queue, so the wait survives a restart. A mail of a
``news`` group goes to the news feed from here; it starts no chat — unless
its sender is a VIP or instructed: the axes are independent — and where its
group is also ``ignored`` it is marked read and moved out of the INBOX once
the feed has it. Old mail — the backfill — is never gated: it is
written delivered and announced without a handover. The model trim is a
later phase.

**Reading and writing are kept apart.** The mirror reads: folders are opened
with EXAMINE and bodies fetched with BODY.PEEK, so no pass over the mailbox
can change a flag. A folder is opened read-write only for the one command
that writes — setting ``\\Seen``, moving a thread's mails, appending the Sent
copy — and is back to read-only before the adapter returns. Nothing is ever
expunged but the very UIDs a move just copied away.

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
participants and counts, ``POST /outbound-note`` so a mail an agent or the
user sent through ``email_client.py`` is not recorded as "some mail client",
``POST /send`` with the pending-send store behind it (``/pending-sends`` and
its verbs, on the SMS gateway's model, plus ``dismiss`` for a send that
failed), ``POST /reply-preview`` — whom a reply would be sent to, by the
function that addresses the send itself, so that what a page shows is what
goes out — ``POST /chats/read|archive|delete``, the last behind the
erase capability as well (``CHAT_ERASE_TOKEN``), and the model trim's two
halves: ``POST /messages/trim-source`` (the numbered lines a model may choose
a ``wanting`` mail's own message from, and their digest) and ``POST
/messages/trim-result`` (the choice, made the record's text from this
gateway's own copy of those lines — the caller is never trusted with text).

**A send is made at most once, and a mail that went out has its record** —
as far as one process can see to that against a mail server it shares no
transaction with. Every send, queued and approved or direct, is an entry of
the pending-send store on the state volume, and its entry is marked as it
goes: ``sending`` (the decision stands; nothing has left), ``data`` (written
the moment before the message is handed to the SMTP server), ``sent``
(written the moment the server says it took it). Then the record with the
rail's event, and the Sent copy — each found again by the mail's Message-ID,
and never made twice. A stop is resumed from the last mark (``Sends.recover``,
``Mirror._resume_sends``). The one stretch no mark can close, between
``data`` and ``sent``, is never resumed by sending again: the mail is looked
for in Sent — which settles it wherever the provider files sent mail itself —
and where the mailbox cannot say, the send ends as failed, saying that its
outcome is not known.

**What a caller is told is what is known, and no later than it is known.**
``/send`` answers once the SMTP server has taken the mail: the Sent copy is
filed by the mirror loop, and neither it nor a pass that has the mirror's
lock holds the answer back — a caller that gave up waiting would report the
mail as not sent, and its retry would be a second mail. A mail that may have
gone out is never answered as failed (``unconfirmed``, ``outcome_unknown``).
A caller may name its request (``request_key``): a repeat under the same key
is answered with what became of the first, and sends nothing. And a send
that did fail does not just vanish from its chat: its entry is listed with
the reason until someone dismisses it. The addresses the server refused are
on the entry, in the answer and on the record (whose recipients are those it
took).

What it tells the web gateway, over the chats rail: every mail that arrived
(an offer: the gate's verdict, a handover, and for a VIP's or an instructed
sender's mail its attachments), every old mail and every mail it sent (held,
no handover — nothing rings and no turn starts), a thread merge, and a
removal. All of it waits in one queue on the state volume while the web
gateway is away, and leaves in order — an event the web gateway fails on
waits its own time and holds back only its own chat's later events
(``Mirror._flush_rail``); the ledger is the record either way. A
mail is recorded exactly once and the rail is told at least once, wherever
the process is stopped: an index change and the event that announces it are
one transaction, and what a stop between the ledger and the index left
undone is found and announced at the next start (``Mirror._prepare``). An
offer is made once in its place in that queue and, while the mail stays
undelivered, again on later polls (``Mirror._offer``).

Everything that speaks IMAP is confined to the "mailbox adapter" section; the
rest works on the adapter's small surface and is unit-tested against a fake
mailbox in tests/test_mail_gateway.py (the adapter itself, against a scripted
IMAP server, in tests/test_mail_gateway_imap.py; sending, the queue and the
mailbox following the chat in tests/test_mail_gateway_send.py; the gate, the
rail's answers and the news rail in tests/test_mail_gateway_gate.py). SMTP is
``email_client``'s, used as a library.
"""
import base64
import codecs
import contextlib
import dataclasses
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
import smtplib
import socket
import sqlite3
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NamedTuple
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import email_client as _ec
import inbound_store as _ibstore
import mail_threads as _threads
import mail_trim as _trim
import triage_policy as _triage
import chat_ingest as _chats
import job_delivery as _jobs
import news_ingest as _news
import build_stamp as _build
import send_origin as _origin
from reply_tokens import ReplyTokenStore

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


def _config(name: str | None, suffix: str):
    """``(Config, why it cannot send | None)``. email_client's Config wants an
    SMTP server, since everything it is for ends in a send; a mailbox is still
    a mailbox without one, and is mirrored — /health and ``POST /send`` then
    say that nothing can be sent, and why. Raises as Config does for anything
    else that is missing."""
    key = "SMTP_HOST" + suffix
    if (os.environ.get(key) or "").strip():
        return _ec.Config(name), None
    had = os.environ.get(key)
    os.environ[key] = "unset.invalid"     # read once, by the constructor below
    try:
        cfg = _ec.Config(name)
    finally:
        if had is None:
            del os.environ[key]
        else:
            os.environ[key] = had
    cfg.smtp_host = ""
    return cfg, (f"{key} is not set: the mailbox is mirrored, but no mail can be "
                 f"sent from it until the SMTP server is configured")


def _load_config():
    """``(configured, Config | None, problem | None, why it cannot send |
    None)`` for this container.

    Not configured means the account's address variable is absent: the
    deployment runs no mail mirror, which is a choice, not a fault. An address
    with the rest missing is a configuration error and is reported as one —
    but for the SMTP server alone (see _config).

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
                             f"(add it to mirror this mailbox)"), None
    if not os.environ.get("EMAIL_USER" + suffix, "").strip():
        return False, None, f"EMAIL_USER{suffix} is not set", None
    try:
        cfg, no_smtp = _config(MAIL_ACCOUNT_NAME or None, suffix)
        return True, cfg, None, no_smtp
    except (_ec.EmailError, ValueError) as exc:
        return True, None, str(exc), None


CONFIGURED, MAIL_CONFIG, CONFIG_PROBLEM, SMTP_PROBLEM = _load_config()
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

# The archive and the Trash folder. A name the deployment SET is the folder,
# whatever the server marks: "set" is the variable being present and not
# empty in this container's environment (compose passes both through, so one
# left out of .env — or left empty there — is not set). Unset, the server's
# own folder for the role is used where it marks one (SPECIAL-USE: \Archive,
# else \All; \Trash), and the default name only where it marks none.
MAIL_ARCHIVE_FOLDER_SET = bool(os.environ.get("MAIL_ARCHIVE_FOLDER", "").strip())
MAIL_TRASH_FOLDER_SET = bool(os.environ.get("MAIL_TRASH_FOLDER", "").strip())
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
# notes, the pending sends), per account so a volume reused for another
# mailbox starts clean.
MAIL_DATA_DIR = Path(os.environ.get("MAIL_DATA_DIR", "/root/.local/share/mail"))
MAIL_STATE_DIR = MAIL_DATA_DIR / (re.sub(r"[^a-z0-9._@+-]", "_", ACCOUNT) or "unconfigured")
MAIL_RECENT_CHATS_MAX = _env_int("MAIL_RECENT_CHATS_MAX", 200, 1)
MAIL_NOTES_MAX = 2000

# ── Writing ───────────────────────────────────────────────────────────────────
# The send policy is EMAIL_SEND_POLICY, keyed by this account's address and
# read by email_client (resolve_category) at every send, so the gateway and
# email_client.py never disagree about it. Unset, or unreadable: verify.
#
# A mail the gateway sent is on record before its copy in Sent can be seen —
# appended by the gateway a moment later, or filed by the provider in its own
# time (SMTP_SAVE_SENT=false). For this long a record of the gateway's own
# send that has no place in the mailbox yet is not looked for, and so not
# called gone.
MAIL_SENT_GRACE_SECONDS = _env_float("MAIL_SENT_GRACE_SECONDS", 600.0)
# The separate capability POST /chats/delete requires (X-Chat-Erase-Token), as
# on every gateway (see telegram-gateway.py). Unset, the endpoint refuses.
CHAT_ERASE_TOKEN = os.environ.get("CHAT_ERASE_TOKEN", "").strip()
SEND_APPROVAL_BASE_URL = os.environ.get("SEND_APPROVAL_BASE_URL", "").rstrip("/")
# Optional override for the /sends/<slug>/<id> segment; normally unset (the
# slug is the Host header's service name — see telegram-gateway.py).
SEND_APPROVAL_SLUG = os.environ.get("SEND_APPROVAL_SLUG", "").strip("/")
# What one /send may carry. The body cap is the other gateways' (attachments
# travel base64 in the JSON); the rest are bounds on a single mail no real
# one reaches.
MAX_SEND_BODY_BYTES = 25 * 1024 * 1024
MAX_SEND_TEXT_CHARS = 1024 * 1024
MAX_SEND_RECIPIENTS = 100
MAX_SEND_ATTACHMENTS = 20
MAX_SUBJECT_CHARS = 500
# How many ids a reply's References header carries: the thread's root and the
# nearest parents (RFC 5322 lets a long chain be cut in the middle).
MAX_REFERENCES = 30
# How long a send waits for the mirror's lock before its record is left to
# the mirror loop (which writes it at once): the mail is out either way, and
# the caller's answer must not wait out a backfill batch.
RECORD_LOCK_SECONDS = 5.0
# And how long a read, an archive or a delete waits for it before answering
# "busy": under the web gateway's own deadline for the call (30 s).
EXCLUSIVE_SECONDS = 20.0
MAIL_PENDING_SENDS_DIR = MAIL_STATE_DIR / "pending-sends"
DEFAULT_SEND_CATEGORY = "verify"

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
# A read or an archive names the mails it is for (``message_ids``), and a
# long thread has many, each as long as a Message-ID may be.
MAX_CHAT_ACTION_BYTES = 1024 * 1024

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
# What the rail is posted with. The web gateway takes an e-mail event only
# with a token (docs/mail-chats.md, "The gate"): CHATS_INGEST_TOKEN where the
# deployment set one, else MAIL_GATEWAY_TOKEN, which both sides hold already —
# this gateway mirrors nothing without it. Not chat_ingest's own value: that
# falls back to CONVERSATION_BACKEND_TOKEN, which is not taken for mail.
RAIL_TOKEN = os.environ.get("CHATS_INGEST_TOKEN", "").strip() or GATEWAY_TOKEN
# How long the look at whether the web gateway answers at all may take, after
# an offer it gave no answer for (_rail_alive).
RAIL_PROBE_TIMEOUT = 5.0
# The rail is a freshness hint, the ledger is the record: an event carries at
# most this much of a mail's text, so one enormous mail cannot be refused by
# the web gateway's body limit.
MAIL_RAIL_TEXT_CHARS = 64 * 1024
# What rides along with the offer of a VIP's or an instructed sender's mail:
# its attachments, base64 in the event, so the turn that works the mail can
# open them. Within the rail's limits — one file may be as large as the web
# gateway takes one (its per-file attachment cap, 25 MB), and one event
# carries so many, of so much in all; a file beyond either stays in the
# ledger, where the chat shows it.
MAIL_RAIL_FILE_BYTES = _env_int("MAIL_RAIL_FILE_BYTES", 25 * 1024 * 1024, 1)
RAIL_FILES_BYTES = 50 * 1024 * 1024
RAIL_FILES_MAX = 20
# A rail call that carries files is given at least this long.
RAIL_FILES_TIMEOUT = 30.0
# How long the job of a turn that works a mail is waited for before the mail
# counts as not taken (and is offered again).
MAIL_RAIL_JOB_TIMEOUT = _env_float("MAIL_RAIL_JOB_TIMEOUT", 3600.0, 60.0)
# A mail the rail answered for and did not take — no acceptance, a job that
# failed — is offered again after RETRY_SECONDS, then twice as long each
# time, up to RETRY_MAX_SECONDS; at most OFFER_BATCH such mails a poll, the
# oldest first. (A news mail the feed refused is retried by the same clock.)
RETRY_SECONDS = 60.0
RETRY_MAX_SECONDS = 3600.0
OFFER_BATCH = 20
# While the web gateway does not answer at all, nothing is offered: the next
# try waits this long after the first failure, twice as long after each
# further one, up to the maximum.
RAIL_RETRY_SECONDS = 30.0
RAIL_RETRY_MAX_SECONDS = 900.0
# After this many failures in a row /health says so, while mails wait; and
# when every offer and event of a pass — this many at least — was answered
# with a failure (or with none while the web gateway answers otherwise), it
# says the rail answers but takes no mail, until one is taken.
RAIL_DOWN_NOTICE = 3
# A rail call that gets no answer in time costs MAIL_RAIL_TIMEOUT. After this
# many in a row the web gateway counts as not answering, for the rest of the
# pass and its backoff: a rail that is up and too slow for every event does
# not hold the mirror for a timeout per owed mail.
RAIL_SILENT_MAX = 3
# A rail event the web gateway fails on (an offer, a told event, a merge or a
# removal) holds back its chat's later events — keeping the order within the
# chat — for this many of its own attempts at most. After that it steps
# aside: one mail the other side always fails on must not stall its thread.
RAIL_HOLD_ATTEMPTS = 3
# The mark on a queued removal that has let its chat's later events go ahead
# of it — it waits for a merge (Mirror._overtaken). Never sent.
OVERTAKEN = "overtaken"

# ── The news rail ─────────────────────────────────────────────────────────────
# A mail of a `news` group is filed to the news feed (news_ingest; the
# endpoint is NEWS_INGEST_URL, the web gateway's). One whose group is also
# `ignored` is then taken out of the INBOX: marked read and moved to
# MAIL_NEWS_FOLDER. Unset, that is the archive folder, by the rules above;
# set and empty, the mail is marked read and stays where it is.
#
# Both were the retired triage gate's, as TRIAGE_NEWS_FOLDER and
# TRIAGE_NEWS_EXCERPT_CHARS: an operator's value for an old name is read
# where the new one is not set (unset and empty stay apart — an empty folder
# keeps news in the INBOX), and the start says so.
def _renamed(name: str, value, old: str, old_value):
    """A setting's value: ``value`` (read under ``name``), else ``old_value``
    where only the old name is set."""
    if value is None and old_value is not None:
        print(f"[mail-gateway] {old} is read as {name}, its new name — rename it in .env",
              flush=True)
        return old_value
    return value


MAIL_NEWS_FOLDER = _renamed("MAIL_NEWS_FOLDER", os.environ.get("MAIL_NEWS_FOLDER"),
                            "TRIAGE_NEWS_FOLDER", os.environ.get("TRIAGE_NEWS_FOLDER"))
if MAIL_NEWS_FOLDER is not None:
    MAIL_NEWS_FOLDER = MAIL_NEWS_FOLDER.strip()
# How much of a mail's text a feed item carries beside its subject.
MAIL_NEWS_EXCERPT_CHARS = _renamed(
    "MAIL_NEWS_EXCERPT_CHARS",
    _env_int("MAIL_NEWS_EXCERPT_CHARS", 600) if os.environ.get("MAIL_NEWS_EXCERPT_CHARS")
    else None,
    "TRIAGE_NEWS_EXCERPT_CHARS",
    _env_int("TRIAGE_NEWS_EXCERPT_CHARS", 600) if os.environ.get("TRIAGE_NEWS_EXCERPT_CHARS")
    else None)
if MAIL_NEWS_EXCERPT_CHARS is None:
    MAIL_NEWS_EXCERPT_CHARS = 600
NEWS_BATCH = 20
# How many filed news mails are remembered, so that a copy met again (a
# folder renumbered, the archive looked at) is not filed a second time.
NEWS_DONE_MAX = 2000
# The link a mail declares for itself (see _declared_url).
MAX_URL_CHARS = 2000

# How many mails one poll looks for again (after a UIDVALIDITY change, a
# rebuilt index, or because they left their folder) before it yields.
RELOCATE_BATCH = 200
# How many mails whose record is gone from the ledger one poll looks for by
# their Message-ID (Mirror._read_lost): each is a search of three folders
# and a mail read whole, and a ledger restored from an old backup may lose
# thousands at once.
LOST_BATCH = 50
# How often such an id is looked for (at the set-aside back-off) before what
# was found of it is read as mail nothing is known of, and the id leaves the
# list: none is looked for for ever.
LOST_TRIES = 8
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


def _sender_policy_path() -> Path:
    """The ``email`` channel's policy file — who is a VIP, who is instructed
    — which the retinue side projects from the address book
    (``contacts.sync_policy``) into the ledger volume's ``policy/`` folder,
    beside the group flags and in the messengers' shape. Read raw off this
    container's mount, as every gateway reads its own."""
    if os.environ.get("TRIAGE_MESSENGER_DIR"):
        return Path(_triage.messenger_policy_path(CHANNEL))
    return INBOUND_STORE_DIR / "policy" / "policy.nt"


SENDER_POLICY_PATH = _sender_policy_path()


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
        # Whether a mail can leave through this gateway at all, and if not,
        # why — one sentence a chat can show. Not "is it connected": a
        # mailbox can be mirrored and still have no SMTP server to send by.
        "can_send": configured and MAIL_CONFIG is not None and SMTP_PROBLEM is None,
        "cannot_send_reason": (SMTP_PROBLEM if configured and MAIL_CONFIG is not None
                               else None),
        # How many of the gateway's own sends the provider was seen to file
        # under a Message-ID of its own. Their Sent copies are matched to
        # the sends (one record, one chat) and answers to them thread — but
        # only where the provider files a copy: one that rewrites the id and
        # files none leaves nothing to see, here or anywhere, and an answer
        # to such a mail starts a chat of its own.
        "rewritten_message_ids": MIRROR.rewritten() if MIRROR is not None else 0,
        "backfill": backfill,
        # What is still owed: how many mirrored mails the chats rail has not
        # taken yet (``rail`` — they are offered again, see Mirror._offer),
        # since when the oldest of them has been in the mailbox (``since``),
        # and how many news mails wait for the feed or for their move out of
        # the INBOX (``news``), and how many rail events the web gateway keeps
        # failing on, which have stepped aside (``failing``), since when the
        # oldest of them (``failing_since``).
        "waiting": (MIRROR.waiting() if MIRROR is not None
                    else {"rail": 0, "since": None, "news": 0, "failing": 0,
                          "failing_since": None}),
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
# The header a mail of the gateway's own carries beside its Message-ID, with
# the same id in it. Some submission services replace a client's Message-ID
# with one of their own and keep every other header: the copy such a provider
# files in Sent is then known for the gateway's own send by this one
# (Mirror._own_send). It is evidence on a copy in Sent only, and only
# together with the rest of what the gateway knows of that send — never on a
# mail that came in, whose headers are its sender's to write.
OWN_ID_HEADER = "X-Retinue-Message-ID"
# The headers of the two large services known to rewrite, in which they keep
# the id they replaced; read like the gateway's own.
_REPLACED_ID_HEADERS = (OWN_ID_HEADER, "X-Microsoft-Original-Message-ID",
                        "X-Google-Original-Message-ID")
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
# The extension of a file name that may travel with a file (Mirror._rail_files).
_FILE_SUFFIX_RE = re.compile(r"\.[A-Za-z0-9]{1,8}")
# A Reply-To field line in a header block as it is stored: whether a reply
# must ask the reader whom the mail named (Mirror.reply_drafts) at all.
_REPLY_TO_LINE_RE = re.compile(rb"(?im)^reply-to[ \t]*:")
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


# When the reading in hand must be done, on the monotonic clock (``until``;
# absent while nothing is being read against the clock) — per thread: the
# mirror reads on its own, and a send's reply is addressed off a header block
# on an HTTP thread meanwhile (Mirror.reply_drafts). The reader process reads
# one mail at a time.
_READ_UNTIL = threading.local()


class _within:
    """``with _within(seconds):`` — what runs inside is a reading that must
    be done in that time. Its own code asks :func:`_tick`."""

    def __init__(self, seconds: float):
        self.seconds = seconds

    def __enter__(self):
        _READ_UNTIL.until = time.monotonic() + self.seconds
        return self

    def __exit__(self, *_exc):
        _READ_UNTIL.until = None


def _tick() -> None:
    """Called wherever the reader's own code is entered in the course of one
    reading; raises :class:`TookTooLong` once the reading is past its
    deadline. A call into the standard library that does not come back is
    not interrupted by this — that is what the reader process is for."""
    until = getattr(_READ_UNTIL, "until", None)
    if until is not None and time.monotonic() > until:
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
    sender: str = ""                        # what it shows as from: From, else Sender; "" if none
    sender_name: str | None = None
    gate_sender: str = ""                   # whom the gate takes it to be from (_gate_sender)
    to: list = field(default_factory=list)  # [(address, display name | None)]
    cc: list = field(default_factory=list)
    reply_to: list = field(default_factory=list)   # likewise; whom an answer goes to
    submitted_as: str | None = None         # another id it says it was sent under (OWN_ID_HEADER)
    in_reply_to: str | None = None
    references: list = field(default_factory=list)
    cited: list = field(default_factory=list)
    list_id: str = ""                       # the raw List-Id header
    archive_url: str = ""                   # the web version it declares of itself (_declared_url)
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


def _split_head(raw: bytes) -> tuple[bytes, bytes, bool]:
    """A raw mail as ``(header block, everything from the empty line on,
    cut)``: the header block cut at the last whole line within
    MAX_HEADER_BYTES, and ``cut`` whether it had to be — whether headers
    of the mail are left out of what is parsed."""
    end = _HEADER_END_RE.search(raw)
    head, rest = (raw[:end.start()], raw[end.start():]) if end else (raw, b"")
    cut = len(head) > MAX_HEADER_BYTES
    if cut:
        head = head[:MAX_HEADER_BYTES]
        head = head[:head.rfind(b"\n") + 1].rstrip(b"\r\n")
    return head, rest, cut


def _head_and_rest(raw: bytes) -> tuple[bytes, bytes]:
    """The same, without saying whether the header block was cut."""
    head, rest, _cut = _split_head(raw)
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


# An http(s) link in an Archived-At value, bracketed or bare.
_DECLARED_URL_RE = re.compile(r"<\s*(https?://[^>\s]+)\s*>|(https?://\S+)")


def _declared_url(msg) -> str:
    """The web version a mail declares of itself, or "": its ``Archived-At``
    (RFC 5064) — the link a feed item made of the mail points at.

    Only that header, which names this one mail. Not ``List-Archive`` (RFC
    2369): that names the list's archive, the same on every mail of the
    list — and the feed keys an item on its link, so every issue after the
    first would be taken for the first, dropped by the feed, and taken out
    of the INBOX all the same. Nor a link picked out of the body, which
    would be a guess — the first URL of a newsletter is as often a tracking
    pixel or an unsubscribe link as the article. A mail without one is
    filed with no link, and the feed keys its item on its text. The value
    is the sender's to write, so it is taken only as what it must be: one
    http(s) URL, on one line, of a length a URL has. Whether the feed shows
    it is the web gateway's call."""
    value = _clean_line(_UNFOLD_RE.sub("", _raw_header(msg, "Archived-At")), MAX_FIELD_BYTES)
    for match in _DECLARED_URL_RE.finditer(value):
        url = (match.group(1) or match.group(2)).strip()
        if len(url) <= MAX_URL_CHARS:
            return url
    return ""


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


# ASCII letters only: what an address is compared by (_gate_sender).
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _folds_into_ascii(ch: str) -> bool:
    """Whether a character that is not ASCII stands in for ASCII: it turns
    into ASCII on its own under a reading another reader may apply — its
    compatibility form is ASCII (a fullwidth letter, a ligature of ASCII
    letters), or its lowercase or case fold is one ASCII character (KELVIN
    SIGN is "k", LONG S is "s").

    Not a letter whose case mapping merely holds ASCII letters beside
    others, or as several: "ß" folds to "ss", "İ" lowercases to "i" and a
    combining dot, "ı" folds to itself. Those are letters of their own in
    an internationalised address, which no reader takes for its ASCII
    neighbour one character for one."""
    if ch.isascii():
        return False
    compatible = unicodedata.normalize("NFKC", ch)
    if compatible and compatible.isascii():
        return True
    return any(len(mapped) == 1 and mapped.isascii() for mapped in (ch.lower(), ch.casefold()))


def _gate_sender(msg) -> str:
    """The address the gate takes a mail to be from (docs/mail-chats.md, "The
    gate"), or "" — when it is from nobody the gate can name.

    Exactly one From header, holding exactly one address — no group, no
    second mailbox — and the address one the gateway's own grammar reads as
    one (_ADDRESS_RE). That is the only form a provider's DMARC check is
    sure to have looked at: it authenticates the From domain, and commonly
    does not evaluate a From with no address, or with several. So the
    Sender header never stands in for it, nor does the first of several
    addresses, nor an entry the grammar rejected while another was kept:
    any of these and the mail is from nobody — it still reaches its chat,
    and buys no turn. (kb:sender, what the chat shows, may still come from
    them: :func:`parse_mail`.)

    Lowercased by ASCII letters alone, and refused outright when it holds a
    character that stands in for ASCII on some reading (_folds_into_ascii):
    a lookalike must not compare equal to the address it imitates in a
    reader that lowercases or normalises further.

    ``msg`` must be the whole header block: of a mail whose block was cut
    at MAX_HEADER_BYTES the From headers are not all known, and the caller
    takes it as from nobody (:func:`parse_mail`)."""
    try:
        if msg is None or len(msg.get_all("From") or []) != 1:
            return ""
        raw = _UNFOLD_RE.sub("", _raw_header(msg, "From")).strip()
        if not raw or len(raw) > MAX_FIELD_BYTES:
            return ""
        pairs = email.utils.getaddresses([raw])
        if len(pairs) != 1:
            return ""
        # The structured reading too: getaddresses reads a group of one as
        # its one member; a From is a mailbox, never a group.
        groups = email.policy.default.header_factory("From", raw).groups
        if len(groups) != 1 or groups[0].display_name is not None \
                or len(groups[0].addresses) != 1:
            return ""
        address = (pairs[0][1] or "").strip()
        if (groups[0].addresses[0].addr_spec or "").strip() != address:
            return ""
    except Exception:  # noqa: BLE001 - a From that cannot be read names nobody
        return ""
    if any(_folds_into_ascii(ch) for ch in address):
        return ""
    address = address.translate(_ASCII_LOWER)
    return address if _ADDRESS_RE.fullmatch(address) else ""


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
    head, rest, cut = _split_head(raw)
    msg = _parse_bytes(head + rest)
    mail = Mail(message_id=_identify(msg, head), arrived=internal_date)
    mail.subject = _header_text(msg, "Subject")
    # What the chat shows the mail as from: the From, else the Sender. The
    # gate goes by _gate_sender alone — and a header block cut at its bound
    # names nobody: a second From past the cut is not seen, so "exactly one"
    # cannot be told.
    sender = _addresses(msg, "From") or _addresses(msg, "Sender")
    if sender:
        mail.sender, mail.sender_name = sender[0]
    mail.gate_sender = "" if cut else _gate_sender(msg)
    mail.to = _addresses(msg, "To")
    mail.cc = _addresses(msg, "Cc")
    mail.reply_to = _addresses(msg, "Reply-To")
    mail.submitted_as = _submitted_as(msg, mail.message_id)
    _read_ids(msg, mail)
    mail.list_id = _clean_line(_UNFOLD_RE.sub("", _raw_header(msg, "List-Id")), 998)
    mail.bulk = _is_bulk(msg, mail.list_id)
    mail.archive_url = _declared_url(msg)
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


def _submitted_as(msg, message_id: str) -> str | None:
    """Another id a mail says it was submitted under (_REPLACED_ID_HEADERS:
    the gateway's own, a provider's note of the one it replaced); None
    where it names none but its own."""
    return next(
        (other for other in (_fit_id(found) for found in (
            _threads.normalize_id(_raw_header(msg, name)) for name in _REPLACED_ID_HEADERS)
            if found) if other != message_id), None)


def stub_mail(raw: bytes, internal_date: float | None, reason: str) -> Mail:
    """What is recorded of a mail that could not be read: who sent it, when,
    the ids that place it in its thread and the list it came through (a
    newsletter is one whether or not it can be read) — nothing else of it.
    And the id it says it was submitted under, which is no part of its
    record: the Sent copy of a send of the gateway's own is known by it
    as that send's (Mirror._own_send). Never raises.

    Its header block alone is parsed (no body, so no parts and no charset),
    each field on its own, and a field that will not read is left out. The
    id is the one the whole mail would have had, so the mail is still one
    record however many folders hold it, and is followed like any other."""
    try:
        head, _rest, cut = _split_head(raw)
    except Exception:  # noqa: BLE001 - whether it was cut is not known: it was
        head, cut = bytes(raw[:MAX_HEADER_BYTES]), True
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
        mail.gate_sender = "" if cut else _gate_sender(msg)

    def date():
        mail.date = _read_date(msg, internal_date)

    def list_id():
        # Whether it is news is decided by these two: an unreadable
        # newsletter is still no chat.
        mail.list_id = _clean_line(_UNFOLD_RE.sub("", _raw_header(msg, "List-Id")), 998)

    def submitted_as():
        mail.submitted_as = _submitted_as(msg, mail.message_id)

    if msg is not None:
        mail.resent = _is_resent(msg)
        for read in (sender, date, list_id, submitted_as, lambda: _read_ids(msg, mail)):
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


# The fields that let a header block call its mail bulk (_is_bulk), as bytes
# anywhere in the block: where none of them is, the block cannot say so.
_BULK_FIELDS_RE = re.compile(rb"(?i)list-id|list-unsubscribe|precedence")


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


def _bulk_facts(head: bytes) -> dict:
    """What the model trim asks of a header block the ledger keeps
    (Mirror._trim_evidence): whether it declares its mail bulk
    (:func:`_is_bulk`), as the ingest read it — ``{"bulk": bool}``."""
    msg = _parse_bytes(head + b"\r\n\r\n")
    return {"bulk": _is_bulk(msg, _clean_line(
        _UNFOLD_RE.sub("", _raw_header(msg, "List-Id")), 998))}


def _send_facts(head: bytes) -> dict:
    """What the writing half asks of a header block the ledger keeps
    (Mirror._sent_like, Mirror.reply_drafts): its Date line as it stands, the
    id the gateway's own header names (OWN_ID_HEADER; None where there is
    none), and the Reply-To addresses (none where they do not read — which
    takes nothing of the rest)."""
    msg = _parse_bytes(head + b"\r\n\r\n")
    try:
        reply_to = [address for address, _name in _addresses(msg, "Reply-To")]
    except Exception:  # noqa: BLE001 - left out, as stub_mail leaves an address out
        reply_to = []
    return {"date": _raw_header(msg, "Date").strip(),
            "own_id": _threads.normalize_id(_raw_header(msg, OWN_ID_HEADER)) or None,
            "reply_to": reply_to}


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
#                      block; W: what the writing half asks of it), the
#                      seconds it may take, a length and an arrival time;
#                      that many bytes
#   reader → gateway   H once, when it is ready; then per request a status,
#                      a length and that many bytes:
#                        M  the mail as read, or its stub; F its facts, or
#                           what the writing half asked (pickled, built-in
#                           types only)
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
                elif kind == b"W":
                    status, answer = b"F", pickle.dumps(_send_facts(raw))
                elif kind == b"B":
                    status, answer = b"F", pickle.dumps(_bulk_facts(raw))
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
        # One request at a time on the pipes: the mirror reads its mail, and
        # an HTTP thread may ask about a header block meanwhile (read_send_facts).
        self._lock = threading.Lock()

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
        with self._lock:
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

    def send_facts(self, head: bytes, seconds: float) -> dict:
        """:func:`_send_facts`, likewise."""
        status, answer = self._ask(b"W", head, seconds, None)
        if status == b"F":
            return _unpickled(answer)
        self._failed(status, answer)

    def bulk_facts(self, head: bytes, seconds: float) -> dict:
        """:func:`_bulk_facts`, likewise."""
        status, answer = self._ask(b"B", head, seconds, None)
        if status == b"F":
            return _unpickled(answer)
        self._failed(status, answer)

    def _failed(self, status: bytes, answer: bytes):
        if status == b"T":
            raise TookTooLong("its reading was not done in time")
        if status in (b"U", b"E"):
            what, why = _unpickled(answer)
            raise UnreadableMail(why, what) if status == b"U" else _ReadFailed(what, why)
        with self._lock:     # (no other caller's request is cut off by it)
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
        # The block was cut to its bound before it was handed on: whether it
        # had to be is known only here, and a cut block names nobody for the
        # gate (_gate_sender) — on the group axis either, which is all a
        # placeholder's gate sender is asked for (Mirror._write).
        end = _HEADER_END_RE.search(raw)
        if (end.start() if end else len(raw)) > MAX_HEADER_BYTES:
            mail.gate_sender = ""
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


def read_send_facts(head: bytes) -> dict | None:
    """:func:`_send_facts` for the gateway — a header block read as guarded
    as a mail (:func:`_read_guarded`), as :func:`read_head` is: None when
    the block cannot be read, :class:`NotRead` when the reading did not come
    back."""
    try:
        return _read_guarded(lambda seconds: READER.send_facts(head, seconds),
                             lambda: _send_facts(head))
    except UnreadableMail:
        return None


def read_bulk_facts(head: bytes) -> dict | None:
    """:func:`_bulk_facts` for the model trim — a header block read as
    guarded as a mail (:func:`_read_guarded`), as :func:`read_head` is: None
    when the block cannot be read, :class:`NotRead` when the reading did not
    come back."""
    try:
        return _read_guarded(lambda seconds: READER.bulk_facts(head, seconds),
                             lambda: _bulk_facts(head))
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


def _span(seconds: float) -> str:
    """A time span as /health words it: minutes, hours or days."""
    minutes = max(0, int(seconds // 60))
    if minutes < 120:
        return f"{minutes} minute(s)"
    if minutes < 48 * 60:
        return f"{minutes // 60} hours"
    return f"{minutes // (24 * 60)} days"


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
#   set_seen(folder, uids)          \Seen on these mails
#   move(folder, uids, to)          these mails into another folder
#   append(folder, raw)             one mail added to a folder (the Sent copy)
#   create(folder)                  a folder made, where it is not there yet
#
# The four that write are the only commands that can change the mailbox, and
# each opens its folder read-write for itself alone (Mailbox._write).
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


class FolderChanged(MailboxError):
    """A write was asked for by UIDs that no longer name the mails they were
    read as (see Mailbox._write). Nothing was written."""


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


class Moved(NamedTuple):
    """What became of a move, UID by UID. ``moved``: left the folder — each
    mapped to its UID in the destination, or to None where the server did not
    say (no COPYUID). ``left``: copied to the destination and still in the
    folder, flagged ``\\Deleted`` — the server offers no way to remove exactly
    these (no MOVE, no UIDPLUS), and nothing here ever expunges a whole
    folder; mapped like ``moved``. ``absent``: no longer in the folder when
    the move was made."""
    moved: dict
    left: dict
    absent: tuple


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


def resolve_folders(entries, sent: str, archive: str, trash: str, *,
                    archive_explicit: bool = False, trash_explicit: bool = False) -> dict:
    """Which folder plays each role: ``{inbox, sent, archive, trash}``, a name
    or None each.

    The server's own SPECIAL-USE attributes (RFC 6154) are honoured first —
    they survive a localised or renamed folder — and the given names are
    the fallback. For the archive, ``\\All`` (Gmail's "All Mail", which is where
    an archived mail lives there) stands in when nothing is ``\\Archive``.

    Not so for an archive or a Trash name the deployment set
    (``archive_explicit``, ``trash_explicit`` — see MAIL_ARCHIVE_FOLDER_SET):
    that name is the folder, and no other stands in for it. While the server
    has no folder of that name the role has none — the archive is then made
    by the first Archive, the Trash is never made (Mirror._destination)."""
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
        "archive": named(archive) if archive_explicit
        else special("\\archive", "\\all") or named(archive),
        "trash": named(trash) if trash_explicit else special("\\trash") or named(trash),
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


def _expand_uid_set(spec: bytes, limit: int = 100_000) -> list:
    """The UIDs of a set as a server writes one (``3,7:9``), in its order.
    ValueError for anything that is not one, or that names more than
    ``limit`` — a set is the server's to write, and is not expanded blindly."""
    out: list = []
    for part in spec.decode("ascii").split(","):
        low, colon, high = part.partition(":")
        low, high = sorted((int(low), int(high) if colon else int(low)))
        if len(out) + high - low >= limit:
            raise ValueError("UID set too large")
        out.extend(range(low, high + 1))
    return out


class Mailbox:
    """The IMAP side of one account.

    email_client.py logs in once per command; this is the first long-running
    IMAP client here, so the connection's life is handled in one place: every
    command runs through :meth:`_run`, which connects on demand, and on a
    dropped or timed-out connection reconnects and tries once more — servers
    close an idle session after half an hour, and that is not an outage.

    Reading cannot change the mailbox: folders are opened with EXAMINE and
    bodies read with BODY.PEEK, so no pass of the mirror sets a flag even by
    accident. The four methods that write (the last section) each open their
    folder with SELECT for that one command and put it back to EXAMINE before
    they return (:meth:`_write`)."""

    def __init__(self, cfg, timeout: float = MAIL_IMAP_TIMEOUT):
        self._cfg = cfg
        self._timeout = timeout
        self._conn = None
        self._selected = None
        self._validity = None     # the selected folder's UIDVALIDITY
        self._caps: frozenset = frozenset()   # the server's, as of the last login
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
        # What the server can do, asked once it knows who is asking (many
        # announce MOVE and UIDPLUS only then). Not known is "cannot": a move
        # then takes the careful path (see move).
        try:
            typ, data = conn.capability()
            said = data[-1] if typ == "OK" and data else b""
            self._caps = frozenset(bytes(said or b"").decode("ascii", "replace").upper().split())
        except (imaplib.IMAP4.error, OSError):
            self._caps = frozenset()
        return conn

    def close(self) -> None:
        with self._lock:
            conn, self._conn, self._selected = self._conn, None, None
        if conn is not None:
            try:
                conn.shutdown()
            except Exception:  # noqa: BLE001 - it is being dropped either way
                pass

    def _run(self, command, attempts: int = 2):
        with self._lock:
            for attempt in range(1, attempts + 1):
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
                    if attempt == attempts:
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
                                   MAIL_ARCHIVE_FOLDER, MAIL_TRASH_FOLDER,
                                   archive_explicit=MAIL_ARCHIVE_FOLDER_SET,
                                   trash_explicit=MAIL_TRASH_FOLDER_SET)
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

    # -- writing ---------------------------------------------------------------
    # The mailbox follows the chat (docs/mail-chats.md): "mark as read" and a
    # reply set \Seen, archiving moves a thread's INBOX mails to the archive
    # folder, deleting moves them to the Trash, and a mail the gateway sent is
    # appended to Sent. These four are everything that changes the mailbox.

    def _write(self, folder: str, validity: int | None, command):
        """Run ``command(conn)`` with ``folder`` opened read-write, and have
        it read-only again before returning. None when the server says there
        is no such folder.

        ``validity`` is the UIDVALIDITY the caller's UIDs were read under.
        A folder that has another one now was renumbered, and the same
        numbers name other mails: nothing is written, and FolderChanged is
        raised. Checked on the very SELECT the write runs in, so there is no
        moment between the check and the write.

        Never tried a second time on a dropped connection, unlike a read: a
        move the server carried out before the line went dead would be made
        twice. So the session is checked first (a NOOP, where reconnecting
        costs nothing — the usual dead connection is one the server closed
        while it sat idle), and a failure after that is the caller's to
        handle; every caller can, since each write is found again by what it
        left in the mailbox."""
        self.ensure()
        quoted = _ec._quote(folder)

        def run(conn):
            self._selected = self._validity = None
            try:
                typ, data = conn.select(quoted, readonly=False)
            except imaplib.IMAP4.readonly as exc:
                raise MailboxError(
                    f"the IMAP server opens {folder!r} read-only: {exc}") from exc
            if typ != "OK":
                said = _response_text(data)
                if "[NONEXISTENT]" in said.upper():
                    return None
                raise MailboxError(f"the IMAP server would not open {folder!r} "
                                   f"for writing: {said or typ}")
            try:
                if validity is not None and self._number(conn, "UIDVALIDITY") != validity:
                    raise FolderChanged(
                        f"{folder!r} was renumbered since the mirror last looked at it; "
                        f"nothing was changed, and its next poll finds the mails again")
                return command(conn)
            finally:
                # Read-only again at once, by opening it afresh. Never CLOSE:
                # on a folder opened read-write that expunges every mail
                # flagged \Deleted in it, whoever flagged it.
                # (With the UIDVALIDITY it announces on opening, as _examine
                # notes it: what a fetch from it says it is under.)
                try:
                    if conn.select(quoted, readonly=True)[0] == "OK":
                        self._selected, self._validity = folder, self._number(
                            conn, "UIDVALIDITY")
                except Exception:  # noqa: BLE001 - the connection is gone; the next command reopens it
                    self._selected = self._validity = None
        return self._run(run, attempts=1)

    @staticmethod
    def _uid_set(uids) -> str:
        return ",".join(str(uid) for uid in sorted({int(uid) for uid in uids}))

    def _present(self, conn, folder: str, uids) -> list:
        """Which of these UIDs the selected folder holds right now."""
        typ, data = conn.uid("search", None, "UID", self._uid_set(uids))
        if typ != "OK":
            raise MailboxError(f"the IMAP server refused a search of {folder!r}: "
                               f"{_response_text(data) or typ}")
        wanted = {int(uid) for uid in uids}
        return sorted({int(token) for chunk in data or [] if chunk
                       for token in chunk.split()} & wanted)

    @staticmethod
    def _response_code(conn, name: str) -> bytes | None:
        """The data of a bracketed response code the last command's answer
        carried (``[COPYUID …]``, ``[APPENDUID …]``), and forget it."""
        _typ, data = conn.response(name)
        last = data[-1] if data else None
        return bytes(last) if isinstance(last, (bytes, bytearray)) else None

    def _copied(self, conn, uids: list) -> dict:
        """``{UID here: UID there}`` from the COPYUID a COPY or MOVE answered
        with (RFC 4315); every value None where the server gave none, or one
        that does not account for exactly these mails."""
        unknown = dict.fromkeys(uids)
        try:
            _validity, source, target = (self._response_code(conn, "COPYUID") or b"").split()
            pairs = dict(zip(_expand_uid_set(source), _expand_uid_set(target), strict=True))
        except ValueError:
            return unknown
        return {uid: pairs[uid] for uid in uids} if set(pairs) == set(uids) else unknown

    def set_seen(self, folder: str, uids, *, validity: int | None = None) -> int | None:
        """Set ``\\Seen`` on these mails; returns how many of them the folder
        holds (a mail a client moved away since the last poll is not there to
        flag). None when the server says there is no such folder."""
        uids = list(uids)
        if not uids:
            return 0

        def command(conn):
            present = self._present(conn, folder, uids)
            if present:
                typ, data = conn.uid("STORE", self._uid_set(present),
                                     "+FLAGS.SILENT", "(\\Seen)")
                if typ != "OK":
                    raise MailboxError(f"the IMAP server refused to flag mail in "
                                       f"{folder!r}: {_response_text(data) or typ}")
            return len(present)
        return self._write(folder, validity, command)

    def move(self, folder: str, uids, to_folder: str, *,
             validity: int | None = None) -> Moved | None:
        """Move these mails from ``folder`` to ``to_folder`` (see
        :class:`Moved`). None when the server says ``folder`` does not exist;
        a destination that does not exist is a refusal, and raises — the
        caller makes it first (:meth:`create`).

        By ``UID MOVE`` where the server has it (RFC 6851: atomic, and the
        only way a server such as Gmail really moves). Else the three steps a
        move is made of — ``UID COPY``, ``\\Deleted`` on the originals, and
        ``UID EXPUNGE`` of exactly those UIDs, which needs UIDPLUS (RFC 4315).
        A server with neither keeps the flagged originals: a plain EXPUNGE
        removes every mail flagged ``\\Deleted`` in the folder, the user's
        own marks included, and is never sent."""
        uids = list(uids)
        if not uids:
            return Moved({}, {}, ())
        target = _ec._quote(to_folder)

        def refused(what, data, typ):
            return MailboxError(f"the IMAP server refused to {what} mail from {folder!r} "
                                f"to {to_folder!r}: {_response_text(data) or typ}")

        def command(conn):
            present = self._present(conn, folder, uids)
            absent = tuple(uid for uid in sorted({int(u) for u in uids}) if uid not in present)
            if not present:
                return Moved({}, {}, absent)
            uid_set = self._uid_set(present)
            self._response_code(conn, "COPYUID")     # an earlier command's
            if "MOVE" in self._caps:
                typ, data = conn.uid("MOVE", uid_set, target)
                if typ != "OK":
                    raise refused("move", data, typ)
                return Moved(self._copied(conn, present), {}, absent)
            typ, data = conn.uid("COPY", uid_set, target)
            if typ != "OK":
                raise refused("copy", data, typ)
            copied = self._copied(conn, present)
            # From here on the copies exist: whatever the next two steps do,
            # the answer says so, or the caller would copy them again.
            try:
                typ, data = conn.uid("STORE", uid_set, "+FLAGS.SILENT", "(\\Deleted)")
                if typ == "OK" and "UIDPLUS" in self._caps:
                    typ, data = conn.uid("EXPUNGE", uid_set)
                    if typ == "OK":
                        return Moved(copied, {}, absent)
            except (imaplib.IMAP4.error, OSError):
                pass    # the originals stay: reported as left
            return Moved({}, copied, absent)
        return self._write(folder, validity, command)

    def append(self, folder: str, raw: bytes, *, when: float | None = None) -> tuple:
        """Add one mail to ``folder``, seen, with ``when`` (epoch) as its
        arrival time. Returns ``(UIDVALIDITY, UID)`` of the new mail where
        the server names them (APPENDUID, RFC 4315), else ``(None, None)`` —
        the caller finds it by Message-ID. A refusal, a folder that does not
        exist included, raises.

        Not tried twice on a dropped connection, like every write: the caller
        looks for the mail before it appends again."""
        self.ensure()

        def command(conn):
            self._response_code(conn, "APPENDUID")   # an earlier command's
            typ, data = conn.append(_ec._quote(folder), "(\\Seen)",
                                    time.time() if when is None else float(when), raw)
            if typ != "OK":
                raise MailboxError(f"the IMAP server refused to append to {folder!r}: "
                                   f"{_response_text(data) or typ}")
            try:
                validity, uid = (self._response_code(conn, "APPENDUID") or b"").split()
                return int(validity), int(uid)
            except ValueError:
                return None, None
        return self._run(command, attempts=1)

    def create(self, folder: str) -> None:
        """Make ``folder``, and subscribe it. That it is there already is no
        failure.

        Subscribed, because that is the list many mail clients show:
        unsubscribed, the folder — and the mail the gateway moved into it —
        would be on the server and not in the user's client. A server that
        will not subscribe it has still made it, which is what the gateway
        itself goes by (it reads LIST), so that refusal is only logged."""
        self.ensure()

        def command(conn):
            typ, data = conn.create(_ec._quote(folder))
            said = _response_text(data)
            if typ != "OK" and "[ALREADYEXISTS]" not in said.upper():
                raise MailboxError(f"the IMAP server would not create {folder!r}: "
                                   f"{said or typ}")
            try:
                typ, data = conn.subscribe(_ec._quote(folder))
            except imaplib.IMAP4.error as exc:
                typ, data = "NO", [str(exc).encode("utf-8", "replace")]
            if typ != "OK":
                print(f"[mail-gateway] {folder!r} was made, but the IMAP server would "
                      f"not subscribe it ({_response_text(data) or typ}): a mail client "
                      f"that lists subscribed folders only will not show it", flush=True)
        self._run(command, attempts=1)


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
#              so it is not read again — but for "news-unread", a news mail
#              that could not be read, which the placeholders' reading again
#              comes back to (Mirror._retry_set_aside). "moved" marks a copy the gateway
#              moved on and the server kept (flagged \Deleted, no way to
#              expunge exactly it): still a place of the mail, never one to
#              move or flag again. "again" is the opposite of the first
#              three, a note with no id: nothing is known of the mail at this
#              UID, and it is to be read (Mirror._read_noted) — what lay
#              beside a placeholder that is gone, what an older build marked
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
#   notes      who authored a mail that will turn up in Sent (/outbound-note,
#              and the gateway's own sends).
#   settling   mails on record whose place in the mailbox the gateway itself
#              has just changed or is still to make: its own send, before
#              the Sent copy is there, and a mail it moved whose new UID the
#              server did not say. Not called gone before `until` — and with
#              no `until`, never: the gateway's own send where the provider
#              files the Sent copy (SMTP_SAVE_SENT=false), which stays a
#              record whether or not a copy ever shows. Gone with the first
#              place the mail is given (Mirror._locate).
#   aliases    the Message-ID a provider gave a mail the gateway sent, where
#              it replaced the one the gateway minted → that minted id, which
#              is what everything else here is keyed by. The mail is looked
#              for in a folder under either (Mirror._find). From the records
#              (kb:sentAs), like the rest.
#   outbox     rail events not yet taken by the web gateway, in order. A row
#              of kind "offer" holds no event: it marks the place in that
#              order at which a mail that arrived is first offered, and the
#              event is made of the mail's record when its turn comes. A row
#              the web gateway answered a failure for, or no answer while it
#              answers otherwise, keeps its place and is tried again once
#              `due` has passed, `attempts` counting those tries and `failed`
#              the first (Mirror._flush_rail); meanwhile only its own chat's
#              later rows wait — for RAIL_HOLD_ATTEMPTS tries, then it steps
#              aside (an offer's row goes; a told event's holds nothing, and
#              is sent as the index has its mail then). A removal waits for
#              every merge queued before it that joins its chat, and is then
#              marked `overtaken` in its payload; a merge that stepped aside
#              queues, as it is taken, the removals of the chats it absorbed
#              (Mirror._removed_under). An offer's row names the chat it
#              was queued in.
#   offers     the mails the chats rail still owes an answer for: every
#              inbound record the ledger holds ``delivered: false`` — the
#              ledger's flag is the queue, and this is its index (rebuilt
#              from it by Mirror._prepare). `queued` while the mail's first
#              offer still has its place in the outbox; after that the mail
#              is offered again once `due` has passed, `attempts` counting
#              the answers that did not take it, and `failed` when the first
#              failure was (for /health).
#   news       the mails of a `news` group: what the feed is to be given of
#              each (`item`), whether the mail is then to leave the INBOX
#              (`consume`), and how far that got — "due" (to be filed),
#              "filed" (the feed has it; the move is owed), "done" (kept a
#              while, so a copy met again is not filed twice).
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
CREATE TABLE IF NOT EXISTS settling (
    message_id TEXT PRIMARY KEY, until REAL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS aliases (
    alias TEXT PRIMARY KEY, message_id TEXT NOT NULL
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS aliases_mid ON aliases (message_id);
CREATE TABLE IF NOT EXISTS lost (
    message_id TEXT PRIMARY KEY, sender TEXT, direction TEXT, chat TEXT, arrived REAL,
    noticed REAL NOT NULL, tried REAL, tries INTEGER NOT NULL DEFAULT 0,
    due REAL NOT NULL DEFAULT 0
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS outbox (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, payload TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL DEFAULT 0, failed REAL
);
CREATE TABLE IF NOT EXISTS offers (
    message_id TEXT PRIMARY KEY, attempts INTEGER NOT NULL DEFAULT 0,
    due REAL NOT NULL DEFAULT 0, queued INTEGER NOT NULL DEFAULT 0, failed REAL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS news (
    message_id TEXT PRIMARY KEY, item TEXT NOT NULL, consume INTEGER NOT NULL,
    state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
    due REAL NOT NULL DEFAULT 0, created REAL NOT NULL
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
                if self._conn.execute("SELECT 1 FROM mails LIMIT 1").fetchone() is None:
                    # A new index: the rail has taken nothing since it began
                    # (Mirror._rail_took). One from before the key has none —
                    # what it took is not known (Mirror.health_notice).
                    self._conn.execute("INSERT OR IGNORE INTO meta (key, value) "
                                       "VALUES ('rail_took', '0')")
            elif version != INDEX_SCHEMA_VERSION:
                raise RuntimeError(f"mailbox index {path}: schema {version}, "
                                   f"expected {INDEX_SCHEMA_VERSION}")
            # An index made before a column was kept gains it: `arrived`
            # empty; a rail event's clock with every event due at once; the
            # first failure of an event or offer unknown. Each column on its
            # own test — every ALTER commits alone, and a stop between two
            # must not leave an index the next start cannot open.
            for table, column, kind in (
                    ("mails", "arrived", "REAL"),
                    ("outbox", "attempts", "INTEGER NOT NULL DEFAULT 0"),
                    ("outbox", "due", "REAL NOT NULL DEFAULT 0"),
                    ("outbox", "failed", "REAL"),
                    ("offers", "failed", "REAL")):
                if column not in {row["name"] for row in
                                  self._conn.execute(f"PRAGMA table_info({table})")}:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

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
# What goes to the web gateway: a mail (the existing /internal/chats/inbound,
# with mail's optional fields), a thread merge and a removal. They are posted
# here rather than through chat_ingest.notify_chat_event because that
# function's answer says nothing about why a call failed — and a queue that
# must not lose an event needs to know whether to retry it. The URL and the
# token are chat_ingest's.
#
# A mail goes in one of two ways. One that arrived is OFFERED (_rail_offer):
# the event carries the gate's verdict and ``handover``, and the answer says
# whether the chat took the mail — which is what turns its record
# ``delivered``. Everything else is only told (_rail_send): old mail, the
# user's own mail, a mail that came back, merges and removals have no answer
# worth more than "taken".

def _rail_url(kind: str) -> str:
    base = _chats.CHATS_INGEST_URL
    if kind == "inbound":
        return base
    head, _slash, _last = base.rstrip("/").rpartition("/")
    return f"{head}/{kind}"


def _rail_post(kind: str, payload: dict, timeout: float) -> tuple:
    """POST one event; ``(outcome, answer)``. The outcome: ``"ok"`` — taken,
    and ``answer`` is the JSON object the web gateway answered with (empty
    when it answered none); ``"drop"`` — refused as malformed, a repeat would
    be refused again; ``"later"`` — this event cannot be taken yet, others
    may (a 404 for a merge or removal: an older web gateway that does not
    know the endpoint); ``"down"`` — the web gateway is not answering, or
    refuses every event alike (the token, a URL that is not the rail);
    ``"unanswered"`` — the web gateway answered with a failure of its own
    (408, 429, 5xx), so it is up and did not take this event; and
    ``"uncertain"`` — no answer in time, or a connection closed before one:
    whether the event landed is not known, nor whether the web gateway is
    up (:func:`_rail_alive` tells that)."""
    request = urllib.request.Request(
        _rail_url(kind), data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json",
                 "X-Conversation-Backend-Token": RAIL_TOKEN,
                 "Authorization": f"Bearer {RAIL_TOKEN}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            print(f"[mail-gateway] the chats rail refused the token (HTTP {exc.code}); "
                  f"check CHATS_INGEST_TOKEN (else MAIL_GATEWAY_TOKEN) on both sides",
                  flush=True)
            return "down", {}
        if exc.code in (404, 408, 429) or exc.code >= 500:
            if exc.code == 404 and kind != "inbound":
                # A merge or removal an older web gateway does not know yet
                # must not hold the mails behind it back.
                return "later", {}
            # A URL that is not the rail refuses every mail alike; a failure
            # the web gateway answered with is about this event.
            return ("down" if exc.code == 404 else "unanswered"), {}
        print(f"[mail-gateway] the chats rail refused a {kind} event "
              f"(HTTP {exc.code})", flush=True)
        return "drop", {}
    except Exception as exc:  # noqa: BLE001 - unreachable, timed out
        print(f"[mail-gateway] the chats rail did not answer ({exc}); will retry", flush=True)
        reason = getattr(exc, "reason", None)
        # Refused, or no such host: nobody is there. Anything else — a
        # timeout, a connection the other side closed without an answer —
        # may be the event's doing as much as the rail's.
        nobody = (ConnectionRefusedError, socket.gaierror)
        return ("down" if isinstance(exc, nobody) or isinstance(reason, nobody)
                else "uncertain"), {}
    try:
        answer = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except (ValueError, UnicodeDecodeError):
        answer = {}
    # An event that was taken and answered with something else than an
    # object was still taken; it just says nothing more.
    return "ok", answer if isinstance(answer, dict) else {}


def _rail_send(kind: str, payload: dict) -> str:
    """Tell the rail one event that needs no answer but "taken": the outcome
    as _rail_post gives it — ``"ok"``, ``"drop"`` (dropped: a repeat would
    be refused again), ``"later"``, ``"down"``, ``"unanswered"`` (a failure
    the web gateway answered with, for this event) or ``"uncertain"`` (no
    answer in time; Mirror._rail_heard asks whether the rail is up)."""
    outcome, _answer = _rail_post(kind, payload, MAIL_RAIL_TIMEOUT)
    return outcome


def _rail_alive() -> bool:
    """Whether the web gateway answers at all — asked after an event it gave
    no answer for (``"uncertain"``), to tell a rail that is away from one
    that is up and failed on that one event. A GET of the rail's URL, which
    takes nothing and which any web server answers with something: any HTTP
    answer, a 404 or a 405 included, is a rail that is up."""
    try:
        with urllib.request.urlopen(urllib.request.Request(
                _rail_url("inbound"), method="GET",
                headers={"X-Conversation-Backend-Token": RAIL_TOKEN}),
                timeout=RAIL_PROBE_TIMEOUT):
            return True
    except urllib.error.HTTPError:
        return True
    except Exception:  # noqa: BLE001 - refused, unreachable, timed out
        return False


def _job_url(job_url) -> str | None:
    """Where the job a rail answer names is polled, or None when it names
    none the rail could have minted: a path on the rail's own host — checked
    as it is parsed, not as it is written (urljoin drops a tab, a CR or an
    LF before it reads a URL, so "/<tab>/host" would be another host's) — and
    the joined URL checked again for the rail's scheme and host. A job is
    polled where the rail is, and nowhere else."""
    if not isinstance(job_url, str) or not job_url.startswith("/") or len(job_url) > 1024:
        return None
    # Printable ASCII, no space and no backslash: what a path the web gateway
    # mints is, and nothing a parser could read two ways.
    if any(not "!" <= ch <= "~" or ch == "\\" for ch in job_url):
        return None
    parts = urlsplit(job_url)
    if parts.scheme or parts.netloc or not parts.path.startswith("/") \
            or parts.path.startswith("//"):
        return None
    base = _chats.CHATS_INGEST_URL
    joined = urljoin(base, job_url)
    return joined if urlsplit(joined)[:2] == urlsplit(base)[:2] else None


def _rail_offer(payload: dict) -> tuple:
    """Offer the rail one mail that arrived, with the handover; ``(outcome,
    answer)`` as _rail_post gives them. Only an answer that says
    ``"accepted": true`` means the chat has the mail; ``job_url`` beside it
    is the turn that works it. ``"uncertain"`` is neither a yes nor a no: the
    mail is offered again, and the web gateway takes a repeat of an event
    once, by its message id."""
    return _rail_post("inbound", payload,
                      max(MAIL_RAIL_TIMEOUT, RAIL_FILES_TIMEOUT) if payload.get("files")
                      else MAIL_RAIL_TIMEOUT)


def _text_only(payload: dict) -> dict:
    """An event with nothing in it that is not text. Everything a mail put
    into an event was scrubbed where it was decoded; this is the door, and a
    lone surrogate that got this far would be passed on as an escape the
    receiver turns back into one — and it writes what it gets into files."""
    return json.loads(json.dumps(payload, ensure_ascii=False)
                      .encode("utf-8", "replace").decode("utf-8"))


# The verdict a mail is TOLD with — every mail that is not offered: old mail
# (the backfill), a mail that came back after it had gone, the user's own.
# Held, nobody's VIP, nobody's instruction: the rail pushes nothing and
# starts no turn. A mail that arrives carries the gate's own verdict
# (Mirror._gate).
MIRROR_GATE = {"forward": False, "vip": False, "instructed": False, "reason": "mirror"}


def _retry_after(attempts: int) -> float:
    """How long a mail that was not taken ``attempts`` times waits for its
    next try."""
    return min(RETRY_SECONDS * 2 ** max(0, min(attempts, 16) - 1), RETRY_MAX_SECONDS)


def _news_send(item: dict) -> bool:
    """File one mail's item to the news feed; whether the feed took it."""
    return _news.forward_news(channel=CHANNEL, **item)


def _news_excerpt(text: str, limit: int) -> str:
    """A mail's text as a feed-sized excerpt: no blank runs, cut with a mark."""
    if limit <= 0:
        return ""
    lines = (line.strip() for line in (text or "").splitlines())
    text = "\n".join(line for line in lines if line)
    return text[:limit].rstrip() + ("…" if len(text) > limit else "")


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
#       like any, and read again later. (What the rail is owed goes with
#       the record: the one written again is owed what the one it replaces
#       was — offered if that was still owed, told if its chat had taken it
#       — and a placeholder is offered from nobody, so no turn works it;
#       Mirror._mirror_one, Mirror._write.) Nothing is dropped first, so no row
#       ever stands for a mail in a record's stead — and what a row knows
#       without a record is no knowledge: a row whose record is gone from
#       the ledger goes, with its places, and what it said (id, sender,
#       direction) is kept as HINTS for finding the mail again (`lost`,
#       Mirror._prepare). Its mail is looked for by its Message-ID in every
#       folder, and one mail found is read as a mail read the first time
#       where it lies — for a record that was the user's, a Sent copy
#       first, then the copy received when the record said, then the rest;
#       for any other, the one From the sender the lost record had — and it
#       is the user's own again only in Sent, or in the archive From that
#       sender and received when the record said (a move keeps the arrival
#       time). A From is never evidence by itself: it is its sender's to
#       write, and whoever was sent a mail knows its id — so a copy From
#       that sender is known to be the record's only when it also came
#       when the record said (Mirror._read_lost, Mirror._read_found). A
#       mail that cannot be told yet stays looked for, and /health counts
#       it — LOST_TRIES times at most; then what is found is read as mail
#       nothing is known of. (Read as old mail, either way: told to the
#       rail, never offered — it was heard of before its record was lost.) Nothing else is carried over to whatever mail
#       is found under its id. The limits, as they stand: while the archive
#       does not answer, every lost id waits (/health says the archive does
#       not answer, and counts them); an id whose mail is in no folder, or
#       no more where it was found, leaves the list with a log line and no
#       "removed" event (there is no record to tombstone), so a mail moved
#       during the last look can be left unmirrored; a ledger restored
#       from an older backup over a live one can leave two record files for
#       one id (a record written as the first time takes a name of its own,
#       and the backup brings back the one it had before); a mail to
#       oneself whose Sent copy is gone comes back as one that came in; so,
#       on a server that stamps a moved mail with a new arrival time, does
#       an archived sent mail; and an arrival time has whole seconds, so a
#       look-alike under the id, From the same address, delivered in the
#       same second, cannot be told from the record's own mail — archived,
#       with the Sent copy gone, it comes back as the user's, and where the
#       two lie in different folders the folder decides which is read as
#       the record's (for a mail that came in, INBOX before the archive).
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
# a place of the mail under its id (skipped NULL — or "moved": a copy the
# gateway moved on and the server kept, flagged \Deleted: it keeps the mail
# from being called gone, is never moved or flagged again, and no search
# hands it out — a delete asks after it, and one a client undeleted is the
# mail's place again, Mirror._whereabouts; a mail whose only place it is
# while it settles is looked for again, Mirror._settle_unlocated); a UID
# with no record of its own ("news", "draft", and "other" beside a
# placeholder — and "news-unread": a news mail that did not read, read again
# with the placeholders, Mirror._retry_set_aside); a UID to be read with nothing known of it ("again": what lay
# beside a placeholder that is gone, an older build's "unreadable"). An id in
# `lost` is no state of a row or of a UID: it is a mail to be looked for.

class Mirror:
    """One account's mailbox mirrored into the ledger.

    ``poll`` is one pass: follow the folders (new mail, vanished mail), look
    again for mails that lost their place and for those whose record the
    ledger lost, read again what is to be, backfill a batch, file what is
    news, and hand the rail what it is owed — the events, and the offer of
    every mail it has not taken yet. Everything it needs to resume is in the
    two indexes and the ledger; a poll that fails midway is simply run again."""

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
        # And one rewrite of a record file at a time, held around that one
        # write (or one chat's) by every writer of a record here, under the
        # mirror's lock or not — never the other way round. The model trim's
        # write holds only this one: it rewrites one record's reading, which
        # no index row says anything of, and must not wait out a pass.
        self._records = threading.RLock()
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
        self._send_said: dict = {}    # send → why it is not finished, as last logged
        # The rail, as the last calls found it: how many in a row the web
        # gateway did not answer, and until when nothing is offered over
        # that; how many calls in a row got no answer in time while it
        # answered otherwise (RAIL_SILENT_MAX); the calls of the pass under
        # way and how many of them it failed on; and whether a pass ended
        # with every one failed, and no mail was taken since — a rail that
        # answers and takes no mail. In memory only — a restart tries at once.
        self._rail_down = 0
        self._rail_wait_until = 0.0
        self._rail_silent = 0
        self._rail_tally = [0, 0]
        self._rail_refusing = False
        # Message-ID → the thread that waits for the job of the turn working
        # that mail (_await_job). In memory too: after a restart the mail is
        # offered again, and the web gateway hands a repeat its job back.
        self._jobs: dict = {}
        self._flags = None            # the group flags, read once per pass over the rail
        self._gate_said = False       # the sender policy was unreadable, as last logged
        self._news_folder_made = False
        self._news_said: dict = {}    # news mail → why it is not consumed, as last logged
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
                 # (With the id a provider gave the mail instead of its own,
                 # where it did: an answer cites that one.)
                 list(dict.fromkeys((f.get("references") or "").split()
                                    + [f.get("in_reply_to") or ""]
                                    + list(f.get("sent_as") or []))),
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
            if self._owed(by_id[mid][1]):
                # Written undelivered: a mail that arrived. What the rail is
                # owed for it is its offer, in its place in the queue.
                owed += self._owe(mid, by_id[mid][1]["chat"])
                continue
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
        # The mails the rail still owes an answer for are the ledger's to
        # say: every inbound record it holds ``delivered: false``, and no
        # other. So the queue of them survives whatever the index did not —
        # a lost state volume, a stop between an acceptance and its note
        # here. One whose first offer has no place in the outbox (any more)
        # is offered by the pass that offers again, oldest first.
        owing = {mid for mid, (_path, fields) in by_id.items() if self._owed(fields)}
        listed = {row["message_id"]: row["queued"] for row in self.index.rows(
            "SELECT message_id, queued FROM offers")}
        marked = set()
        for row in self.index.rows("SELECT payload FROM outbox WHERE kind = 'offer'"):
            try:
                marked.add(json.loads(row["payload"]).get("message_id"))
            except (ValueError, AttributeError):
                pass
        self.index.atomic(
            [("DELETE FROM offers WHERE message_id = ?", (mid,))
             for mid in listed if mid not in owing]
            + [("INSERT INTO offers (message_id) VALUES (?)", (mid,))
               for mid in owing if mid not in listed]
            + [("UPDATE offers SET queued = 0 WHERE message_id = ?", (mid,))
               for mid, queued in listed.items()
               if queued and mid in owing and mid not in marked])
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
        self.index.run("DELETE FROM settling WHERE message_id NOT IN "
                       "(SELECT message_id FROM mails)")
        # The ids a provider gave the gateway's own mails, as the records
        # have them — and as they stood for a record the ledger lost, while
        # its mail is looked for (_read_lost): it is in the mailbox under
        # them, and under its own id nowhere.
        self.index.atomic(
            [("DELETE FROM aliases WHERE message_id NOT IN (SELECT message_id FROM lost)", ())]
            + [("INSERT OR REPLACE INTO aliases (alias, message_id) VALUES (?, ?)", (alias, mid))
               for mid, (_path, fields) in by_id.items()
               for alias in fields.get("sent_as") or []])
        if unknown or owed or lost or noted:
            print(f"[mail-gateway] mailbox index reconciled with the ledger: "
                  f"{len(unknown)} record(s) adopted, {len(lost)} row(s) with no record "
                  f"dropped (their mails looked for again: {self.lost()} in all), "
                  f"{noted} place(s) to be read again; "
                  f"announced to the rail: {len(claimed)} mail(s), {len(merged)} merge(s), "
                  f"{sum(map(len, removed.values()))} removal(s)", flush=True)
        # Where the thread index and the records disagree, a merge was begun
        # and not finished (or the rebuild just found one) — a lost id's hint
        # included (_apply_merges).
        for row in self.index.rows("SELECT message_id, chat FROM mails UNION ALL "
                                   "SELECT message_id, chat FROM lost WHERE chat IS NOT NULL"):
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
            # And the news mails that could not be read (_mirror_one): no
            # record, no chat, and the feed still owed them.
            aside = sorted(self._placeholders | {row["message_id"] for row in self.index.rows(
                "SELECT l.message_id FROM locations l "
                "JOIN folders f ON f.role = l.role AND f.gen = l.gen "
                "WHERE l.skipped = 'news-unread' AND l.message_id IS NOT NULL")})
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
        renumbered). Never a place marked as another mail's (_mirror_one).
        For a news mail that could not be read, its place as that."""
        if message_id not in self._placeholders:
            return self.index.one(
                "SELECT l.role, l.uid FROM locations l "
                "JOIN folders f ON f.role = l.role AND f.gen = l.gen "
                "WHERE l.message_id = ? AND l.skipped = 'news-unread' "
                "ORDER BY l.role = 'archive', l.role, l.uid LIMIT 1", (message_id,))
        return self.index.one(
            "SELECT l.role, l.uid FROM locations l "
            "JOIN folders f ON f.role = l.role AND f.gen = l.gen "
            "JOIN mails m ON m.message_id = l.message_id "
            "WHERE l.message_id = ? AND l.skipped IS NULL AND m.removed = 0 "
            "AND m.record != '' ORDER BY l.role = 'archive', l.role, l.uid LIMIT 1",
            (message_id,))

    def _read_again(self, message_id: str, policy) -> None:
        # A placeholder read at last is old mail; a news mail that could not
        # be read is still one that arrived, and is filed once it reads.
        news = message_id not in self._placeholders
        place = self._set_aside_at(message_id)
        name = self._folders.get(place["role"]) if place is not None else None
        if name and place["role"] == "archive" and not self._settled:
            return   # the archive is not answering: another round's
        if name:
            self._ingest(place["role"], name, place["uid"], policy, backfill=not news)
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

    def rewritten(self) -> int:
        """How many of the gateway's own sends the provider was seen to file
        in Sent under a Message-ID of its own (see :meth:`_own_send`)."""
        try:
            return int(self.index.get("rewritten", 0))
        except ValueError:
            return 0

    def waiting(self) -> dict:
        """What is still owed, for /health: ``rail`` — how many mirrored
        mails the chats rail has not taken (each is offered again);
        ``since`` — when the oldest of them reached the mailbox; ``news`` —
        how many news mails wait for the feed, or for their move out of the
        INBOX; ``failing`` — how many rail events (offers among them) the
        web gateway failed on RAIL_HOLD_ATTEMPTS times or more, which have
        stepped aside and hold nothing back (Mirror._flush_rail), and
        ``failing_since`` — when the oldest of them first failed."""
        rail = self.index.one(
            "SELECT COUNT(*) AS n, MIN(COALESCE(m.arrived, m.ts)) AS since FROM offers o "
            "LEFT JOIN mails m ON m.message_id = o.message_id")
        news = self.index.one("SELECT COUNT(*) AS n FROM news WHERE state != 'done'")
        failing = self._failing()
        return {"rail": rail["n"], "since": rail["since"], "news": news["n"],
                "failing": failing["n"], "failing_since": failing["since"]}

    def _failing(self):
        """The rail events that stepped aside: how many (``n``), and when the
        oldest (``since``) and the newest (``newest``) of them first failed."""
        return self.index.one(
            "SELECT COUNT(*) AS n, MIN(failed) AS since, MAX(failed) AS newest FROM ("
            "SELECT failed FROM outbox WHERE attempts >= ? "
            "UNION ALL SELECT failed FROM offers WHERE queued = 0 AND attempts >= ? "
            "AND failed IS NOT NULL)", (RAIL_HOLD_ATTEMPTS, RAIL_HOLD_ATTEMPTS))

    def health_notice(self) -> str | None:
        """What an operator should know while the mirror works."""
        if self.notice:
            return self.notice
        if self._rail_down >= RAIL_DOWN_NOTICE:
            owed = self.waiting()["rail"]
            if owed:
                return (f"the web gateway's chats rail is not answering: {owed} mail(s) are "
                        f"mirrored and wait to be taken by their chats — they are offered "
                        f"again when it answers")
        if self._rail_refusing:
            owed = self.waiting()["rail"]
            told = self.index.one("SELECT COUNT(*) AS n FROM outbox WHERE kind != 'offer' "
                                  "AND attempts > 0")["n"]
            return (f"the web gateway's chats rail answers but takes no mail: it failed on "
                    f"every offer and event of a pass — {owed} mail(s) wait to be taken by "
                    f"their chats, {told} other event(s) to be heard; each is tried again "
                    f"on its own clock, and all of them once one is taken")
        failing = self._failing()
        if failing["n"]:
            since, newest = failing["since"], failing["newest"]
            age = (f" — the oldest for {_span(self._clock() - since)}"
                   if since is not None else "")
            try:
                took = float(self.index.get("rail_took"))
            except (TypeError, ValueError):
                took = None
            if took is None or newest is None:
                # No take was ever recorded (an index from before the key), or
                # no first failure (one from before that column): whether
                # others were taken since is not known — until the first take,
                # or a pass that takes nothing (the notice above).
                return (f"{failing['n']} rail event(s) keep failing{age}: the web "
                        f"gateway fails on each of them, which is tried again on its own "
                        f"clock (the mail gateway's log names them)")
            if took < newest:
                # Nothing was taken since the newest began to fail: that says
                # nothing of one mail — the rail may take none at all (a pass
                # with too few calls to tell does not get to the notice above).
                return (f"{failing['n']} rail event(s) keep failing and the rail has "
                        f"taken nothing since the newest of them began to fail, "
                        f"{_span(self._clock() - newest)} ago: look at the web gateway, not "
                        f"at one mail; each is tried again on its own clock (the mail "
                        f"gateway's log names them)")
            return (f"{failing['n']} rail event(s) keep failing while others are "
                    f"taken{age}: the web gateway fails on each of them, which is tried "
                    f"again on its own clock and holds back nothing (the mail gateway's log "
                    f"names them)")
        if not self.rewritten() or MAIL_CONFIG is None or not MAIL_CONFIG.save_sent:
            return None
        return ("the mail provider files its own copy of every sent mail, under a "
                "Message-ID of its own: with the gateway filing one too, each mail it "
                "sends is in the Sent folder twice — set SMTP_SAVE_SENT=false")

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
                with self._records:
                    _ibstore.update_message(self._record_path(row["record"]), author=author)

    # -- the model trim (docs/mail-chats.md, "The model trim") -----------------
    #
    # Where the deterministic trim left a mail ``wanting``, the web gateway may
    # spend one model turn on it, on its first view. This side is the two
    # halves that never trust the caller with text: what the model may choose
    # from (trim_source) and what its choice makes of the record (trim_result).
    # Both are built from the record's own parts, which nothing here changes;
    # the text a choice keeps is assembled from this gateway's copy of the
    # lines (mail_trim.apply_keep), so an answer can narrow a bubble and never
    # add a word to it. And the lines are the bubble's own: a source rebuilt
    # from the parts that is not the record's text is never handed out. Only
    # a judgement settles a mail: a choice (valid or not), the caller's word
    # that the answer was no choice (``failed``), a mail too long to hand over,
    # or one whose parts no longer give its text. A model that could not be
    # asked changes nothing here — the caller just does not call.
    #
    # Neither half waits for the mirror's lock, which a pass over the mailbox
    # holds for as long as it takes: the one write is the record's reading,
    # under the record lock (``_records``), checked there against the record
    # as it stands.

    _TRIM_PARTS = ("body_part", "html_part", "header_part")
    # What a source was built from and held against: a record that differs
    # in any of them under the lock is not the one the source is about.
    _TRIM_BASIS = _TRIM_PARTS + ("text",)

    def _trim_target(self, chat: str, message_id: str) -> tuple:
        """``(path, fields)`` of the mail a model-trim call names, read off the
        ledger now. :class:`TrimRefused` with the answer when there is none
        to trim: 404 for no such mail — or one the call does not name by its
        chat — and 410 for a mail that is gone from the mailbox.

        The mail is named by its Message-ID (as the record keys it, or as its
        header has it), and the chat is a check that the caller means this
        mail: its chat on record, or a chat that has since been merged into
        that one — a chat key is its root's id, and the thread index knows
        which chat that id is in now. So a call made before a merge lands on
        the same record after it, and a mail of another thread is refused
        whatever its id."""
        raw = message_id.strip()
        row = None
        for mid in dict.fromkeys(m for m in (raw, _fit_id(_threads.normalize_id(raw))) if m):
            row = self.index.one("SELECT message_id, record, removed FROM mails "
                                 "WHERE message_id = ? AND record != ''", (mid,))
            if row is not None:
                break
        if row is None:
            raise TrimRefused(404, {"error": "not found"})
        path = self._record_path(row["record"])
        fields = _ibstore.read_record(path)
        if (fields is None or fields.get("message_id") != row["message_id"]
                or (fields.get("account") or "").lower() != ACCOUNT):
            raise TrimRefused(404, {"error": "not found"})
        current = fields.get("chat") or ""
        if chat != current and self.threads.chat_of(chat) != current:
            raise TrimRefused(404, {"error": "not found"})
        if row["removed"] or fields.get("removed_at"):
            raise TrimRefused(410, {"error": "the mail is gone from the mailbox"})
        return path, fields

    @staticmethod
    def _trim_evidence(fields: dict) -> dict:
        """What :meth:`_reading_text` gave the trim when the mail was read,
        from the record: its subject, whether it cites a parent — the record
        keeps every id it cited (kb:inReplyTo, kb:references, see _read_ids),
        so either is there exactly when one was — and whether it calls
        itself bulk, which is the header block's to say again (a List-Id is
        on the record only as normalised, and List-Unsubscribe and
        Precedence not at all). :class:`TrimRefused` 503 when the header
        part the record references cannot be read, as for a body part: that
        says nothing about the mail. A record that references none is read
        by its List-Id.

        The block is read as the mirror reads every header block: guarded,
        in the reader process (:func:`read_bulk_facts`) — never parsed
        here, where a block the parser does not come back from would hold
        this thread — and only where it can say bulk at all: a block that
        names none of the three fields is not (the parser matches a field's
        name whatever its case, and no name is encoded), so most mails ask
        the reader nothing and never wait behind the mirror's reading of a
        mail. A reading that did not come back is no answer (I1): 503,
        nothing settled; a block the reader cannot read leaves the record's
        List-Id to say it."""
        bulk = bool(fields.get("list_id"))
        ref = fields.get("header_part")
        if ref:
            media_id = _ibstore.media_id_of(ref)
            got = _ibstore.load_media(INBOUND_STORE_DIR, media_id) if media_id else None
            if got is None:
                raise TrimRefused(503, {"error": "a part of the mail could not be read"})
            # Stored with every CRLF folded to LF (parse_mail). Folded back,
            # the parser meets the line ends the ingest's met: a CR CR LF
            # that ended the block then ends it here too, and what followed
            # it is no header now either.
            head = got[0].replace(b"\n", b"\r\n")
            if not _BULK_FIELDS_RE.search(head):
                bulk = False
            else:
                try:
                    facts = read_bulk_facts(head)
                except (NotRead, MirrorFault, ReaderDown):
                    raise TrimRefused(503, {"error": "the mail's header block could not be "
                                                     "read just now"}) from None
                if facts is not None and facts.get("bulk") is not None:
                    bulk = bool(facts["bulk"])
        return {"subject": fields.get("subject_line") or "",
                "is_reply": bool(fields.get("in_reply_to") or fields.get("references")),
                "bulk": bulk}

    @staticmethod
    def _trim_source_of(fields: dict, evidence: dict):
        """The record's :func:`mail_trim.model_source`, from its stored parts
        (the very text the ingest trimmed: the parts are that text, stored as
        UTF-8). :class:`TrimRefused` 503 when a part it references cannot be
        read — that says nothing about the mail, so nothing is settled."""
        parts = []
        for key in ("body_part", "html_part"):
            ref = fields.get(key)
            if not ref:
                parts.append(None)
                continue
            media_id = _ibstore.media_id_of(ref)
            got = _ibstore.load_media(INBOUND_STORE_DIR, media_id) if media_id else None
            if got is None:
                raise TrimRefused(503, {"error": "a part of the mail could not be read"})
            parts.append(got[0].decode("utf-8", "replace"))
        return _trim.model_source(parts[0], parts[1], evidence["subject"],
                                  is_reply=evidence["is_reply"], bulk=evidence["bulk"])

    @staticmethod
    def _trim_unoffered(fields: dict, source) -> str | None:
        """Why a record's source is not handed to a model, or None when it is
        handed over: ``too_long`` when :func:`mail_trim.model_source` gave
        none (beyond what may be handed over, or nothing that shows), and
        ``source_differs`` when the text it was split from, capped as the
        ingest caps it (:meth:`_reading_text`), is not the record's kb:text.

        The second is what keeps the model to what the bubble shows. The
        parts are immutable, but the evidence is read again (a header block
        that parses otherwise now) and the rules are the code's of today, not
        of the day the mail was read: a source that differs could offer
        lines the rules had cut — history, a footer — and a choice of them
        would put them back. Such a mail keeps the text it has."""
        if source is None:
            return "too_long"
        text = _scrub(source.text)
        if len(text) > MAX_RECORD_TEXT_CHARS:
            text = text[:MAX_RECORD_TEXT_CHARS].rstrip() + "\n…"
        return None if text == (fields.get("text") or "") else "source_differs"

    @staticmethod
    def _trim_unread(fields: dict) -> bool:
        """Whether the record is a placeholder (see UNREADABLE_TEXT): a mail
        not read yet, with nothing of its own to trim. It is never handed to
        a model — that would spend a turn on our own sentence — and it is
        not settled either: once it reads, the record written in its place
        (I3) is the mail's first reading, and a ``wanting`` one gets its
        first view then. Told by the record as _prepare tells it."""
        return (fields.get("text") == UNREADABLE_TEXT and not fields.get("body_part")
                and not fields.get("html_part"))

    @contextlib.contextmanager
    def _trim_writing(self):
        """The record lock, for a model trim's write: waited for
        EXCLUSIVE_SECONDS, :class:`Busy` when not free by then. It is held
        around one record's write, or one chat's, and never for a pass — so
        a busy answer is rare, and its caller offers the judgement again."""
        if not self._records.acquire(timeout=EXCLUSIVE_SECONDS):
            raise Busy("a record of the mailbox is being rewritten; try again in a moment")
        try:
            yield
        finally:
            self._records.release()

    def _settle_trim(self, path: Path, *, state: str, text: str | None = None) -> dict:
        """Write a trim's outcome onto the record and read it back; the
        record as it now stands. Under the record lock (the caller's): a
        record has one writer at a time. ``update_message`` touches the text
        and the state and nothing else — not a part, not an identity — and
        says nothing of whether it wrote, so the record is read back."""
        _ibstore.update_message(path, text=text, trim=state)
        after = _ibstore.read_record(path)
        if (after is None or after.get("trim") != state
                or (text is not None and after.get("text") != text)):
            raise OSError("the record could not be rewritten")
        return after

    def trim_source(self, chat: str, message_id: str) -> tuple[int, dict]:
        """POST /messages/trim-source: what a model may choose this mail's
        message from — or that there is nothing to do. Read-only, but for a
        mail whose source is not handed over (:meth:`_trim_unoffered`), which
        is settled here and now: asking again would only find it the same.
        ``{"state": "unread"}`` for a placeholder (:meth:`_trim_unread`):
        nothing to choose from yet, and nothing settled."""
        _path, fields = self._trim_target(chat, message_id)
        if self._trim_unread(fields):
            return 200, {"state": "unread"}
        state = fields.get("trim") or "deterministic"
        if state != "wanting":
            return 200, {"state": state}
        evidence = self._trim_evidence(fields)
        source = self._trim_source_of(fields, evidence)
        why = self._trim_unoffered(fields, source)
        if why is not None:
            with self._trim_writing():
                # Looked at again under the lock: settled or gone meanwhile,
                # or written anew from other parts (then its own source).
                path, now = self._trim_target(chat, message_id)
                if self._trim_unread(now):
                    return 200, {"state": "unread"}
                state = now.get("trim") or "deterministic"
                if state != "wanting":
                    return 200, {"state": state}
                if any(now.get(key) != fields.get(key) for key in self._TRIM_BASIS):
                    fields, evidence = now, self._trim_evidence(now)
                    source = self._trim_source_of(now, evidence)
                    why = self._trim_unoffered(now, source)
                if why is not None:
                    self._settle_trim(path, state="deterministic")
                    print(f"[mail-gateway] {now['message_id']!r} is not handed to a model "
                          f"({why}); its text stays as the rules trimmed it", flush=True)
                    return 200, {"state": "deterministic", "settled": why}
        sender = fields.get("sender") or ""
        return 200, {"state": "wanting", "lines": source.lines, "digest": source.digest,
                     "subject": evidence["subject"], "sender": sender if "@" in sender else "",
                     "is_reply": evidence["is_reply"], "bulk": evidence["bulk"]}

    def trim_result(self, chat: str, message_id: str, digest: str, *,
                    keep=None, failed: str | None = None) -> tuple[int, dict]:
        """POST /messages/trim-result: a model's choice (``keep``), or the
        caller's word that there is no choice (``failed``: its answer was
        none, or the mail had all its turns and gave none), made of the
        source whose digest it names.

        Refused (409 ``stale``) unless the mail is still ``wanting`` and its
        source is still that one, and still its text — a choice is about the
        lines it was made from and no others. A valid choice becomes the
        record's text, capped as every kb:text is, with ``kb:trim "model"``;
        an invalid one, and ``failed``, settle ``deterministic`` and leave
        the text as it is. A placeholder is refused with ``"state":
        "unread"`` (:meth:`_trim_unread`): no choice is about it, and it is
        not settled.

        The mirror writes a record again in its own place (I3) — a copy read
        under another id, a tombstone's mail come back, a placeholder that
        reads — and may do so between the offer of a judgement and its
        write. A record that is not, under the lock, the one the source was
        built from (other parts, another text, another state) refuses the
        judgement, as one settled meanwhile does: the choice was made of
        another record's lines. A record that is now ``wanting`` is asked
        about again, from its own source; a placeholder is ``unread``.

        Nothing is told on the rail: a mail's text changing on its record is
        how a better reading has always reached the chat (the life store
        indexes the rewrite, and a page's poll re-renders a message whose
        text or trim changed), and the caller — the web gateway — has the
        new text in this answer."""
        path, fields = self._trim_target(chat, message_id)

        def stale(record):
            state = "unread" if self._trim_unread(record) else record.get("trim")
            return 409, {"error": "stale", "state": state or "deterministic"}

        if self._trim_unread(fields) or fields.get("trim") != "wanting":
            return stale(fields)
        # Built outside the lock — a trim of a long mail takes a moment, and
        # the record lock is held for writes, not for reading — and checked
        # under it: the same parts give the same source.
        source = self._trim_source_of(fields, self._trim_evidence(fields))
        if (source is None or source.digest != digest
                or self._trim_unoffered(fields, source) is not None):
            return 409, {"error": "stale", "state": "wanting"}
        text = None if failed is not None else _trim.apply_keep(
            source.lines, keep, ended=source.ended)
        with self._trim_writing():
            path, now = self._trim_target(chat, message_id)
            if self._trim_unread(now) or now.get("trim") != "wanting":
                return stale(now)
            if any(now.get(key) != fields.get(key) for key in self._TRIM_BASIS):
                return 409, {"error": "stale", "state": "wanting"}
            if text is None:
                after = self._settle_trim(path, state="deterministic")
                why = (f"no choice ({_clean_line(failed, 120)})" if failed is not None
                       else "a choice that is none")
                print(f"[mail-gateway] model trim of {now['message_id']!r}: {why}; its text "
                      f"stays as the rules trimmed it", flush=True)
            else:
                if len(text) > MAX_RECORD_TEXT_CHARS:
                    text = text[:MAX_RECORD_TEXT_CHARS].rstrip() + "\n…"
                after = self._settle_trim(path, state="model", text=text)
        return 200, {"state": after["trim"], "text": after.get("text") or ""}

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
        with self._lock, self.rail_pass():
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
            # A record a send could not write on its way out, before Sent is
            # read: its copy there is then one more place of a mail on
            # record, whatever id it is filed under (_own_send).
            self._resume_sends(mailbox=False)
            if sync:
                self._read = self._settled = False
                self._read, self._settled = self._sync(policy)
            # What a send left for the mailbox — its Sent copy, an outcome
            # nobody saw — once the folders are known.
            self._resume_sends(mailbox=bool(self._folders))
            more = False
            if work and self._settled:
                # In this order: a mail must be found again before it can be
                # called gone, and known mail before the backfill reads the rest
                # — a move a stop cut short is put right first.
                self._recover_move()
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
            if work and self._read:
                # (With the rest of the work: filing and moving news is not
                # following the folders, and a fault in it must not hold
                # that back — I4.)
                self._file_news(NEWS_BATCH)
            if rail_up and self._flush_rail():
                # And the mails that were offered before and not taken.
                self._offer_again(OFFER_BATCH)
            return more

    def offer_from_ledger(self) -> None:
        """The rail's half of a pass, which needs no mailbox: the queued
        events and first offers, and the mails offered before and not
        taken. Everything an offer is made of is in the ledger (the record,
        its attachments) and the policy files — so a mailbox that does not
        answer holds back what arrives, and none of what has arrived
        (_loop_step). Not while the index and the ledger disagree: a poll
        matches them up first."""
        with self._lock, self.rail_pass():
            if not self._reconcile and self._flush_rail():
                self._offer_again(OFFER_BATCH)

    @staticmethod
    def _policy():
        try:
            return _triage.load_email_policy(POLICY_PATH)
        except Exception as exc:  # noqa: BLE001 - no flags is the safe reading
            print(f"[mail-gateway] e-mail policy unreadable ({exc}); mirroring "
                  f"as with no flags", flush=True)
            return _triage.EmailPolicy()

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
            f"no archive folder {MAIL_ARCHIVE_FOLDER!r} on the server yet (MAIL_ARCHIVE_FOLDER "
            f"names it; it is made the first time a chat is archived): until then a mail "
            f"moved out of INBOX shows as removed"
            if "archive" in missing and MAIL_ARCHIVE_FOLDER_SET else
            f"no archive folder ({MAIL_ARCHIVE_FOLDER!r}) on the server: a mail moved "
            f"out of INBOX shows as removed — set MAIL_ARCHIVE_FOLDER"
            if "archive" in missing else None)
        if refusal != self._archive_said:
            print(f"[mail-gateway] {self.notice}" if refusal
                  else "[mail-gateway] the archive folder answers again", flush=True)
            self._archive_said = refusal
        return read, read and refusal is None

    def _first_sight(self, role: str, name: str) -> bool:
        """Take a folder into the index. Everything in it now is old mail,
        the backfill's to read (or, in the archive, nobody's); what arrives
        later is new. False when the server says it has no such folder."""
        listing = self.mailbox.list_uids(name, since=self._clock())
        if listing is None:
            return False
        newest = self._newest(name, listing)
        self.index.run(
            "INSERT INTO folders (role, name, uidvalidity, gen, ceiling, hwm) "
            "VALUES (?, ?, ?, 1, ?, ?)", (role, name, listing.uidvalidity, newest, newest))
        self.index.set(f"top:{role}", newest)
        return True

    def _sync_folder(self, role: str, name: str, policy) -> bool:
        """Follow one folder. False only when the server says it has no such
        folder; a folder it would not open or search raises (MailboxError)."""
        folder = self._folder(role)
        if folder is None:
            return self._first_sight(role, name)
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

    def _recorded_send_facts(self, message_id: str) -> dict | None:
        """What the writing half asks of that header block
        (:func:`read_send_facts`); None when there is none, or it cannot be
        read. Raises :class:`NotRead` as :meth:`_recorded_facts` does."""
        head = self._recorded_head(message_id)
        return read_send_facts(head) if head is not None else None

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
        # (And under every id it may be filed under: a mail of the gateway's
        # own whose provider replaced its Message-ID is in the mailbox under
        # that one — _known_as.)
        known = self._known_as(message_id)
        if all(map(_searchable, known)):
            try:
                carried = ts is None or self._carried_whole(message_id)
            except NotRead:
                return [], False   # its own header block, not read: not known
            if carried:
                found = self._find_by_id(name, known)
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
            if any(wanted in ids for wanted in known):
                found.append(uid)
                if not every:
                    break
        return found, sure

    def _known_as(self, message_id: str) -> list:
        """The ids a mail may be filed under in the mailbox: its own, and
        any a provider gave it instead (``aliases``)."""
        return [message_id] + [row["alias"] for row in self.index.rows(
            "SELECT alias FROM aliases WHERE message_id = ? ORDER BY alias", (message_id,))]

    def _find_by_id(self, name: str, ids: list) -> Matches:
        """The server's search for a mail under each id it may have: every
        mail under any of them (see :class:`Matches`), the provider's ids'
        first — where there is one, that is the copy the mailbox holds — and
        newest first under each. ``whole`` once every id was answered in
        full."""
        uids, whole = [], True
        for wanted in reversed(ids):
            found = self.mailbox.find(name, wanted)
            uids += [uid for uid in found.uids if uid not in uids]
            whole = whole and found.whole
        return Matches(tuple(uids), whole)

    def _locate(self, role: str, uid: int, message_id: str | None, skipped=None,
                also=None) -> None:
        """Give a mail its place (``also``: a statement that must stand or
        fall with it)."""
        folder = self._folder(role)
        self.index.atomic([
            ("INSERT OR REPLACE INTO locations (role, gen, uid, message_id, skipped) "
             "VALUES (?, ?, ?, ?, ?)", (role, folder["gen"], uid, message_id, skipped)),
            # A mail with a place is followed like any other from here on.
            ("DELETE FROM settling WHERE message_id = ?", (message_id,))
            if message_id else None,
            also])

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
            # (With what the place was marked as: a copy the gateway moved on
            # and the server kept is still that once renumbered — "moved".)
            if head is not None and self._place(role, uid, message_id, head, skipped):
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
        tombstoned like any mail, its reading again pending or not. (A copy
        the gateway moved on and the server kept, "moved", is still one.)

        A mail whose place the gateway itself is changing (``settling``) is
        not called gone while that settles: its own send has a record before
        the Sent copy can be seen, and a mail it moved may not show in its
        new folder's search at once. Such a mail is looked for and placed,
        only not buried — and one that settles never (the provider files the
        Sent copy, or does not) is not looked for at all: a folder's own look
        places it when its copy arrives, and till then, or for good, the
        record stands for a mail that was sent.

        A mail the gateway moved whose only place is the copy the server
        kept (``moved``) is looked for while it settles, like one with no
        place: its new place was not told (_follow_move, _recover_move). A
        leftover the server flags ``\\Deleted`` no more is its place again
        (_undeleted: the move never took, or a client undid it). It is never
        buried while that leftover stands; settled and in no other folder,
        it is looked for no more — the leftover is its place, as it was."""
        after = self._settle_after or (-1.0, "")
        waiting = self.index.rows(
            "SELECT m.*, s.until AS settling, EXISTS (SELECT 1 FROM locations l "
            "WHERE l.message_id = m.message_id AND l.skipped = 'moved') AS kept "
            "FROM mails m LEFT JOIN settling s ON s.message_id = m.message_id "
            "WHERE m.removed = 0 AND m.record != '' AND NOT EXISTS "
            "(SELECT 1 FROM locations l WHERE l.message_id = m.message_id "
            "AND l.skipped IS NOT 'other' AND l.skipped IS NOT 'moved') "
            "AND (s.message_id IS NULL OR s.until IS NOT NULL) "
            "AND (s.until IS NOT NULL OR NOT EXISTS (SELECT 1 FROM locations l "
            "WHERE l.message_id = m.message_id AND l.skipped = 'moved')) "
            "AND (m.ts, m.message_id) > (?, ?) "
            "ORDER BY m.ts, m.message_id LIMIT ?", (*after, limit + 1))
        now = self._clock()
        gone, failure = [], None
        try:
            for row in waiting[:limit]:
                order = ("sent", "archive", "inbox") if row["direction"] == "out" \
                    else ("inbox", "archive", "sent")
                unsure = placed = False
                for role in order if row["kept"] else ():
                    back = self._undeleted(role, row["message_id"])
                    unsure = unsure or back is UNSURE
                    if back is not None and back is not UNSURE:
                        self._locate(role, back, row["message_id"])
                        placed = True
                        break
                for role in order if not placed else ():
                    uid = self._adopt(role, row["message_id"], row["ts"], row["arrived"])
                    if uid is UNSURE:
                        unsure = True
                    elif uid is not None:
                        placed = True
                        break
                if placed or unsure or (row["settling"] is not None and row["settling"] > now):
                    continue
                if row["kept"]:
                    # Settled, and in no folder but as the leftover: that is
                    # its place, as it was before it was looked for.
                    self.index.run("DELETE FROM settling WHERE message_id = ?",
                                   (row["message_id"],))
                else:
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
            # A mail still owed keeps its flag (``keep_delivered``): the
            # tombstone is offered to nobody (_owed), but a mail that comes
            # back is owed what it was owed when it went (_mirror_one).
            with self._records:
                buried_now = _ibstore.tombstone_message(self._record_path(row["record"]),
                                                        self._clock(), own_blobs=True,
                                                        keep_delivered=True)
            if not buried_now:
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
                (f"DELETE FROM settling WHERE message_id IN ({marks})", tuple(message_ids)),
                # Nothing is offered for a mail that no longer exists — its
                # tombstone may still say it was owed, for when it comes back.
                (f"DELETE FROM offers WHERE message_id IN ({marks})", tuple(message_ids)),
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
        it are not held back. At the LOST_TRIES-th time in vain, what is
        found of it is read as mail nothing is known of, and it goes. A
        UIDVALIDITY change asks nothing of this: nothing in `lost` names a
        UID."""
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
                if tries >= LOST_TRIES:
                    # Never told, however often it was looked for: what is
                    # there is read as what it is, and the id waits no more.
                    self._read_found(hint, policy, plain=True)
                    continue
                self.index.run(
                    "UPDATE lost SET tries = ?, due = ? WHERE message_id = ?",
                    (tries, now + SET_ASIDE_RETRY_SECONDS * (1 << min(tries, 5)),
                     hint["message_id"]))
        return len(due) > LOST_BATCH

    def _read_found(self, hint, policy, plain: bool = False) -> bool:
        """One id of `lost`: find its mail and read it, or know it is in no
        folder. False when that cannot be told yet.

        The mail read as the record's is ONE of what lies under the id. For
        a row that said "out": a Sent copy, whenever the server received it
        (a client may append its Sent copy late); then the copy the server
        received when the row said the mail arrived, an archive copy first;
        then the rest. For any other: the copy From the sender the lost row
        named — the one received when the row said, else the first. A match
        whose header block does not come back counts as From nobody, and
        comes last (in Sent, still before the rest): it holds up nothing
        where the first is known to be the record's — a Sent copy, or one
        From that sender received when the row said (in the archive, for
        "out") — and holds up the id where it may be the record's itself.
        A From alone is no evidence: whoever was sent a mail knows its id
        and can write any From. The one is read as a mail read for the
        first time where it lies (_ingest), with one thing more: in the
        archive, From that sender and received when the row said, where the
        row said "out", it is the user's own — a move keeps the arrival
        time, a look-alike delivered a second or more later does not have it
        (_mirror_one). Never in INBOX, nor in the archive on its From alone.
        (An INTERNALDATE has whole seconds: a look-alike delivered in the
        same second has all the record's own mail has, and where the two
        lie in different folders, the folder decides.) The others are then
        read as any further copy is: one more place of the record, marked as
        another mail's, or a placeholder, by their own evidence.

        When the row said the mail arrived is the search's only bound where
        an id cannot be searched for (_find), and a copy received just then
        is read without waiting for the scan to be whole. ``plain``: the id
        was looked for LOST_TRIES times in vain, and nothing more is waited
        for — what is found is read as mail nothing is known of (no hint),
        and the id leaves `lost`, found or not (_read_lost). Every match
        gone since the search — a mail the user moved in that moment, it may
        be — it is looked for so once more, and then leaves all the same."""
        mid = hint["message_id"]
        found, sure = [], True
        for order, role in enumerate(("sent", "inbox", "archive")):
            name = self._folders.get(role)
            if not name:
                continue   # no such folder: nothing of the mail can be there
            uids, whole = self._find(role, mid, None, hint["arrived"], every=True)
            sure = sure and whole
            found += [(order, role, name, uid) for uid in uids]
        if not sure and not plain and (not found or _searchable(mid)):
            return False
        if plain:
            print(f"[mail-gateway] the mail {mid!r}, whose record is gone from the ledger, "
                  f"was looked for {LOST_TRIES} times: what was found of it "
                  f"({len(found)} {'copy' if len(found) == 1 else 'copies'}) is read as mail "
                  f"nothing is known of, and it is looked for no more", flush=True)
        if not found:
            self.index.run("DELETE FROM lost WHERE message_id = ?", (mid,))
            if sure:
                print(f"[mail-gateway] the mail {mid!r}, whose record is gone from the "
                      f"ledger, is in no folder: there is no record to tombstone", flush=True)
            return True
        if len(found) > 1 or not sure:
            # Which one is the record's: where it lies, who it is From, and
            # when it came.
            out, arrived = hint["direction"] == "out", hint["arrived"]
            ranked, timely, missed = [], False, False
            for order, role, name, uid in found:
                got = self._peek(name, uid)
                if got is None:
                    if plain:
                        missed = True
                        continue
                    return False   # gone since the search: where to, the next one says
                try:
                    sender, unread = (read_head(_head_of(got.raw)) or {}).get("sender"), False
                except NotRead:
                    sender, unread = None, True
                theirs = not (hint["sender"] and sender == hint["sender"])
                # (An INTERNALDATE has whole seconds; what the record keeps
                # of it may not.)
                late = not (got.date is not None and arrived is not None
                            and abs(got.date - arrived) < 1.0)
                timely = timely or not late
                # (An archive copy received later than the row said is no
                # more the user's than any other: ranked like any other.)
                key = ((role != "sent", unread, late, role != "archive" or late, theirs)
                       if out else (unread, theirs, late))
                ranked.append(((*key, got.date is None, got.date or 0.0, order, uid),
                               (order, role, name, uid), unread, theirs, late))
            if not ranked:
                return self._gone_since_search(hint)   # every match, on the last try
            if not sure and not plain and not timely:
                return False   # the scan reads on: the mail may be among the rest
            ranked.sort(key=lambda entry: entry[0])
            _rank, (_order, top, _name, _uid), _unread, stranger, toplate = ranked[0]
            # Known to be the record's: a Sent copy (only the user's client
            # or the gateway puts one there), or a copy From that sender
            # received when the row said — a From alone is anybody's to
            # write, and a move keeps the arrival time.
            told = (top == "sent" or (top == "archive" and not stranger and not toplate)) \
                if out else not (stranger or toplate)
            if missed and not told:
                # On the last try a match gone since the search may be the
                # record's, and what is left is not known to be: not read in
                # its place.
                return self._gone_since_search(hint)
            if not plain and not told and len(ranked) > 1 \
                    and any(unread for _rank, _where, unread, _theirs, _late in ranked):
                # A match that did not come back may be the record's, and the
                # first of those that did is not known to be: which one is,
                # is not known yet (I1).
                return False
            found = [where for _rank, where, _unread, _theirs, _late in ranked]
        for n, (_order, role, name, uid) in enumerate(found):
            first = n == 0 and not plain
            try:
                self._ingest(role, name, uid, policy, backfill=True,
                             lost=hint if first else None)
            except NotRead:
                return False   # its header block did not come back: not read (I1)
            if first and not self._placed(role, uid):
                # Not read after all: the server holds no such mail there any
                # more (moved since the search), or not under the UIDs it was
                # found under. The next look tells where it is.
                return False
        if plain and not any(self._placed(role, uid) for _order, role, _name, uid in found):
            return self._gone_since_search(hint)   # every match, on the last try
        self.index.run("DELETE FROM lost WHERE message_id = ?", (mid,))
        return True

    def _placed(self, role: str, uid: int) -> bool:
        """Has the mail at a UID of a folder as it stands a place?"""
        return self.index.one(
            "SELECT 1 FROM locations l JOIN folders f ON f.role = l.role "
            "AND f.gen = l.gen WHERE l.role = ? AND l.uid = ?", (role, uid)) is not None

    def _gone_since_search(self, hint) -> bool:
        """On the last try (_read_found), every match of a lost id is gone
        since the search — or one is, and what is left is not known to be
        the record's: moved, it may be, between the search and the fetch.
        The id is looked for once more, as plainly, an hour on (its tries,
        at LOST_TRIES, say that it was); gone again, or named by no search,
        it leaves `lost` — and a mail moved under both looks can be left
        unmirrored. True: nothing more is waited for."""
        mid = hint["message_id"]
        if hint["tries"] < LOST_TRIES:
            self.index.run("UPDATE lost SET tries = ?, due = ? WHERE message_id = ?",
                           (LOST_TRIES, self._clock() + SET_ASIDE_RETRY_SECONDS, mid))
            return True
        self.index.run("DELETE FROM lost WHERE message_id = ?", (mid,))
        print(f"[mail-gateway] the mail {mid!r}, whose record is gone from the "
              f"ledger, is no more where it was found: there is no record to "
              f"tombstone", flush=True)
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
        if fetched.size > len(fetched.raw) and not _HEADER_END_RE.search(fetched.raw):
            # Fetched up to MAIL_MAX_MAIL_BYTES, and its header block goes
            # on past that: the From headers after the cut are not known, so
            # the mail is from nobody the gate can name (_gate_sender).
            mail.gate_sender = ""
        # On record under another id than it reads under now: the id was one
        # of its bytes, made when its header block did not read (_bare_stub),
        # or the block reads differently than it did. The mail is the same —
        # where its header block says so: the block whose bytes the id was
        # made of, or the one the record keeps (I2) — so what is known of it
        # goes on to the new record (its direction), and the old one gives
        # way in the same step: on what was read at its own place, not later
        # for want of one.
        head = _head_of(fetched.raw)
        # (Under an id its provider gave a mail of the gateway's own, it may
        # be that mail as it is on record: _mirror_one says, on the send's
        # own evidence — _send_copy, _own_send.)
        renamed = at is not None and at["message_id"] != mail.message_id \
            and mail.message_id not in self._known_as(at["message_id"])
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
                             at["direction"] if renamed else None, lost=lost,
                             owed_as=at["message_id"] if renamed else None)
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
                    backfill: bool, direction: str | None = None, *, lost=None,
                    owed_as: str | None = None) -> None:
        """Mirror one mail read at one UID (see _ingest). ``direction`` and
        ``owed_as``: what the record it had under another id said, and that
        id (_ingest) — the record written now is owed the rail what that one
        was owed (_offer), and no more."""
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
        # Under an id a provider gave a mail of the gateway's own (aliases,
        # from _own_send) — with no record of its own, or one that came in —
        # a copy shown to be that send's (_send_copy) IS that mail, as it is
        # on record under the id the gateway minted. In Sent (renumbered, the
        # copy moved out and back): live, it is one more place of that
        # record; a tombstone, the record is written again in its own place
        # (author and kb:sentAs kept, see _write) — never a second record
        # under the alias. Elsewhere it is only ever one more place of the
        # live record: nothing there is the user's send on a recipient's
        # evidence, and every recipient holds that id and every other header
        # of the mail but the trace fields. (Unless that record is a
        # placeholder made from this very copy: then the copy takes its
        # place, as for any id — as what the record already is.) Any other
        # mail under it is a mail of its own, under its own id, and no place
        # of the send's.
        mapped = False
        if known is None or known["direction"] == "in":
            minted = self.index.one(
                "SELECT m.* FROM aliases a JOIN mails m ON m.message_id = a.message_id "
                "WHERE a.alias = ? AND m.record != ''", (mail.message_id,))
            if minted is not None and self._send_copy(role, mail, minted):
                placeholder = minted["message_id"] in self._placeholders
                if role != "sent" and not placeholder:
                    self._locate(role, uid, minted["message_id"])
                    return
                mail, known = dataclasses.replace(mail, message_id=minted["message_id"]), minted
                outbound = role == "sent" or known["direction"] == "out"
                mapped = role == "sent"
        # (A copy whose alias is not known yet: _own_send says whether it is
        # one — also beside a mail under that id that came in first.)
        if not mapped and role == "sent" and (known is None or known["direction"] == "in") \
                and self._own_send(uid, mail):
            return
        sent_as = None
        if lost is not None and known is None \
                and mail.message_id in self._known_as(lost["message_id"]) \
                and mail.sender and mail.sender == lost["sender"] \
                and lost["direction"] == "out" and (role == "sent" or (
                    role == "archive" and fetched.date is not None
                    and lost["arrived"] is not None
                    and abs(fetched.date - lost["arrived"]) < 1.0)):
            # The mail of a record gone from the ledger (_read_found), From the
            # very sender that record had, which was the user's own, in Sent,
            # or in the archive received when that record said (a move keeps
            # the arrival time; a look-alike delivered a second or more later
            # does not have it): the user's again, whatever address that is (a
            # plus-address, an alias nobody listed). Never on the strength of
            # its From alone — that is its sender's to write, and whoever was
            # sent a mail knows its id: a look-alike archived, or in an "all
            # mail" archive, stays a mail that came in, and so do a mail to
            # oneself whose Sent copy is gone and, on a server that stamps a
            # moved mail with a new arrival time, an archived sent mail. (A
            # look-alike delivered in the same second as the Sent copy, and
            # archived, with that copy gone, has all the record's own has: it
            # comes back as the user's.) Any other is read as a first reading
            # here is. (Under an id the provider gave it — kept for the lost
            # id, see _prepare — it is the record under the id the gateway
            # minted again, with that one as kb:sentAs and the author noted
            # under it.)
            outbound = True
            if mail.message_id != lost["message_id"]:
                sent_as = [mail.message_id]
                mail = dataclasses.replace(mail, message_id=lost["message_id"],
                                           cited=[*mail.cited, mail.message_id])
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
        # A record written again in the place of another — the one on record
        # under this id (I3), or under the id the mail had before (_ingest)
        # — is owed the rail what that one was owed, and no more: a
        # placeholder the chat had taken is not offered again once it reads,
        # one it had not is. A mail that came back is owed what it was owed
        # when it went: its tombstone kept the flag (_bury), and the offers
        # row went.
        owed = self.index.one(
            "SELECT 1 FROM offers WHERE message_id = ?",
            (known["message_id"] if known is not None else owed_as or "",)) is not None
        if revived and not owed:
            was = _ibstore.read_record(self._record_path(known["record"]))
            owed = bool(was and was.get("type") == _ibstore.T_INBOUND
                        and not was.get("delivered"))
        flags = self._news_flags(mail, policy) \
            if not outbound and known is None and direction is None else None
        if flags is not None:
            # A news sender or list starts no chat (the feed has the mail) —
            # unless the mail continues a thread that is already mirrored,
            # normally because the user wrote in it: then it is the answer
            # to their post, and is in that chat as well.
            joins = any(self.threads.chat_of(cited) for cited in mail.cited)
            if not joins:
                # And unless the sender is a VIP or instructed: the two axes
                # are independent (docs/mail-chats.md, "The gate"). Such a
                # mail is filed as well, and gets its chat, its offer and
                # its turn — and, being in a chat, is never taken out of the
                # INBOX here. (An unreadable sender policy names nobody.)
                who = self._gate(mail.gate_sender, mail.list_id)
                joins = bool(who["vip"] or who["instructed"])
            # Filed is mail that ARRIVES. Old mail is not: a first backfill
            # would pour a season of newsletters into the feed, and move
            # them about the mailbox. And taken out of the INBOX once filed
            # is only a mail of an `ignored` group that is in no chat: what
            # happens to the mailbox copy of a mail in a chat is the chat's
            # to say (docs/mail-chats.md, "The mailbox follows the chat").
            filing = None if backfill else self._news_filing(
                mail, consume=bool(flags.get("ignored")) and not joins and role == "inbox")
            if not joins:
                if mail.unreadable is not None and not backfill:
                    # Not read, so nothing to file yet — and no chat: an
                    # unreadable newsletter is still no chat. Marked so
                    # that the set-aside retry reads it again, as it does a
                    # placeholder (_retry_set_aside): what made it
                    # unreadable may have been a moment, and the feed is
                    # owed it once it reads.
                    self._locate(role, uid, mail.message_id, "news-unread")
                    return
                # One transaction: the mail is not read again, and the feed
                # is owed it.
                self._locate(role, uid, mail.message_id, "news", also=filing)
                return
            if filing is not None:
                self.index.atomic([filing])
        # A copy in Sent of a mail the gateway gave up on as "outcome
        # unknown": it is that send's mail, by whoever wrote it.
        given_up = self._given_up(mail) if role == "sent" and known is None else []
        if given_up and self._author_noted(mail) is None:
            self.expect_sent(mail.message_id, given_up[0]["author"])
        # Offered to the rail — gated, and owed until its chat takes it — is
        # a mail that ARRIVED: one first seen now as somebody's mail to the
        # user. Not old mail (the backfill, a lost record's mail found again:
        # _read_found), not a mail that came back after it had gone and had
        # been taken when it first came, not a placeholder read at last —
        # under its own id or under the one it was on record by (_ingest) —
        # unless what this record replaces was still owed (a mail that left
        # while owed included): then the mail is, whatever became of its
        # record.
        fresh = (known is None and direction is None) or (
            known is not None and known["direction"] == "out" and not revived)
        # (A record written again replaces the one on record in its place,
        # and only once it stands: should this fail, the mail is still the
        # record it was, and is read again from wherever it is — _write.)
        self._write(mail, fetched, outbound=outbound,
                    backfill=backfill and not revived, replace=known, sent_as=sent_as,
                    offer=not outbound and (owed or (fresh and not backfill)))
        self._locate(role, uid, mail.message_id)
        if mail.unreadable is None:
            # No placeholder (any more): what else lies under its id is a
            # second copy like any mail's — one more place of the mail on
            # record, not set apart from it.
            self.index.run("UPDATE locations SET skipped = NULL WHERE message_id = ? "
                           "AND skipped = 'other'", (mail.message_id,))
        self._went_out_after_all(given_up, mail)

    def _own_send(self, uid: int, mail: Mail) -> bool:
        """A copy in Sent under a Message-ID nobody here knows — or one a
        mail that came in has (a recipient's, met first: it stays its own):
        is it the provider's copy of a mail the gateway itself sent, filed
        under an id the provider gave it in place of the one the gateway
        minted? True when it is — it is then one more place of that mail's
        record, and no mail of its own: one bubble, one chat.

        Which send it would be, the copy says itself where the provider kept
        the gateway's header (``submitted_as``); where it kept none, the
        send is known by what the mail is (:meth:`_sent_like`). The header
        alone proves nothing (every recipient of the mail has it). It is
        taken as that send's copy only with the rest of the evidence: the
        copy is in Sent, From the account, addressed to exactly the people
        the gateway sent that mail to, and dated when it did. Then the id
        the provider gave is put on the record (kb:sentAs) and into both
        indexes — an answer cites that id, not the minted one, and must land
        in this chat — before the copy is given its place: a stop in between
        reads the copy again and finds the rest done.

        A send whose record is still to be written is given it first; and
        one nobody saw the SMTP server answer (``sending``, "data") is
        answered by this copy: the mail went out.

        A copy that could not be read (a placeholder, :func:`stub_mail`) has
        no recipients read, and no text to be known by: it is that send's
        on what it still carries — the gateway's header naming it, From the
        account, dated when it went — and never a placeholder of its own in
        a chat of its own."""
        whole = mail.unreadable is None
        minted, alias = mail.submitted_as or (whole and self._sent_like(mail)), mail.message_id
        if not minted:
            return False
        find = ("SELECT * FROM mails WHERE message_id = ? AND record != '' "
                "AND direction = 'out' AND removed = 0", (minted,))
        row = self.index.one(*find)
        if row is None:
            entry = SENDS.by_message_id(minted) or {}
            waits = entry.get("status") == "sending" and entry.get("phase") == "data"
            if not waits and not (entry.get("status") == "approved"
                                  and not entry.get("recorded")):
                return False
            if not self._copy_of(mail, entry["to"] + entry["cc"], float(entry["date"]),
                                 addressed=whole):
                return False
            if waits:
                print(f"[mail-gateway] send {entry['id']}: its copy is in Sent, under an "
                      f"id of the provider's — it went out", flush=True)
                _went_out(entry, self)
            else:
                _finish_send(entry, self, mailbox=False, wake=False)
            row = self.index.one(*find)
            if row is None:
                raise OSError(f"the record of the gateway's own send {minted!r} could not "
                              f"be written; its copy in Sent is read again once it is")
        path = self._record_path(row["record"])
        fields = _ibstore.read_record(path) or {}
        sent_to = [*(fields.get("recipients") or []), *(fields.get("cc") or []),
                   *(fields.get("refused") or [])]
        # (A placeholder's recipients were never read: its copy is told by
        # the rest.)
        if not self._copy_of(mail, sent_to, row["ts"],
                             addressed=whole and minted not in self._placeholders):
            return False
        if alias not in (fields.get("sent_as") or []):
            with self._records:
                _ibstore.update_message(path, sent_as=alias)
            if alias not in ((_ibstore.read_record(path) or {}).get("sent_as") or []):
                raise OSError(f"could not note on {row['record']} the id its mail was sent as")
            seen = int(self.index.get("rewritten", 0)) + 1
            self.index.atomic([
                ("INSERT OR REPLACE INTO aliases (alias, message_id) VALUES (?, ?)",
                 (alias, minted)),
                ("INSERT INTO meta (key, value) VALUES ('rewritten', ?) ON CONFLICT (key) "
                 "DO UPDATE SET value = excluded.value", (str(seen),))])
            if seen == 1:
                print(f"[mail-gateway] the mail provider files sent mail under a Message-ID "
                      f"of its own ({alias!r} for {minted!r}): its copies are matched to "
                      f"the gateway's own sends", flush=True)
        # (Every time, like any mail's threading: a repeat changes nothing —
        # and an answer that came first, citing the provider's id, started a
        # chat of its own, which this merges.)
        self._merges_due.update(self.threads.resolve(
            minted, [alias], row["arrived"] or row["ts"], None).merged)
        self._locate("sent", uid, minted)
        return True

    def _send_copy(self, role: str, mail: Mail, minted) -> bool:
        """Whether a mail under an id a provider gave a send of the
        gateway's own (``minted``: its row, live or a tombstone) is that
        send's copy — on the send's own evidence, never on the id alone,
        which every recipient has. In Sent, :meth:`_own_send`'s: naming no
        other send (``submitted_as``), From the account, addressed to
        exactly the people the record says it went to and dated when it
        went (a tombstone keeps no addresses, nor does a placeholder, whose
        were never read: its copy is told by the rest). Elsewhere, only the
        very header block the live record keeps (:meth:`_own_copy`): every
        recipient holds the id, the gateway's header naming the send, From,
        To, Cc and Date, and can write them all on a mail of their own — and
        a tombstone, whose copy only Sent tells, is no mail's outside it."""
        if role != "sent":
            return not minted["removed"] and self._own_copy(minted["message_id"], mail.headers)
        sent_to = []
        addressed = mail.unreadable is None and not minted["removed"] \
            and minted["direction"] == "out" and minted["message_id"] not in self._placeholders
        if addressed:
            fields = _ibstore.read_record(self._record_path(minted["record"])) or {}
            sent_to = [*(fields.get("recipients") or []), *(fields.get("cc") or []),
                       *(fields.get("refused") or [])]
        return mail.submitted_as in (None, minted["message_id"]) \
            and self._copy_of(mail, sent_to, minted["ts"], addressed=addressed)

    def _sent_like(self, mail: Mail) -> str | None:
        """The send of the gateway's own that a copy in Sent is, where
        nothing in the copy names it — a provider that replaced the
        Message-ID and kept no header of the gateway's. Its minted id, or
        None unless exactly one send fits: a mail the gateway built (its
        record keeps the header block it built, which says so), dated to
        the very second this copy is — the Date line is the same, character
        for character — from the account, to the same people, with the same
        subject and the same text."""
        if mail.sender != ACCOUNT or mail.date is None:
            return None
        # (A mail is dated when it is built and on record from when the
        # server took it: the record's time is the later one, by as long as
        # the hand-over took.)
        rows = self.index.rows(
            "SELECT m.message_id, m.record FROM mails m JOIN notes n "
            "ON n.message_id = m.message_id WHERE m.direction = 'out' AND m.removed = 0 "
            "AND m.record != '' AND m.ts BETWEEN ? AND ?", (mail.date - 5, mail.date + 3600))
        if not rows:
            return None
        # (Both header blocks are read as guarded as any (read_send_facts):
        # one that cannot be read has no Date line to go by, and one whose
        # reading does not come back is no answer — NotRead, which ends the
        # pass before this copy is mirrored as a mail of its own.)
        date = (read_send_facts(mail.headers.encode("utf-8", "replace")) or {}).get("date")
        if not date:
            return None
        people, words = {a for a, _n in mail.to + mail.cc}, " ".join((mail.plain or "").split())
        fits = []
        for row in rows:
            header = self._recorded_send_facts(row["message_id"])
            fields = _ibstore.read_record(self._record_path(row["record"])) or {}
            if header is not None and header["date"] == date \
                    and header["own_id"] == row["message_id"] \
                    and (fields.get("subject_line") or "") == mail.subject \
                    and " ".join((fields.get("text") or "").split()) == words \
                    and {*(fields.get("recipients") or []), *(fields.get("cc") or []),
                         *(fields.get("refused") or [])} == people:
                fits.append(row["message_id"])
        return fits[0] if len(fits) == 1 else None

    @staticmethod
    def _copy_of(mail: Mail, sent_to, sent_at: float, addressed: bool = True) -> bool:
        """Whether a copy in Sent fits a send of the gateway's own: From the
        account, to exactly those addresses (unless not ``addressed``: a
        placeholder's are not read), dated within the days a provider may
        take to file it."""
        when = mail.date if mail.date is not None else mail.arrived
        return (mail.sender == ACCOUNT
                and (not addressed or {a for a, _n in mail.to + mail.cc} == set(sent_to))
                and when is not None and abs(when - sent_at) <= SCAN_WINDOW_SECONDS)

    def _given_up(self, mail: Mail) -> list:
        """The sends a copy in the mailbox is the late answer to: ones the
        gateway handed to the SMTP server, never saw answered, and ended as
        "outcome unknown" (the copy came after the grace, or under an id of
        the provider's). Under the copy's own id — one the gateway minted,
        which no other mail has — or, with the evidence :meth:`_own_send`
        asks for, under the id the copy says it was submitted as."""
        found = []
        for message_id, proven in ((mail.message_id, True), (mail.submitted_as, False)):
            entry = SENDS.by_message_id(message_id) if message_id else None
            if entry and entry.get("status") == "error" and entry.get("outcome_unknown") \
                    and (proven or self._copy_of(mail, entry["to"] + entry["cc"],
                                                 float(entry.get("created") or 0.0))):
                found.append(entry)
        return found

    def _went_out_after_all(self, entries: list, mail: Mail) -> None:
        """The outcome of these sends is known now — their mail has just
        been mirrored as a message of the user's — and no failed send may
        stand beside it: the entry says sent, or (a direct send nobody will
        ask about again) goes."""
        for entry in entries:
            try:
                if entry.get("direct") and not entry.get("request_key"):
                    SENDS.remove(entry["id"])
                else:
                    SENDS.update(entry["id"], drop=("error", "outcome_unknown", "dismissed"),
                                 status="approved", done=True, recorded=True, filed=True,
                                 sent_at=mail.date or self._clock(), attachments=[])
                print(f"[mail-gateway] send {entry['id']}: its copy is in the mailbox after "
                      f"all — it went out", flush=True)
            except OSError as exc:
                print(f"[mail-gateway] could not note that send {entry['id']} went out: "
                      f"{exc}", flush=True)

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

    def _news_flags(self, mail: Mail, policy) -> dict | None:
        """The group verdict (``email_gate_decision``) of a mail whose group
        is ``news``; None for every other mail.

        And None for every mail where no news rail is configured
        (NEWS_INGEST_URL emptied): the flag says "this belongs in the feed",
        and with no feed to take it the mail would be shown nowhere at all —
        so it is mirrored like any other, as the news rail always fell back
        to the ordinary path when it could not file."""
        if not _news.news_enabled():
            return None
        try:
            # The group axis, keyed like the gate's own (Mirror._gate): by
            # the List-Id, else by the gate sender — never the Sender header.
            flags = _triage.email_gate_decision(mail.gate_sender, mail.list_id, pol=policy)
        except Exception as exc:  # noqa: BLE001 - when in doubt, mirror
            print(f"[mail-gateway] e-mail gate failed ({exc}); mirroring the mail", flush=True)
            return None
        return flags if flags.get("news") else None

    def _news_filing(self, mail: Mail, *, consume: bool) -> tuple | None:
        """The statement that queues one news mail for the feed — for a
        transaction with what makes the mail not be read again. None when
        there is nothing to file: a placeholder, or a mail with neither a
        subject nor text (it is left as it is, in the INBOX).

        What the feed is given is made here, once, of the mail as it was
        read: the queue then needs no second look at the mailbox, and a
        retry files the very same item. The mail's sender as the source (one
        feed source per sending address), its subject as the title, its
        trimmed text as an excerpt, and the link it declares for itself
        (:func:`_declared_url`), never one picked out of its body. A mail
        already queued or filed under this id is not queued again."""
        if mail.unreadable is not None:
            return None
        text = (mail.reading or self._reading_text(mail))[0]
        body = _news_excerpt(text, MAIL_NEWS_EXCERPT_CHARS)
        if not mail.subject and not body:
            return None
        item = {
            "source": mail.sender_name or mail.sender or "E-mail",
            # Subject first, so the title the feed would derive from the
            # first line and the one stated here agree.
            "text": f"{mail.subject}\n\n{body}".strip(),
            "url": mail.archive_url or None,
            "title": mail.subject or None,
            "source_id": f"email:{mail.sender}" if mail.sender else None,
        }
        return ("INSERT OR IGNORE INTO news (message_id, item, consume, state, created) "
                "VALUES (?, ?, ?, 'due', ?)",
                (mail.message_id, json.dumps(_text_only(item), ensure_ascii=False),
                 int(consume), self._clock()))

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
               replace=None, sent_as=None, offer: bool = False) -> None:
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
        row should this write fail. ``sent_as``: ids a provider gave the
        mail, beside those the record it replaces has and those `aliases`
        names for it — kept on every record written again, inbound ones
        included.

        ``offer``: the mail arrived, and the rail is owed it (see
        _mirror_one). Its record is then written ``delivered: false`` —
        persisted before anything is decided about it, so that no failure
        after this line can lose it — and what is queued is not an event but
        the mail's place in the queue (:meth:`_owe`): the gate is asked, and
        the event made, when the offer is (:meth:`_offer`). Every other
        mail is written delivered: nothing is owed for it, and a record it
        replaces is owed nothing any more either. So is every mail where no
        rail is configured — there is nobody to take it, and the reader has
        the ledger.

        A placeholder is from nobody the gate can name, whatever its header
        block says (kb:gateSender is left out): what a turn would work is a
        mail it cannot read, and a turn's done would deliver it. Once the
        mail reads, the record written in the placeholder's place is offered
        with its gate sender if the placeholder was still owed (_mirror_one)."""
        offer = offer and not outbound and _chats.chats_enabled()
        gate_sender = "" if outbound or mail.unreadable is not None else mail.gate_sender
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
                cc=[a for a, _n in mail.cc],
                # Whom an answer goes to: a reply is built from the record.
                reply_to=[a for a, _n in mail.reply_to],
                in_reply_to=mail.in_reply_to, references=mail.references,
                list_id=_triage.email_list_id(mail.list_id) or None,
                body_part=refs.get("body_part"), html_part=refs.get("html_part"),
                header_part=refs["header_part"], trim=state,
                content_ids=refs["content_ids"], arrived_at=arrived)
            # Every id a provider gave the mail (kb:sentAs: what its answers
            # cite), whichever way the record is written again — those it
            # had, and those `aliases` names for it: a restart builds the
            # same aliases off the records (_prepare).
            known_as = list(dict.fromkeys(
                [*((was or {}).get("sent_as") or []), *self._known_as(mail.message_id)[1:],
                 *(sent_as or [])])) or None
            if outbound:
                # Whichever client sent it: the own-device echo, unless
                # someone said who wrote it. A record written again in the
                # place of the user's own keeps who that said wrote the mail
                # (the gateway's own send is on record with its author from
                # the start, and its note may be pruned by now).
                own = was if was is not None and was.get("type") == _ibstore.T_OUTBOUND \
                    else {}
                author = self._author_noted(mail) or own.get("author") or "device"
            with self._records:
                # The record in its place as it stands now, under the lock
                # every writer of a record holds: a model's judgement written
                # since `was` was read is seen here, and kept where it is
                # one about this very reading (_judged_reading).
                judged = self._judged_reading(old, mail, refs, state) if old is not None \
                    else None
                if judged is not None:
                    text, state = judged
                    common.update(text=text, trim=state)
                if outbound:
                    _urn, path = _ibstore.write_outbound(
                        INBOUND_STORE_DIR, author=author, sender=mail.sender or None,
                        replace=old, sent_as=known_as, **common)
                else:
                    _urn, path = _ibstore.write_message(
                        INBOUND_STORE_DIR, sender=mail.sender, gate_sender=gate_sender or None,
                        delivered=not offer, replace=old, sent_as=known_as, **common)
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
            announce = self._owe(mail.message_id, chat) if offer else [
                self._event("inbound", self._inbound_event(
                    direction=direction, chat=chat, message_id=mail.message_id, ts=ts,
                    sender=mail.sender or None, sender_name=mail.sender_name,
                    gate_sender=gate_sender, text=text,
                    attachments=refs["attachments"], author=author,
                    subject=mail.subject or None, backfill=backfill, seen=seen,
                    trim=state))]
            self.index.atomic([
                ("UPDATE mails SET record = ? WHERE message_id = ?",
                 (path.name, mail.message_id)),
                ("DELETE FROM claims WHERE message_id = ?", (mail.message_id,)),
                # (A record written delivered in the place of one that was
                # owed: nothing is owed for the mail any more.)
                ("DELETE FROM offers WHERE message_id = ?", (mail.message_id,))
                if replace is not None and not offer else None,
                *announce])
        except Exception:
            # The record stands and the index does not know it. Nothing may be
            # ingested before the two are matched up (poll does that first).
            self._reconcile = True
            raise
        if mail.unreadable is not None:
            self._placeholders.add(mail.message_id)
        else:
            self._placeholders.discard(mail.message_id)

    def _judged_reading(self, old: Path, mail: Mail, refs: dict, state: str):
        """``(kb:text, "model")`` of the record at ``old`` where a model's
        judgement stands on it that is one about the reading written in its
        place; None for anything else. Under the record lock (the caller's).

        A judgement is cached on its record (docs/mail-chats.md, "The model
        trim"): it is a choice of the lines the rules gave that record, and
        a record written again in its own place (I3: the INBOX copy whose
        Sent copy shows, a copy read under a provider's id) is a new reading
        whose rules may give other lines. It is kept only where they cannot:
        the new reading is ``wanting`` too, and its trim's inputs are the old
        record's — the same subject, the same word on whether it answers a
        mail, and parts of the same bytes (the header block among them, which
        says whether it is bulk). Then its lines are the very ones the choice
        was made of, and the mail is not asked about again. Anything else —
        another copy's bytes, a placeholder, a tombstone, whose judgement
        went with its text — is the new reading as the rules left it, and a
        ``wanting`` one gets its own first view. Comparing bytes, not trims:
        no mail is trimmed or parsed in this process (the reader does that),
        and none under a lock."""
        if state != "wanting" or mail.unreadable is not None:
            return None
        now = _ibstore.read_record(old)
        if (now is None or now.get("trim") != "model" or now.get("removed_at")
                or not now.get("text")):
            return None
        if ((now.get("subject_line") or "") != (mail.subject or "").strip()
                or bool(now.get("in_reply_to") or now.get("references"))
                != bool(mail.in_reply_to or mail.references)):
            return None
        for key in self._TRIM_PARTS:
            before, after = now.get(key), refs.get(key)
            if not before and not after:
                continue
            ids = [_ibstore.media_id_of(ref or "") for ref in (before, after)]
            if not all(ids):
                return None
            got = [_ibstore.load_media(INBOUND_STORE_DIR, media_id) for media_id in ids]
            if None in got or got[0][0] != got[1][0]:
                return None
        return now["text"], "model"

    def _inbound_event(self, *, direction: str, chat: str, message_id: str, ts: float,
                       sender, sender_name, text: str, attachments: list, author,
                       subject, backfill: bool, seen: bool, gate: dict | None = None,
                       gate_sender: str | None = None, trim: str | None = None) -> dict:
        """What the rail is told of one mirrored mail. ``gate``: the verdict
        on a mail that is offered; every other mail is told as held.

        ``trim``: the record's kb:trim. Where it is ``model`` the event says
        so (``"trim": "model"``): its text is then a model's choice of the
        mail's own lines, which the mail can steer, and a turn that quotes it
        marks it as such (the web gateway's _mail_arrival_quote) — as the
        companion's note marks the record. A mail is trimmed by a model on
        its first view, never on arrival; an offer made later (the rail was
        down, a turn failed) is made of the record as it stands then.

        ``sender`` is whom the chat shows the mail as from; ``gate_sender``
        whom the gate took it to be from (:func:`_gate_sender`), "" for
        nobody — what the web gateway judges an arrival by, its class, its
        prior and its permits, never ``sender``."""
        thread = self.thread(chat, claimed=message_id) or {}
        event = {
            "direction": direction, "channel": CHANNEL, "chat": chat, "account": ACCOUNT,
            "sender": sender, "sender_name": sender_name, "gate_sender": gate_sender or "",
            "chat_name": thread.get("name") or None, "group": bool(thread.get("group")),
            "message_id": message_id, "ts": ts, "text": text[:MAIL_RAIL_TEXT_CHARS],
            "attachments": attachments, "author": author,
            "gate": dict(gate or MIRROR_GATE), "subject": subject,
            "participants": thread.get("participants") or [], "peer": thread.get("peer"),
        }
        if trim == "model":
            event["trim"] = "model"
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
        event = self._inbound_event(
            direction="out" if outbound else "in", chat=fields["chat"],
            message_id=fields["message_id"], ts=self._record_time(fields),
            sender=sender if "@" in sender else None, sender_name=fields.get("sender_name"),
            gate_sender=None if outbound else fields.get("gate_sender"),
            text=fields.get("text") or "", attachments=list(fields.get("attachments") or []),
            author=fields.get("author") if outbound else None,
            subject=fields.get("subject_line") or None, backfill=backfill, seen=seen,
            trim=fields.get("trim"))
        if outbound and fields.get("refused"):
            event["refused"] = list(fields["refused"])
        return event

    # -- merges ----------------------------------------------------------------

    def _apply_merges(self) -> None:
        """Re-key the records of chats the thread index merged away, then tell
        the web gateway — only once the ledger is clean (rekey_chat fails
        closed), so a merge a full disk interrupts is finished by a later poll.
        The index and the event are one transaction; a stop after the ledger
        and before it is announced by the next start (_prepare). The ids of
        `lost` whose hint names a chat merged away follow it in that same
        transaction: a delete of the chat they are in now looks for them
        (delete_chat) — also where such an id is all the chat had left."""
        merged: dict = {}
        for old in sorted(self._merges_due):
            member = self.index.one(
                "SELECT message_id FROM mails WHERE chat = ? UNION ALL "
                "SELECT message_id FROM lost WHERE chat = ? LIMIT 1", (old, old))
            into = self.threads.chat_of(member["message_id"]) if member else None
            if not into or into == old:
                self._merges_due.discard(old)
                continue
            # The chat's records are the ones the index names — every record
            # of this account is indexed (_prepare) — so only they are read.
            records = [self._record_path(row["record"]) for row in self.index.rows(
                "SELECT record FROM mails WHERE chat = ? AND record != ''", (old,))]
            try:
                with self._records:
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
                [(f"UPDATE {table} SET chat = ? WHERE chat = ?", (into, old))
                 for old in absorbed for table in ("mails", "lost")]
                + [self._event("merge", {"channel": CHANNEL, "account": ACCOUNT,
                                         "from": absorbed, "into": into})])
            self._merges_due.difference_update(absorbed)
            # A send queued in an absorbed chat waits in the survivor now.
            for old in absorbed:
                SENDS.rekey(old, into)
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
        """Hand the web gateway everything it has not taken, in order: the
        events, and — each in its place among them — the first offer of every
        mail that arrived (:meth:`_offer`). False when the web gateway is not
        answering at all: nothing behind that point is sent, so the order
        holds, and nothing is tried again until RAIL_RETRY_SECONDS have
        passed (longer each time it fails again).

        **One event never holds back another.** An event the web gateway
        answered a failure for (a 408, 429 or 5xx), or gave no answer for
        while it answers otherwise, keeps its place and waits its own time
        (:func:`_retry_after`), as a mail not taken does — and only its own
        chat's later events wait behind it, so a reply from a mail client is
        not heard before the mail it answers — while the failure is young:
        for RAIL_HOLD_ATTEMPTS of its own attempts. Then it steps aside (a
        first offer leaves the outbox for :meth:`_offer_again`; a told
        event keeps its row and its own clock, and holds nothing back) and
        its chat's later events go on: one mail the other side always fails
        on must not stall its thread, a VIP's reply in it included. Every
        other chat's go on throughout. An event it cannot take yet
        (``"later"``: a merge or removal an older web gateway does not know)
        stays queued and holds nothing back.

        **A late event says what is true when it is sent.** One that has
        stepped aside is out of its chat's order — what was heard of its
        mail since may have gone ahead of it — so it is not sent as it was
        queued but as the index has its mail now (:meth:`_as_now`); a young
        held event keeps its payload — the hold keeps its chat's order. And
        a removal waits for every merge queued before it that joins its
        chat, whatever that merge's attempts: in the web gateway its mail
        may still be under the other chat, which the merge folds in with its
        arrivals; one queued after it cannot have moved the mail the
        removal names, which goes first, in its order. While it waits its
        chat's later events go on — nothing else waits for a merge that
        stepped aside — so it is marked as out of order (:meth:`_overtaken`),
        and sent as a late event is. That wait is for a merge of its own
        chat, not for a chain of them; so a merge that stepped aside queues,
        in the transaction that takes it, the removals of the chats it
        absorbed again (:meth:`_removed_under`)."""
        if not _chats.chats_enabled():
            return True
        if self._clock() < self._rail_wait_until:
            return False
        self._flags = None
        now = self._clock()
        held: set = set()     # chats with an event that waits: their later ones wait too
        merging: dict = {}    # each merge still queued (by seq) → the chats it joins
        for row in self.index.rows("SELECT seq, payload FROM outbox WHERE kind = 'merge'"):
            try:
                merging[row["seq"]] = self._event_chats("merge", json.loads(row["payload"]))
            except ValueError:
                pass
        after = 0
        while True:
            batch = self.index.rows(
                "SELECT seq, kind, payload, attempts, due FROM outbox WHERE seq > ? "
                "ORDER BY seq LIMIT 200", (after,))
            if not batch:
                return True
            for row in batch:
                after = row["seq"]
                try:
                    payload = json.loads(row["payload"])
                except ValueError:
                    payload = None
                chats = self._event_chats(row["kind"], payload)
                if chats & held:
                    held |= chats   # behind an event of its chat: in order
                    continue
                if row["kind"] == "removed" and any(
                        seq < row["seq"] and chats & joined
                        for seq, joined in merging.items()):
                    # It waits for the merge, and holds nothing back: marked
                    # first, so whatever of its chat goes ahead of it now, it
                    # is sent as the index has its mails then.
                    self._overtaken(row["seq"], payload)
                    continue
                if row["due"] > now:
                    if row["attempts"] < RAIL_HOLD_ATTEMPTS:
                        held |= chats
                    continue
                if row["kind"] == "offer":
                    mail = payload.get("message_id") if isinstance(payload, dict) else None
                    if not self._offer(mail if isinstance(mail, str) else "", seq=row["seq"]):
                        return False
                    if self.index.one("SELECT seq FROM outbox WHERE seq = ?", (row["seq"],)):
                        held |= chats   # its first offer failed, and keeps its place
                    continue
                if isinstance(payload, dict) and (row["attempts"] >= RAIL_HOLD_ATTEMPTS
                                                  or payload.get(OVERTAKEN)):
                    payload = self._as_now(row["kind"], payload)
                    if payload is None:
                        print(f"[mail-gateway] a late {row['kind']} event of chat(s) "
                              f"{sorted(chats)!r} is not true any more; it is dropped",
                              flush=True)
                outcome = "drop" if payload is None else self._rail_heard(
                    _rail_send(row["kind"], payload), f"a {row['kind']} event")
                if outcome == "down":
                    return False
                if outcome in ("ok", "drop"):
                    owed = self._removed_under(payload) if (
                        outcome == "ok" and row["kind"] == "merge"
                        and row["attempts"] >= RAIL_HOLD_ATTEMPTS) else []
                    if not owed:
                        self.index.run("DELETE FROM outbox WHERE seq = ?", (row["seq"],))
                    else:
                        # One transaction: a stop must not leave the merge
                        # taken and the removals it owes unsaid.
                        self.index.atomic(
                            [("DELETE FROM outbox WHERE seq = ?", (row["seq"],))] + owed)
                        print(f"[mail-gateway] a merge of chat(s) {sorted(chats)!r} was taken "
                              f"late: the removed mails it carried are announced again",
                              flush=True)
                    merging.pop(row["seq"], None)
                    if outcome == "ok":
                        self._rail_took()
                elif outcome == "unanswered":
                    attempts = row["attempts"] + 1
                    self.index.run(
                        "UPDATE outbox SET attempts = ?, due = ?, failed = COALESCE(failed, ?) "
                        "WHERE seq = ?", (attempts, now + _retry_after(attempts), now, row["seq"]))
                    if attempts < RAIL_HOLD_ATTEMPTS:
                        held |= chats
                    if attempts == 1:
                        print(f"[mail-gateway] the chats rail failed on a {row['kind']} event "
                              f"of chat(s) {sorted(chats)!r}; it keeps its place and is told "
                              f"again later, and only its chat's later events wait for it",
                              flush=True)
                    elif attempts == RAIL_HOLD_ATTEMPTS:
                        print(f"[mail-gateway] the chats rail failed on a {row['kind']} event "
                              f"of chat(s) {sorted(chats)!r} {attempts} times; it steps aside: "
                              f"told again on its own clock, it holds back nothing any more",
                              flush=True)

    def _overtaken(self, seq: int, payload) -> None:
        """Mark a queued removal that lets its chat's later events go ahead
        of it (it waits for a merge): it is sent as the index has its mails
        when it goes (:meth:`_as_now`), as a removal that stepped aside is.
        Written before anything goes ahead of it — one statement, so a stop
        leaves the row marked or as it was, and nothing overtook it then."""
        if not isinstance(payload, dict) or payload.get(OVERTAKEN):
            return
        text = json.dumps({**payload, OVERTAKEN: True}, ensure_ascii=False)
        self.index.run("UPDATE outbox SET payload = ? WHERE seq = ?", (text, seq))

    def _removed_under(self, merge: dict) -> list:
        """The removals a merge that stepped aside owes once it is taken
        (:meth:`_flush_rail`): the statements that queue them, for the
        transaction that takes the merge off the outbox.

        A removal waits only for the merges queued before it that join its
        own chat. Behind a chain of them — this one joined X to Y, a later
        one Y to Z, and the removal names Z, where the index has its mail —
        it went once the later merge was taken, while in the web gateway the
        mail was still under X; this merge now carries it into the survivor
        with its arrival. So every tombstone of the chats it absorbed is
        removed again: one removal per chat the index has them in now (the
        chat an absorbed key is in now, by the thread index), built as
        :meth:`_as_now` builds one — the ids still gone, the chat's ``live``
        as now — and queued behind everything, in its chat's order. The
        index does not remember which chat a mail was in before a merge, so
        that chat's other tombstones go along; a removal of a mail the web
        gateway holds as gone, or never had, changes nothing. A mail a
        removal still queued names is left out: that one goes after this
        merge, and says it."""
        if not _chats.chats_enabled() or not isinstance(merge, dict):
            return []
        chats = set()
        for key in merge.get("from") or []:
            if isinstance(key, str):
                chats.add(self.threads.chat_of(key) or key)
        queued: set = set()
        for row in self.index.rows("SELECT payload FROM outbox WHERE kind = 'removed'"):
            try:
                ids = json.loads(row["payload"]).get("message_ids")
            except (ValueError, AttributeError):
                continue
            if isinstance(ids, list):
                queued.update(str(mid) for mid in ids)
        owed = []
        for chat in sorted(chats):
            gone = [row["message_id"] for row in self.index.rows(
                "SELECT message_id FROM mails WHERE chat = ? AND removed = 1 "
                "AND record != '' ORDER BY ts, message_id", (chat,))
                if row["message_id"] not in queued]
            if not gone:
                continue
            live = self.index.one(
                "SELECT COUNT(*) AS n FROM mails WHERE chat = ? AND removed = 0 "
                "AND record != ''", (chat,))["n"]
            owed.append(self._event("removed", {"channel": CHANNEL, "account": ACCOUNT,
                                                "chat": chat, "message_ids": gone,
                                                "live": live}))
        return owed

    def _as_now(self, kind: str, payload: dict) -> dict | None:
        """A queued rail event as the index has its mail now, for one that
        may be sent out of its chat's order (:meth:`_flush_rail`); None when
        nothing of it is true any more, and it is dropped.

        A removal names only the mails that are still gone — one that came
        back since was announced so, and is not taken away again — and the
        chat's ``live`` as it is now. A told mail that is gone now, or no
        longer known here (its chat erased), is not told: its removal was or
        will be heard, and the chat must not show it again. One that is
        there is told as its record says now, under the chat it is in now,
        with what only the event knew kept (old mail's ``backfill`` and
        ``seen``, the press a send came of). A merge is sent as it was."""
        payload = {key: value for key, value in payload.items() if key != OVERTAKEN}
        if kind == "removed":
            chat, ids = payload.get("chat"), payload.get("message_ids")
            if not isinstance(chat, str) or not isinstance(ids, list):
                return payload
            ids = [str(mid) for mid in ids]
            back: set = set()
            for start in range(0, len(ids), 500):
                batch = tuple(ids[start:start + 500])
                marks = ",".join("?" * len(batch))
                back.update(row["message_id"] for row in self.index.rows(
                    f"SELECT message_id FROM mails WHERE removed = 0 "
                    f"AND message_id IN ({marks})", batch))
            gone = [mid for mid in ids if mid not in back]
            if not gone:
                return None
            live = self.index.one(
                "SELECT COUNT(*) AS n FROM mails WHERE chat = ? AND removed = 0 "
                "AND record != ''", (chat,))["n"]
            return {**payload, "message_ids": gone, "live": live}
        if kind != "inbound":
            return payload
        mid = str(payload.get("message_id") or "")
        row = self.index.one("SELECT record, chat, removed FROM mails WHERE message_id = ?",
                             (mid,))
        if row is None or row["removed"]:
            return None
        fields = _ibstore.read_record(self._record_path(row["record"])) \
            if row["record"] else None
        as_queued = {**payload, "chat": row["chat"]}
        if fields is None or fields.get("message_id") != mid or not fields.get("chat"):
            # Being recorded anew, or not readable just now: what the event
            # says of the mail is still the mail — under its chat of now.
            return as_queued
        try:
            event = self._event_of_record(fields, backfill=bool(payload.get("backfill")),
                                          seen=bool(payload.get("seen")))
        except Exception as exc:  # noqa: BLE001 - one event, told as it was queued
            if _environment(exc):
                raise
            return as_queued
        if payload.get("request_key"):
            event["request_key"] = payload["request_key"]
        return event

    def _event_chats(self, kind: str, payload) -> set:
        """The chats a queued rail event is about — what keeps it in order
        behind another (:meth:`_flush_rail`): a merge is about every chat it
        joins, the first offer of a mail about the chat it was queued in and
        the one the mail is in now — a merge since must not take it out of
        its old chat's hold, nor ahead of the merge event itself."""
        if not isinstance(payload, dict):
            return set()
        if kind == "offer":
            row = self.index.one("SELECT chat FROM mails WHERE message_id = ?",
                                 (str(payload.get("message_id") or ""),))
            queued = payload.get("chat")
            return ({row["chat"]} if row else set()) | (
                {queued} if isinstance(queued, str) else set())
        if kind == "merge":
            named = list(payload.get("from") or []) + [payload.get("into")]
            return {chat for chat in named if isinstance(chat, str)}
        chat = payload.get("chat")
        return {chat} if isinstance(chat, str) else set()

    @contextlib.contextmanager
    def rail_pass(self):
        """One pass over the rail — a poll, or its rail's half between polls.
        At its end: if every offer and event of it (RAIL_DOWN_NOTICE at
        least) was answered with a failure, or with none while the web
        gateway answers otherwise, the rail answers and takes no mail, and
        /health says so until a mail is taken (:meth:`_rail_took`)."""
        self._rail_tally = [0, 0]
        try:
            yield
        finally:
            calls, failed = self._rail_tally
            self._rail_tally = [0, 0]
            if calls >= RAIL_DOWN_NOTICE and failed == calls and not self._rail_refusing:
                self._rail_refusing = True
                print(f"[mail-gateway] the chats rail failed on all {calls} offers and events "
                      f"of a pass: it answers, and takes no mail", flush=True)

    def _rail_heard(self, outcome: str, what: str) -> str:
        """One rail call's outcome, read for the queue, with the rail's state
        brought up to date by it; ``what`` names the call for the log.

        ``"uncertain"`` — no answer in time — is settled by asking whether
        the web gateway answers at all (:func:`_rail_alive`): away, it is
        ``"down"``; up, the call is ``"unanswered"``, about this one event —
        except the RAIL_SILENT_MAX-th of those in a row, after which the rail
        counts as down for the rest of the pass: one that is up and too slow
        for every event must not hold the mirror for a timeout per owed
        mail. ``"down"`` backs every call off (:meth:`_rail_failed`);
        anything the web gateway answered with — a failure included — says
        it is up."""
        self._rail_tally[0] += 1
        if outcome == "uncertain":
            if not _rail_alive():
                outcome = "down"
            else:
                self._rail_silent += 1
                self._rail_tally[1] += 1
                if self._rail_silent >= RAIL_SILENT_MAX:
                    print(f"[mail-gateway] the chats rail gave no answer in time to "
                          f"{self._rail_silent} calls in a row, {what} the last, though it "
                          f"answers otherwise; it counts as not answering for now",
                          flush=True)
                    outcome = "down"
                else:
                    print(f"[mail-gateway] the chats rail gave no answer for {what}; whether "
                          f"it landed is unknown — tried again later (the rail answers "
                          f"otherwise: this one waits its own time)", flush=True)
                    return "unanswered"
            self._rail_failed()
            return outcome
        if outcome == "down":
            self._rail_failed()
            return outcome
        if outcome == "unanswered":
            self._rail_tally[1] += 1
        self._rail_answered()
        return outcome

    def _rail_failed(self) -> None:
        self._rail_down += 1
        self._rail_silent = 0
        self._rail_wait_until = self._clock() + min(
            RAIL_RETRY_SECONDS * 2 ** min(self._rail_down - 1, 16), RAIL_RETRY_MAX_SECONDS)

    def _rail_answered(self) -> None:
        if self._rail_down:
            print("[mail-gateway] the chats rail answers again", flush=True)
        self._rail_down, self._rail_wait_until, self._rail_silent = 0, 0.0, 0

    def _rail_took(self) -> None:
        """The rail took a mail. If it had failed on every one of a pass,
        what waited over that is due again at once — each such mail's own
        wait grew while the fault lasted, up to an hour — and /health stops
        saying it takes none. When it took one is kept, for /health: events
        that keep failing while others are taken — one was taken after the
        newest of them began to fail — are about themselves; otherwise the
        notice is about the rail (:meth:`health_notice`)."""
        self.index.set("rail_took", self._clock())
        if not self._rail_refusing:
            return
        self._rail_refusing = False
        print("[mail-gateway] the chats rail takes mail again; what waited for it is "
              "offered and told again now", flush=True)
        self.index.atomic([
            ("UPDATE offers SET due = 0 WHERE queued = 0 AND attempts > 0", ()),
            ("UPDATE outbox SET due = 0 WHERE attempts > 0", ())])

    # -- the gate: a mail that arrived is offered, until its chat takes it ------
    # The messengers' inbound flow (docs/messenger-chats.md, "Inbound flow"),
    # for a channel whose backlog is a mailbox: persist ``delivered: false``
    # (_write), gate, offer the rail a handover, and flip the flag only on a
    # real acceptance — a plain one, or the job of the turn that works the
    # mail reporting done. There is no fallback to forward to: a mail that
    # was not taken is offered again.

    @staticmethod
    def _owed(fields: dict) -> bool:
        """Whether a record is a mail the rail still owes an answer for."""
        return (fields.get("type") == _ibstore.T_INBOUND and not fields.get("delivered")
                and not fields.get("removed_at"))

    def _owe(self, message_id: str, chat: str) -> list:
        """The statements that put a mail that arrived into the rail's queue
        — for the transaction that confirms its record: its row among the
        mails owed, and its place in the outbox, behind every event queued
        before it. In its place, because the order tells: the user's reply
        from a mail client reads the chat up to it, and must not be heard
        before the mail it answers. The place remembers ``chat``, the one
        the mail is queued in (:meth:`_event_chats`)."""
        marker = self._event("offer", {"message_id": message_id, "chat": chat})
        return [("INSERT OR REPLACE INTO offers (message_id, attempts, due, queued) "
                 "VALUES (?, 0, 0, ?)", (message_id, int(marker is not None))), marker]

    def _gate(self, sender: str, list_id: str) -> dict:
        """The gate's verdict on one mail, as the rail is told it:
        ``{"forward", "vip", "instructed", "reason"}``.

        Two axes, independent of each other. The GROUP — the mail's
        ``List-Id``, else its sender — says how loud the arrival is:
        ``quieted`` or ``ignored`` and the chat takes the mail silently
        (``forward`` false). The SENDER says whether a model turn is owed:
        a VIP's mail rings and is worked, an instructed sender's is worked
        by their instruction, and neither is stopped by the group.
        ``sender`` is the mail's gate sender (:func:`_gate_sender`) and
        nothing else; "" — nobody — matches no person and no group of one.

        Asked when the offer is made, not when the mail arrived: a mail
        that waited is judged by the policy as it stands.

        The group flags unreadable: no flags, as the mirror reads them (fail
        open — the mail reaches its chat as any mail). The sender policy
        unreadable: nobody is special (fail closed, for turns only) — a
        mail must not buy a model turn because a file could not be read.
        And the gate is never switched off for mail (``enabled=False``
        would make every sender a VIP)."""
        if self._flags is None:
            self._flags = self._policy()
        try:
            group = _triage.email_gate_decision(sender, list_id, pol=self._flags)
        except Exception as exc:  # noqa: BLE001 - no flags is the safe reading
            print(f"[mail-gateway] e-mail gate failed ({exc}); offering the mail as with "
                  f"no flags", flush=True)
            group = {"group": sender}
        try:
            who = _triage.gate_decision(CHANNEL, sender, group.get("group") or None,
                                        path=SENDER_POLICY_PATH)
        except Exception:  # noqa: BLE001 - as an unreadable policy
            who = {"reason": "policy-unreadable"}
        unreadable = who.get("reason") == "policy-unreadable"
        if unreadable != self._gate_said:
            self._gate_said = unreadable
            print(f"[mail-gateway] the sender policy {SENDER_POLICY_PATH} cannot be read: "
                  f"until it can, no mail buys a model turn (every mail still reaches "
                  f"its chat)" if unreadable else
                  "[mail-gateway] the sender policy can be read again", flush=True)
        vip = not unreadable and who.get("vip") is True
        instructed = not unreadable and who.get("instructed") is True
        held = ("group-ignored" if group.get("ignored") else
                "group-quieted" if group.get("quieted") else None)
        reason = "+".join(part for part in (
            "vip" if vip else None, "instructed" if instructed else None, held) if part)
        return {"forward": held is None, "vip": vip, "instructed": instructed,
                "reason": reason or ("policy-unreadable" if unreadable else "open")}

    def _rail_files(self, fields: dict) -> list:
        """A mail's attachments in the shape the rail takes files in
        (``{"filename", "content_type", "data"}``, base64), for the turn that
        works the mail — within the rail's limits (MAIL_RAIL_FILE_BYTES a
        file; RAIL_FILES_MAX files and RAIL_FILES_BYTES an event). What does
        not fit is left out here and is in the chat all the same.

        The name and the type are the sender's claims. Of the name only a
        short extension travels, on a name of the gateway's own — it is what
        lets the reading session type the file; the type goes as the one
        clean line the ledger keeps it as."""
        files, total, left = [], 0, 0
        for ref in fields.get("attachments") or []:
            media_id = _ibstore.media_id_of(ref)
            meta = _ibstore.media_meta(INBOUND_STORE_DIR, media_id) if media_id else None
            if not meta or not meta["size"]:
                continue
            if (len(files) >= RAIL_FILES_MAX or meta["size"] > MAIL_RAIL_FILE_BYTES
                    or total + meta["size"] > RAIL_FILES_BYTES):
                left += 1
                continue
            got = _ibstore.load_media(INBOUND_STORE_DIR, media_id)
            if got is None:
                continue
            suffix = Path(meta.get("file_name") or "").suffix
            total += len(got[0])
            files.append({
                "filename": f"attachment-{len(files) + 1}"
                            + (suffix if _FILE_SUFFIX_RE.fullmatch(suffix) else ""),
                "content_type": got[1],
                "data": base64.b64encode(got[0]).decode("ascii")})
        if left:
            print(f"[mail-gateway] {left} attachment(s) of {fields.get('message_id')!r} do "
                  f"not fit a rail event and stay in the ledger", flush=True)
        return files

    def _offer_event(self, chat: str, fields: dict) -> dict:
        """The event a mail is offered with, made of its record as it stands
        now — and under the chat it is in now: a mail whose thread was
        merged away since it arrived is offered in the surviving chat."""
        sender = fields.get("sender") or ""
        # (An inbound record of a mail with no readable From says "unknown".)
        sender = sender if "@" in sender else ""
        # Gated on the gate sender alone — never on what the chat shows,
        # which may be the Sender header (_gate_sender). A record without
        # one is from nobody: it reaches its chat, and buys no turn.
        gate_sender = fields.get("gate_sender") or ""
        gate = self._gate(gate_sender, fields.get("list_id") or "")
        event = _text_only(self._inbound_event(
            direction="in", chat=chat, message_id=fields["message_id"],
            ts=self._record_time(fields), sender=sender or None,
            sender_name=fields.get("sender_name"), gate_sender=gate_sender,
            text=fields.get("text") or "",
            attachments=list(fields.get("attachments") or []), author=None,
            subject=fields.get("subject_line") or None, backfill=False, seen=False,
            gate=gate, trim=fields.get("trim")))
        # The offer: if the chat takes this mail, that is its delivery.
        event["handover"] = True
        if gate["vip"] or gate["instructed"]:
            # A turn will work this mail, and may need to open what came
            # with it. For anyone else the chat shows the attachments from
            # the ledger, and nothing has to travel.
            files = self._rail_files(fields)
            if files:
                event["files"] = files
        return event

    def _offer(self, message_id: str, seq: int | None = None) -> bool:
        """Offer the rail one mail it is owed, and act on its answer. False
        when the web gateway did not answer: nothing is changed then, the
        mail keeps its place, and the caller stops.

        ``seq`` is the mail's marker in the outbox when this is its first
        offer, made in its place among the events; the marker goes with any
        answer but a young failure about this mail (``unanswered``, below),
        and from then on the mail is offered by :meth:`_offer_again`.

          * ``accepted`` — the chat has the mail: its record is marked
            delivered, and nothing is owed.
          * ``accepted`` with ``job_url`` — a turn is working it (a VIP's
            mail, an instructed sender's). Delivered only when that job
            reports done (:meth:`_await_job`); a job that fails, expires or
            never ends leaves the mail owed.
          * anything else the web gateway answers — no acceptance, a refusal
            — the mail stays ``delivered: false`` and is offered again once
            its wait is over (:func:`_retry_after`). Nothing forwards it
            anywhere else: there is no triage for mail.
          * no answer in time, or none at all (``uncertain``) — whether the
            event landed is not known. Neither a yes nor a no, and the mail
            is offered again, which is safe, since the web gateway takes a
            repeat once, by its message id, and hands back the job it
            started for it. Whether everything behind it waits is the
            rail's to say (:meth:`_rail_heard`): one that does not answer
            at all is down, and nothing behind the mail goes; one that
            answers is up, and did not answer for this mail — which then
            waits its own time, like a mail the web gateway answered a
            failure for (``unanswered``). One mail the other side keeps
            failing on must hold back neither the other chats' mails nor
            their events: a first offer that failed keeps its place in the
            outbox, where only its own chat's later events wait for it
            (:meth:`_flush_rail`), and is tried there again — for
            RAIL_HOLD_ATTEMPTS tries in all. Then it steps aside: the marker
            goes, the mail is offered by :meth:`_offer_again` on its own
            clock, and its chat's later events no longer wait for it.

        A mail that is gone by now — tombstoned, erased with its chat,
        recorded anew as the user's own — is offered to nobody: its row
        goes, and the marker with it. A record that is there and could not
        be read just now is not gone: the mail waits its time and is
        offered again."""
        settle = [("DELETE FROM outbox WHERE seq = ?", (seq,))] if seq is not None else []
        row = self.index.one(
            "SELECT record, chat FROM mails WHERE message_id = ? AND record != '' "
            "AND removed = 0 AND direction = 'in'", (message_id,))
        fields = _ibstore.read_record(self._record_path(row["record"])) if row else None
        if row is not None and fields is None and self._record_path(row["record"]).exists():
            # (EIO, EMFILE, a volume that faltered: the offers row carries
            # the mail from here, and _offer_again comes back to it.)
            known = self.index.one("SELECT attempts, failed FROM offers WHERE message_id = ?",
                                   (message_id,))
            attempts = (known["attempts"] if known else 0) + 1
            if attempts == 1:
                print(f"[mail-gateway] the record of {message_id!r} could not be read; it "
                      f"is offered again later", flush=True)
            failed = known["failed"] if known and known["failed"] is not None else self._clock()
            self.index.atomic(settle + [
                ("INSERT OR REPLACE INTO offers (message_id, attempts, due, queued, failed) "
                 "VALUES (?, ?, ?, 0, ?)",
                 (message_id, attempts, self._clock() + _retry_after(attempts), failed))])
            return True
        if fields is None or not self._owed(fields):
            self.index.atomic(settle + [
                ("DELETE FROM offers WHERE message_id = ?", (message_id,))])
            return True
        now = self._clock()
        job = self._jobs.get(message_id)
        if job is not None and job.is_alive():
            # The turn that took it is still at work.
            self.index.atomic(settle + [
                ("UPDATE offers SET queued = 0, due = MAX(due, ?) WHERE message_id = ?",
                 (now + RETRY_SECONDS, message_id))])
            return True
        self._jobs.pop(message_id, None)
        reason, stepped = "?", None
        try:
            event = self._offer_event(row["chat"], fields)
            reason = event["gate"]["reason"]
        except Exception as exc:  # noqa: BLE001 - one mail's offer, not every pass after it
            if _environment(exc):
                raise
            print(f"[mail-gateway] the offer of {message_id!r} could not be made "
                  f"({type(exc).__name__}: {exc}); will retry\n{traceback.format_exc()}",
                  flush=True)
            outcome, answer = "drop", {}
        else:
            outcome, answer = _rail_offer(event)
            outcome = self._rail_heard(outcome, f"the offer of {message_id!r}")
            if outcome == "down":
                return False
            if outcome == "unanswered" and seq is not None:
                # Its first offer: the mail keeps its place, so its chat's
                # later events still come after it, and waits its own time —
                # for RAIL_HOLD_ATTEMPTS tries. Then it steps aside, below.
                marker = self.index.one("SELECT attempts, failed FROM outbox WHERE seq = ?",
                                        (seq,))
                tries = (marker["attempts"] if marker else 0) + 1
                first = marker["failed"] if marker and marker["failed"] is not None else now
                if tries < RAIL_HOLD_ATTEMPTS:
                    self.index.run("UPDATE outbox SET attempts = ?, due = ?, failed = ? "
                                   "WHERE seq = ?", (tries, now + _retry_after(tries), first, seq))
                    if tries == 1:
                        print(f"[mail-gateway] the chats rail did not take {message_id!r} (it "
                              f"failed on it); it stays undelivered, keeps its place and is "
                              f"offered again", flush=True)
                    return True
                stepped = (tries, first)
                print(f"[mail-gateway] the chats rail failed on {message_id!r} {tries} times; "
                      f"it steps aside and is offered again on its own clock — its chat's "
                      f"later events go on", flush=True)
        accepted = outcome == "ok" and answer.get("accepted") is True
        job_url = answer.get("job_url") if accepted else None
        resolved = _job_url(job_url) if job_url is not None else None
        if job_url is not None and resolved is None:
            # A job is polled where the rail is, and nowhere else.
            print(f"[mail-gateway] the chats rail named a job for {message_id!r} that is "
                  f"no path on it; taken as no acceptance", flush=True)
            accepted, job_url = False, None
        if accepted:
            self._rail_took()
        if accepted and not job_url:
            # Only an explicit acceptance is a handover. The chat has the
            # mail, nothing is owed a model, and that is the delivery.
            with self._records:
                delivered = _ibstore.mark_delivered(self._record_path(row["record"]))
            if delivered:
                self.index.atomic(settle + [
                    ("DELETE FROM offers WHERE message_id = ?", (message_id,))])
                print(f"[mail-gateway] the chat took {message_id!r} ({reason})", flush=True)
                return True
            print(f"[mail-gateway] the chat took {message_id!r}, but its record could not "
                  f"be marked delivered; it is offered again", flush=True)
        known = self.index.one("SELECT attempts, failed FROM offers WHERE message_id = ?",
                               (message_id,))
        # A first offer that stepped aside goes on from its marker's count.
        attempts = stepped[0] if stepped else (known["attempts"] if known else 0) + 1
        failed = known["failed"] if known else None
        if stepped:
            failed = stepped[1]
        elif failed is None and not (accepted and job_url):
            failed = now   # not taken: since when, for /health (waiting)
        self.index.atomic(settle + [
            ("INSERT OR REPLACE INTO offers (message_id, attempts, due, queued, failed) "
             "VALUES (?, ?, ?, 0, ?)",
             (message_id, attempts, now + _retry_after(attempts), failed))])
        if accepted and job_url:
            print(f"[mail-gateway] the chat's companion turn took {message_id!r} "
                  f"({reason})", flush=True)
            self._await_job(message_id, resolved)
        elif attempts == 1:
            # Said once per mail; /health counts what waits.
            said = {"drop": "refused", "unanswered": "it failed on it"}.get(
                outcome, "no acceptance")
            print(f"[mail-gateway] the chats rail did not take {message_id!r} ({said}); it "
                  f"stays undelivered and is offered again", flush=True)
        return True

    def _offer_again(self, limit: int) -> None:
        """Offer again the mails that were offered and not taken — and those
        the ledger holds undelivered with no offer on record (a rebuilt
        index): the oldest first, at most ``limit`` offers a pass, each only
        once its own wait is over, and none while the web gateway is not
        answering.

        A mail whose turn is still at work is passed over, and takes none
        of the ``limit``: its job settles it (:meth:`_await_job`), and a
        backlog of turns at work must not keep every other mail waiting."""
        if not _chats.chats_enabled() or self._clock() < self._rail_wait_until:
            return
        self._flags = None
        busy = sorted(mid for mid, job in self._jobs.items() if job.is_alive())
        marks = ",".join("?" * len(busy))
        if busy:
            # Due again a little after its job is: a restart that forgets
            # the job then offers the mail once its wait is over.
            self.index.run(f"UPDATE offers SET due = MAX(due, ?) WHERE message_id IN ({marks})",
                           (self._clock() + RETRY_SECONDS, *busy))
        for row in self.index.rows(
                "SELECT o.message_id FROM offers o "
                "LEFT JOIN mails m ON m.message_id = o.message_id "
                "WHERE o.queued = 0 AND o.due <= ? "
                + (f"AND o.message_id NOT IN ({marks}) " if busy else "")
                + "ORDER BY COALESCE(m.arrived, m.ts), o.message_id LIMIT ?",
                (self._clock(), *busy, limit)):
            if not self._offer(row["message_id"]):
                return

    def _await_job(self, message_id: str, job_url: str) -> None:
        """Wait, on a thread of its own, for the job of the turn that works
        a mail; the mail is delivered when it reports done, and not before
        (job_delivery). Every other end — the job failed, expired, or did
        not finish within MAIL_RAIL_JOB_TIMEOUT — leaves the record as it
        is, and the mail is offered again. ``job_url`` is the URL as
        :func:`_job_url` resolved and checked it."""
        self._jobs = {mid: thread for mid, thread in self._jobs.items() if thread.is_alive()}
        self._jobs[message_id] = _jobs.confirm_delivery(
            job_url,
            lambda: self._job_done(message_id),
            log=lambda said: print(f"[mail-gateway] the turn for {message_id!r}: {said}",
                                   flush=True),
            thread_name="mail-job", timeout=MAIL_RAIL_JOB_TIMEOUT)

    def _job_done(self, message_id: str) -> None:
        """The turn that worked a mail is done: the mail is delivered. Under
        the mirror's lock — a record has one writer at a time — and by the
        record the mail has now."""
        with self._lock:
            row = self.index.one(
                "SELECT record FROM mails WHERE message_id = ? AND record != ''", (message_id,))
            if row is None:
                return
            with self._records:
                delivered = _ibstore.mark_delivered(self._record_path(row["record"]))
            if delivered:
                self.index.run("DELETE FROM offers WHERE message_id = ?", (message_id,))
                print(f"[mail-gateway] the turn for {message_id!r} is done: delivered",
                      flush=True)

    # -- the news rail ---------------------------------------------------------
    # A mail of a `news` group is a reference for the feed, whoever sent it
    # (_mirror_one queues it: _news_filing). Filing is at-least-once and
    # needs no mailbox; taking the mail out of the INBOX comes strictly after
    # the feed has it. So a feed that refuses — or is away — leaves the mail
    # exactly as it arrived, and it is tried again.

    def _file_news(self, limit: int) -> None:
        """File what is queued for the feed, the oldest first, and take out
        of the INBOX what is to leave it once filed. One refusal ends the
        pass (a feed that is away is asked once a pass, not once a mail);
        the refused mail waits its own, growing, time, so one item the feed
        will never take does not hold the others back."""
        if not _news.news_enabled():
            return
        now, settled = self._clock(), False
        for row in self.index.rows(
                "SELECT message_id, item, consume, state, attempts FROM news "
                "WHERE state != 'done' AND due <= ? ORDER BY created, rowid LIMIT ?",
                (now, limit)):
            message_id, state = row["message_id"], row["state"]
            if state == "due":
                try:
                    item = json.loads(row["item"])
                except ValueError:
                    item = None
                if isinstance(item, dict) and not _news_send(item):
                    attempts = row["attempts"] + 1
                    self.index.run("UPDATE news SET attempts = ?, due = ? WHERE message_id = ?",
                                   (attempts, now + _retry_after(attempts), message_id))
                    if attempts == 1:
                        print(f"[mail-gateway] the news feed did not take {message_id!r}; "
                              f"the mail is left as it is and filed again later", flush=True)
                    break
                state = "filed" if row["consume"] and isinstance(item, dict) else "done"
                self.index.run("UPDATE news SET state = ?, attempts = 0, due = 0 "
                               "WHERE message_id = ?", (state, message_id))
                if isinstance(item, dict):
                    print(f"[mail-gateway] news: filed {message_id!r} to the feed", flush=True)
            if state == "filed":
                try:
                    done = self._consume_news(message_id)
                    said = None
                except MailboxError as exc:
                    done, said = False, str(exc)
                if said != self._news_said.get(message_id):
                    self._news_said[message_id] = said
                    if said:
                        print(f"[mail-gateway] news: {message_id!r} is filed, but could not "
                              f"be taken out of the INBOX ({said}); will retry", flush=True)
                if not done:
                    attempts = row["attempts"] + 1
                    self.index.run("UPDATE news SET attempts = ?, due = ? WHERE message_id = ?",
                                   (attempts, now + _retry_after(attempts), message_id))
                    continue
                self._news_said.pop(message_id, None)
                self.index.run("UPDATE news SET state = 'done' WHERE message_id = ?",
                               (message_id,))
                state = "done"
            settled = settled or state == "done"
        if settled:
            self.index.run(
                "DELETE FROM news WHERE state = 'done' AND message_id NOT IN (SELECT message_id "
                "FROM news WHERE state = 'done' ORDER BY created DESC, rowid DESC LIMIT ?)",
                (NEWS_DONE_MAX,))

    def _news_target(self) -> tuple:
        """Where a filed news mail goes: ``(folder, mirrored)`` — the
        archive folder unless MAIL_NEWS_FOLDER names another, by the rules
        every move of the gateway's goes by (the name the deployment set,
        else the server's own folder for the role, else the default name;
        made, and subscribed, when missing). ``(None, False)`` to leave the
        mail where it is: MAIL_NEWS_FOLDER set and empty — or naming the
        INBOX or Sent, which no news mail is moved into."""
        if MAIL_NEWS_FOLDER == "":
            return None, False
        if MAIL_NEWS_FOLDER is None or MAIL_NEWS_FOLDER == self._folders.get("archive"):
            target = self._destination("archive", MAIL_ARCHIVE_FOLDER)
            if self._folder("archive") is None:
                # A folder the mirror has not looked at yet (see archive()).
                self._first_sight("archive", target)
            return target, True
        if MAIL_NEWS_FOLDER in (self._folders.get("inbox"), self._folders.get("sent")):
            return None, False
        if not self._news_folder_made:
            self.mailbox.create(MAIL_NEWS_FOLDER)   # (there already is no failure)
            self._news_folder_made = True
        return MAIL_NEWS_FOLDER, False

    def _consume_news(self, message_id: str) -> bool:
        """Take a filed news mail out of the INBOX: ``\\Seen``, and moved to
        the news folder — by the adapter's write half, like everything the
        gateway does to the mailbox, and followed by the mirror's own index,
        so the next poll reads none of it as mail that came or went. True
        when nothing is owed any more; False to look again later; raises
        MailboxError when the mailbox would not do it.

        Only where the mail still is where it arrived: one a client moved or
        deleted since is left alone."""
        folder, name = self._folder("inbox"), self._folders.get("inbox")
        if folder is None or not name or folder["name"] != name:
            return False
        places = self.index.rows(
            "SELECT gen, uid FROM locations WHERE role = 'inbox' AND message_id = ? "
            "AND skipped = 'news'", (message_id,))
        uids = [row["uid"] for row in places if row["gen"] == folder["gen"]]
        if not uids:
            # (A place a generation behind is found again first: _reidentify.)
            return not places
        self.mailbox.set_seen(name, uids, validity=folder["uidvalidity"])
        target, mirrored = self._news_target()
        if target is None or target == name:
            return True
        result = self.mailbox.move(name, uids, target, validity=folder["uidvalidity"])
        if result is None:
            raise MailboxError("the server says there is no INBOX")
        there = self._folder("archive") if mirrored else None
        for uid in result.absent:
            self.index.run("DELETE FROM locations WHERE role = 'inbox' AND gen = ? AND uid = ?",
                           (folder["gen"], uid))
        for uid, new in {**result.moved, **result.left}.items():
            self.index.atomic([
                # A copy the server kept, flagged \Deleted, is still a place
                # of the mail — never one to move or flag again.
                ("UPDATE locations SET skipped = 'moved' WHERE role = 'inbox' AND gen = ? "
                 "AND uid = ?" if uid in result.left else
                 "DELETE FROM locations WHERE role = 'inbox' AND gen = ? AND uid = ?",
                 (folder["gen"], uid)),
                # Its place in the archive, where the server named it: still
                # news there, should the archive ever be looked at.
                ("INSERT OR REPLACE INTO locations (role, gen, uid, message_id, skipped) "
                 "VALUES ('archive', ?, ?, ?, 'news')", (there["gen"], new, message_id))
                if new is not None and there is not None and there["name"] == target
                else None])
        print(f"[mail-gateway] news: {message_id!r} marked read and moved to {target!r}",
              flush=True)
        return True

    # -- the mailbox follows the chat ------------------------------------------
    # What the user did to a chat, done to its mails (docs/mail-chats.md, "The
    # mailbox follows the chat, never the reverse"). Each works from the
    # places the index holds — where the mirror last saw the thread's mails —
    # under the UIDVALIDITY they were read with, and tells the index what it
    # did at once: the next poll finds the mailbox as the index says it is,
    # and reads none of it as mail that came or went.

    @contextlib.contextmanager
    def exclusive(self):
        """The mirror's lock for a caller that is not the poll, waited for
        EXCLUSIVE_SECONDS: these calls rewrite the index and the ledger,
        which have one writer at a time. :class:`Busy` when a pass holds it
        longer — the caller's request has a deadline of its own, and is made
        again."""
        if not self._lock.acquire(timeout=EXCLUSIVE_SECONDS):
            raise Busy("the mirror is in the middle of a pass over the mailbox; "
                       "try again in a moment")
        try:
            yield
        finally:
            self._lock.release()

    def _places(self, chat: str, roles: tuple, only=None) -> dict:
        """Where a thread's mails are: ``{role: {UID: Message-ID}}``, from
        the index, in each folder's current numbering — without the copies
        the gateway moved on already (``moved``). ``only``: of the mails
        with these ids, and no other."""
        out: dict = {}
        for row in self.index.rows(
                "SELECT l.role, l.uid, l.message_id FROM locations l "
                "JOIN folders f ON f.role = l.role AND f.gen = l.gen "
                "JOIN mails m ON m.message_id = l.message_id "
                "WHERE m.chat = ? AND m.record != '' AND l.skipped IS NULL "
                f"AND l.role IN ({','.join('?' * len(roles))}) "
                "ORDER BY l.role, l.uid", (chat, *roles)):
            if only is None or row["message_id"] in only:
                out.setdefault(row["role"], {})[row["uid"]] = row["message_id"]
        return out

    def _destination(self, role: str, configured: str, *, make: bool = True) -> str:
        """The folder a move goes to — the one the mailbox has for the role
        (:func:`resolve_folders`: the name the deployment set, else the
        server's own, else the default name) — made, and subscribed, if it
        has none and ``make`` allows.

        The archive is made. A Trash never is: a folder the gateway invents
        is one no mail client treats as the trash and no server ever
        empties, so "moved to the Trash" would not be true of it — the
        delete fails instead, and says what to set."""
        self._folders = self.mailbox.folders()
        name = self._folders.get(role)
        if name:
            return name
        if not make:
            raise MailboxError(
                f"the mailbox has no Trash folder — "
                + (f"none named {configured!r}, as MAIL_TRASH_FOLDER says"
                   if MAIL_TRASH_FOLDER_SET else
                   f"none the server marks as its Trash, and none named {configured!r}")
                + ": set MAIL_TRASH_FOLDER to the name of the account's trash folder")
        self.mailbox.create(configured)
        self._folders = self.mailbox.folders()
        name = self._folders.get(role)
        if not name:
            raise MailboxError(f"the mailbox has no {role} folder, and {configured!r} "
                               f"could not be made one")
        print(f"[mail-gateway] made the {role} folder {name!r}", flush=True)
        return name

    def mark_read(self, chat: str, only=None) -> int:
        """``\\Seen`` on every mail of the thread in INBOX or the archive
        folder; how many were flagged — counted where they are: a mail the
        mailbox shows in both folders (a label mailbox) is flagged, and
        counted, in each. A reply, or "Mark as read".

        ``only`` (a set of Message-IDs) is what the chat held when the user
        acted: the call is made after the act and served later still — it
        waits for the pass that has the mirror's lock, and that pass may
        just have mirrored a new mail of the thread, which the chat counts
        as unread. Given the ids, exactly those mails are flagged, whatever
        the thread holds by now; without them, the thread as it stands."""
        with self.exclusive():
            seen = 0
            for role, uids in self._places(chat, ("inbox", "archive"), only).items():
                folder = self._folder(role)
                seen += self.mailbox.set_seen(folder["name"], list(uids),
                                              validity=folder["uidvalidity"]) or 0
            return seen

    def archive(self, chat: str, only=None) -> dict:
        """Move the thread's INBOX mails to the archive folder (made if
        absent). ``{"moved", "flagged"}``: how many are in the archive now,
        and how many of those the INBOX still holds, flagged ``\\Deleted``,
        because the server has no way to remove exactly them.

        ``only`` as for :meth:`mark_read`: the mails the chat held when it
        was archived. One that arrived since has un-archived the chat, and
        stays in the INBOX.

        The mirror follows by its own index: each mail's place moves with
        it, so nothing is tombstoned and the rail hears nothing."""
        with self.exclusive():
            self._recover_move()
            uids = self._places(chat, ("inbox",), only).get("inbox")
            if not uids:
                return {"moved": 0, "flagged": 0}
            target = self._destination("archive", MAIL_ARCHIVE_FOLDER)
            if self._folder("archive") is None:
                # A folder the mirror has not looked at yet — the one just
                # made: taken in now, as its next poll would, so that the
                # mails moved into it have their place at once, by the UIDs
                # the server names, and are found there by whatever is done
                # to the chat next (a delete follows an archive by seconds).
                self._first_sight("archive", target)
            folder = self._folder("inbox")
            self._note_move("inbox", uids, "archive")
            result = self.mailbox.move(folder["name"], list(uids), target,
                                       validity=folder["uidvalidity"])
            if result is None:
                self._moved_noted()
                raise MailboxError("the server says there is no INBOX")
            self._follow_move("inbox", uids, result, "archive", target)
            self._moved_noted()
            if result.left:
                print(f"[mail-gateway] {len(result.left)} mail(s) of chat {chat!r} were "
                      f"copied to {target!r} and are still in the INBOX, flagged \\Deleted: "
                      f"the server offers neither MOVE nor UIDPLUS", flush=True)
            return {"moved": len(result.moved) + len(result.left), "flagged": len(result.left)}

    def _note_move(self, source: str, uids: dict, target: str | None) -> None:
        """Before the server is asked to move mails (``{UID: Message-ID}`` in
        the folder of ``source``, to the folder of ``target``, a role or None
        for the Trash): what is being done, so that a stop between the
        server's move and the index following it (_follow_move) is put
        right by the next pass (:meth:`_recover_move`). One move at a time:
        each is made, and followed, under the mirror's lock."""
        self.index.set("moving", json.dumps({
            "role": source, "gen": self._folder(source)["gen"], "target": target,
            "uids": {str(uid): message_id for uid, message_id in uids.items()}}))

    def _moved_noted(self) -> None:
        """The move noted is followed: nothing to put right."""
        self.index.run("DELETE FROM meta WHERE key = 'moving'")

    def _recover_move(self) -> None:
        """Put right what a stop during a move of the gateway's own left
        (_note_move): a source UID the index still places although the
        server has it no more — gone, or flagged ``\\Deleted`` (kept, on a
        server without MOVE or UIDPLUS) — is that: gone from the index, or
        marked ``moved``. And a mail so left with no place but such a
        leftover settles, as _follow_move would have had it — the settle
        pass finds its copy in the folder it was moved to (_adopt), and puts
        an undone move back (_undeleted). A UID the server still holds as it
        was is the mail's place still: the move never took. A source folder
        renumbered since asks nothing of this (its places are found again
        anyway, _reidentify)."""
        note = self.index.get("moving")
        if note is None:
            return
        try:
            moving = json.loads(note)
            role, uids, target = moving["role"], dict(moving["uids"]), moving["target"]
        except (ValueError, KeyError, TypeError):
            moving, role, uids, target = {}, None, {}, None
        folder = self._folder(role) if role else None
        if folder is not None and folder["gen"] == moving.get("gen"):
            for uid, message_id in uids.items():
                uid = int(uid)
                here = self.index.one(
                    "SELECT skipped FROM locations WHERE role = ? AND gen = ? AND uid = ? "
                    "AND message_id = ?", (role, folder["gen"], uid, message_id))
                if here is not None and here["skipped"] is None:
                    fetched = self.mailbox.fetch(folder["name"], uid, limit=1)
                    if fetched is not None \
                            and fetched.uidvalidity not in (None, folder["uidvalidity"]):
                        break   # renumbered since: the UIDs say nothing
                    if fetched is not None and "\\Deleted" not in fetched.flags:
                        continue    # not moved: where it is, is its place
                    self.index.run(
                        "DELETE FROM locations WHERE role = ? AND gen = ? AND uid = ?"
                        if fetched is None else
                        "UPDATE locations SET skipped = 'moved' WHERE role = ? AND gen = ? "
                        "AND uid = ?", (role, folder["gen"], uid))
                if target is not None and self.index.one(
                        "SELECT 1 FROM locations WHERE message_id = ? AND skipped IS NOT "
                        "'moved' AND skipped IS NOT 'other'", (message_id,)) is None:
                    self.index.run(
                        "INSERT OR REPLACE INTO settling (message_id, until) VALUES (?, ?)",
                        (message_id, self._clock() + MAIL_SENT_GRACE_SECONDS))
            print(f"[mail-gateway] a move of {len(uids)} mail(s) out of {folder['name']!r} "
                  f"was stopped before the index followed it: put right", flush=True)
        self._moved_noted()

    def _follow_move(self, source: str, uids: dict, result: Moved,
                     target: str | None = None, target_name: str | None = None) -> None:
        """Make the index say what a move of the gateway's own did: the old
        place gone (or marked ``moved``, where the server kept the copy), the
        new one known — by the UID the server named, else by looking for the
        mail there. ``target`` is the destination's role when it is a
        mirrored folder; the Trash is none, and a mail gone there has no
        place left.

        A mail whose new place could not be told is no mail that left the
        mailbox: it settles (``settling``), and the settle pass finds it —
        also where the server kept the copy here: a ``moved`` leftover is
        never a mail's only place for good (_settle_unlocated)."""
        gen = self._folder(source)["gen"]
        there = self._folder(target) if target else None
        placeable = there is not None and there["name"] == target_name
        for uid in result.absent:
            # Not there to be moved: a client moved it first. Where it went
            # is the settle pass's to find, like any mail that left a folder.
            self.index.run("DELETE FROM locations WHERE role = ? AND gen = ? AND uid = ?",
                           (source, gen, uid))
        for uid, new in {**result.moved, **result.left}.items():
            message_id, kept = uids[uid], uid in result.left
            if not self.index.run(
                    "UPDATE locations SET skipped = 'moved' WHERE role = ? AND gen = ? AND uid = ?"
                    if kept else
                    "DELETE FROM locations WHERE role = ? AND gen = ? AND uid = ?",
                    (source, gen, uid)) and kept:
                # A copy that had no place here — a mail of `lost`, which has
                # no row to place it under (delete_chat) — is kept all the
                # same: it is that, and no new mail to be read.
                self.index.run(
                    "INSERT OR REPLACE INTO locations (role, gen, uid, message_id, skipped) "
                    "VALUES (?, ?, ?, ?, 'moved')", (source, gen, uid, message_id))
            if target is None:
                continue
            if new is None and placeable:
                # Looked for as any mail that lost its place is (_adopt): under
                # every id it may be filed under, and — for a placeholder —
                # only its own copy, by the header block, is its place there.
                row = self.index.one("SELECT ts, arrived FROM mails WHERE message_id = ?",
                                     (message_id,))
                try:
                    found = self._adopt(target, message_id, row["ts"] if row else None,
                                        row["arrived"] if row else None)
                except (MailboxError, MirrorFault, ReaderDown):
                    found = UNSURE
                new = None if found is UNSURE else found
            if new is not None and placeable:
                self._locate(target, new, message_id)
            else:
                self.index.run(
                    "INSERT OR REPLACE INTO settling (message_id, until) VALUES (?, ?)",
                    (message_id, self._clock() + MAIL_SENT_GRACE_SECONDS))

    def _whereabouts(self, role: str, message_id: str, ts, arrived, *, placed: bool = True):
        """Where a mail is in one mirrored folder, for a delete: as
        :meth:`_adopt` — which places what it finds, and for a placeholder
        only its own copy (I2); not ``placed``, an id of `lost`, which has
        no row to place a mail under: only found (:meth:`_find`) — but
        there "the mirror has no row for this folder" is answered None,
        which is right for a poll (its next look reads the folder) and
        wrong here, where None lets a thread be erased. A folder the server
        has and the mirror has not read under that name — the archive an
        Archive just made, a role that names another folder since the last
        poll — is UNSURE: its mails are where no index says. So is a reading
        of a header block that did not come back, or a reader at fault (I1):
        nothing is erased on it.

        A copy the gateway moved on and the server kept (``moved``: flagged
        \\Deleted, on a server without MOVE or UIDPLUS — this very delete's,
        or an earlier archive's) is no place of the mail: the search does
        not hand it out (a UID with a place is spoken for), and taken for
        one it would be moved to the Trash a second time. It is answered as
        not held — while the server still flags it ``\\Deleted``. A client
        may have cleared that flag since (an undelete keeps the UID): then it
        is the mail's place again, and the caller's ``_locate`` makes it one.
        (A real copy that came back to the folder since has a UID of its
        own, and the search finds that one first.)"""
        name, folder = self._folders.get(role), self._folder(role)
        if not name:
            return None     # the mailbox has no such folder: nothing is in it
        if folder is None or folder["name"] != name:
            return UNSURE
        try:
            if placed:
                found = self._adopt(role, message_id, ts, arrived)
            else:
                uids, sure = self._find(role, message_id, None, arrived)
                found = uids[0] if uids else None if sure else UNSURE
        except (MirrorFault, ReaderDown) as exc:
            print(f"[mail-gateway] could not tell where {message_id!r} is in {name!r}: "
                  f"{exc}", flush=True)
            return UNSURE
        if found is not None and found is not UNSURE:
            return found
        back = self._undeleted(role, message_id)
        return found if back is None else back

    def _undeleted(self, role: str, message_id: str):
        """A copy of the mail the gateway moved on and the server kept
        (``moved``) that is its place again in that folder: the server flags
        it ``\\Deleted`` no more — a client undeleted it, or the move never
        took (a stop before the server did it, _recover_move). Its UID, or
        None; UNSURE where the folder was renumbered since it was looked at
        (the UID says nothing). The caller places it (``_locate``)."""
        name, folder = self._folders.get(role), self._folder(role)
        if not name or folder is None:
            return None
        for row in self.index.rows(
                "SELECT uid FROM locations WHERE role = ? AND gen = ? AND message_id = ? "
                "AND skipped = 'moved' ORDER BY uid DESC", (role, folder["gen"], message_id)):
            # (Its flags as the server has them now; one byte of it is read.)
            fetched = self.mailbox.fetch(name, row["uid"], limit=1)
            if fetched is None:
                continue    # the server's answer: expunged since
            if fetched.uidvalidity not in (None, folder["uidvalidity"]):
                return UNSURE   # renumbered since it was looked at: the UID says nothing
            if "\\Deleted" not in fetched.flags:
                return row["uid"]
        return None

    def delete_chat(self, chat: str) -> dict:
        """The user's delete: every mail of the thread, in every mirrored
        folder, moved to the Trash — **then** the mirror of it erased.
        Nothing is expunged.

        In that order, and all or nothing on the second half: nothing is
        erased while any live mail of the thread is neither *positively in
        the Trash* (this call, or an earlier one, moved it there — or it is
        in a mirrored folder that is the Trash) nor *positively absent from
        every mirrored folder* (each of them answered that it does not hold
        it). A chat that vanished here while its mails stayed in the mailbox
        would only come back, and one erased before its mails were safe in
        the Trash could not.

        A mail the index places and the folder no longer holds is neither
        yet. Two things look like that from the folder: a client moved the
        mail since the last poll — or this very call did, through the
        mail's other place. A mailbox may show one message in two mirrored
        folders (a label mailbox: its INBOX and its archive), and moving it
        to the Trash from the one takes it out of the other. So such a mail
        is no error by itself: once every place was tried it is looked for,
        in every mirrored folder, within the same call (as a mail without a
        place is, before anything moves). Found, it is moved to the Trash
        from where it is; in none of them, it is positively absent. Only a
        folder that cannot say leaves it open — the answer then reports
        ``errors``, with the reason in ``error``, and the chat stays. What
        was moved stays moved; the call is made again and finishes.

        ``delete_chat``'s answer (inbound_store) plus ``trashed``,
        ``pending_sends`` and ``recent``."""
        result = {"messages": 0, "media": 0, "parts": 0, "errors": 0, "subjects": [],
                  "message_ids": [], "pending_sends": 0, "recent": 0, "trashed": 0}
        with self.exclusive():
            # The folders as the server has them now — not as of the last
            # poll: an Archive since then may have made one. (And a move a
            # stop cut short is put right before its places are moved.)
            self._folders = self.mailbox.folders()
            self._recover_move()
            # A mail of the thread the index has no place for right now may
            # be in a mirrored folder all the same — moved there by a client
            # since the last poll, or in a folder renumbered and not read
            # again yet. It is looked for first, in every one of them. (A
            # place marked as another mail's, or a copy the gateway moved on,
            # is no place of it: _places moves neither.) So is a mail of the
            # thread whose record the ledger lost (`lost`, its hint naming
            # this chat): it is in the mailbox until it is found to be in no
            # folder, under any of the ids it is known by (the aliases of a
            # lost id are kept) — and found, it goes to the Trash with the
            # rest, from where it is, with no place of its own: there is no
            # row to place it under.
            lost = {row["message_id"]: row["arrived"] for row in self.index.rows(
                "SELECT message_id, arrived FROM lost WHERE chat = ?", (chat,))}
            unplaced = [(row["message_id"], row["ts"], row["arrived"]) for row in self.index.rows(
                "SELECT m.message_id, m.ts, m.arrived FROM mails m WHERE m.chat = ? "
                "AND m.record != '' AND m.removed = 0 AND NOT EXISTS (SELECT 1 FROM "
                "locations l JOIN folders f ON f.role = l.role AND f.gen = l.gen "
                "WHERE l.message_id = m.message_id AND l.skipped IS NULL)", (chat,))]
            found_lost: dict = {}
            for message_id, ts, arrived in unplaced + [(mid, None, when)
                                                     for mid, when in lost.items()]:
                for role in ("inbox", "archive", "sent"):
                    try:
                        uid = self._whereabouts(role, message_id, ts, arrived,
                                                placed=message_id not in lost)
                    except MailboxError:
                        uid = UNSURE
                    if uid is UNSURE:
                        result["errors"] += 1
                    elif uid is not None and message_id in lost:
                        found_lost.setdefault(role, {})[uid] = message_id
                    elif uid is not None:
                        self._locate(role, uid, message_id)
                    if uid is not None:
                        break
            if result["errors"]:
                print(f"[mail-gateway] chat {chat!r} is not deleted: the mailbox cannot say "
                      f"yet where {result['errors']} of its mail(s) are", flush=True)
                result["error"] = (
                    f"the mailbox cannot say yet where {result['errors']} mail(s) of this "
                    f"thread are; nothing was deleted — try again in a minute")
                return result
            places = self._places(chat, ("inbox", "sent", "archive"))
            for role, uids in found_lost.items():
                places.setdefault(role, {}).update(uids)
            trash = None
            if places:
                try:
                    trash = self._destination("trash", MAIL_TRASH_FOLDER, make=False)
                except MailboxError as exc:
                    print(f"[mail-gateway] chat {chat!r} is not deleted: {exc}", flush=True)
                    result["errors"] = sum(map(len, places.values()))
                    result["error"] = str(exc)[:500]
                    return result
            # The mails that were not where the index had them, by id, in
            # the order met.
            missing: dict = {}
            for role, uids in places.items():
                missing.update(dict.fromkeys(self._to_trash(chat, role, uids, trash, result)))
            # Each of those is looked for now, in every mirrored folder —
            # after all the moves, since one of them may be what took it out
            # of its other place. Where it is found it goes to the Trash
            # from there; where every folder says it is not held, it is
            # gone, and no reason to keep the chat.
            unfound = 0
            for message_id in missing:
                row = self.index.one("SELECT ts, arrived FROM mails WHERE message_id = ?",
                                     (message_id,))
                unsure = False
                for role in ("inbox", "archive", "sent"):
                    try:
                        uid = self._whereabouts(
                            role, message_id, row["ts"] if row else None,
                            row["arrived"] if row else lost.get(message_id),
                            placed=message_id not in lost)
                    except MailboxError:
                        uid = UNSURE
                    if uid is UNSURE:
                        unsure = True
                    elif uid is not None:
                        if message_id not in lost:
                            self._locate(role, uid, message_id)
                        # (Gone again while this call looked: still open.)
                        unsure = bool(self._to_trash(chat, role, {uid: message_id}, trash,
                                                     result)) or unsure
                unfound += unsure
            if unfound:
                print(f"[mail-gateway] chat {chat!r} is not deleted: {unfound} of its mail(s) "
                      f"were moved in the mailbox since the mirror last looked, and the "
                      f"mailbox cannot say yet where they are", flush=True)
                result["errors"] += unfound
                result.setdefault("error", (
                    f"{unfound} mail(s) of this thread were moved in the mailbox since the "
                    f"mirror last looked, and the mailbox cannot say yet where they are; "
                    f"the chat is not deleted until they are found — try again in a minute"))
            if result["errors"]:
                return result
            with self._records:
                erased = _ibstore.delete_chat(INBOUND_STORE_DIR, chat, ACCOUNT)
            result.update(erased)
            self._forget_chat(chat, whole=not erased["errors"])
            if not erased["errors"]:
                result["pending_sends"] = SENDS.purge(chat)
            print(f"[mail-gateway] erased chat {chat!r}: {result['trashed']} mail(s) to the "
                  f"Trash, {result['messages']} record(s), {result['media']} blob(s), "
                  f"{result['errors']} error(s)", flush=True)
            return result

    def _to_trash(self, chat: str, role: str, uids: dict, trash: str, result: dict) -> list:
        """One step of a delete: move these mails of a chat — ``{UID:
        Message-ID}`` in the folder of ``role`` — to the Trash, have the
        index follow, and count into ``result`` what went (``trashed``) and
        what the server refused to move (``errors``, with its ``error``).
        Returns the ids of the mails that were not there to be moved: no
        error yet — the caller finds out where they are."""
        folder = self._folder(role)
        if folder["name"] == trash:
            return []       # (a mailbox whose Trash is one of the mirrored folders)
        self._note_move(role, uids, None)
        try:
            moved = self.mailbox.move(folder["name"], list(uids), trash,
                                      validity=folder["uidvalidity"])
        except MailboxError as exc:
            print(f"[mail-gateway] {len(uids)} mail(s) of chat {chat!r} could not be "
                  f"moved from {folder['name']!r} to the Trash: {exc}", flush=True)
            result["errors"] += len(uids)
            result["error"] = str(exc)[:500]
            self._moved_noted()
            return []
        if moved is None:
            moved = Moved({}, {}, tuple(uids))   # the folder is gone
        self._follow_move(role, uids, moved)
        self._moved_noted()
        result["trashed"] += len(moved.moved) + len(moved.left)
        return [uids[uid] for uid in moved.absent]

    def _forget_chat(self, chat: str, *, whole: bool) -> None:
        """Take an erased chat out of the indexes: every mail of it whose
        record is gone from the ledger — all of them when the erasure was
        ``whole``, and then the thread itself and what the rail was still
        owed about it. A place marked ``moved`` stays: that copy is still in
        its folder, and must not be read as a new mail. A place marked
        ``other`` — another mail under the id of an erased placeholder — is
        another mail's no longer: it is noted to be read again, as the mail
        it is (_read_noted), as when a placeholder is buried (_bury).
        Erased ``whole``, the ids of `lost` whose hint names it go too: their
        mails were taken to the Trash, or are in no folder (delete_chat)."""
        gone = [row["message_id"] for row in self.index.rows(
            "SELECT message_id, record FROM mails WHERE chat = ?", (chat,))
            if whole or not row["record"] or not self._record_path(row["record"]).exists()]
        for start in range(0, len(gone), 500):
            batch = tuple(gone[start:start + 500])
            marks = ",".join("?" * len(batch))
            self.index.atomic(
                [(f"DELETE FROM {table} WHERE message_id IN ({marks})", batch)
                 for table in ("mails", "claims", "notes", "settling", "offers")]
                + [(f"DELETE FROM locations WHERE message_id IN ({marks}) "
                    f"AND skipped IS NULL", batch),
                   (f"UPDATE locations SET message_id = NULL, skipped = 'again' "
                    f"WHERE skipped = 'other' AND message_id IN ({marks})", batch)])
        self._placeholders.difference_update(gone)
        if not whole:
            return
        self.index.run("DELETE FROM lost WHERE chat = ?", (chat,))
        self._merges_due.discard(chat)
        self.threads.forget_chat(chat)
        for row in self.index.rows(
                "SELECT seq, payload FROM outbox WHERE kind IN ('inbound', 'removed')"):
            try:
                about = json.loads(row["payload"]).get("chat")
            except (ValueError, AttributeError):
                continue
            if about == chat:
                self.index.run("DELETE FROM outbox WHERE seq = ?", (row["seq"],))

    # -- the gateway's own sends -----------------------------------------------
    # A mail sent through POST /send is on record at once — written here, by
    # the ledger's one writer, from what was sent — and its copy in Sent is
    # the same mail by the same Message-ID: whichever of the two the mirror
    # meets first, there is one record. (How a send gets here, and what a
    # stop on the way leaves: the "Sending" section below.)

    def reply_draft(self, chat: str, mode: str) -> dict:
        """How a reply in this thread is addressed and threaded, in one
        ``mode`` (:meth:`reply_drafts`)."""
        return self.reply_drafts(chat, (mode,))[mode]

    def reply_drafts(self, chat: str, modes=("all", "sender")) -> dict:
        """How a reply in this thread is addressed and threaded — built from
        the mirror's own records of it, never from the caller
        (docs/mail-chats.md, "Writing"): ``{mode: {chat, subject, to, cc,
        in_reply_to, references, cited, names}}`` for each of ``modes``, out
        of one look at the records. :class:`SendRefused` for a thread this
        mailbox does not mirror (400) or one with nothing left to answer
        (409).

        Answered is the latest mail still in the mailbox that is not the
        user's own (else the latest): its id is In-Reply-To, its chain plus
        its id the References. It goes to that mail's sender — its Reply-To,
        where it named one — and, ``mode`` "all", in copy to everyone else on
        its To and Cc but the user. Where the latest mail is the user's own,
        the reply is a follow-up to it: to whom that went, again. The user's
        own is a mail on record as theirs (sent); one that came in From an
        address of theirs is left out of all this — its headers are its
        sender's to write — and where only such mails are left, the newest
        of them is followed up like the user's own: to whom it went but the
        user, else to that address.

        Everything here was a stranger's to write. So what becomes a header
        is only what has the shape of its field — an address that reads as
        one (:func:`_sendable`), an id that can stand between angle brackets
        — and the subject is one clean line; anything else is left out, not
        escaped, and an address left out is named in ``skipped``: whoever
        sends, or looks at the recipients first, is told who gets no mail."""
        rows = self.index.rows(
            "SELECT * FROM mails WHERE chat = ? AND record != '' "
            "ORDER BY ts DESC, message_id DESC", (chat,))
        if not rows:
            if any(entry.get("status") in ("pending", "sending")
                   for entry in SENDS.listed(chat)):
                # A new mail that waits for approval (or is on its way) is a
                # chat already, and holds nothing a reply could answer yet.
                raise SendRefused(409, "this thread holds only a mail that has not been "
                                       "sent yet: there is nothing to reply to until it is")
            raise SendRefused(400, "unknown chat: this mailbox mirrors no thread under that key")
        live = [row for row in rows if not row["removed"]]
        if not live:
            raise SendRefused(409, "this thread has no mail left to reply to: every mail "
                                   "of it is gone from the mailbox")

        def own(row) -> bool:
            return row["direction"] == "out"

        def from_self(row) -> bool:
            # A mail that came in From an address of the user's: no mail of
            # theirs to follow up — its To and Cc are its sender's to write,
            # and whoever was sent the user's mail can send one — and nobody
            # else's to answer.
            return not own(row) and (row["sender"] or "") in OWN_ADDRESSES

        def listed(row, column) -> list:
            try:
                return [str(a) for a, _n in json.loads(row[column] or "[]")]
            except (ValueError, TypeError):
                return []

        skipped: dict = {"to": [], "cc": []}

        def sendable(addresses, field="to") -> list:
            skipped[field] += [a for a in addresses if a and not _sendable(a)]
            return list(dict.fromkeys(a for a in addresses if _sendable(a)))

        # (Such mails have no say in how the reply is addressed, unless they
        # are all the thread has left: then the newest of them is followed
        # up — to whom it went, as any mail's reply-all goes to its To and
        # Cc, and the preview shows them; to that address where it went to
        # nobody else. Beside a real mail, a forgery From the user steers
        # nothing; the cost: a mail the user wrote from an address of theirs
        # adds nobody to a reply in a thread with real mails.)
        basis = [row for row in live if not from_self(row)]
        latest = (basis or live)[0]
        answered = next((row for row in basis if not own(row)), latest)
        fields = _ibstore.read_record(self._record_path(answered["record"])) or {}
        if own(latest) or not basis:
            to = sendable(listed(latest, "recipients"))
            cc = sendable(listed(latest, "cc"), "cc")
            others = [a for a in to if a not in OWN_ADDRESSES]
            copied = [a for a in cc if a not in OWN_ADDRESSES]
            if basis:
                # (A mail to oneself alone is answered to oneself.)
                to = others or to or copied
            else:
                # (One that came in to nobody else: to the address it is From.)
                to = others or copied or sendable([latest["sender"] or ""])
            cc = [a for a in cc if a not in OWN_ADDRESSES and a not in to]
        else:
            # The record keeps Reply-To from this phase on; an older one has
            # it in the header block it kept.
            reply_to = list(fields.get("reply_to") or [])
            head = None if reply_to else self._recorded_head(answered["message_id"])
            if head is not None and _REPLY_TO_LINE_RE.search(head):
                # (Read as guarded as any header block — in the reader
                # process, one request at a time — and not under the mirror's
                # lock, which a send's answer never waits for. A reading that
                # does not come back is no answer: the reply is not
                # addressed on a guess. Only where the block has a Reply-To
                # line at all: the reader may be in the middle of the
                # mirror's reading of a heavy mail, and a reply to a mail
                # that named none — the ordinary one — waits for nothing.)
                try:
                    header = read_send_facts(head)
                except (NotRead, MirrorFault, ReaderDown) as exc:
                    raise Busy(f"the mail to answer could not be read just now ({exc}); "
                               f"try again in a moment") from exc
                reply_to = list((header or {}).get("reply_to") or [])
            to = sendable(reply_to) or sendable([answered["sender"] or ""])
            cc = [a for a in sendable(listed(answered, "recipients")
                                      + listed(answered, "cc"), "cc")
                  if a not in OWN_ADDRESSES and a not in to]
        if not to:
            raise SendRefused(409, "the mail to answer names nobody a reply could go to")
        # Every id the answered mail cited, then its own: what the thread
        # index goes by. The headers carry those of them that are ids a
        # server ever saw (not the mirror's stand-ins for a missing or
        # overlong one), the root and the nearest parents.
        chain = (fields.get("references") or "").split()
        parent = fields.get("in_reply_to")
        # (A mail of the gateway's own that its provider filed under another
        # id is cited under that one too, and answered under it: it is the
        # id the recipients have.)
        known_as = self._known_as(answered["message_id"])
        cited = list(dict.fromkeys([*chain, *([parent] if parent else []), *known_as]))
        cited = cited[:1] + cited[1:][-(_threads.MAX_CITED_IDS - 1):]
        citable = [i for i in cited if _citable(i)]
        thread = self.thread(chat) or {}
        subject = ("Re: " + _clean_line(self.threads.subject_of(chat),
                                        MAX_SUBJECT_CHARS)).strip()
        names = {p["address"]: p["name"] for p in thread.get("participants") or []
                 if p.get("name")}

        def draft(mode: str) -> dict:
            copied, left_out = (cc, skipped["cc"]) if mode != "sender" else ([], [])
            return {
                "chat": chat,
                "subject": subject,
                "to": to[:MAX_SEND_RECIPIENTS],
                "cc": copied[:max(0, MAX_SEND_RECIPIENTS - len(to))],
                "in_reply_to": next((i for i in reversed(known_as) if _citable(i)), None),
                "references": citable[:1] + citable[1:][-(MAX_REFERENCES - 1):],
                "cited": list(cited),
                "names": dict(names),
                # On the mail answered and not written to: no address a mail
                # can be sent to (one clean line each — a stranger's text).
                "skipped": list(dict.fromkeys(
                    _clean_line(a, 254) for a in skipped["to"] + left_out
                    if a not in OWN_ADDRESSES))[:MAX_SEND_RECIPIENTS],
                "answers": answered["message_id"],
            }
        return {mode: draft(mode) for mode in modes}

    def expect_sent(self, message_id: str, author: str) -> None:
        """Before the mail leaves: who wrote it, so that its Sent copy — if
        the mirror reads that before the record is written, or instead of it
        (a stop right after the server took the mail) — is credited rightly."""
        self.index.run(
            "INSERT INTO notes (message_id, author, created) VALUES (?, ?, ?) "
            "ON CONFLICT (message_id) DO UPDATE SET author = excluded.author, "
            "created = excluded.created", (message_id, author, self._clock()))

    def record_sent(self, entry: dict) -> str:
        """Write the record of a mail the gateway sent, and queue the rail's
        word of it; returns the chat it is in. Under the mirror's lock.

        A mail already on record — its Sent copy was mirrored first, or an
        earlier try got this far — is left as it is, and the parts stored
        for this record go. Otherwise as :meth:`_write` does it, and for the
        same reason: the claim, the record, then the claim confirmed with
        the event in one transaction."""
        message_id, refs = entry["message_id"], entry.get("refs") or {}
        known = self.index.one(
            "SELECT chat, record FROM mails WHERE message_id = ? AND record != ''",
            (message_id,))
        if known is not None:
            # Which record it is decides what goes: the one an earlier try of
            # this very send wrote refers to these parts, and keeps them; the
            # one read off the Sent copy has parts of its own.
            fields = _ibstore.read_record(self._record_path(known["record"]))
            if fields is not None:
                kept = {fields.get("body_part"), fields.get("header_part"),
                        *(fields.get("attachments") or [])}
                _discard_parts({"attachments": [
                    ref for ref in [refs.get("body_part"), refs.get("header_part"),
                                    *(refs.get("attachments") or [])]
                    if ref and ref not in kept]})
            return known["chat"]
        ts = float(entry["sent_at"])
        subject = entry.get("subject") or ""
        cited = list(entry.get("cited") or [])
        resolution = self.threads.resolve(message_id, cited, ts, subject)
        chat = resolution.chat
        self._merges_due.update(resolution.merged)
        name = _clean_line(MAIL_CONFIG.from_name, 200) or None
        text = entry.get("message") or ""
        attachments = list(refs.get("attachments") or [])
        # The record's recipients are those the server took the mail for: an
        # address it refused got no mail, is named as that (kb:refused), and
        # is not written to again by a follow-up "to whom that went". (The
        # mail's own headers — the header block, the Sent copy — name
        # everyone it was addressed to, as the recipients read them.)
        refused = [a for a in entry.get("refused") or [] if a in entry["to"] + entry["cc"]]
        to = [a for a in entry["to"] if a not in refused]
        cc = [a for a in entry["cc"] if a not in refused]
        self.index.atomic([
            ("INSERT OR REPLACE INTO mails (message_id, record, direction, chat, ts, sender, "
             "sender_name, recipients, cc, removed, arrived) "
             "VALUES (?, '', 'out', ?, ?, ?, ?, ?, ?, 0, ?)",
             (message_id, chat, ts, ACCOUNT, name,
              json.dumps([[a, None] for a in to]),
              json.dumps([[a, None] for a in cc]), ts)),
            ("INSERT OR REPLACE INTO claims (message_id, backfill, seen) VALUES (?, 0, 1)",
             (message_id,)),
            # Not called gone while its Sent copy is still to appear — and
            # where the provider files that copy, or does not, never.
            ("INSERT OR REPLACE INTO settling (message_id, until) SELECT ?, ? WHERE NOT EXISTS "
             "(SELECT 1 FROM locations WHERE message_id = ?)",
             (message_id, ts + MAIL_SENT_GRACE_SECONDS if MAIL_CONFIG.save_sent else None,
              message_id))])
        try:
            with self._records:
                _urn, path = _ibstore.write_outbound(
                    INBOUND_STORE_DIR, channel=CHANNEL, chat=chat, account=ACCOUNT, text=text,
                    author=entry["author"], message_id=message_id, timestamp=ts,
                    attachment_urls=attachments, sender=ACCOUNT, sender_name=name,
                    subject=subject or None, recipients=to, cc=cc, refused=refused,
                    in_reply_to=cited[-1] if cited else None, references=cited,
                    body_part=refs.get("body_part"), header_part=refs.get("header_part"),
                    trim="deterministic", arrived_at=ts)
        except Exception:
            try:
                self.index.atomic([
                    ("DELETE FROM mails WHERE message_id = ? AND record = ''", (message_id,)),
                    ("DELETE FROM claims WHERE message_id = ?", (message_id,))])
            except sqlite3.Error:
                # A claim outlives no pass (see the states above Mirror):
                # _prepare drops it, before anything is ingested.
                self._reconcile = True
            raise
        try:
            if resolution.merged:
                self._apply_merges()
            event = self._inbound_event(
                direction="out", chat=chat, message_id=message_id, ts=ts, sender=ACCOUNT,
                sender_name=name, text=text, attachments=attachments, author=entry["author"],
                subject=subject or None, backfill=False, seen=False)
            if refused:
                event["refused"] = refused
            if entry.get("request_key"):
                # The press this mail came of, for whoever shows it as still
                # being sent and never got the answer that named its id.
                event["request_key"] = entry["request_key"]
            self.index.atomic([
                ("UPDATE mails SET record = ? WHERE message_id = ?", (path.name, message_id)),
                ("DELETE FROM claims WHERE message_id = ?", (message_id,)),
                self._event("inbound", event)])
        except Exception:
            # As in _write: the record stands and the index does not know it.
            self._reconcile = True
            raise
        return chat

    def sent_copy(self, message_id: str):
        """Whether Sent holds this mail: its UID, None when the folder says
        it does not, UNSURE when that cannot be told now."""
        name = self._folders.get("sent")
        if not name:
            return UNSURE
        # (Every mail under the ids of a mail the gateway minted is that mail:
        # nobody else files mail in Sent under them. The newest is its copy.)
        found = self._find_by_id(name, self._known_as(message_id))
        return found.uids[0] if found.uids else None if found.whole else UNSURE

    def place_sent(self, uid: int, message_id: str, validity: int | None = None) -> None:
        """Give a mail its place in Sent — where the index's view of the
        folder is the one the UID was read under. (Where it is not, the
        folder's own look places the mail: it is on record.)"""
        folder, name = self._folder("sent"), self._folders.get("sent")
        if folder is not None and (not name or folder["name"] == name) \
                and validity in (None, folder["uidvalidity"]):
            self._locate("sent", uid, message_id)

    def file_sent(self, message_id: str, raw: bytes, when: float, *, again: bool) -> None:
        """Append the gateway's own mail to Sent and place it. ``again``:
        an earlier try may have got through without its answer, so the mail
        is looked for first — one copy, however often this is run. Raises
        MailboxError while it cannot be done; the caller tries again."""
        name = self._folders.get("sent") or self.mailbox.folders().get("sent")
        if not name:
            raise MailboxError("the mailbox has no Sent folder to keep the copy in"
                               + (f" — set SENT_FOLDER (configured: "
                                  f"{MAIL_CONFIG.sent_folder!r})" if MAIL_CONFIG else ""))
        validity = uid = None
        if again:
            found = self.mailbox.find(name, message_id)
            if not found.uids and not found.whole:
                raise MailboxError(f"{name!r} cannot say yet whether it holds the copy")
            uid = found.uids[0] if found.uids else None
        if uid is None:
            validity, uid = self.mailbox.append(name, raw, when=when)
        if uid is None:
            uid = next(iter(self.mailbox.find(name, message_id).uids), None)
        if isinstance(uid, int):
            self.place_sent(uid, message_id, validity)

    def keep_settling(self, message_id: str) -> None:
        """A send whose Sent copy is still owed is not called gone meanwhile."""
        self.index.run("UPDATE settling SET until = ? WHERE message_id = ? "
                       "AND until IS NOT NULL",
                       (self._clock() + MAIL_SENT_GRACE_SECONDS, message_id))

    def _resume_sends(self, *, mailbox: bool) -> None:
        """Finish what sends left undone (see ``_finish_send``): a record the
        mirror's lock was not free for, a Sent copy the mailbox would not
        take, a send a stop interrupted. Never raises — one send's trouble
        is logged and tried again, and does not cost the mirror its pass."""
        if self._reconcile:
            return     # the index is matched to the ledger first (poll)
        for request_id in SENDS.due():
            try:
                _resume_send(request_id, self, mailbox=mailbox)
            except Exception as exc:  # noqa: BLE001
                _send_trouble(request_id, f"{type(exc).__name__}: {exc}")


class Busy(Exception):
    """The mirror's lock was not free in time (Mirror.exclusive)."""


class TrimRefused(Exception):
    """A model-trim call that names no mail to trim: the HTTP answer that
    says so (Mirror._trim_target)."""

    def __init__(self, status: int, body: dict):
        super().__init__(body.get("error"))
        self.status, self.body = status, body


# ── Sending ───────────────────────────────────────────────────────────────────
# POST /send takes a reply or a new mail, lets the send policy decide whether
# it goes now or waits for the user's approval, and either way makes it an
# entry of the pending-send store: one JSON file per send on the state volume,
# as on the SMS gateway (whose store, statuses and endpoints these are), with
# mail's own fields. A queued entry is what /sends lists and the chat shows as
# pending. The same entry is the journal its sending is resumed from:
#
#   pending ──approve──▶ sending ──▶ approved        rejected / retracted / error
#     (a direct send is born sending)
#
# GET /pending-sends lists what is not a message (yet): ``pending``,
# ``sending`` and ``error`` — each entry says which by its ``status``, so
# whoever offers an approval offers it for ``pending`` alone. An ``error``
# stays listed, with its reason and whether the mail may have gone out all
# the same (``outcome_unknown``), until POST …/dismiss; nothing in any
# status but ``pending`` can be approved, so a failed mail is never sent
# by a later press on the approval page.
#
#   sending, no phase   The decision stands; nothing was handed to the SMTP
#                       server. After a stop an approved entry is sent — the
#                       approval was given. A direct one is not: its caller
#                       was given no answer, and may have told its user "not
#                       confirmed yet". One that named its request ends as an
#                       error that says so for certain — "stopped before the
#                       mail was handed over; it was not sent" — and is
#                       listed, where the chat finds it; one that named none
#                       has nobody to ask after it, and is dropped.
#   sending, "data"     Written the moment before the message itself is handed
#                       to the server (email_client._smtp_send, on_data). If
#                       nothing follows it, nobody saw the server's answer,
#                       and the mail is never sent again. It is looked for in
#                       Sent: found, it went out. Not found — at once where the
#                       gateway files the Sent copy itself, after
#                       MAIL_SENT_GRACE_SECONDS where the provider does — the
#                       send ends as an error that says its outcome is not
#                       known.
#   approved            The server took the mail (``sent_at``) — written the
#                       moment it says so, before the session is closed
#                       (on_taken). What remains
#                       is done once each, and is found again by the mail's
#                       Message-ID if a stop comes in between: the record with
#                       the rail's event (``recorded``; Mirror.record_sent
#                       writes none for a mail already on record) and the Sent
#                       copy (``filed``; looked for before it is appended a
#                       second time). ``done`` when both are. The record
#                       is written by the send itself where the mirror is
#                       free for it; the copy is filed by the mirror loop —
#                       at once while the mailbox answers, else at its next
#                       poll — and never by the request, which has answered
#                       by then.
#   error               It did not go (``error`` says why; ``outcome_unknown``
#                       false: nothing left the gateway), or nobody knows
#                       (``outcome_unknown`` true). Kept and listed: a queued
#                       send's always; a direct send's when nobody knows, or
#                       when its caller had gone before the answer was ready
#                       — one whose caller has the failure as its answer is
#                       over, and is cleared.
#
# So a send is made at most once, and a mail the server took has its record:
# the only stop that costs anything is one between "data" and the server's
# answer, where no mark can be written in time and only the mailbox can say.
# And one press is one mail: a request that names itself (``request_key``)
# is carried out once, and every repeat of it within REQUEST_KEY_SECONDS is
# answered from its entry (``_repeat_send``) — and says that it is one
# (``"repeat": true``): what the answer tells of happened before, and whoever
# acts on a mail going out (a draft cleared, a chat marked read) did so then.
#
# **A send that failed for certain ends its press; only an unknown outcome
# keeps its key.** What a repeat under a known key does, by the entry it
# finds — after a stop as well, since each line is what the disk says:
#
#   pending                         answered 202 from the entry; sends nothing
#   sending, no phase, approved     answered 202 "sending"; the approval is
#                                   carried out once (at a start: by the start)
#   sending, no phase, direct       in flight: waited for, then answered from
#                                   the entry — or, where that is gone by then,
#                                   from how the flight ended. After a stop it
#                                   is no longer this — it became the certain
#                                   error below
#   sending, "data"                 502 ``unconfirmed``; sends nothing — the
#                                   mail was handed over, only Sent can say
#   approved (done or not)          200 "sent", the same Message-ID
#   error, outcome_unknown true     502 ``unconfirmed`` + ``outcome_unknown``;
#                                   sends nothing, dismissed or not, for as
#                                   long as the key is remembered
#   error, outcome_unknown false    **a new attempt**: nothing left the gateway
#                                   under this key, so the entry is removed
#                                   and the mail composed and sent afresh
#                                   (queued afresh, where the policy queues)
#   rejected / retracted            409; sends nothing
#
# "For certain" is said only where the gateway knows it: the SMTP server
# refused, or the send ended before the mark "data" — which is on disk before
# any of the message is on the wire. Everything after that mark and before
# the server's answer is "unknown", and is never sent again.
#
# The answers say which it is in fields of their own, never by their wording
# or their status alone: ``"sent": false`` — nothing left the gateway under
# this request (the certain failure's 502, and the 503s of a gateway that
# cannot send or could not write the send down); ``"unconfirmed": true`` —
# the mail may have gone out, or still may. /send has no 5xx with neither: a
# fault nobody planned for is answered as one of the two (_send_unplanned).

def _now() -> float:
    """The sending side's clock (one place, so a test can set it)."""
    return time.time()


def _atomic_json(path: Path, data) -> None:
    """Write a store file whole or not at all — and on the disk before this
    returns: the marks of a send are what a restart goes by."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class SendRefused(Exception):
    """A send that will not be made as asked: the HTTP status, and one
    sentence for whoever asked."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


_REQUEST_ID_RE = re.compile(r"[0-9a-f]{32}")
_PENDING_SEND_RE = re.compile(
    r"^/pending-sends/([0-9a-f]{32})(?:/(approve|reject|retract|dismiss))?$")
# What an entry holds while its mail is still to be recorded: the blobs of
# the mail's own parts, the ids the thread index goes by. And what it holds
# for the gateway alone, left out of every answer: those, and the digest of
# the request it was made from (see REQUEST_KEY_SECONDS).
_ENTRY_WORKING = ("refs", "cited")
_ENTRY_PRIVATE = (*_ENTRY_WORKING, "press")
# A decided entry is kept for the approval page and the thread that asked —
# this long, then cleared away (Sends.sweep).
PENDING_KEEP_SECONDS = 30 * 86400.0
# One press of Send is one mail. A caller may name its request
# (``request_key``, a token of its own making, one per press): a /send whose
# key the gateway has seen within this long is answered with what became of
# the first — the same Message-ID — and sends nothing. So a caller that lost
# the answer (a timeout, a dropped connection) can ask again without a second
# mail going out. The entry of a keyed send is what remembers it, and is
# kept that long even where an unkeyed one would be cleared at once.
REQUEST_KEY_SECONDS = 86400.0
_REQUEST_KEY_RE = re.compile(r"[\x21-\x7e]{1,64}")
# How long a repeat waits for the first request, when that is still handing
# its mail to the SMTP server, before it answers "not confirmed yet".
REPEAT_WAIT_SECONDS = 60.0
UNKNOWN_OUTCOME = (
    "the mail server was handed this mail and its answer was never seen: it is not "
    "known whether the mail went out. It was not sent again, and no copy of it is "
    "in the Sent folder — check with the recipient before sending it anew")
# How a direct send ends that a stop caught before anything was handed over.
STOPPED_UNSENT = ("the mail gateway was stopped before this mail was handed to the mail "
                  "server; it was not sent")
_send_said: dict = {}


def _failed_for_certain(entry: dict) -> bool:
    """Whether a send ended with nothing having left the gateway: an error
    whose entry *says* the outcome is known. An entry that does not say
    (``outcome_unknown`` true, or absent) is never taken for unsent — this is
    what decides whether a repeat of its request may send."""
    return entry.get("status") == "error" and entry.get("outcome_unknown") is False


def _send_trouble(request_id: str, said: str | None) -> None:
    """Log why a send is not finished — once per reason, not once per poll."""
    if said is None:
        _send_said.pop(request_id, None)
    elif _send_said.get(request_id) != said:
        _send_said[request_id] = said
        print(f"[mail-gateway] send {request_id} is not finished yet ({said}); "
              f"will retry", flush=True)


class Sends:
    """The pending-send store: one ``<id>.json`` per send, under one lock.

    Nothing is held in memory but which entries the mirror loop still has
    work on (``due``): /pending-sends and the approval page read the files,
    so an entry that is not on disk could never be approved, and a failed
    write is the caller's to answer with a retryable error."""

    def __init__(self, directory):
        self._dir = Path(directory)
        self._lock = threading.RLock()
        self._due: set = set()
        self._swept = 0.0
        self._ids = None    # Message-ID → request id, once asked for (by_message_id)

    def _path(self, request_id) -> Path | None:
        """The file of a request id, found by enumerating the directory —
        never built from the caller's string, so it cannot leave the store."""
        if not _REQUEST_ID_RE.fullmatch(str(request_id or "")):
            return None
        try:
            for path in self._dir.iterdir():
                if path.suffix == ".json" and path.stem == request_id and path.is_file():
                    return path
        except OSError:
            pass
        return None

    @staticmethod
    def _read(path) -> dict | None:
        try:
            entry = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return entry if isinstance(entry, dict) else None

    def get(self, request_id) -> dict | None:
        path = self._path(request_id)
        return self._read(path) if path else None

    def entries(self) -> list:
        """Every entry, oldest first. (Only ``<32 hex>.json`` files holding
        an object: a stray file in the directory is no send.)"""
        try:
            paths = [p for p in self._dir.glob("*.json") if _REQUEST_ID_RE.fullmatch(p.stem)]
        except OSError:
            return []
        found = [entry for entry in map(self._read, paths) if entry is not None]

        def created(entry):
            try:
                return float(entry.get("created") or 0.0), str(entry.get("id"))
            except (TypeError, ValueError):
                return 0.0, str(entry.get("id"))
        return sorted(found, key=created)

    def waiting(self, chat: str | None = None) -> list:
        """The sends that wait for the user's approval — of one chat."""
        return [entry for entry in self.entries() if entry.get("status") == "pending"
                and (chat is None or entry.get("chat") == chat)]

    def listed(self, chat: str | None = None) -> list:
        """What a chat shows of its sends that are not messages (yet): those
        waiting for approval, those being sent, and those that failed —
        until someone dismissed them. A mail the user queued or sent never
        just leaves the list: it becomes a message, or it stays here with
        the reason it did not."""
        return [entry for entry in self.entries()
                if (entry.get("status") in ("pending", "sending")
                    or (entry.get("status") == "error" and not entry.get("dismissed")))
                and (chat is None or entry.get("chat") == chat)]

    def by_key(self, key: str) -> dict | None:
        """The send a ``request_key`` stands for, while the key is
        remembered (REQUEST_KEY_SECONDS). One that is not over for certain
        before one that is: the latter only waits to be replaced
        (:meth:`create`)."""
        failed = None
        for entry in reversed(self.entries()):
            try:
                fresh = _now() - float(entry.get("created") or 0.0) <= REQUEST_KEY_SECONDS
            except (TypeError, ValueError):
                fresh = False
            if fresh and entry.get("request_key") == key:
                if not _failed_for_certain(entry):
                    return entry
                failed = failed or entry
        return failed

    def by_message_id(self, message_id: str) -> dict | None:
        """The send whose mail has this Message-ID. The mirror asks for
        every mail it reads off Sent, so the ids are kept in memory — read
        off the store once, and again after anything was removed from it."""
        with self._lock:
            if self._ids is None:
                self._ids = {entry.get("message_id"): entry.get("id")
                             for entry in self.entries() if entry.get("message_id")}
            request_id = self._ids.get(message_id)
        return self.get(request_id) if request_id else None

    def _files(self, request_id) -> Path:
        """Where a send's attachments are kept (for an id the store minted
        or found — never a caller's string)."""
        return self._dir / f"{request_id}.files"

    def create(self, entry: dict, images: list | None = None) -> dict | None:
        """Add a send. Its attachments (``images``, the /send shape) go into
        a file of their own beside the entry, written first so that no entry
        stands without them: an entry is read at every listing, the bytes
        only when the mail is built.

        An entry that names its request (``request_key``) is added only if
        no send of that key is remembered: otherwise nothing is written and
        that earlier entry is returned — looked for and added under one
        lock, so two requests of one press cannot both be new.

        But for an earlier send that failed for certain: nothing left the
        gateway under that key, the press it named is over, and this is a
        new attempt. The new entry is written, then the failed one removed —
        in that order, so that a disk that takes no write leaves the failed
        send where the chat shows it."""
        with self._lock:
            earlier = self.by_key(entry["request_key"]) if entry.get("request_key") else None
            if earlier is not None and not _failed_for_certain(earlier):
                return earlier
            self._dir.mkdir(parents=True, exist_ok=True)
            if images:
                _atomic_json(self._files(entry["id"]), images)
            _atomic_json(self._dir / f"{entry['id']}.json", entry)
            if self._ids is not None and entry.get("message_id"):
                self._ids[entry["message_id"]] = entry["id"]
            if earlier is not None:
                print(f"[mail-gateway] send {earlier['id']} failed with nothing sent; its "
                      f"request_key now names a new attempt ({entry['id']})", flush=True)
                try:
                    self.remove(earlier["id"])
                except OSError as exc:
                    # (It stays, as failed; by_key answers with the new one.)
                    print(f"[mail-gateway] could not clear the failed send "
                          f"{earlier['id']}: {exc}", flush=True)
            return None

    def images(self, request_id) -> list:
        """A send's attachments as they came in; none once it is over."""
        if not _REQUEST_ID_RE.fullmatch(str(request_id or "")):
            return []
        try:
            images = json.loads(self._files(request_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return images if isinstance(images, list) else []

    def release(self, request_id) -> None:
        """Drop a send's attachments: nothing will build its mail again.
        (Their names stay on the entry, or — once sent — their blobs' ids.)"""
        if _REQUEST_ID_RE.fullmatch(str(request_id or "")):
            with contextlib.suppress(OSError):
                self._files(request_id).unlink(missing_ok=True)

    def update(self, request_id, drop=(), **changes) -> dict | None:
        """Change an entry on disk; returns it, or None when it is gone."""
        with self._lock:
            path = self._path(request_id)
            entry = self._read(path) if path else None
            if entry is None:
                return None
            entry.update(changes)
            for key in drop:
                entry.pop(key, None)
            _atomic_json(path, entry)
            return entry

    def remove(self, request_id) -> None:
        with self._lock:
            path = self._path(request_id)
            if path is not None:
                path.unlink(missing_ok=True)
            self.release(request_id)
            self._due.discard(request_id)
            self._ids = None

    def decide(self, request_id, verb: str) -> tuple:
        """Approve, reject (the user) or retract (whoever queued it) a
        pending send: ``(entry | None, whether this call decided it)``. An
        entry that is no longer pending is answered as it is.

        The decision is on disk before anything acts on it: a file still
        saying "pending" could be approved a second time after the first
        mail went out. A failed write raises (a retryable error to the
        caller) and nothing is sent."""
        with self._lock:
            path = self._path(request_id)
            entry = self._read(path) if path else None
            if entry is None:
                return None, False
            if entry.get("status") != "pending":
                return entry, False
            entry["status"] = {"approve": "sending", "reject": "rejected",
                               "retract": "retracted"}[verb]
            _atomic_json(path, entry)
            if verb != "approve":
                self.release(request_id)
            return entry, True

    def dismiss(self, request_id) -> tuple:
        """Clear a failed send out of its chat: ``(entry | None, whether this
        call did)``. Only one that ended as an error — a send that waits is
        rejected or retracted, and one on its way has no outcome to dismiss.
        Nothing is ever sent by this.

        A send whose outcome is not known, and that named its request, stays
        — marked ``dismissed``, no longer listed — for as long as its key is
        remembered: a repeat of that request must still be told that the
        mail may have gone out, and send nothing. A dismissed entry is over
        for every reader; it is kept for that answer alone. Every other
        failed send is removed: one that failed for certain has nothing to
        remember (a repeat under its key is a new attempt, see
        :meth:`create`)."""
        with self._lock:
            path = self._path(request_id)
            entry = self._read(path) if path else None
            if entry is None:
                return None, False
            if entry.get("status") != "error" or entry.get("dismissed"):
                return entry, False
            if entry.get("request_key") and not _failed_for_certain(entry):
                entry["dismissed"] = True
                _atomic_json(path, entry)
            else:
                self.remove(request_id)
            return {**entry, "dismissed": True}, True

    def owe(self, request_id) -> None:
        """The mirror loop has something to finish for this send."""
        with self._lock:
            self._due.add(request_id)

    def settled(self, request_id) -> None:
        with self._lock:
            self._due.discard(request_id)
        _send_trouble(request_id, None)

    def due(self) -> list:
        with self._lock:
            return sorted(self._due)

    def recover(self) -> list:
        """At start, what a stop left behind (see the table above). Returns
        the ids of approved sends that were never begun — the caller's to
        send now; entries with a mark are left to the mirror loop, and old
        decided entries are cleared.

        A direct send without a mark was stopped before anything was handed
        over (the mark is on disk first), and its caller was given no
        answer. One that named its request ends as a failed send — for
        certain, and listed: whoever was told "not confirmed yet" finds it
        in the chat with its reason, and a repeat under its key sends the
        mail, for the first time. One that named none is dropped: nobody can
        ask after it."""
        begin = []
        for entry in self.entries():
            request_id, status = entry.get("id"), entry.get("status")
            if status == "sending" and not entry.get("phase"):
                if not entry.get("direct"):
                    begin.append(request_id)
                elif entry.get("request_key"):
                    print(f"[mail-gateway] send {request_id} was stopped before anything "
                          f"left; it ends as not sent, and is listed as failed", flush=True)
                    try:
                        self.update(request_id, status="error", error=STOPPED_UNSENT,
                                    outcome_unknown=False)
                        self.release(request_id)
                    except OSError as exc:
                        # (It stays "sending"; the next start ends it.)
                        print(f"[mail-gateway] could not note the end of send "
                              f"{request_id}: {exc}", flush=True)
                else:
                    print(f"[mail-gateway] send {request_id} was stopped before anything "
                          f"left; dropped (its caller was given no answer, and no key "
                          f"to ask by)", flush=True)
                    self.remove(request_id)
            elif status == "sending" or (status == "approved" and not entry.get("done")):
                self.owe(request_id)
        self.sweep(force=True)
        # Attachments whose entry never came to stand (a stop between the two
        # files of one create) belong to no send.
        with contextlib.suppress(OSError):
            for path in self._dir.glob("*.files"):
                if not path.with_suffix(".json").exists():
                    path.unlink(missing_ok=True)
        return begin

    def sweep(self, force: bool = False) -> None:
        """Clear away the entries nobody will ask for any more — at a start,
        and about hourly from the mirror loop. A decided entry (sent,
        rejected, retracted, failed) after PENDING_KEEP_SECONDS; one that is
        kept only because its request named a key — a direct send that is
        over, one of unknown outcome that was dismissed — once the key is
        forgotten.
        Never one that waits, is being sent, or still has work owed."""
        now = _now()
        if not force and now - self._swept < 3600.0:
            return
        self._swept = now
        for entry in self.entries():
            request_id, status = entry.get("id"), entry.get("status")
            if status in ("pending", "sending") or (status == "approved"
                                                    and not entry.get("done")):
                continue
            try:
                age = now - float(entry.get("created") or 0.0)
            except (TypeError, ValueError):
                continue
            over = entry.get("dismissed") or (entry.get("direct") and status == "approved")
            if age > (REQUEST_KEY_SECONDS if over else PENDING_KEEP_SECONDS):
                try:
                    self.remove(request_id)
                except OSError:
                    pass    # the next sweep

    def purge(self, chat: str) -> int:
        """Delete a chat's entries (its erasure); how many. Not one that is
        being sent right now: its outcome is still to be written."""
        with self._lock:
            removed = _ibstore.purge_pending_sends(
                self._dir, lambda entry: entry.get("chat") == chat)
            for request_id in removed:
                self.release(request_id)
            self._due.difference_update(removed)
            self._ids = None
            return len(removed)

    def rekey(self, old: str, into: str) -> None:
        """A merge: what waited in the absorbed chat waits in the survivor."""
        for entry in self.entries():
            if entry.get("chat") == old:
                try:
                    self.update(entry.get("id"), chat=into)
                except OSError as exc:
                    print(f"[mail-gateway] could not re-key pending send "
                          f"{entry.get('id')}: {exc}", flush=True)


SENDS = Sends(MAIL_PENDING_SENDS_DIR)
_reply_tokens = None


def reply_tokens() -> ReplyTokenStore:
    """The store that signs and reads reply tokens (scripts/reply_tokens.py):
    a token names a thread of this mailbox without the agent typing its key.
    Made on first use — its key file belongs on the state volume of a
    gateway that runs, not of one that idles unconfigured."""
    global _reply_tokens
    if _reply_tokens is None:
        _reply_tokens = ReplyTokenStore(MAIL_STATE_DIR / "reply-tokens")
    return _reply_tokens


def _public(entry: dict) -> dict:
    """An entry as the API answers it."""
    return {key: value for key, value in entry.items() if key not in _ENTRY_PRIVATE}


def _approval_slug(host_header) -> str:
    """The /sends/<slug>/… segment for approval links this gateway emits."""
    if SEND_APPROVAL_SLUG:
        return SEND_APPROVAL_SLUG
    host = (host_header or "").split(":", 1)[0].strip().strip("/")
    return host or "mail-gateway"


def _send_category() -> str:
    """The send-control category of THIS mailbox's address, by
    EMAIL_SEND_POLICY as email_client reads it. The recipient is never
    consulted, and a policy that cannot be read is the strictest one."""
    try:
        return _ec.resolve_category(MAIL_CONFIG.user)
    except Exception as exc:  # noqa: BLE001 - EmailError on a policy that does not parse
        print(f"[mail-gateway] EMAIL_SEND_POLICY cannot be read ({exc}); every send "
              f"queues for approval", flush=True)
        return DEFAULT_SEND_CATEGORY


def _send_is_direct(category: str, user_approved: bool) -> bool:
    """Only the policy decides; no caller-supplied field goes around it (see
    telegram-gateway.py for why ``author`` is not one)."""
    return category == "allow" or (category == "trust" and user_approved)


# -- what a caller may ask to be sent ------------------------------------------
# The caller is an agent, or the web gateway passing on what a person typed;
# either way a request's fields end up in header lines and in the envelope,
# so each is reduced to the shape its field has before it is used: an address
# is one address, a subject one line. What does not have the shape is refused
# — nothing is repaired into something its sender did not write.

# What a mail may be addressed to, here: one address in the plainest form
# there is, because that is the only form every reader of a header — this
# program's writer, the server, the recipients' clients — reads the same way.
#
#   address = local "@" domain          (at most 254 characters)
#   local   = atom *("." atom)          (at most 64; an atom is one or more of
#                                        A-Z a-z 0-9 ! # $ % & ' * + / = ? ^ _ ` { | } ~ -)
#   domain  = label *("." label)        (at most 253; a label is letters, digits
#                                        and inner hyphens, at most 63)
#
# and nowhere in it "=?", the opener of an RFC 2047 encoded word: a header
# writer decodes one, and the header would then name other people than the
# envelope does. No quoted local part, no comment, no address literal, no
# address that is not ASCII (the submission here asks for no SMTPUTF8).
_SEND_ATOM = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
_SEND_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_SEND_ADDRESS_RE = re.compile(
    rf"({_SEND_ATOM}(?:\.{_SEND_ATOM})*)@({_SEND_LABEL}(?:\.{_SEND_LABEL})*)")
# A caller may write a recipient with a name before it: `Name <address>`. The
# name is not used, and may be a quoted string (anything in it) or plain
# words — nothing that separates addresses or opens another one.
_NAMED_ADDRESS_RE = re.compile(r"\s*((?:\"(?:[^\"\\]|\\.)*\"|[^<>()\[\]\\,;:@\"])*)<([^<>\s]*)>\s*")
_TEXT_CONTROL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_FROM_LINE_RE = re.compile(r"^From ", re.MULTILINE)
_CITABLE_ID_RE = re.compile(r"[\x21-\x3b\x3d\x3f-\x7e]{1,%d}" % MAX_ID_CHARS)
_MEDIA_TYPE_RE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}")


def _sendable(address) -> bool:
    """Whether a mail can be addressed to this: exactly one address of the
    grammar above — whoever supplied it, a caller or a mail's header."""
    if not isinstance(address, str) or len(address) > 254 or "=?" in address:
        return False
    match = _SEND_ADDRESS_RE.fullmatch(address)
    return bool(match) and len(match.group(1)) <= 64 and len(match.group(2)) <= 253


def _citable(message_id) -> bool:
    """Whether an id can stand in In-Reply-To or References: one a server
    ever saw (:func:`_searchable`), in printable ASCII, no bracket in it —
    and no encoded word, which a header writer would decode into another id."""
    return (isinstance(message_id, str) and _searchable(message_id)
            and "=?" not in message_id
            and bool(_CITABLE_ID_RE.fullmatch(message_id)))


def _send_address(value) -> str:
    """One recipient as a caller names it — ``a@x.org`` or ``Name <a@x.org>``
    — as the bare address it is sent to. ValueError for anything else.

    The display name is not used: what goes into To and Cc is the address,
    which is also what the approval page shows, so no name can make a header
    read as going somewhere it does not. One entry is one address, or the
    request is refused: a comma or a semicolon, a second address before the
    brackets, a second pair of brackets, an encoded word, a line break —
    each makes it none, and nothing is dropped or repaired to make it one
    (``ann@x.org, Bob <bob@y.org>`` is not a mail to Bob)."""
    if not isinstance(value, str):
        raise ValueError("an address must be a string")
    shown = _clean_line(value, 80)
    text = value.strip()
    refused = ValueError(f"not an address: {shown!r} — one address per entry, as "
                         f"name@example.org or Name <name@example.org>")
    if _UNSAFE_LINE_RE.search(text) or "=?" in text:
        raise refused
    named = _NAMED_ADDRESS_RE.fullmatch(text)
    address = (named.group(2) if named else text).lower()
    if not _sendable(address):
        raise refused
    # And as the library's own reader takes the entry: one address, this one.
    try:
        parsed = email.utils.getaddresses([text])
    except Exception:  # noqa: BLE001
        parsed = []
    if len(parsed) != 1 or parsed[0][1].strip().lower() != address:
        raise refused
    return address


def _send_addresses(value, field: str) -> list:
    """A request's ``to`` or ``cc``: a list of addresses (one string is a
    list of one), each once."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ValueError(f"'{field}' must be a list of addresses")
    if len(value) > MAX_SEND_RECIPIENTS:
        raise ValueError(f"too many addresses in '{field}' (at most {MAX_SEND_RECIPIENTS})")
    return list(dict.fromkeys(_send_address(item) for item in value))


def _send_text(value) -> str:
    """A request's text as a mail's body: its line ends made one kind, no
    other control character, nothing that is not text."""
    text = _scrub(value).replace("\r\n", "\n").replace("\r", "\n")
    return _TEXT_CONTROL_RE.sub("", text).strip()


def _send_files(images) -> list:
    """A request's attachments (``images``: the other gateways' /send shape —
    the key is historical, a file of any type) as ``[(bytes, content type,
    file name)]``. ValueError for a malformed one."""
    if not images:
        return []
    if not isinstance(images, list):
        raise ValueError("'images' must be a list")
    if len(images) > MAX_SEND_ATTACHMENTS:
        raise ValueError(f"at most {MAX_SEND_ATTACHMENTS} attachments per mail")
    files = []
    for image in images:
        # The shared reader of that shape: it decodes, names and types the
        # file as every gateway does. Only its bytes are wanted here.
        path, content_type, _name = _ibstore.materialize_outbound(image)
        try:
            data = path.read_bytes()
        finally:
            _ibstore.discard_outbound(path)
        # A type the mail can carry as a leaf; a container type would have
        # the MIME writer parse the bytes as parts of the mail.
        if not _MEDIA_TYPE_RE.fullmatch(content_type) \
                or content_type.startswith(("multipart/", "message/")):
            content_type = "application/octet-stream"
        files.append((data, content_type, _clean_line(path.name, 200) or "attachment"))
    return files


def _mint_message_id() -> str:
    """A new mail's Message-ID, without brackets — minted when a send is
    accepted, before it is queued, so the chat it starts is known at once."""
    domain = ACCOUNT.rpartition("@")[2]
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", domain):
        domain = "mail-gateway.invalid"
    return _threads.normalize_id(email.utils.make_msgid(domain=domain))


def _recipient_line(to: list, cc: list, names: dict) -> str:
    """Whom a send goes to, as one line for a list: the first recipient —
    by name where the thread knows one — and how many more."""
    first = to[0]
    more = len(to) + len(cc) - 1
    return ((f"{names[first]} <{first}>" if names.get(first) else first)
            + (f", +{more}" if more else ""))


def _compose(payload: dict, mirror) -> tuple:
    """Read one /send body into ``(the entry's mail fields, its files)``.
    :class:`SendRefused` for anything that is not a send this gateway makes."""
    def refuse(message, status=400):
        return SendRefused(status, message)

    author = str(payload.get("author") or "agent").strip().lower()
    if author not in _NOTE_AUTHORS:
        raise refuse("'author' must be one of " + "|".join(_NOTE_AUTHORS))
    account = str(payload.get("account") or "").strip().lower()
    if account and account != ACCOUNT:
        raise refuse("not this gateway's account", 404)
    message = payload.get("message") if payload.get("message") is not None \
        else payload.get("text")
    subject = payload.get("subject")
    if not isinstance(message, (str, type(None))) or not isinstance(subject, (str, type(None))):
        raise refuse("'message' and 'subject' must be text")
    text = _send_text(message or "")
    if len(text) > MAX_SEND_TEXT_CHARS:
        raise refuse(f"the text is too long for one mail (at most {MAX_SEND_TEXT_CHARS} "
                     f"characters)")
    try:
        files = _send_files(payload.get("images"))
    except ValueError as exc:
        raise refuse(str(exc)) from exc
    if not text and not files:
        raise refuse("an e-mail needs a text or an attachment")
    if payload.get("bcc"):
        raise refuse("blind copies are not supported: every recipient is in 'to' or 'cc'")
    chat = str(payload.get("recipient") or "").strip()
    token = str(payload.get("reply_to") or "").strip()
    if token:
        chat = reply_tokens().resolve(token) or ""
        if not chat:
            raise refuse("unknown or invalid reply_to token; name the thread by its "
                         "chat key instead")
    mode = payload.get("reply")
    if chat:
        if payload.get("to") or payload.get("cc") or subject:
            raise refuse("a reply's recipients and subject are the thread's own — the "
                         "gateway builds them from the mail it answers; leave 'to', "
                         "'cc' and 'subject' out, or leave 'recipient' out for a new mail")
        if mode not in (None, "all", "sender"):
            raise refuse("'reply' must be \"all\" or \"sender\"")
        draft = mirror.reply_draft(chat, mode or "all")
    else:
        if mode is not None:
            raise refuse("'reply' answers a thread: name it in 'recipient' (its chat key)")
        try:
            to = _send_addresses(payload.get("to"), "to")
            cc = [a for a in _send_addresses(payload.get("cc"), "cc") if a not in to]
        except ValueError as exc:
            raise refuse(str(exc)) from exc
        if not to:
            raise refuse("no recipients: a new mail needs an address in 'to' (a reply "
                         "names its thread in 'recipient')")
        if len(to) + len(cc) > MAX_SEND_RECIPIENTS:
            raise refuse(f"too many recipients (at most {MAX_SEND_RECIPIENTS})")
        draft = {"chat": None, "to": to, "cc": cc, "in_reply_to": None, "references": [],
                 "cited": [], "names": {}, "skipped": [], "answers": None,
                 "subject": _clean_line(_scrub(subject or ""), MAX_SUBJECT_CHARS)}
    message_id = _mint_message_id()
    names, skipped = draft.pop("names"), draft.pop("skipped")
    draft.pop("answers")
    if skipped:
        print(f"[mail-gateway] a reply in chat {chat!r} leaves out {len(skipped)} "
              f"address(es) of the mail it answers that no mail can be sent to: "
              f"{', '.join(map(repr, skipped[:5]))}", flush=True)
    return {
        **draft,
        # (Said in the answer and kept on the entry: who gets no mail.)
        **({"skipped": skipped} if skipped else {}),
        # A new mail starts the chat its own id names.
        "chat": draft["chat"] or message_id,
        "message_id": message_id,
        "message": text,
        "author": author,
        "reply": (mode or "all") if chat else None,
        "from": ACCOUNT,
        "recipient": _recipient_line(draft["to"], draft["cc"], names),
        # Until the mail is sent: the files' names, for whoever approves it —
        # and how many there are, so a mail is never built with fewer.
        "attachments": [name for _data, _type, name in files],
        "files": len(files),
    }, files


class _OutgoingPolicy(email.policy.EmailPolicy):
    """The standard policy, with In-Reply-To and References folded as what
    they are: ids, one after another, never broken and never rewritten.

    The standard folding takes them for free text, and a text that will not
    fit a line is wrapped by encoding it — so an id longer than a line (the
    ids of the large providers are) leaves as an RFC 2047 encoded word, which
    no mail client reads as an id, and the reply falls out of its thread.

    And a Subject in which something reads as an encoded word is written as
    one itself. Left to the standard folding such a word leaves as it stands,
    and every reader decodes it: the recipient would read a subject nobody
    wrote — and a thread's subject is its first sender's to choose."""

    def _ids(self, name: str, value) -> str | None:
        if name.lower() not in ("in-reply-to", "references", OWN_ID_HEADER.lower()):
            return None
        lines = [f"{name}:"]
        for message_id in str(value).split():
            if len(lines[-1]) + 1 + len(message_id) > self.max_line_length \
                    and lines[-1] != f"{name}:":
                lines.append("")
            lines[-1] += " " + message_id
        return self.linesep.join(lines) + self.linesep

    def _subject(self, name: str, value) -> str | None:
        if name.lower() != "subject" or "=?" not in str(value):
            return None
        return (f"{name}: " + email.header.Header(str(value), "utf-8", header_name=name)
                .encode(linesep=self.linesep) + self.linesep)

    def fold(self, name, value):
        return (self._ids(name, value) or self._subject(name, value)
                or super().fold(name, value))

    def fold_binary(self, name, value):
        folded = self._ids(name, value) or self._subject(name, value)
        return folded.encode("ascii") if folded else super().fold_binary(name, value)


_OUTGOING = _OutgoingPolicy()


def _build_outgoing(entry: dict, when: float, files: list | None = None) -> tuple:
    """The mail an entry describes, as ``(message, its bytes)`` — built with
    email_client's own message builder, dated ``when``. The same mail
    whenever it is built (but for the MIME boundaries), which is what lets
    the Sent copy be made after a stop.

    Every header value was reduced to its field's shape where it came in
    (_compose, Mirror.reply_draft), and the builder refuses a line break in
    one on top of that. The envelope is ``to`` + ``cc`` as they stand in the
    entry, never anything read back out of a header — and the mail is not
    handed out unless its headers, read back off the bytes, say exactly what
    the entry says (:func:`_check_written`)."""
    if files is None:
        files = _stored_files(entry)
    text, subject = entry.get("message") or "", entry.get("subject") or ""
    msg = _ec._build_message(
        MAIL_CONFIG, entry["to"], subject, text,
        cc=entry.get("cc") or None,
        in_reply_to=f"<{entry['in_reply_to']}>" if entry.get("in_reply_to") else None,
        references=" ".join(f"<{i}>" for i in entry.get("references") or []) or None,
        message_id=f"<{entry['message_id']}>")
    msg.policy = _OUTGOING
    if "=?" in subject:
        # Given to the header as the encoded word it will leave as: its
        # value is then the subject itself, character for character, and not
        # what the word in it would decode to (see _OutgoingPolicy).
        msg.replace_header("Subject", email.header.Header(subject, "utf-8")
                           .encode(maxlinelen=0))
    msg[OWN_ID_HEADER] = f"<{entry['message_id']}>"
    if _FROM_LINE_RE.search(text):
        # smtplib writes a mail out the way a mailbox file is written, with a
        # ">" put before every line of the text that begins "From " — so the
        # recipient would read what the author did not write. Encoded, the
        # text has no such line, and arrives as it was typed.
        msg.set_content(text, cte="base64")
    msg["Date"] = email.utils.format_datetime(datetime.fromtimestamp(when, timezone.utc))
    for data, content_type, name in files:
        maintype, _slash, subtype = content_type.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    raw = msg.as_bytes(policy=_OUTGOING.clone(linesep="\r\n"))
    _check_written(entry, raw)
    return msg, raw


def _check_written(entry: dict, raw: bytes) -> None:
    """ValueError unless the mail, as it was written, says what its entry
    says: To and Cc name exactly the addresses of the envelope, in its order
    and with nothing beside them; From is the account; the ids and the
    subject are the entry's.

    The net under everything above. Each value was checked where it came in,
    against a grammar chosen so that a header writer has nothing to decode,
    quote or split — and still what decides where a reply-all of the
    recipients goes, and what the user's own Sent copy says, is the bytes.
    They are read back twice, as the library's current parser reads them and
    as its older one does, and a mail whose headers and envelope disagree
    under either is never queued and never sent."""
    head = _head_and_rest(raw)[0] + b"\r\n\r\n"
    modern = email.message_from_bytes(head, policy=email.policy.default)
    plain = email.message_from_bytes(head)

    def unfolded(name) -> str:
        return _UNFOLD_RE.sub("", str(plain.get(name) or "")).strip()

    def wrong(what) -> ValueError:
        return ValueError(f"its {what} header would not say what is sent")

    for name, wanted in (("To", entry["to"]), ("Cc", entry.get("cc") or []),
                         ("From", [MAIL_CONFIG.user]), ("Bcc", [])):
        fields = modern.get_all(name) or []
        if len(fields) != (1 if wanted else 0):
            raise wrong(name)
        if not wanted:
            continue
        first = [(a.display_name, a.addr_spec) for a in fields[0].addresses]
        second = email.utils.getaddresses([unfolded(name)])
        if name == "From":
            first, second = [("", a) for _n, a in first], [("", a) for _n, a in second]
        if first != [("", a) for a in wanted] or second != first:
            raise wrong(name)
    for name, wanted in (
            ("Message-Id", [entry["message_id"]]), (OWN_ID_HEADER, [entry["message_id"]]),
            ("In-Reply-To", [entry["in_reply_to"]] if entry.get("in_reply_to") else []),
            ("References", entry.get("references") or [])):
        if len(plain.get_all(name) or []) != (1 if wanted else 0) \
                or unfolded(name).split() != [f"<{i}>" for i in wanted]:
            raise wrong(name)
    subject = " ".join((entry.get("subject") or "").split())
    if " ".join(str(modern.get("Subject") or "").split()) != subject \
            or _header_text(plain, "Subject", MAX_SUBJECT_CHARS + 100) != subject:
        raise wrong("Subject")


def _stored_files(entry: dict) -> list:
    """A stored send's attachments, read back for building its mail — all of
    them, or the send fails: a mail must not leave without a file that the
    one who approved it was shown."""
    files = _send_files(SENDS.images(entry.get("id")))
    if len(files) != int(entry.get("files") or 0):
        raise RuntimeError("the attachments of this send are no longer in the store")
    return files


def _store_sent_parts(entry: dict, raw: bytes, files: list, stored: list) -> dict:
    """Store what the record of a sent mail refers to — its text, its header
    block, its attachments — as blobs; ``{body_part, header_part,
    attachments}``. Each id is appended to ``stored`` as it is written, for
    the caller to undo if the mail does not go."""
    def keep(data: bytes, content_type: str, file_name=None) -> str:
        media_id = _ibstore.store_media(INBOUND_STORE_DIR, data, content_type,
                                        file_name=file_name)
        stored.append(media_id)
        return _MEDIA_REF + media_id

    head = _head_and_rest(raw)[0].decode("utf-8", errors="replace").replace("\r\n", "\n")
    return {
        "body_part": keep((entry.get("message") or "").encode("utf-8", "replace"),
                          "text/plain; charset=utf-8"),
        "header_part": keep(head.encode("utf-8", "replace"), "text/plain; charset=utf-8"),
        "attachments": [keep(data, content_type, name) for data, content_type, name in files],
    }


def _discard_parts(refs: dict | None) -> None:
    for ref in [(refs or {}).get("body_part"), (refs or {}).get("header_part"),
                *((refs or {}).get("attachments") or [])]:
        media_id = _ibstore.media_id_of(ref or "")
        if media_id:
            _ibstore.discard_media(INBOUND_STORE_DIR, media_id)


def _smtp_refusal(exc: BaseException) -> bool:
    """Whether what a send raised is the SMTP server's own no — an answer,
    after which the mail is known not to have been taken. (email_client
    wraps smtplib's error; the one it wraps is the context.)"""
    cause = exc.__context__ if isinstance(exc, _ec.EmailError) else exc
    return isinstance(cause, (smtplib.SMTPResponseException, smtplib.SMTPRecipientsRefused,
                              smtplib.SMTPNotSupportedError))


def _press_digest(payload: dict) -> str:
    """What one request asks to be sent, as a digest: a ``request_key`` names
    one press of Send, and a second request under the same key that asks for
    another mail is a caller's mistake — answered as one, not with the first
    mail's outcome."""
    said = [payload.get(name) for name in
            ("recipient", "reply_to", "reply", "to", "cc", "subject", "account")]
    said.append(payload.get("message") if payload.get("message") is not None
                else payload.get("text"))
    # The attachments themselves, not their number: the same words with
    # another file are another mail.
    images = payload.get("images")
    said.append(images if isinstance(images, list) else [])
    return hashlib.sha256(json.dumps(said, sort_keys=True, default=str)
                          .encode("utf-8", "replace")).hexdigest()


def _send_answer(entry: dict, host_header) -> tuple:
    """``(HTTP status, answer)`` for a send as its entry stands — to the
    request that made it, and to every repeat of that request.

    A mail that may have gone out is never answered as not sent: while the
    SMTP server's word on it is missing the answer is 502 with
    ``unconfirmed``, and it stays that when the word never came
    (``outcome_unknown``). Only a send that is known not to have left —
    refused by the server, or failed before anything was handed over — is
    answered as a plain failure, and that answer says so in a field of its
    own (``"sent": false``): the status, 502, is the unconfirmed answer's
    too, and whoever reads it must not have to tell the two by their
    wording."""
    status = entry.get("status")
    ids = {"message_id": entry["message_id"], "chat": entry["chat"]}
    told = {key: entry[key] for key in ("refused", "skipped") if entry.get(key)}
    if status == "approved":
        # (``refused``: the mail went to the others; these the server would
        # not take. ``skipped``: on the mail answered, and no address.)
        return 200, {"status": "sent", **ids, "ts": entry["sent_at"],
                     "attachments": entry.get("attachments") or [], **told}
    if status == "pending" or (status == "sending" and not entry.get("direct")):
        path = f"/sends/{_approval_slug(host_header)}/{entry['id']}"
        return 202, {
            "status": "pending_approval" if status == "pending" else "sending",
            "request_id": entry["id"],
            "approval_url": SEND_APPROVAL_BASE_URL + path if SEND_APPROVAL_BASE_URL else path,
            **ids, **told,
            "note": "This e-mail requires web-gateway approval. "
                    "Visit the approval URL to allow or deny." if status == "pending"
                    else "This e-mail was approved and is being sent."}
    if status == "sending":
        # Handed over (or about to be), the server's answer not seen.
        return 502, {"error": "the mail server has not confirmed this mail; it is not "
                              "known yet whether it went out. It is not sent again, and "
                              "shows in the thread if it did.",
                     "unconfirmed": True, "request_id": entry["id"], **ids}
    if status == "error" and not _failed_for_certain(entry):
        # (Also an entry that does not say either way: never taken for unsent.)
        return 502, {"error": f"send not confirmed: {entry.get('error') or UNKNOWN_OUTCOME}",
                     "unconfirmed": True, "outcome_unknown": True,
                     "request_id": entry["id"], **ids}
    if status == "error":
        return 502, {"error": f"send failed: {entry.get('error') or 'the send was lost'}",
                     "sent": False}
    return 409, {"error": f"this send was {status} before it went out; nothing was sent",
                 "status": status, "request_id": entry["id"], **ids}


class _Flight(threading.Event):
    """One direct send while its request works on it: set when the SMTP
    session is over, with the entry as the send ended (``final``) — which a
    repeat that waited is answered from when the entry itself is gone by
    then (a send that failed for certain, cleared by its own request)."""
    final = None


# The direct sends that are with the SMTP server right now: request id → its
# flight, for a repeat of the request to wait on.
_IN_FLIGHT: dict = {}


def _repeat_send(earlier: dict, press: str, host_header) -> tuple:
    """A /send whose ``request_key`` stands for an earlier send: what became
    of that send. Nothing is composed, queued or sent.

    The answer is that send's own, and says that it is a repeat
    (``"repeat": true``): what it tells of — a mail that went out, one that
    waits — happened before this request, and this request did none of it.
    A caller that does something when a mail goes out (clears a draft,
    marks a chat read) must not do it a second time on this answer.

    (An earlier send that failed for certain never comes here from
    ``_accept_send`` — a request under its key is a new attempt. The one way
    it does: this request waited for a send still in flight, and that send
    then failed. Its caller has the failure as its answer, and so has this
    one — one press, one attempt: "send failed", ``"sent": false``, as a
    repeat. The request that made the attempt clears its entry at once, so
    the answer is then made from what the flight says of its end.)"""
    if earlier.get("press") not in (None, press):
        return 409, {"error": "this request_key was already used for another mail; "
                              "nothing was sent — a key names one press of Send"}
    flight = _IN_FLIGHT.get(earlier["id"])
    if flight is not None:
        flight.wait(REPEAT_WAIT_SECONDS)
    entry = SENDS.get(earlier["id"]) or (flight.final if flight is not None else None) \
        or earlier
    print(f"[mail-gateway] a repeat of send {entry['id']} (same request_key) is answered "
          f"with its outcome ({entry.get('status')}); nothing is sent", flush=True)
    status, answer = _send_answer(entry, host_header)
    return status, {**answer, "repeat": True}


def _send_fault(entry: dict, exc: BaseException) -> dict:
    """``_execute_send`` is written never to raise. Where it does all the
    same, what is known of the send is what its entry says on disk, and the
    answer is made from that — never from the fault, which could have come
    before the hand-over or after it:

      * no mark "data": nothing was handed over (the mark is written
        first) — the send ends as failed, for certain;
      * the mark, and no answer of the server's noted: nobody knows — left
        to the mirror loop, which asks Sent; the caller hears "unconfirmed";
      * anything else: the entry as it stands (sent, or already ended).

    An entry that is gone is taken for the second case: a send that went
    out and was cleared looks the same from here."""
    request_id = entry["id"]
    said = _clean_line(exc, 300) or type(exc).__name__
    print(f"[mail-gateway] send {request_id}: an unexpected fault ({said})\n"
          f"{traceback.format_exc()}", flush=True)
    stands = SENDS.get(request_id)
    if stands is None:
        return {**entry, "status": "sending", "phase": "data"}
    if stands.get("status") == "sending" and not stands.get("phase"):
        return _fail_send(stands, said)
    if stands.get("status") == "sending":
        SENDS.owe(request_id)
        _wake()
    return stands


def _accept_send(payload: dict, host_header, caller_waits=None) -> tuple:
    """POST /send: ``(HTTP status, answer)``.

    A direct send is answered as soon as the SMTP server has taken the mail:
    its record is written on the way out where the mirror is free for it,
    and its Sent copy is the mirror loop's to file — nothing the answer
    waits for can still decide whether the mail went.

    ``caller_waits`` says whether whoever asked is still on the line. A
    direct send that failed for certain is over once its caller has that
    answer; one whose caller is gone (it timed out, and may have told its
    user "not confirmed yet") keeps its entry as a failed send, where the
    chat finds it.

    A request under a ``request_key`` that stands for an earlier send is
    answered from that send and sends nothing (``_repeat_send``) — unless
    that send failed for certain: then nothing left the gateway under the
    key, and this request is a new attempt, composed, decided by the policy
    and sent afresh; the failed entry gives way to the new one
    (``Sends.create``). A send of unknown outcome is never such a one."""
    mirror = MIRROR
    key = payload.get("request_key")
    if key is not None and not (isinstance(key, str) and _REQUEST_KEY_RE.fullmatch(key)):
        return 400, {"error": "'request_key' must be a token of 1 to 64 printable ASCII "
                              "characters, without spaces"}
    press = _press_digest(payload) if key else None
    # What became of an earlier send is said before anything else — also by
    # a gateway that cannot send (any more): the mail may be out.
    earlier = SENDS.by_key(key) if key else None
    if earlier is not None and not _failed_for_certain(earlier):
        return _repeat_send(earlier, press, host_header)
    # (From here on nothing stands under the key. An answer that nothing was
    # sent says so — ``"sent": false`` — where its status is one a caller
    # could read as "the outcome is open".)
    if mirror is None or MAIL_CONFIG is None:
        return 503, {"error": "the mail gateway is not configured", "sent": False}
    if SMTP_PROBLEM is not None:
        return 503, {"error": SMTP_PROBLEM, "sent": False}
    try:
        entry, files = _compose(payload, mirror)
        # Built once now: no approval is asked for a mail that could only
        # fail to be written.
        _build_outgoing(entry, _now(), files)
    except SendRefused as exc:
        return exc.status, {"error": str(exc)}
    except (ValueError, LookupError, TypeError) as exc:
        return 400, {"error": f"this mail cannot be written: {_clean_line(exc, 200)}"}
    except Exception as exc:  # noqa: BLE001 - before any entry stands: nothing was sent
        print(f"[mail-gateway] a send could not be composed: {exc}\n"
              f"{traceback.format_exc()}", flush=True)
        return 502, {"error": f"send failed: {_clean_line(exc, 300) or type(exc).__name__}",
                     "sent": False}
    category = _send_category()
    direct = _send_is_direct(category, payload.get("user_approved") is True)
    entry.update(id=uuid.uuid4().hex, category=category, created=_now(),
                 thread=_origin.valid_thread(payload.get("thread")),
                 status="sending" if direct else "pending", direct=direct)
    if key:
        entry.update(request_key=key, press=press)
    # (In flight from before its entry stands: a repeat that finds the entry
    # finds this to wait on — and until this request is done with the entry,
    # so that a repeat never finds the one gone and the other too.)
    flight = _IN_FLIGHT[entry["id"]] = _Flight()
    try:
        try:
            earlier = SENDS.create(entry, payload.get("images") or [])
        except OSError as exc:
            print(f"[mail-gateway] could not persist a send: {exc}", flush=True)
            return 503, {"error": "could not queue the send; retry later", "sent": False}
        if earlier is not None:
            # (The same press, twice at once: the other request was first.)
            return _repeat_send(earlier, press, host_header)
        if not direct:
            print(f"[mail-gateway] pending send registered for chat {entry['chat']!r} "
                  f"(category={category}, id={entry['id']})", flush=True)
            return _send_answer(entry, host_header)
        try:
            final = _execute_send(entry["id"])
        except Exception as exc:  # noqa: BLE001 - answered from the entry, not the fault
            final = _send_fault(entry, exc)
        # (No entry to carry out: nothing was begun, and nothing left.)
        final = final or {**entry, "status": "error", "error": "the send was lost",
                          "outcome_unknown": False}
        flight.final = final
        flight.set()
        if _failed_for_certain(final) and (caller_waits is None or caller_waits()):
            try:
                SENDS.dismiss(final["id"])
            except OSError as exc:
                print(f"[mail-gateway] could not clear the failed send {final['id']}: {exc}",
                      flush=True)
        return _send_answer(final, host_header)
    finally:
        flight.set()
        _IN_FLIGHT.pop(entry["id"], None)


def _send_unplanned(payload: dict, host_header, exc: BaseException) -> tuple:
    """``(HTTP status, answer)`` for a /send that raised where nothing was
    planned to. The fault says nothing of the mail — it could have come
    before the hand-over or after it — so the answer is not made from it:

      * the request named itself (``request_key``): what stands under the
        key is what became of it. An entry — answered as it stands. None —
        nothing was queued or handed over (a request that names itself has
        its entry from before anything is sent until its key is forgotten):
        failed, for certain;
      * it named nothing: nobody can say from here. That is answered as
        ``unconfirmed`` — never as "failed", which a caller would follow
        with a second mail."""
    said = _clean_line(exc, 300) or type(exc).__name__
    key = payload.get("request_key")
    if isinstance(key, str) and _REQUEST_KEY_RE.fullmatch(key):
        try:
            entry = SENDS.by_key(key)
            if entry is None:
                return 502, {"error": f"send failed: {said}", "sent": False}
            return _send_answer(entry, host_header)
        except Exception:  # noqa: BLE001 - then nobody can say
            pass
    return 502, {"error": f"the mail gateway failed while it worked on this send ({said}); "
                          f"it is not known here whether the mail went out. It is not sent "
                          f"again — look in the thread before writing it anew.",
                 "unconfirmed": True}


def _reply_preview(body: dict) -> tuple:
    """POST /reply-preview: ``(HTTP status, answer)`` — whom a reply in this
    chat would be sent to, right now, in either mode.

    Addressed by the very function a send is addressed by
    (:meth:`Mirror.reply_draft`), so what a page shows from this answer is
    what ``/send`` would do at this moment, and nothing a browser worked out
    for itself. It reads; nothing is composed, queued or sent. The refusals
    are /send's own, with their sentence: 400 for a chat this mailbox does
    not mirror, 409 for one in which no reply is possible.

    ``{"all": Addressing, "sender": Addressing, "choice", "subject"}``, an
    Addressing being ``{"to": [{"address", "name"}], "cc": [...]}`` with the
    display name on record or null; ``choice`` says whether the two modes
    differ. Beside the contract's four: ``answers`` — the Message-ID of the
    mail the reply would answer — and, when any, ``skipped``: addresses on
    that mail a reply is not sent to because no mail can be addressed to
    them."""
    mirror = MIRROR
    if mirror is None or MAIL_CONFIG is None:
        return 503, {"error": "the mail gateway is not configured"}
    chat = str(body.get("chat") or "").strip()
    if not chat:
        return 400, {"error": "chat (the chat key) is required"}
    if str(body.get("account") or "").strip().lower() != ACCOUNT or not ACCOUNT:
        return 404, _NOT_OURS
    if SMTP_PROBLEM is not None:
        return 503, {"error": SMTP_PROBLEM}
    try:
        drafts = mirror.reply_drafts(chat, ("all", "sender"))
    except SendRefused as exc:
        return exc.status, {"error": str(exc)}
    except Busy as exc:
        return 503, {"error": str(exc)}

    def addressing(draft: dict) -> dict:
        def people(addresses):
            return [{"address": a, "name": draft["names"].get(a)} for a in addresses]
        return {"to": people(draft["to"]), "cc": people(draft["cc"])}

    answer = {mode: addressing(draft) for mode, draft in drafts.items()}
    answer.update(choice=answer["all"] != answer["sender"],
                  subject=drafts["all"]["subject"], answers=drafts["all"]["answers"])
    if drafts["all"]["skipped"]:
        answer["skipped"] = drafts["all"]["skipped"]
    return 200, answer


def _decide_send(request_id: str, verb: str) -> dict | None:
    """POST /pending-sends/<id>/approve|reject|retract. An approval is
    carried out off the approving request (a slow SMTP server must not hold
    it open); the entry is answered as "sending" and says the rest later."""
    entry, decided = SENDS.decide(request_id, verb)
    if entry is None:
        return None
    if decided and verb == "approve":
        _in_background(_execute_send, request_id)
    elif decided:
        print(f"[mail-gateway] pending send {request_id} {entry['status']}", flush=True)
    return entry


def _execute_send(request_id: str) -> dict | None:
    """Carry out one send whose decision stands and of which nothing has
    left yet; returns the entry as it ends. Never raises.

    It returns once the SMTP server has answered — with the record written
    if the mirror was free for that, and the Sent copy left to the mirror
    loop: the mail is out, and neither can undo that or is worth making
    whoever asked wait."""
    entry = SENDS.get(request_id)
    if entry is None or entry.get("status") != "sending" or entry.get("phase"):
        return entry
    mirror, stored, handed, taken = MIRROR, [], [], {}
    refused: dict = {}
    try:
        if mirror is None or MAIL_CONFIG is None:
            raise RuntimeError("the mail gateway is not configured")
        if SMTP_PROBLEM is not None:
            raise RuntimeError(SMTP_PROBLEM)
        when = _now()
        files = _stored_files(entry)
        msg, raw = _build_outgoing(entry, when, files)
        refs = _store_sent_parts(entry, raw, files, stored)
        # Who wrote it, before it can turn up in Sent.
        mirror.expect_sent(entry["message_id"], entry["author"])

        def on_data():
            # The mark of "handed over", on disk before any of the message
            # is (a failure to write it aborts the send: nothing has left).
            SENDS.update(request_id, phase="data", date=when, refs=refs)
            handed.append(True)

        def on_taken():
            # The mark of "sent", the moment the server says so — before the
            # session is even closed: what lies between its answer and this
            # is the one stretch a stop cannot be told from.
            taken.update(status="approved", phase="sent", sent_at=_now(),
                         attachments=refs["attachments"])
            try:
                SENDS.update(request_id, **taken)
            except OSError as exc:
                # The mail is out and the mark is not: the send is finished
                # from memory. (A stop now leaves "data", and the mailbox
                # is asked.)
                print(f"[mail-gateway] send {request_id}: could not note that the mail "
                      f"was sent ({exc}); finishing regardless", flush=True)

        refused = _ec._smtp_send(MAIL_CONFIG, msg, entry["to"] + entry["cc"],
                                 on_data=on_data, on_taken=on_taken) or {}
    except Exception as exc:  # noqa: BLE001 - every failure ends the send, as what it is
        said = _clean_line(exc, 500) or type(exc).__name__
        if taken:
            # After the server took the mail (closing the session failed):
            # nothing that follows makes it unsent.
            print(f"[mail-gateway] send {request_id}: sent; the SMTP session then "
                  f"failed ({said})", flush=True)
        elif handed and not _smtp_refusal(exc):
            print(f"[mail-gateway] send {request_id}: the SMTP server was handed the mail "
                  f"and did not answer ({said}); not sent again — looking for it in Sent",
                  flush=True)
            return _settle_unknown({**entry, "phase": "data", "date": when, "refs": refs},
                                   mirror)
        else:
            print(f"[mail-gateway] send {request_id} failed: {said}", flush=True)
            for media_id in stored:
                _ibstore.discard_media(INBOUND_STORE_DIR, media_id)
            return _fail_send(entry, said)
    if not taken:
        # (A server answer the hook did not see as 250 and smtplib took for
        # success cannot happen; a send helper without the hook can.)
        taken.update(status="approved", phase="sent", sent_at=_now(),
                     attachments=refs["attachments"])
    entry = {**entry, "date": when, "refs": refs, **taken}
    if refused:
        # The server took the mail for the others. Who got none is on the
        # entry, in the answer, on the record and in the rail's word of it.
        entry["refused"] = sorted(refused)
        print(f"[mail-gateway] send {request_id}: the SMTP server refused "
              f"{', '.join(entry['refused'])}; the mail went to the others", flush=True)
    try:
        entry = SENDS.update(request_id, **taken,
                             **({"refused": entry["refused"]} if refused else {})) or entry
    except OSError as exc:
        print(f"[mail-gateway] send {request_id}: its entry could not be written "
              f"({exc}); finishing regardless", flush=True)
    print(f"[mail-gateway] send {request_id} sent to {entry['recipient']} "
          f"(chat {entry['chat']!r}, author={entry['author']})", flush=True)
    return _finish_send(entry, mirror, mailbox=False)


def _fail_send(entry: dict, said: str, *, unknown: bool = False) -> dict:
    """End a send that did not go, or of which nobody can say: its entry
    becomes "error", with the reason — for the approval page, the thread that
    asked, and the chat, which lists it as failed until someone dismisses
    it. ``unknown``: the mail may have gone out (``outcome_unknown``), and
    whoever reads the entry must not take it for unsent.

    (A direct send that failed for certain, and whose caller has that
    answer, is cleared by the request itself: ``_accept_send``. And any send
    that failed for certain gives way to a new attempt under its
    ``request_key``: ``Sends.create``.)"""
    request_id = entry["id"]
    changes = {"status": "error", "error": said, "outcome_unknown": bool(unknown)}
    ended = {**entry, **changes}
    try:
        ended = SENDS.update(request_id, drop=("refs", "phase", "date"), **changes) or ended
        SENDS.release(request_id)
    except OSError as exc:
        print(f"[mail-gateway] could not note the outcome of send {request_id}: {exc}",
              flush=True)
    SENDS.settled(request_id)
    return ended


def _settle_unknown(entry: dict, mirror) -> dict:
    """A send that was handed to the SMTP server and whose answer nobody
    saw: ask the mailbox. The mail is in Sent — it went out, and the send is
    finished as any other. It is not, and would be by now — the send ends as
    an error that says its outcome is not known. Sent cannot say yet — the
    entry waits for the mirror loop's next look."""
    request_id, message_id = entry["id"], entry["message_id"]
    try:
        found = mirror.sent_copy(message_id)
    except MailboxError:
        found = UNSURE
    if found is not UNSURE and found is not None:
        mirror.place_sent(found, message_id)
        print(f"[mail-gateway] send {request_id}: the mail is in Sent — it went out",
              flush=True)
        return _went_out(entry, mirror)
    waited = _now() - float(entry.get("date") or 0.0) > MAIL_SENT_GRACE_SECONDS
    # (After the grace, a mailbox that still cannot say has said as much as
    # one that says no: nobody will ever know.)
    if waited or (found is None and MAIL_CONFIG.save_sent):
        print(f"[mail-gateway] send {request_id}: {UNKNOWN_OUTCOME}", flush=True)
        _discard_parts(entry.get("refs"))
        return _fail_send(entry, UNKNOWN_OUTCOME, unknown=True)
    SENDS.owe(request_id)
    return entry


def _went_out(entry: dict, mirror) -> dict:
    """Finish a send whose answer nobody saw, now that its copy in Sent says
    it went out (:func:`_settle_unknown`, and the mirror meeting that copy
    under an id of the provider's — Mirror._own_send)."""
    changes = dict(status="approved", phase="sent", sent_at=entry["date"], filed=True,
                   attachments=(entry.get("refs") or {}).get("attachments") or [])
    return _finish_send(SENDS.update(entry["id"], **changes) or {**entry, **changes}, mirror)


def _finish_send(entry: dict, mirror, *, raw: bytes | None = None,
                 mailbox: bool = True, wake: bool = True) -> dict:
    """What follows the SMTP server's acceptance, each step once: the record
    with the rail's event, then the copy in Sent. Returns the entry as it
    stands; never raises.

    A step that cannot be done now is left to the mirror loop, which comes
    here again (``Sends.owe``): the record while a pass over the mailbox
    holds the mirror's lock, the Sent copy while the mailbox will not take
    it — and always when it is the send itself that calls (``mailbox``
    false does only what needs none): the copy is filed by the loop at its
    next turn at the mailbox, off every request. ``wake`` false is the loop
    itself calling, which needs no telling."""
    request_id, message_id = entry["id"], entry["message_id"]
    changes: dict = {}
    trouble = None
    if not entry.get("recorded"):
        if mirror._lock.acquire(timeout=RECORD_LOCK_SECONDS):
            try:
                if mirror._reconcile:
                    trouble = "the mailbox index is being matched to the ledger"
                else:
                    changes.update(recorded=True, chat=mirror.record_sent(entry))
            except Exception as exc:  # noqa: BLE001 - tried again by the loop
                trouble = f"its record could not be written: {type(exc).__name__}: {exc}"
            finally:
                mirror._lock.release()
        else:
            trouble = "the mirror is busy"
    if not entry.get("filed"):
        if not MAIL_CONFIG.save_sent:
            changes["filed"] = True     # the provider files the copy itself
        elif mailbox:
            try:
                again = bool(entry.get("filing"))
                if not again:
                    SENDS.update(request_id, filing=True)
                if raw is None:
                    raw = _build_outgoing(entry, float(entry["date"]))[1]
                mirror.file_sent(message_id, raw, float(entry["sent_at"]), again=again)
                changes["filed"] = True
            except Exception as exc:  # noqa: BLE001 - tried again by the loop
                trouble = f"its Sent copy could not be filed: {type(exc).__name__}: {exc}"
                mirror.keep_settling(message_id)
    entry = {**entry, **changes}
    done = bool(entry.get("recorded") and entry.get("filed"))
    try:
        if done and entry.get("direct") and not entry.get("request_key"):
            SENDS.remove(request_id)
        elif done:
            # (A direct send that named its request stays, as what a repeat
            # of that request is answered from: Sends.sweep clears it.)
            entry = SENDS.update(request_id, drop=_ENTRY_WORKING, done=True, **changes) or entry
            SENDS.release(request_id)
        elif changes:
            SENDS.update(request_id, **changes)
    except OSError as exc:
        done, trouble = False, f"its entry could not be written: {exc}"
    if done:
        SENDS.settled(request_id)
    else:
        SENDS.owe(request_id)
        # (Only what went wrong is worth a line: a copy that waits for the
        # loop's next turn is the ordinary course.)
        _send_trouble(request_id, trouble)
    if wake:
        _wake()      # the rail is told by the mirror loop, off this request
    return entry


def _resume_send(request_id: str, mirror, *, mailbox: bool) -> None:
    """One entry the mirror loop has work on (Mirror._resume_sends)."""
    entry = SENDS.get(request_id)
    status = (entry or {}).get("status")
    if status == "approved" and not entry.get("done"):
        _finish_send(entry, mirror, mailbox=mailbox, wake=False)
    elif status == "sending" and entry.get("phase") == "data":
        if mailbox:
            _settle_unknown(entry, mirror)
    else:
        SENDS.settled(request_id)


def _recover_sends() -> None:
    """At start: carry out the approved sends a stop left unbegun, and leave
    the rest of what it left to the mirror loop (Sends.recover)."""
    for request_id in SENDS.recover():
        print(f"[mail-gateway] send {request_id} was approved and not begun before "
              f"the last stop; sending it now", flush=True)
        _in_background(_execute_send, request_id)


def _in_background(send, request_id: str) -> None:
    """Carry a send out off the request (or the start) that set it going."""
    threading.Thread(target=send, args=(request_id,), name=f"send-{request_id[:8]}",
                     daemon=True).start()


_WAKE = threading.Event()


def _wake() -> None:
    """Have the mirror loop do, now, what needs no mailbox (_between_polls)."""
    _WAKE.set()


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
    # (Whether a wake between two turns may reach for the mailbox: only
    # after a turn that went through — see _mirror_loop.)
    loop["up"] = False
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
        SENDS.sweep()
        # The mailbox answered this turn, whatever became of the rest (a
        # fault of the mirror's own is no word on the mailbox).
        loop["up"] = True
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
        # What has arrived is offered all the same: an offer needs the
        # ledger, not the mailbox. Not while the policy is awaited — the
        # gate would then be asked without it.
        if _policy_problem() is None:
            try:
                mirror.offer_from_ledger()
            except Exception as rail_exc:  # noqa: BLE001 - the next turn tries again
                print(f"[mail-gateway] could not offer while the mailbox is away: "
                      f"{rail_exc}", flush=True)
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


def _between_polls(mirror: Mirror, mailbox: bool = True) -> bool:
    """What a send leaves for the mirror loop that cannot wait for the next
    poll: a record its request could not write (the mirror's lock was held),
    the rail's word of a mail just sent — and then its copy in Sent, but only
    while the mailbox answered at the loop's last turn (``mailbox``).

    False when the mailbox was asked and something is still owed: the loop
    then leaves it alone until a poll has gone through again. Never raises."""
    try:
        with mirror._lock, mirror.rail_pass():
            mirror._resume_sends(mailbox=False)
            if mirror._flush_rail() and _policy_problem() is None:
                # (A wake is no time to wait for: what is due is offered.)
                mirror._offer_again(OFFER_BATCH)
            if mailbox and mirror._folders and SENDS.due():
                mirror._resume_sends(mailbox=True)
                return not SENDS.due()
    except Exception as exc:  # noqa: BLE001 - the next poll does the same
        print(f"[mail-gateway] could not finish a send between polls: {exc}", flush=True)
        return False
    return True


def _mirror_loop(mirror: Mirror) -> None:
    loop: dict = {}
    while True:
        # The pause between two turns is the mailbox's rhythm, and a send
        # does not shorten it: a wake does its own small work and waits on.
        # That work reaches for the mailbox — to file the Sent copy of the
        # mail just sent — only while the last turn found it answering, and
        # not again after it failed there (a refused login retried at every
        # send is how a mailbox gets locked): the next poll finds out.
        until = time.monotonic() + _loop_step(mirror, loop)
        while _WAKE.wait(max(0.0, until - time.monotonic())):
            _WAKE.clear()
            if not _between_polls(mirror, mailbox=bool(loop.get("up"))):
                loop["up"] = False


# ── HTTP API ──────────────────────────────────────────────────────────────────

# Who may be named as a mail's author, by a send or by a note: whoever wrote
# it. ("device" is the mirror's own word for a mail some client sent.)
_NOTE_AUTHORS = ("agent", "user")
_CHAT_ACTIONS = ("/chats/read", "/chats/archive", "/chats/delete")
_NOT_OURS = {"error": "not this gateway's account"}


def _same_secret(given: str, expected: str) -> bool:
    """Compared as bytes: compare_digest refuses text that is not ASCII, and
    a header is whatever bytes the peer sent (read here as Latin-1) — a
    secret with one such byte is a wrong secret, not a dropped connection."""
    return bool(given) and hmac.compare_digest(
        given.encode("latin-1", "replace"), expected.encode("utf-8", "surrogateescape"))


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
        return _same_secret(token, GATEWAY_TOKEN)

    def _read_body(self, limit: int) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            return None
        if length <= 0 or length > limit:
            return None
        return self.rfile.read(length)

    def _read_object(self, limit: int = MAX_BODY_BYTES) -> dict | None:
        """The request's body as a JSON object; None once a 400 has said why
        it is none."""
        raw = self._read_body(limit)
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except (ValueError, UnicodeDecodeError):
            body = None
        if not isinstance(body, dict):
            self._reply(400, {"error": "body must be a JSON object" if raw
                              else "empty or oversized body"})
            return None
        return body

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
        if path == "/pending-sends":
            # The sends that are not messages (yet) — of one chat, with
            # ?chat=<key>: each says by its ``status`` whether it waits for
            # approval ("pending"), is being sent ("sending") or failed
            # ("error", with ``error`` and ``outcome_unknown``).
            chat = (parse_qs(urlsplit(self.path).query).get("chat") or [None])[0]
            self._reply(200, {"pending": [_public(entry) for entry in SENDS.listed(chat)]})
            return
        pending = _PENDING_SEND_RE.match(path)
        if pending and not pending.group(2):
            # One entry, in whatever status — also one the list above no
            # longer names. ``dismissed`` true on an "error" means it is
            # over: kept only so that a repeat of its request is still told
            # that the mail may have gone out (Sends.dismiss), and nothing
            # for a chat to show. And an "error" is read with its
            # ``outcome_unknown``: true is "nobody knows", never "not sent".
            entry = SENDS.get(pending.group(1))
            if entry is None:
                self._reply(404, {"error": "not found"})
            else:
                self._reply(200, _public(entry))
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
        decision = _PENDING_SEND_RE.match(path)
        if decision and decision.group(2):
            self._decide(decision.group(1), decision.group(2))
        elif path in _CHAT_ACTIONS:
            self._chat_action(path.rsplit("/", 1)[1])
        elif path == "/send":
            self._send()
        elif path == "/reply-preview":
            body = self._read_object()
            if body is not None:
                self._reply(*_reply_preview(body))
        elif path == "/outbound-note":
            self._outbound_note()
        elif path in ("/messages/trim-source", "/messages/trim-result"):
            self._model_trim(path.rsplit("-", 1)[1])
        else:
            self._reply(404, {"error": "not found"})

    def _decide(self, request_id: str, verb: str) -> None:
        try:
            if verb == "dismiss":
                entry, dismissed = SENDS.dismiss(request_id)
                if entry is not None and not dismissed and not entry.get("dismissed"):
                    self._reply(409, {
                        "error": "only a send that failed can be dismissed; this one is "
                                 + str(entry.get("status")), "status": entry.get("status")})
                    return
                if dismissed:
                    print(f"[mail-gateway] failed send {request_id} dismissed", flush=True)
            else:
                entry = _decide_send(request_id, verb)
        except OSError as exc:
            print(f"[mail-gateway] could not record the approval decision: {exc}", flush=True)
            self._reply(503, {"error": "could not record the decision; retry later"})
            return
        if entry is None:
            self._reply(404, {"error": "pending send not found"})
        else:
            self._reply(200, _public(entry))

    def _caller_waits(self) -> bool:
        """Whether the peer is still on the line for its answer. A caller
        that gave up (its own deadline passed) has closed the connection,
        and the socket then reads as at its end."""
        try:
            readable, _w, _x = select.select([self.connection], [], [], 0)
            return not readable or self.connection.recv(1, socket.MSG_PEEK) != b""
        except (OSError, ValueError):
            return False

    def _send(self) -> None:
        payload = self._read_object(MAX_SEND_BODY_BYTES)
        if payload is None:
            return
        try:
            status, answer = _accept_send(payload, self.headers.get("Host"),
                                          caller_waits=self._caller_waits)
        except Exception as exc:  # noqa: BLE001 - an answer, never a dropped connection
            print(f"[mail-gateway] /send failed: {exc}\n{traceback.format_exc()}", flush=True)
            status, answer = _send_unplanned(payload, self.headers.get("Host"), exc)
        try:
            self._reply(status, answer)
        except OSError as exc:
            # The send is what its entry says, whoever heard of it.
            print(f"[mail-gateway] the answer to a /send could not be delivered ({exc})",
                  flush=True)

    def _chat_action(self, action: str) -> None:
        """POST /chats/read|archive|delete — the mailbox follows the chat."""
        if action == "delete":
            # The separate capability, checked before anything is read, as on
            # every gateway: unset, the endpoint refuses.
            if not CHAT_ERASE_TOKEN:
                self._reply(503, {"error": "chat erasure is not configured "
                                           "(CHAT_ERASE_TOKEN is unset)"})
                return
            if not _same_secret((self.headers.get("X-Chat-Erase-Token") or "").strip(),
                                CHAT_ERASE_TOKEN):
                self._reply(403, {"error": "chat erasure needs X-Chat-Erase-Token"})
                return
        body = self._read_object(MAX_CHAT_ACTION_BYTES)
        if body is None:
            return
        chat = str(body.get("chat") or "").strip()
        if not chat:
            self._reply(400, {"error": "chat (the chat key) is required"})
            return
        # One mailbox per gateway: a thread of another account is not here to
        # be flagged, moved or erased, whatever key it has.
        if str(body.get("account") or "").strip().lower() != ACCOUNT or not ACCOUNT:
            self._reply(404, _NOT_OURS)
            return
        # What the caller counted: the ids of the mails the chat held when
        # the user read or archived it (Mirror.mark_read). Absent, the
        # thread as it stands; an empty list is "none of them".
        only = body.get("message_ids")
        if only is not None and action != "delete":
            if not isinstance(only, list) or not all(isinstance(i, str) for i in only):
                self._reply(400, {"error": "'message_ids' must be a list of Message-IDs"})
                return
            only = {i.strip() for i in only} | {
                _fit_id(i) for i in map(_threads.normalize_id, only) if i}
        else:
            only = None
        if MIRROR is None:
            self._reply(503, {"error": "the mail gateway is not configured"})
            return
        try:
            if action == "read":
                answer = {"ok": True, "seen": MIRROR.mark_read(chat, only)}
            elif action == "archive":
                answer = {"ok": True, **MIRROR.archive(chat, only)}
            else:
                answer = {"status": "deleted", **MIRROR.delete_chat(chat)}
        except Busy as exc:
            self._reply(503, {"error": str(exc)})
            return
        except MailboxError as exc:
            print(f"[mail-gateway] {action} of chat {chat!r} failed: {exc}", flush=True)
            self._reply(502, {"error": str(exc)[:500]})
            return
        except Exception as exc:  # noqa: BLE001 - an answer, never a dropped connection
            print(f"[mail-gateway] {action} of chat {chat!r} failed: {exc}\n"
                  f"{traceback.format_exc()}", flush=True)
            self._reply(500, {"error": f"{action} failed: {_clean_line(exc, 300)}"})
            return
        self._reply(200, answer)

    def _outbound_note(self) -> None:
        body = self._read_object()
        if body is None:
            return
        message_id = _threads.normalize_id(str(body.get("message_id") or ""))
        author = str(body.get("author") or "").strip().lower()
        if not message_id:
            self._reply(400, {"error": "message_id (a Message-ID) is required"})
            return
        if author not in _NOTE_AUTHORS:
            self._reply(400, {"error": "'author' must be one of " + "|".join(_NOTE_AUTHORS)})
            return
        # email_client.py says whose mailbox the mail left from; a note about
        # another account's mail is not this gateway's to keep.
        account = str(body.get("account") or "").strip().lower()
        if account and account != ACCOUNT:
            self._reply(404, _NOT_OURS)
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

    def _model_trim(self, half: str) -> None:
        """POST /messages/trim-source | /messages/trim-result — the model
        trim's two halves (Mirror.trim_source, Mirror.trim_result).

        The mail is named as on the other chat endpoints: its chat key, the
        account, which must be this gateway's own (else 404), and its
        Message-ID. A result is either ``keep`` — any value: whether it is a
        choice is apply_keep's to say, and one that is not settles the mail
        — or ``failed``, a short reason (``unparseable``, ``too_long``,
        ``gave_up`` for a mail whose turns all ended without a judgement:
        any reason settles it alike), never both."""
        body = self._read_object()
        if body is None:
            return
        chat, message_id = body.get("chat"), body.get("message_id")
        if (not isinstance(chat, str) or not chat.strip()
                or not isinstance(message_id, str) or not message_id.strip()):
            self._reply(400, {"error": "chat (the chat key) and message_id (a Message-ID) "
                                       "are required"})
            return
        if str(body.get("account") or "").strip().lower() != ACCOUNT or not ACCOUNT:
            self._reply(404, _NOT_OURS)
            return
        if half == "result":
            digest, failed = body.get("digest"), body.get("failed")
            if not isinstance(digest, str) or not digest:
                self._reply(400, {"error": "digest (the source's, from trim-source) is required"})
                return
            if ("keep" in body) == (failed is not None):
                self._reply(400, {"error": "exactly one of keep and failed is required"})
                return
            if failed is not None and (not isinstance(failed, str) or not failed.strip()):
                self._reply(400, {"error": "'failed' must be a short reason"})
                return
        if MIRROR is None:
            self._reply(503, {"error": "the mail gateway is not configured"})
            return
        try:
            if half == "source":
                status, answer = MIRROR.trim_source(chat.strip(), message_id)
            else:
                status, answer = MIRROR.trim_result(chat.strip(), message_id, digest,
                                                    keep=body.get("keep"), failed=failed)
        except TrimRefused as exc:
            status, answer = exc.status, exc.body
        except Busy as exc:
            status, answer = 503, {"error": str(exc)}
        except (sqlite3.Error, OSError) as exc:
            print(f"[mail-gateway] model trim ({half}) of {message_id[:200]!r} failed: {exc}",
                  flush=True)
            status, answer = 503, {"error": "the record could not be read or written; "
                                            "retry later"}
        except Exception as exc:  # noqa: BLE001 - an answer, never a dropped connection
            print(f"[mail-gateway] model trim ({half}) of {message_id[:200]!r} failed: {exc}\n"
                  f"{traceback.format_exc()}", flush=True)
            status, answer = 500, {"error": f"model trim failed: {_clean_line(exc, 300)}"}
        self._reply(status, answer)


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
        print(f"[mail-gateway] mirroring {ACCOUNT} (backfill {MAIL_BACKFILL_DAYS} days; "
              + (f"sending under send policy {_send_category()!r}" if SMTP_PROBLEM is None
                 else f"not sending: {SMTP_PROBLEM}") + ")", flush=True)
        if not _chats.chats_enabled():
            print("[mail-gateway] CHATS_INGEST_URL is not set: mail is mirrored into the "
                  "ledger and offered to nobody — no chat is told, nothing rings, no turn "
                  "is started", flush=True)
        if not _news.news_enabled():
            print("[mail-gateway] NEWS_INGEST_URL is not set: mail of a news sender or list "
                  "is mirrored like any other, since no feed can take it", flush=True)
        # Before the loop's first turn: what the last stop left of a send.
        _recover_sends()
        threading.Thread(target=_mirror_loop, args=(MIRROR,), name="mail-mirror",
                         daemon=True).start()
    _serve_http()


if __name__ == "__main__":
    if sys.argv[1:2] == [READER_FLAG]:
        _serve_reader(int(sys.argv[2]))
    else:
        main()
