#!/usr/bin/env python3
"""Provider-independent IMAP+SMTP e-mail client for the Secretary agent.

Implements issue #35: full read/search/download/move/flag/send/draft/forward
workflow with attachment support, using only the Python standard library
(imaplib + smtplib + email) so it runs in the baked image without extra deps.

Configuration comes from the environment so no credentials live in the repo.
A *default* account uses the unsuffixed variables; *named* accounts append an
upper-cased suffix (e.g. ``--account ari`` -> ``EMAIL_USER_ARI``). A named
account must fully define its own identity: there is **no** fallback to the
unsuffixed (default-account) variables. Selecting an account whose
``EMAIL_USER_<NAME>`` is unset fails with "no such account"; a partially
configured one fails with "incomplete account data". This stops a missing
suffixed value from silently routing a named account to the default mailbox.

For per-account configuration, define credentials in the system-wide ``.env``
file using an upper-cased suffix (e.g. ``EMAIL_USER_ARI``) and select the account
with ``--account NAME``.  All such credentials are covered by the web-gateway
credential isolation.  Avoid ``--env-file`` / ``EMAIL_ENV_FILE``: a file loaded
that way is readable by the agent, bypasses the isolation, and gives it direct
SMTP/IMAP access — circumventing the send-control policy.

    EMAIL_USER        full address / login
    EMAIL_FROM_NAME   optional display name (e.g. "Ari der Allerbeste")
    EMAIL_PASS        app-password (never the normal account password)
    IMAP_HOST         e.g. imap.gmail.com / imap.zoho.eu
    IMAP_PORT         default 993 (implicit TLS)
    SMTP_HOST         e.g. smtp.gmail.com / smtpout-mail.zoho.eu
    SMTP_PORT         default 587 (STARTTLS)
    SENT_FOLDER       IMAP folder for the Sent copy (default "Sent";
                      Gmail: "[Gmail]/Sent Mail")
    DRAFTS_FOLDER     IMAP folder for drafts (default "Drafts";
                      Gmail: "[Gmail]/Drafts")
    SMTP_SAVE_SENT    "true"/"false" — append a Sent copy after sending
                      (default true; set false for Gmail, which saves it itself)

Every command prints JSON to stdout. Errors print {"error": ...} and exit 1.

Examples
--------
    email_client.py list --folder INBOX --limit 20
    email_client.py search --folder INBOX --from schaerer --subject rezept
    email_client.py read --uid 1234 --folder INBOX
    email_client.py fetch-attachment --uid 1234 --part 1 --out /tmp/rezept.pdf
    email_client.py move --uid 1234 --from INBOX --to "Archiv/Apotheke"
    email_client.py flag --uid 1234 --folder INBOX --read
    email_client.py send --to a@b.ch --subject Hallo --body "..." --attach /tmp/x.pdf
    email_client.py draft --to a@b.ch --subject Hallo --body "..." --attach /tmp/x.pdf
    email_client.py forward --uid 1234 --folder INBOX --to a@b.ch --prepend "FYI"
    email_client.py --account ari list --folder INBOX
"""

import argparse
import base64
import email
import email.header
import email.policy
import imaplib
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from email.utils import (formataddr, getaddresses, make_msgid, parseaddr,
                         parsedate_to_datetime)

# imaplib caps literals at 10 kB by default; raise it for large attachments.
imaplib._MAXLINE = 100 * 1024 * 1024


class EmailError(Exception):
    """Raised for any recoverable e-mail error.

    The CLI entry point turns it into a ``{"error": ...}`` JSON line and exit 1,
    while importers (e.g. the web gateway) can catch it instead of the process
    being torn down by ``sys.exit``.
    """


def die(msg):
    raise EmailError(str(msg))


# --------------------------------------------------------------------------- #
# backend proxy (keeps SMTP/IMAP credentials out of the agent's environment)
# --------------------------------------------------------------------------- #
# When EMAIL_BACKEND_URL is set, this process holds no mailbox credentials and
# instead forwards the whole invocation to the privileged web gateway, which
# runs email_client.py with the credentials in *its* environment and returns the
# result. This means the agent can never read EMAIL_PASS* nor bypass the
# send-control policy by talking to SMTP/IMAP directly. The gateway runs the
# real command with EMAIL_BACKEND_URL stripped, so there is no proxy loop.
def _proxy_to_backend(url, argv):
    """Forward argv to the e-mail backend; mirror its stdout/stderr/exit code."""
    token = os.environ.get("EMAIL_BACKEND_TOKEN", "")
    payload = json.dumps({"argv": list(argv)}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json",
                 "X-Email-Backend-Token": token},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace").strip()[:500]
        print(json.dumps({"error": f"email backend returned HTTP {e.code}: {body}"}))
        return 1
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(json.dumps({"error": f"email backend unreachable: {e}"}))
        return 1
    out = data.get("stdout", "")
    if out:
        sys.stdout.write(out)
    err = data.get("stderr", "")
    if err:
        sys.stderr.write(err)
    return int(data.get("exit", 0))


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
def load_env_file(path):
    """Load KEY=VALUE lines from a dotenv-style file into os.environ.

    Existing environment variables are never overwritten, so an explicit env
    always wins over the file. Supports comments (#), blank lines, an optional
    `export ` prefix, and single/double-quoted values. Intended for a
    project-scoped, gitignored secrets file.
    """
    if not os.path.isfile(path):
        die(f"env-file not found: {path}")
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                if (len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'"):
                    val = val[1:-1]
                if key and key not in os.environ:
                    os.environ[key] = val
    except OSError as e:
        die(f"cannot read env-file {path}: {e}")


class Config:
    def __init__(self, account):
        self.account = account

        # A named account must fully define its own identity. We deliberately do
        # NOT fall back to the unsuffixed (default-account) variables: a silent
        # fallback let ``--account ari`` connect to the default mailbox whenever
        # a suffixed value was missing (e.g. EMAIL_USER_ARI unset -> EMAIL_USER),
        # which is an identity mix-up, not a convenience. Missing per-account
        # data fails loudly instead.
        if account and os.environ.get(f"EMAIL_USER_{account.upper()}") is None:
            die(f"no such account {account!r}: "
                f"EMAIL_USER_{account.upper()} is not set")

        def cfg(key, default=None, required=False):
            if account:
                val = os.environ.get(f"{key}_{account.upper()}")
            else:
                val = os.environ.get(key)
            if val is None:
                val = default
            if required and not val:
                name = f"{key}_{account.upper()}" if account else key
                if account:
                    die(f"incomplete account data for {account!r}: "
                        f"required variable {name} is not set")
                die(f"missing required environment variable {name}")
            return val

        self.user = cfg("EMAIL_USER", required=True)
        self.from_name = cfg("EMAIL_FROM_NAME")
        self.password = cfg("EMAIL_PASS", required=True)
        self.imap_host = cfg("IMAP_HOST", required=True)
        self.imap_port = int(cfg("IMAP_PORT", "993"))
        self.smtp_host = cfg("SMTP_HOST", required=True)
        self.smtp_port = int(cfg("SMTP_PORT", "587"))
        self.sent_folder = cfg("SENT_FOLDER", "Sent")
        self.drafts_folder = cfg("DRAFTS_FOLDER", "Drafts")
        self.save_sent = cfg("SMTP_SAVE_SENT", "true").lower() in ("1", "true", "yes")


# --------------------------------------------------------------------------- #
# IMAP helpers
# --------------------------------------------------------------------------- #
def imap_connect(cfg):
    ctx = ssl.create_default_context()
    M = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, ssl_context=ctx)
    try:
        M.login(cfg.user, cfg.password)
    except imaplib.IMAP4.error as e:
        die(f"IMAP login failed: {e}")
    return M


def imap_utf7_encode(s):
    """Encode a folder name to IMAP modified UTF-7 (RFC 3501 §5.1.3).

    ASCII (0x20-0x7e) passes through unchanged except '&' -> '&-'; other runs
    are base64(UTF-16BE) with '/' -> ',' wrapped in '&...-'. Needed so folders
    like "[Gmail]/Entwürfe" work in SELECT/APPEND/COPY.
    """
    out = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if 0x20 <= ord(ch) <= 0x7e:
            out.append("&-" if ch == "&" else ch)
            i += 1
        else:
            j = i
            while j < n and not 0x20 <= ord(s[j]) <= 0x7e:
                j += 1
            b = base64.b64encode(s[i:j].encode("utf-16-be")).decode("ascii").rstrip("=")
            out.append("&" + b.replace("/", ",") + "-")
            i = j
    return "".join(out)


def imap_utf7_decode(s):
    """Decode an IMAP modified UTF-7 folder name back to a normal str."""
    out = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch == "&":
            j = s.find("-", i)
            if j == -1:
                j = n
            chunk = s[i + 1:j]
            if chunk == "":
                out.append("&")
            else:
                b = chunk.replace(",", "/")
                b += "=" * ((-len(b)) % 4)
                out.append(base64.b64decode(b).decode("utf-16-be"))
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _quote(folder):
    return '"%s"' % imap_utf7_encode(folder).replace('"', '\\"')


def imap_select(M, folder, readonly=False):
    typ, data = M.select(_quote(folder), readonly=readonly)
    if typ != "OK":
        die(f"cannot select folder {folder!r}: {data}")


def _decode(value):
    """Decode RFC2047-encoded header to a plain str."""
    if value is None:
        return None
    parts = email.header.decode_header(value)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def _parse_message(raw):
    return email.message_from_bytes(raw, policy=email.policy.default)


def _iter_attachments(msg):
    """Yield (index, part) for each attachment, numbered from 1."""
    idx = 0
    for part in msg.walk():
        if part.is_multipart():
            continue
        disp = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        if disp == "attachment" or (filename and disp != "inline"):
            idx += 1
            yield idx, part


def _body_text(msg):
    """Best-effort plain-text body."""
    if msg.is_multipart():
        plain = None
        html = None
        for part in msg.walk():
            if part.is_multipart():
                continue
            if part.get_content_disposition() == "attachment":
                continue
            ctype = part.get_content_type()
            if ctype == "text/plain" and plain is None:
                plain = part.get_content()
            elif ctype == "text/html" and html is None:
                html = part.get_content()
        if plain is not None:
            return plain
        if html is not None:
            return html
        return ""
    try:
        return msg.get_content()
    except Exception:
        return msg.get_payload(decode=True).decode("utf-8", errors="replace")


def _summary(M, uid):
    typ, data = M.uid(
        "fetch", uid,
        "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)] FLAGS)")
    if typ != "OK" or not data or data[0] is None:
        return None
    header_bytes = b""
    flags = ()
    for item in data:
        if isinstance(item, tuple):
            header_bytes = item[1]
            flags = imaplib.ParseFlags(item[0]) if item[0] else ()
    hdr = _parse_message(header_bytes)
    date = hdr.get("Date")
    try:
        iso = parsedate_to_datetime(date).isoformat() if date else None
    except Exception:
        iso = date
    return {
        "uid": uid.decode() if isinstance(uid, bytes) else str(uid),
        "from": _decode(hdr.get("From")),
        "to": _decode(hdr.get("To")),
        "subject": _decode(hdr.get("Subject")),
        "date": iso,
        # Carried in every listing so a caller can key handled-state on the
        # Message-ID without a second round-trip per message.
        "message_id": (hdr.get("Message-ID") or "").strip() or None,
        "flags": [f.decode() if isinstance(f, bytes) else f for f in flags],
        "unread": b"\\Seen" not in flags,
    }


# --------------------------------------------------------------------------- #
# thread / answered: the mailbox is the record of what was sent
# --------------------------------------------------------------------------- #
# A reply carries the Message-ID it answers in In-Reply-To, and the whole chain
# in References. So "have I already answered this?" is a server-side IMAP SEARCH
# over the Sent folder -- not a fact that needs to be mirrored into a separate
# store that can be lost, reset, or never populated.
def _quote_mid(message_id):
    """An IMAP quoted-string for a Message-ID (angle brackets are fine inside)."""
    mid = message_id.strip()
    if mid.startswith("<") and mid.endswith(">"):
        mid = mid[1:-1]  # match on the bare id: servers differ on the brackets
    return '"%s"' % mid.replace("\\", "").replace('"', "")


def _search_replies_to(M, message_id):
    """UIDs in the selected folder whose In-Reply-To or References cite *message_id*."""
    q = _quote_mid(message_id)
    try:
        typ, data = M.uid("search", None,
                          "OR",
                          "HEADER", "In-Reply-To", q,
                          "HEADER", "References", q)
    except imaplib.IMAP4.error as e:
        die(f"search failed: {e}")
    if typ != "OK":
        die(f"search failed: {data}")
    return data[0].split()


def _search_by_message_id(M, message_id):
    q = _quote_mid(message_id)
    try:
        typ, data = M.uid("search", None, "HEADER", "Message-Id", q)
    except imaplib.IMAP4.error:
        return []
    return data[0].split() if typ == "OK" else []


_RE_PREFIX = re.compile(r"^\s*(re|aw|fwd?|wg)\s*(\[\d+\])?\s*:\s*", re.I)
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _base_subject(subject):
    """Strip any number of Re:/Fwd:/AW: prefixes, for comparing two subjects."""
    s = (subject or "").strip()
    while True:
        stripped = _RE_PREFIX.sub("", s, count=1)
        if stripped == s:
            return s.casefold()
        s = stripped


def _imap_date(iso):
    """IMAP SINCE wants DD-Mon-YYYY in English, independent of the C locale."""
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    return f"{dt.day:02d}-{_MONTHS[dt.month - 1]}-{dt.year}"


def _search_sent_to(M, address, since_iso):
    """UIDs of messages sent TO *address* on or after *since_iso*.

    The second dedup signal, and the one that actually matters: Ari's runaway
    replies to Mara carried no In-Reply-To at all, so a header-only test
    declares her message unanswered and would answer it an 82nd time. A reply
    that does not thread is still a reply.
    """
    criteria = ["TO", '"%s"' % address.replace('"', "")]
    day = _imap_date(since_iso) if since_iso else None
    if day:
        criteria += ["SINCE", day]
    try:
        typ, data = M.uid("search", None, *criteria)
    except imaplib.IMAP4.error:
        return []
    return data[0].split() if typ == "OK" else []


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_list(cfg, args):
    M = imap_connect(cfg)
    imap_select(M, args.folder, readonly=True)
    typ, data = M.uid("search", None, "ALL")
    if typ != "OK":
        die(f"search failed: {data}")
    uids = data[0].split()
    uids = uids[-args.limit:][::-1]  # newest first
    messages = [s for u in uids if (s := _summary(M, u))]
    M.logout()
    print(json.dumps({"folder": args.folder, "count": len(messages), "messages": messages}, ensure_ascii=False, indent=2))


def cmd_search(cfg, args):
    M = imap_connect(cfg)
    imap_select(M, args.folder, readonly=True)
    criteria = []
    if args.from_:
        criteria += ["FROM", args.from_]
    if args.subject:
        criteria += ["SUBJECT", args.subject]
    if args.text:
        criteria += ["TEXT", args.text]
    if args.since:
        criteria += ["SINCE", args.since]  # DD-Mon-YYYY
    if args.unseen:
        criteria += ["UNSEEN"]
    if not criteria:
        criteria = ["ALL"]
    # IMAP SEARCH only needs a CHARSET when a criterion carries non-ASCII text.
    # For ASCII criteria (UNSEEN, dates, ASCII FROM/SUBJECT) sending a charset is
    # unnecessary and some servers reject the (correctly-formed) CHARSET clause,
    # so only add it when actually required. Note: M.uid() raises IMAP4.error on a
    # BAD/NO response, so a "typ != OK" check alone never catches a parse error.
    needs_charset = any(isinstance(c, str) and not c.isascii() for c in criteria)
    try:
        if needs_charset:
            encoded = [c.encode("utf-8") if isinstance(c, str) else c for c in criteria]
            typ, data = M.uid("search", "CHARSET", "UTF-8", *encoded)
        else:
            typ, data = M.uid("search", None, *criteria)
    except imaplib.IMAP4.error:
        # Fall back to a plain ASCII search if the server rejects the charset.
        typ, data = M.uid("search", None, *criteria)
    if typ != "OK":
        die(f"search failed: {data}")
    uids = data[0].split()
    uids = uids[-args.limit:][::-1]
    messages = [s for u in uids if (s := _summary(M, u))]
    M.logout()
    print(json.dumps({"folder": args.folder, "count": len(messages), "messages": messages}, ensure_ascii=False, indent=2))


def cmd_answered(cfg, args):
    """Has a reply to this message already been sent? Ask the Sent folder.

    Two independent signals, either of which counts as answered:
      threaded  -- a sent message cites this Message-ID in In-Reply-To/References
      untracked -- a sent message went to the same correspondent, on or after the
                   message's date, under the same base subject (Re: stripped)

    Exit 0 = answered, do not answer again.
    Exit 3 = no trace of a reply -- the message is genuinely unanswered.
    Exit 1 = error: the state is unknown, so the caller must not send.
    """
    sent = args.folder or cfg.sent_folder
    M = imap_connect(cfg)

    # The anchor gives us the correspondent and the date; without it only the
    # header test can run.
    imap_select(M, args.in_folder, readonly=True)
    anchor = next((s for u in _search_by_message_id(M, args.message_id)
                   if (s := _summary(M, u))), None)

    imap_select(M, sent, readonly=True)
    threaded = [s for u in _search_replies_to(M, args.message_id)
                if (s := _summary(M, u))]

    untracked = []
    if anchor:
        addr = parseaddr(anchor.get("from") or "")[1]
        base = _base_subject(anchor.get("subject"))
        if addr:
            seen = {m["uid"] for m in threaded}
            for uid in _search_sent_to(M, addr, anchor.get("date")):
                s = _summary(M, uid)
                if s and s["uid"] not in seen and _base_subject(s.get("subject")) == base:
                    untracked.append(s)
    M.logout()

    replies = sorted(threaded + untracked, key=lambda m: m.get("date") or "")
    basis = ([f"threaded ({len(threaded)})"] if threaded else []) + \
            ([f"untracked ({len(untracked)})"] if untracked else [])
    print(json.dumps({
        "message_id": args.message_id,
        "folder": sent,
        "answered": bool(replies),
        "basis": basis or ["none"],
        "reply_count": len(replies),
        "replies": replies,
    }, ensure_ascii=False, indent=2))
    return 0 if replies else 3


def cmd_thread(cfg, args):
    """The whole conversation around a message: what they wrote, what we sent.

    Walks the References chain of the anchor message and searches both INBOX and
    the Sent folder for every message citing any id in that chain, so the caller
    can read the exchange before composing a reply instead of reacting to the
    latest message in isolation.
    """
    folders = args.folders or ["INBOX", cfg.sent_folder]
    M = imap_connect(cfg)

    chain = {args.message_id.strip()}
    for folder in folders:  # the anchor's own References extend the chain
        imap_select(M, folder, readonly=True)
        for uid in _search_by_message_id(M, args.message_id):
            typ, data = M.uid("fetch", uid,
                              "(BODY.PEEK[HEADER.FIELDS (REFERENCES IN-REPLY-TO)])")
            if typ != "OK" or not data or data[0] is None:
                continue
            for item in data:
                if isinstance(item, tuple):
                    hdr = _parse_message(item[1])
                    chain.update((hdr.get("References") or "").split())
                    chain.update((hdr.get("In-Reply-To") or "").split())

    found = {}  # message_id -> summary (+folder), deduped across folders
    for folder in folders:
        imap_select(M, folder, readonly=True)
        uids = set()
        for mid in chain:
            uids.update(_search_replies_to(M, mid))
            uids.update(_search_by_message_id(M, mid))
        for uid in uids:
            summary = _summary(M, uid)
            if summary:
                found.setdefault(summary["message_id"] or f"{folder}:{uid}",
                                 {**summary, "folder": folder})

    messages = sorted(found.values(), key=lambda m: m.get("date") or "")
    # Long threads: only the tail carries bodies. The rest stays as headers, so
    # recalling a 40-message history costs a listing, not 40 downloads.
    tail = messages[-args.limit:] if args.limit else messages
    for msg in tail:
        imap_select(M, msg["folder"], readonly=True)
        typ, data = M.uid("fetch", msg["uid"], "(BODY.PEEK[])")
        if typ != "OK" or not data or data[0] is None:
            continue
        raw = next((i[1] for i in data if isinstance(i, tuple)), b"")
        body = _body_text(_parse_message(raw)).strip()
        msg["body"] = body[:args.chars] + ("…" if len(body) > args.chars else "")
    M.logout()

    print(json.dumps({
        "anchor": args.message_id,
        "folders": folders,
        "count": len(messages),
        "bodies_for_last": len(tail),
        "messages": messages,
    }, ensure_ascii=False, indent=2))


def cmd_read(cfg, args):
    M = imap_connect(cfg)
    imap_select(M, args.folder, readonly=True)
    typ, data = M.uid("fetch", str(args.uid), "(RFC822 FLAGS)")
    if typ != "OK" or not data or data[0] is None:
        die(f"message uid {args.uid} not found in {args.folder}")
    raw = b""
    flags = ()
    for item in data:
        if isinstance(item, tuple):
            raw = item[1]
            flags = imaplib.ParseFlags(item[0]) if item[0] else ()
    msg = _parse_message(raw)
    attachments = []
    for idx, part in _iter_attachments(msg):
        payload = part.get_payload(decode=True) or b""
        attachments.append({
            "part": idx,
            "filename": _decode(part.get_filename()),
            "content_type": part.get_content_type(),
            "size": len(payload),
        })
    date = msg.get("Date")
    try:
        iso = parsedate_to_datetime(date).isoformat() if date else None
    except Exception:
        iso = date
    out = {
        "uid": str(args.uid),
        "folder": args.folder,
        "from": _decode(msg.get("From")),
        "to": _decode(msg.get("To")),
        "cc": _decode(msg.get("Cc")),
        "subject": _decode(msg.get("Subject")),
        "date": iso,
        "message_id": msg.get("Message-ID"),
        "flags": [f.decode() if isinstance(f, bytes) else f for f in flags],
        "body": _body_text(msg),
        "attachments": attachments,
    }
    M.logout()
    print(json.dumps(out, ensure_ascii=False, indent=2))


def cmd_fetch_attachment(cfg, args):
    M = imap_connect(cfg)
    imap_select(M, args.folder, readonly=True)
    typ, data = M.uid("fetch", str(args.uid), "(RFC822)")
    if typ != "OK" or not data or data[0] is None:
        die(f"message uid {args.uid} not found in {args.folder}")
    raw = next(item[1] for item in data if isinstance(item, tuple))
    msg = _parse_message(raw)
    target = None
    for idx, part in _iter_attachments(msg):
        if idx == args.part:
            target = part
            break
    if target is None:
        die(f"attachment part {args.part} not found on uid {args.uid}")
    payload = target.get_payload(decode=True) or b""
    out_path = args.out or _decode(target.get_filename()) or f"attachment-{args.part}"
    with open(out_path, "wb") as fh:
        fh.write(payload)
    M.logout()
    print(json.dumps({
        "saved": out_path,
        "filename": _decode(target.get_filename()),
        "content_type": target.get_content_type(),
        "size": len(payload),
    }, ensure_ascii=False))


def cmd_move(cfg, args):
    M = imap_connect(cfg)
    imap_select(M, args.from_)
    uid = str(args.uid)
    # Prefer MOVE (RFC 6851); fall back to COPY+delete.
    try:
        typ, data = M.uid("move", uid, _quote(args.to))
        if typ != "OK":
            raise imaplib.IMAP4.error(data)
        moved_via = "MOVE"
    except imaplib.IMAP4.error:
        typ, data = M.uid("copy", uid, _quote(args.to))
        if typ != "OK":
            die(f"copy to {args.to} failed: {data}")
        M.uid("store", uid, "+FLAGS", "(\\Deleted)")
        M.expunge()
        moved_via = "COPY+EXPUNGE"
    M.logout()
    print(json.dumps({"moved": uid, "from": args.from_, "to": args.to, "method": moved_via}))


def cmd_flag(cfg, args):
    if args.read == args.unread:
        die("specify exactly one of --read / --unread")
    M = imap_connect(cfg)
    imap_select(M, args.folder)
    op = "+FLAGS" if args.read else "-FLAGS"
    typ, data = M.uid("store", str(args.uid), op, "(\\Seen)")
    if typ != "OK":
        die(f"flag update failed: {data}")
    M.logout()
    print(json.dumps({"uid": str(args.uid), "folder": args.folder, "read": bool(args.read)}))


# --------------------------------------------------------------------------- #
# message construction / sending
# --------------------------------------------------------------------------- #
def _build_message(cfg, to, subject, body, cc=None, bcc=None, attachments=None,
                   in_reply_to=None, references=None):
    msg = EmailMessage()
    msg["From"] = formataddr((cfg.from_name, cfg.user)) if cfg.from_name else cfg.user
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject or ""
    if "Message-Id" not in msg:
        msg["Message-Id"] = make_msgid()
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body or "")
    for path in attachments or []:
        if not os.path.isfile(path):
            die(f"attachment not found: {path}")
        with open(path, "rb") as fh:
            data = fh.read()
        import mimetypes
        ctype, _ = mimetypes.guess_type(path)
        maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype,
                            filename=os.path.basename(path))
    return msg


def _smtp_send(cfg, msg, recipients):
    ctx = ssl.create_default_context()
    try:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as s:
            s.ehlo()
            s.starttls(context=ctx)
            s.ehlo()
            s.login(cfg.user, cfg.password)
            s.send_message(msg, from_addr=cfg.user, to_addrs=recipients)
    except smtplib.SMTPException as e:
        die(f"SMTP send failed: {e}")


_APPENDUID_RE = re.compile(rb"APPENDUID\s+\d+\s+(\d+)")


def _append(cfg, folder, msg, seen=True):
    """Append a message to *folder*; return its IMAP UID (as str) when known."""
    M = imap_connect(cfg)
    flags = "(\\Seen)" if seen else "()"
    try:
        typ, data = M.append(_quote(folder), flags,
                             imaplib.Time2Internaldate(datetime.now().timestamp()),
                             msg.as_bytes())
    except imaplib.IMAP4.error as e:
        M.logout()
        die(f"append to {folder} failed: {e}")
    uid = _appenduid_from_response(data)
    if uid is None:
        uid = _uid_by_message_id(M, folder, msg.get("Message-Id"))
    M.logout()
    return uid


def _appenduid_from_response(data):
    """Extract the assigned UID from an APPEND response (RFC 4315 UIDPLUS)."""
    for item in data or []:
        if isinstance(item, (bytes, bytearray)):
            m = _APPENDUID_RE.search(item)
            if m:
                return m.group(1).decode()
    return None


def _uid_by_message_id(M, folder, message_id):
    """Fallback UID lookup by Message-Id for servers without UIDPLUS."""
    if not message_id:
        return None
    try:
        imap_select(M, folder, readonly=True)
        typ, data = M.uid("search", None, "HEADER", "Message-Id", message_id)
    except imaplib.IMAP4.error:
        return None
    if typ != "OK" or not data or not data[0]:
        return None
    uids = data[0].split()
    return uids[-1].decode() if uids else None


# --------------------------------------------------------------------------- #
# send-control policy (sender-address based) + pending-request store (Drafts)
# --------------------------------------------------------------------------- #
# Every outgoing e-mail is governed by the *control category* of its sender
# address, configured as a JSON array in EMAIL_SEND_POLICY:
#
#   EMAIL_SEND_POLICY=[
#     {"address": "reto@example.com", "category": "verify"},
#     {"address": "ari@example.com",  "category": "allow", "account": "ari"},
#     {"address": "*",                "category": "trust"}
#   ]
#
#   verify — never sent directly; registered as a pending request (a draft) that
#            the user approves on the web gateway. Approval happens *only* via
#            the web interface, never from the CLI (which holds no such command),
#            so an agent cannot approve its own pending sends.
#   trust  — sent directly only when the model passes --user-approved; otherwise
#            it falls back to the verify flow.
#   allow  — sent directly, no confirmation (e.g. Ari's own mailbox).
#
# Addresses not listed fall back to the "*" wildcard entry, or — absent that — to
# DEFAULT_SEND_CATEGORY ("verify", the fail-safe choice).
#
# A pending request is simply a draft in the Drafts folder, so it survives
# restarts server-side and the approval page is just a view onto Drafts. Every
# non-deleted draft is treated as a pending send request, and its IMAP UID is
# used directly as the request id (custom-header IMAP SEARCH is unreliable on
# some servers, e.g. Zoho, which does not index custom headers). The resolved
# category is still recorded as an informational header for display:
#   X-Send-Request-Category  the resolved category at registration time
VALID_CATEGORIES = ("verify", "trust", "allow")
DEFAULT_SEND_CATEGORY = "verify"
REQUEST_CATEGORY_HEADER = "X-Send-Request-Category"
_REQUEST_HEADERS = (REQUEST_CATEGORY_HEADER,)


def load_send_policy():
    """Parse EMAIL_SEND_POLICY (a JSON array) into a list of policy entries."""
    raw = os.environ.get("EMAIL_SEND_POLICY", "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        die(f"invalid EMAIL_SEND_POLICY JSON: {e}")
    if not isinstance(data, list):
        die("EMAIL_SEND_POLICY must be a JSON array")
    policy = []
    for entry in data:
        if not isinstance(entry, dict):
            die("EMAIL_SEND_POLICY entries must be JSON objects")
        address = str(entry.get("address", "")).strip()
        category = str(entry.get("category", "")).strip().lower()
        account = entry.get("account")
        account = str(account).strip() if account not in (None, "") else None
        if not address or category not in VALID_CATEGORIES:
            die(f"EMAIL_SEND_POLICY entry needs 'address' and a 'category' in {VALID_CATEGORIES}")
        policy.append({"address": address, "category": category, "account": account})
    return policy


def resolve_category(address, policy=None):
    """Resolve the control category for a sender address."""
    if policy is None:
        policy = load_send_policy()
    wildcard = None
    for entry in policy:
        if entry["address"] == "*":
            wildcard = entry["category"]
            continue
        if entry["address"].lower() == (address or "").lower():
            return entry["category"]
    return wildcard or DEFAULT_SEND_CATEGORY


def policy_accounts(policy=None):
    """Accounts (suffix or None) that may hold pending sends and need scanning.

    The default account (``None``) is always included: unlisted addresses fall
    back to ``verify``/wildcard, so pending drafts can accrue there even with an
    empty policy. ``allow`` accounts never create pending drafts, so scanning
    them is harmless; they are only skipped to avoid needless IMAP connections.
    """
    if policy is None:
        policy = load_send_policy()
    accounts = [None]
    seen = {"default"}
    for entry in policy:
        if entry["category"] == "allow":
            continue
        acc = entry["account"]
        key = acc or "default"
        if key not in seen:
            seen.add(key)
            accounts.append(acc)
    return accounts


def approval_url(account, request_id):
    """Public URL of the approval page for a pending request (relative if no base)."""
    base = (os.environ.get("SEND_APPROVAL_BASE_URL")
            or os.environ.get("CONVERSATION_BASE_URL", "")).rstrip("/")
    path = f"/sends/{account or 'default'}/{request_id}"
    return (base + path) if base else path


def _recipients_of(msg):
    """All recipient addresses across To/Cc/Bcc of a parsed message."""
    addrs = []
    for field in ("To", "Cc", "Bcc"):
        values = [str(v) for v in msg.get_all(field, [])]
        addrs += [a for _, a in getaddresses(values) if a]
    return addrs


def register_pending_send(cfg, msg, category):
    """Save a message to Drafts as a pending send request.

    The draft's IMAP UID is returned and used as the request id; only the
    category is recorded (as a header) for informational display.
    """
    msg[REQUEST_CATEGORY_HEADER] = category
    uid = _append(cfg, cfg.drafts_folder, msg, seen=False)
    if uid is None:
        die("saved pending draft but could not determine its IMAP UID "
            "(server lacks UIDPLUS and Message-Id lookup failed)")
    return uid


def _find_pending_uid(M, request_id):
    """The request id *is* the IMAP UID; return it if the draft still exists."""
    if not request_id:
        return None
    try:
        typ, data = M.uid("fetch", request_id, "(UID)")
    except imaplib.IMAP4.error:
        return None
    if typ != "OK" or not data or data[0] is None:
        return None
    return request_id


def list_pending_sends(cfg):
    """Return metadata for every pending send request (non-deleted Drafts)."""
    M = imap_connect(cfg)
    try:
        imap_select(M, cfg.drafts_folder, readonly=True)
        typ, data = M.uid("search", None, "NOT", "DELETED")
        if typ != "OK":
            die(f"search for pending sends failed: {data}")
        out = []
        for uid in data[0].split():
            uid = uid.decode() if isinstance(uid, (bytes, bytearray)) else uid
            fields = f"FROM TO CC SUBJECT DATE {REQUEST_CATEGORY_HEADER}"
            typ, fdata = M.uid("fetch", uid,
                               f"(BODY.PEEK[HEADER.FIELDS ({fields})])")
            if typ != "OK" or not fdata or fdata[0] is None:
                continue
            header_bytes = next((it[1] for it in fdata if isinstance(it, tuple)), b"")
            hdr = _parse_message(header_bytes)
            out.append({
                "request_id": uid,
                "category": (hdr.get(REQUEST_CATEGORY_HEADER) or "").strip(),
                "from": _decode(hdr.get("From")),
                "to": _decode(hdr.get("To")),
                "cc": _decode(hdr.get("Cc")),
                "subject": _decode(hdr.get("Subject")),
                "date": _decode(hdr.get("Date")),
            })
        return out
    finally:
        M.logout()


def get_pending_send(cfg, request_id):
    """Return full details (incl. body) for one pending send request, or None."""
    M = imap_connect(cfg)
    try:
        imap_select(M, cfg.drafts_folder, readonly=True)
        uid = _find_pending_uid(M, request_id)
        if uid is None:
            return None
        typ, data = M.uid("fetch", uid, "(RFC822)")
        if typ != "OK" or not data or data[0] is None:
            return None
        raw = next(item[1] for item in data if isinstance(item, tuple))
        msg = _parse_message(raw)
        return {
            "request_id": request_id,
            "category": (msg.get(REQUEST_CATEGORY_HEADER) or "").strip(),
            "from": _decode(msg.get("From")),
            "to": _decode(msg.get("To")),
            "cc": _decode(msg.get("Cc")),
            "bcc": _decode(msg.get("Bcc")),
            "subject": _decode(msg.get("Subject")),
            "date": _decode(msg.get("Date")),
            "body": _body_text(msg),
            "attachments": [
                _decode(part.get_filename()) for _, part in _iter_attachments(msg)
            ],
        }
    finally:
        M.logout()


def approve_pending_send(cfg, request_id):
    """Send a pending draft (stripping the request metadata) and remove it.

    Intentionally *not* exposed as a CLI subcommand: approval is performed only
    by the web gateway, so an agent running the CLI cannot approve a send.
    """
    M = imap_connect(cfg)
    try:
        imap_select(M, cfg.drafts_folder)
        uid = _find_pending_uid(M, request_id)
        if uid is None:
            die(f"no pending send request {request_id}")
        typ, data = M.uid("fetch", uid, "(RFC822)")
        if typ != "OK" or not data or data[0] is None:
            die(f"cannot fetch pending send request {request_id}")
        raw = next(item[1] for item in data if isinstance(item, tuple))
        msg = _parse_message(raw)
        recipients = _recipients_of(msg)
        if not recipients:
            die(f"pending send request {request_id} has no recipients")
        for h in _REQUEST_HEADERS:
            del msg[h]
        del msg["Bcc"]  # never expose Bcc in the dispatched / stored copy
        _smtp_send(cfg, msg, recipients)
        if cfg.save_sent:
            _append(cfg, cfg.sent_folder, msg, seen=True)
        M.uid("store", uid, "+FLAGS", "(\\Deleted)")
        M.expunge()
        return {"approved": request_id, "sent": True, "to": recipients,
                "subject": _decode(msg.get("Subject")), "saved_to_sent": cfg.save_sent}
    finally:
        M.logout()


def delete_pending_draft(cfg, request_id):
    """Delete a pending draft without sending it (retract / reject)."""
    M = imap_connect(cfg)
    try:
        imap_select(M, cfg.drafts_folder)
        uid = _find_pending_uid(M, request_id)
        if uid is None:
            die(f"no pending send request {request_id}")
        M.uid("store", uid, "+FLAGS", "(\\Deleted)")
        M.expunge()
        return request_id
    finally:
        M.logout()


def _dispatch_message(cfg, msg, to, cc=None, bcc=None, attachments=None,
                      account=None, user_approved=False, extra=None):
    """Send *msg* through the send-control policy and return a result dict.

    This is the single choke point for outgoing mail: `allow` (and `trust`
    with --user-approved) send directly; everything else queues a pending
    request for web approval. `send` and `reply` both go through here so a
    reply can never bypass the verify/trust/allow gate. The direct-send path
    captures the Sent-folder UID and echoes the outgoing Message-Id so the
    caller can record them (e.g. in the triage status store) and later verify
    the message really went out.
    """
    policy = load_send_policy()
    category = resolve_category(cfg.user, policy)
    account = account or "default"
    extra = extra or {}

    send_directly = category == "allow" or (category == "trust" and user_approved)
    if send_directly:
        recipients = list(to) + list(cc or []) + list(bcc or [])
        _smtp_send(cfg, msg, recipients)
        saved_sent = False
        sent_uid = None
        if cfg.save_sent:
            sent_uid = _append(cfg, cfg.sent_folder, msg, seen=True)
            saved_sent = True
        return {
            "sent": True, "category": category,
            "approved_by_model": bool(user_approved) if category == "trust" else False,
            "to": to, "cc": cc or [], "bcc": bcc or [],
            "subject": msg.get("Subject") or "", "attachments": attachments or [],
            "saved_to_sent": saved_sent,
            "message_id": msg.get("Message-Id"),
            "sent_uid": sent_uid,
            **extra,
        }

    # verify, or trust without --user-approved: register a pending request.
    request_id = register_pending_send(cfg, msg, category)
    return {
        "sent": False, "pending": True, "category": category,
        "request_id": request_id, "account": account,
        "approval_url": approval_url(account, request_id),
        "to": to, "cc": cc or [], "bcc": bcc or [],
        "subject": msg.get("Subject") or "", "attachments": attachments or [],
        "message_id": msg.get("Message-Id"),
        "note": ("Web approval required before this e-mail is sent. "
                 "Share the approval_url with the user."),
        **extra,
    }


def cmd_send(cfg, args):
    msg = _build_message(cfg, args.to, args.subject, args.body,
                         cc=args.cc, bcc=args.bcc, attachments=args.attach,
                         in_reply_to=args.in_reply_to, references=args.references)
    result = _dispatch_message(cfg, msg, args.to, cc=args.cc, bcc=args.bcc,
                               attachments=args.attach, account=args.account,
                               user_approved=args.user_approved)
    print(json.dumps(result, ensure_ascii=False))


def cmd_pending(cfg, args):
    print(json.dumps({
        "account": args.account or "default",
        "pending": list_pending_sends(cfg),
    }, ensure_ascii=False, indent=2))


def cmd_retract(cfg, args):
    print(json.dumps({"retracted": delete_pending_draft(cfg, args.request_id)},
                     ensure_ascii=False))


def cmd_reject(cfg, args):
    print(json.dumps({"rejected": delete_pending_draft(cfg, args.request_id)},
                     ensure_ascii=False))


def cmd_draft(cfg, args):
    msg = _build_message(cfg, args.to, args.subject, args.body,
                         cc=args.cc, attachments=args.attach,
                         in_reply_to=getattr(args, 'in_reply_to', None),
                         references=getattr(args, 'references', None))
    _append(cfg, cfg.drafts_folder, msg, seen=False)
    print(json.dumps({
        "draft_saved": cfg.drafts_folder, "to": args.to,
        "subject": args.subject, "attachments": args.attach or [],
    }, ensure_ascii=False))


def cmd_forward(cfg, args):
    M = imap_connect(cfg)
    imap_select(M, args.folder, readonly=True)
    typ, data = M.uid("fetch", str(args.uid), "(RFC822)")
    if typ != "OK" or not data or data[0] is None:
        die(f"message uid {args.uid} not found in {args.folder}")
    raw = next(item[1] for item in data if isinstance(item, tuple))
    M.logout()
    orig = _parse_message(raw)

    orig_subject = _decode(orig.get("Subject")) or ""
    subject = args.subject or (orig_subject if orig_subject.lower().startswith(("fwd:", "wg:")) else f"Fwd: {orig_subject}")
    intro = (args.prepend + "\n\n") if args.prepend else ""
    quoted_header = (
        f"---------- Forwarded message ----------\n"
        f"From: {_decode(orig.get('From'))}\n"
        f"Date: {orig.get('Date')}\n"
        f"Subject: {orig_subject}\n"
        f"To: {_decode(orig.get('To'))}\n\n"
    )
    body = intro + quoted_header + (_body_text(orig) or "")

    msg = _build_message(cfg, args.to, subject, body, cc=args.cc)
    # re-attach original attachments
    forwarded = []
    for idx, part in _iter_attachments(orig):
        payload = part.get_payload(decode=True) or b""
        fname = _decode(part.get_filename()) or f"attachment-{idx}"
        maintype, _, subtype = part.get_content_type().partition("/")
        msg.add_attachment(payload, maintype=maintype, subtype=subtype, filename=fname)
        forwarded.append(fname)

    recipients = list(args.to) + list(args.cc or [])
    _smtp_send(cfg, msg, recipients)
    if cfg.save_sent:
        _append(cfg, cfg.sent_folder, msg, seen=True)
    print(json.dumps({
        "forwarded": str(args.uid), "to": args.to, "subject": subject,
        "attachments": forwarded, "saved_to_sent": cfg.save_sent,
    }, ensure_ascii=False))


def _reply_subject(orig_subject):
    """A reply subject: keep an existing Re:/AW:, otherwise prefix 'Re: '."""
    s = (orig_subject or "").strip()
    return s if s.lower().startswith(("re:", "aw:")) else f"Re: {s}"


def cmd_reply(cfg, args):
    """Reply to a message by UID, deriving threading headers from the source.

    The whole point of this verb: In-Reply-To / References are computed from
    the source message's own Message-ID and References chain, so a reply is
    always correctly threaded without the caller having to remember to pass
    --in-reply-to. That threading is what the "already-answered" check relies
    on; omitting it (as happened once in a free-form dashboard reply) caused a
    duplicate proposal. Recipient and subject default to the source too, and
    can be overridden. Goes through the same send-control policy as `send`.
    """
    M = imap_connect(cfg)
    imap_select(M, args.folder, readonly=True)
    typ, data = M.uid("fetch", str(args.uid), "(RFC822)")
    if typ != "OK" or not data or data[0] is None:
        die(f"message uid {args.uid} not found in {args.folder}")
    raw = next(item[1] for item in data if isinstance(item, tuple))
    M.logout()
    orig = _parse_message(raw)

    src_mid = (orig.get("Message-ID") or "").strip()
    if not src_mid:
        die(f"source message uid {args.uid} has no Message-ID to thread against; "
            "use `send` with an explicit subject instead")

    # Reply goes to the source's Reply-To if present, else its From. An explicit
    # --to overrides (e.g. replying to a list post off-list).
    if args.to:
        to = args.to
    else:
        reply_target = _decode(orig.get("Reply-To")) or _decode(orig.get("From"))
        _, addr = parseaddr(reply_target or "")
        if not addr:
            die(f"could not determine a reply address from uid {args.uid}; pass --to")
        to = [addr]

    subject = args.subject or _reply_subject(_decode(orig.get("Subject")))

    # References = the source's own References chain (or its In-Reply-To) plus
    # the source Message-ID itself; In-Reply-To = the source Message-ID. This is
    # the RFC 5322 threading contract.
    prior_refs = (orig.get("References") or orig.get("In-Reply-To") or "").split()
    references = " ".join(prior_refs + [src_mid])

    msg = _build_message(cfg, to, subject, args.body,
                         cc=args.cc, bcc=args.bcc, attachments=args.attach,
                         in_reply_to=src_mid, references=references)
    result = _dispatch_message(
        cfg, msg, to, cc=args.cc, bcc=args.bcc, attachments=args.attach,
        account=args.account, user_approved=args.user_approved,
        extra={"in_reply_to": src_mid, "replied_to_uid": str(args.uid)},
    )
    print(json.dumps(result, ensure_ascii=False))


def cmd_folders(cfg, args):
    M = imap_connect(cfg)
    typ, data = M.list()
    M.logout()
    if typ != "OK":
        die(f"list failed: {data}")
    folders = []
    for line in data:
        if line is None:
            continue
        text = line.decode(errors="replace") if isinstance(line, bytes) else line
        # crude parse: last quoted token (or last token) is the name
        name = text.split(' "')[-1].strip().strip('"') if '"' in text else text.split()[-1]
        folders.append(imap_utf7_decode(name))
    print(json.dumps({"folders": folders}, ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main():
    backend = os.environ.get("EMAIL_BACKEND_URL")
    if backend:
        # No credentials here: hand the whole invocation to the gateway.
        sys.exit(_proxy_to_backend(backend, sys.argv[1:]))

    p = argparse.ArgumentParser(description="IMAP+SMTP e-mail client for the Secretary agent")
    p.add_argument("--account", help="named account suffix (e.g. 'ari' -> EMAIL_USER_ARI); omit for default")
    p.add_argument("--env-file", dest="env_file",
                   help="load credentials from a project-scoped dotenv file (does not override existing env); "
                        "or set EMAIL_ENV_FILE")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("list", help="list recent messages in a folder")
    sp.add_argument("--folder", default="INBOX")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("search", help="search messages")
    sp.add_argument("--folder", default="INBOX")
    sp.add_argument("--from", dest="from_", help="sender substring")
    sp.add_argument("--subject", help="subject substring")
    sp.add_argument("--text", help="full-text substring")
    sp.add_argument("--since", help="date DD-Mon-YYYY, e.g. 01-Jun-2026")
    sp.add_argument("--unseen", action="store_true", help="only unread")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("answered",
                        help="has a reply to this message been sent? (exit 0 = yes, 3 = no)")
    sp.add_argument("--message-id", dest="message_id", required=True)
    sp.add_argument("--folder", help="Sent folder to search (default: the account's)")
    sp.add_argument("--in-folder", dest="in_folder", default="INBOX",
                    help="folder holding the original message (default: INBOX)")
    sp.set_defaults(func=cmd_answered)

    sp = sub.add_parser("thread", help="the full conversation around a message")
    sp.add_argument("--message-id", dest="message_id", required=True)
    sp.add_argument("--folders", nargs="+",
                    help="folders to search (default: INBOX and the Sent folder)")
    sp.add_argument("--limit", type=int, default=10,
                    help="fetch bodies for the last N messages (0 = all)")
    sp.add_argument("--chars", type=int, default=1500,
                    help="truncate each body to N characters")
    sp.set_defaults(func=cmd_thread)

    sp = sub.add_parser("read", help="read one message by UID")
    sp.add_argument("--uid", required=True)
    sp.add_argument("--folder", default="INBOX")
    sp.set_defaults(func=cmd_read)

    sp = sub.add_parser("fetch-attachment", help="download an attachment by part number")
    sp.add_argument("--uid", required=True)
    sp.add_argument("--folder", default="INBOX")
    sp.add_argument("--part", type=int, required=True, help="attachment index from 'read' output")
    sp.add_argument("--out", help="output path (default: original filename)")
    sp.set_defaults(func=cmd_fetch_attachment)

    sp = sub.add_parser("move", help="move a message to another folder")
    sp.add_argument("--uid", required=True)
    sp.add_argument("--from", dest="from_", default="INBOX")
    sp.add_argument("--to", required=True)
    sp.set_defaults(func=cmd_move)

    sp = sub.add_parser("flag", help="mark a message read/unread")
    sp.add_argument("--uid", required=True)
    sp.add_argument("--folder", default="INBOX")
    sp.add_argument("--read", action="store_true")
    sp.add_argument("--unread", action="store_true")
    sp.set_defaults(func=cmd_flag)

    sp = sub.add_parser("send", help="send an e-mail (optionally with attachments)")
    sp.add_argument("--to", required=True, nargs="+")
    sp.add_argument("--cc", nargs="+")
    sp.add_argument("--bcc", nargs="+")
    sp.add_argument("--subject", required=True)
    sp.add_argument("--body", default="")
    sp.add_argument("--attach", nargs="+", help="file path(s)")
    sp.add_argument("--in-reply-to", dest="in_reply_to", help="Message-ID of the message being replied to")
    sp.add_argument("--references", help="References header (space-separated Message-IDs)")
    sp.add_argument("--user-approved", dest="user_approved", action="store_true",
                    help="assert the user approved this send (only honoured for 'trust' "
                         "addresses; ignored for 'allow', insufficient for 'verify')")
    sp.set_defaults(func=cmd_send)

    sp = sub.add_parser("draft", help="save a draft to the IMAP Drafts folder")
    sp.add_argument("--to", required=True, nargs="+")
    sp.add_argument("--cc", nargs="+")
    sp.add_argument("--subject", required=True)
    sp.add_argument("--body", default="")
    sp.add_argument("--attach", nargs="+")
    sp.add_argument("--in-reply-to", dest="in_reply_to", help="Message-ID of the message being replied to")
    sp.add_argument("--references", help="References header (space-separated Message-IDs)")
    sp.set_defaults(func=cmd_draft)

    sp = sub.add_parser("forward", help="forward a message with its attachments")
    sp.add_argument("--uid", required=True)
    sp.add_argument("--folder", default="INBOX")
    sp.add_argument("--to", required=True, nargs="+")
    sp.add_argument("--cc", nargs="+")
    sp.add_argument("--subject", help="override subject (default: 'Fwd: ...')")
    sp.add_argument("--prepend", help="intro text added above the forwarded body")
    sp.set_defaults(func=cmd_forward)

    sp = sub.add_parser("reply", help="reply to a message by UID with correct "
                                      "threading headers derived from the source")
    sp.add_argument("--uid", required=True,
                    help="UID of the message being replied to (its Message-ID, "
                         "subject and sender drive the reply's headers)")
    sp.add_argument("--folder", default="INBOX",
                    help="folder the source message is in (default INBOX)")
    sp.add_argument("--body", default="")
    sp.add_argument("--to", nargs="+",
                    help="override recipient(s) (default: source Reply-To/From)")
    sp.add_argument("--cc", nargs="+")
    sp.add_argument("--bcc", nargs="+")
    sp.add_argument("--subject",
                    help="override subject (default: 'Re: ' + source subject)")
    sp.add_argument("--attach", nargs="+", help="file path(s)")
    sp.add_argument("--user-approved", dest="user_approved", action="store_true",
                    help="assert the user approved this send (only honoured for "
                         "'trust' addresses; ignored for 'allow', insufficient "
                         "for 'verify')")
    sp.set_defaults(func=cmd_reply)

    sp = sub.add_parser("folders", help="list available IMAP folders")
    sp.set_defaults(func=cmd_folders)

    sp = sub.add_parser("pending", help="list pending send requests awaiting approval")
    sp.set_defaults(func=cmd_pending)

    sp = sub.add_parser("reject", help="reject a pending request: delete the draft without sending")
    sp.add_argument("--request-id", dest="request_id", required=True)
    sp.set_defaults(func=cmd_reject)

    sp = sub.add_parser("retract", help="retract a pending request you created: delete the draft")
    sp.add_argument("--request-id", dest="request_id", required=True)
    sp.set_defaults(func=cmd_retract)

    args = p.parse_args()
    try:
        env_file = args.env_file or os.environ.get("EMAIL_ENV_FILE")
        if env_file:
            load_env_file(env_file)
        cfg = Config(args.account)
        # Commands that answer a question (`answered`) signal it in the exit
        # code; the rest return None, i.e. success.
        sys.exit(args.func(cfg, args) or 0)
    except EmailError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
