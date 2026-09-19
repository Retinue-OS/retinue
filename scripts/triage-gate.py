#!/usr/bin/env python3
"""Credit-free triage delivery gate (e-mail, pull side).

Runs as a scheduler `command` job, so the scheduler spends no Claude credits to
invoke it (the `agent-self-review` pattern). It checks — for free, over IMAP —
whether anything worth a model turn has arrived, and only then spawns a single
`claude -p` triage session. An empty inbox costs one IMAP round-trip and nothing
more.

Two modes:

  * ``frequent`` — spawn the model only for INBOX mail from a **whitelisted
    sender** (an exact address, or a hand-added ``*@domain`` / ``*@*.domain``
    wildcard). This is what runs on the fast cadence (~30 min). Mail from any
    other sender waits, untouched, for the daily run.
  * ``daily`` — first refresh the whitelist from the Sent folder (so addresses we
    have written to become trusted automatically), then spawn the model for
    INBOX mail from **any** sender, so nothing a narrow whitelist skipped is
    ever lost.

Both modes scan **the INBOX**, not the unread subset, and both first run the
Sent reconciliation (``reconcile_answered``), which archives any mail whose
thread the user has already replied to. See those two functions for why.

Which of the two a message qualifies for is not the whole story, because a mail
belongs to two things at once: a **sender** and a **group** (its mailing list,
or — for a listless newsletter — its own address; see ``triage_policy``). The
sender decides *how urgently* it is triaged, the group decides *where else it
goes*, and the gate asks ``triage_policy.email_gate_decision()`` for both in one
call:

  * ``news`` group — file it into the feed for the Herald to score, credit-free.
  * ``ignored`` group — never worth a model turn; filing it is all that happens.
  * ``quieted`` group (or no flag at all) — reaches triage on the daily sweep.
    On a pull channel those two coincide: the daily sweep *is* the quiet tier.
    So ``news`` + ``quieted`` is how you say "in the feed **and** in the triage",
    which is the case for a list one both reads and writes to.

A mail on the news rail alone is marked read, moved out of the INBOX and
recorded in the triage status store, so it never triggers a spawn. A mail on
**both** rails is filed to the feed but otherwise left untouched — unread, in
the INBOX, no status record — because triage still has to see it. Re-filing it
on the next tick is a no-op: feed item ids are a hash of the content, and the
store skips ids it already holds.

**What arms the gate is new work, not unread mail.** Triage deliberately never
touches mailbox flags — ``unread ≠ unhandled``, the status store is the single
source of handled-state — so a message it has already classified keeps sitting
unread in the INBOX until its disposition is executed, which for an omnibus
batch means waiting on the user's approval. Keying the spawn on "is anything
unread" therefore re-spawns a session on every tick over the same settled stack,
and each of those sessions is a fresh chance for a stray Phase-5 nudge. So the
gate honours the rule it writes: only messages with **no status record** arm it.
Once armed, the payload still carries every routed message, recorded ones
included, so reconciliation and Phase 5 see the same set as before.

Sender whitelist and group policy both live in ``triage_policy.py``, persisted as
N-Triples the life store indexes. The gate reads them off disk; only the daily
run writes (the whitelist it derives from Sent). See
``docs/triage-delivery-gate.md``.

Messenger (Signal / WhatsApp / Telegram) is push-driven and gated inside each
gateway, not here — this script is the e-mail half of the design.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import news_ingest  # noqa: E402  (local, after sys.path tweak)
import triage_policy as tp  # noqa: E402  (local, after sys.path tweak)
import claude_auth  # noqa: E402  (local, after sys.path tweak)
import session_env  # noqa: E402  (local, after sys.path tweak)

EMAIL_CLIENT = os.environ.get("EMAIL_CLIENT_PATH", "/workspace/scripts/email_client.py")
SENT_FOLDER = os.environ.get("SENT_FOLDER", "Sent")
SENT_DERIVE_LIMIT = int(os.environ.get("TRIAGE_SENT_DERIVE_LIMIT", "500"))
INBOX_SCAN_LIMIT = int(os.environ.get("TRIAGE_INBOX_SCAN_LIMIT", "100"))
# Hard ceiling for the widened re-scan when the first pass saturates. The scan
# window is newest-first, so a backlog larger than INBOX_SCAN_LIMIT would hide
# its own oldest mail from every future run — the one failure mode that gets
# worse the longer it lasts. Raise only if a mailbox legitimately holds more
# unread mail than this.
INBOX_SCAN_MAX = int(os.environ.get("TRIAGE_INBOX_SCAN_MAX", "2000"))
# Scan only unread INBOX mail (the pre-2026-09 behaviour). Off by default:
# `unread` is a mailbox flag, and keying scope on it silently drops any mail
# that was merely opened. See `inbox_messages`.
SCAN_UNREAD_ONLY = os.environ.get("TRIAGE_SCAN_UNREAD_ONLY", "0").strip() == "1"
# Settle INBOX mail that has already been answered, by matching it against the
# Sent folder. Credit-free, and the one check that keeps "answered" and "still
# in the INBOX" from drifting apart. See `reconcile_answered`.
SENT_RECONCILE = os.environ.get("TRIAGE_SENT_RECONCILE", "1").strip() != "0"
# How many Sent messages the reconciliation *prefilter* lists. Deliberately its
# own bound rather than SENT_DERIVE_LIMIT, which exists for whitelist
# derivation: the two answer different questions and should not move together.
# The bound is an optimisation only. When the window saturates, the gate stops
# trusting it and exact-checks every INBOX message this tick, so an older reply
# cannot fall into a permanent blind spot (see `reconcile_answered`).
RECONCILE_SCAN_LIMIT = int(os.environ.get("TRIAGE_RECONCILE_SCAN_LIMIT", "500"))
# Where an answered mail goes. Archive, never delete: no inference about a
# reply, however well grounded, should be able to destroy a message.
ANSWERED_FOLDER = os.environ.get("TRIAGE_ANSWERED_FOLDER", "Archive").strip()
# How many messages the spawn prompt enumerates. Only the prompt is capped —
# the gate itself scans the whole backlog, and the prompt says so, so a long
# backlog costs a truncated listing rather than unseen mail.
PROMPT_LIST_LIMIT = int(os.environ.get("TRIAGE_PROMPT_LIST_LIMIT", "150"))
# How long a message may sit in the INBOX on a non-terminal status before the
# gate treats it as stalled and re-arms it. Longer than the nudge cycle
# (EMAIL_PROCESSING_INTERVAL), so an item merely awaiting the user is not
# re-collected while the reminder mechanism is still working on it.
STALL_DAYS = float(os.environ.get("TRIAGE_STALL_DAYS", "7"))
# How long the triage skill accrues archive/delete candidates before sending one
# omnibus digest — the skill's own cadence knob, read here under the same name
# and meaning. The gate needs it because the gate owns every triage spawn: an
# accrued bundle sits on a non-terminal status, which by design does *not* arm
# the gate, so without a due check nothing would ever come back to send it.
OMNIBUS_INTERVAL = float(os.environ.get("EMAIL_PROCESSING_INTERVAL", "86400"))
# Where a filed newsletter goes. Non-destructive by default: the news rail files
# a *reference* into the feed, so the mail itself is archived, never deleted.
# Set empty to leave it in the INBOX (triage's Phase-1 backstop then moves it).
NEWS_FOLDER = os.environ.get("TRIAGE_NEWS_FOLDER", "Archive").strip()
NEWS_EXCERPT_CHARS = int(os.environ.get("TRIAGE_NEWS_EXCERPT_CHARS", "600"))
TRIAGE_STATE_DIR = Path(os.environ.get("TRIAGE_STATE_DIR", "/root/.retinue/triage"))
CLAUDE_MODEL = os.environ.get(
    "RETINUE_TRIAGE_MODEL", os.environ.get("RETINUE_CLAUDE_MODEL", "")
).strip()
PERMISSION_MODE = os.environ.get("CLAUDE_PERMISSION_MODE", "acceptEdits")


def _email_client_rc(*args: str) -> tuple[int, dict | None]:
    """Run email_client.py; return its exit code and parsed JSON stdout.

    Most subcommands answer "did it work?", and for those `_email_client` below
    is the right wrapper. A few answer a *question* in the exit code instead —
    `answered` exits 3 for "no reply found", which is a real answer and not a
    failure — and collapsing that to None would make "nobody replied" and "the
    backend is down" indistinguishable. Hence the raw code.

    Under the scheduler this process holds no mailbox credentials;
    email_client.py proxies through EMAIL_BACKEND_URL, which job_env() already
    sets. An invocation that never ran at all reports -1.
    """
    cmd = ["python3", EMAIL_CLIENT, *args]
    try:
        out = subprocess.run(
            cmd, cwd="/workspace", capture_output=True, text=True, timeout=120
        )
    except Exception as e:  # noqa: BLE001 — any failure means "skip this tick"
        print(f"[triage-gate] email_client invocation failed: {e}", file=sys.stderr)
        return -1, None
    try:
        payload = json.loads(out.stdout)
    except json.JSONDecodeError:
        payload = None
    return out.returncode, payload


def _email_client(*args: str) -> dict | None:
    """Run email_client.py and parse its JSON stdout. None on any failure.

    A failure here (backend down, timeout, non-JSON) must degrade to "gate
    found nothing", never crash the scheduler tick.
    """
    rc, payload = _email_client_rc(*args)
    if rc != 0:
        if rc != -1:
            print(f"[triage-gate] email_client rc={rc} for {args[0]!r}",
                  file=sys.stderr)
        return None
    if payload is None:
        print(f"[triage-gate] non-JSON from email_client {args[0]!r}",
              file=sys.stderr)
    return payload


def _sender_address(msg: dict) -> str:
    return parseaddr(msg.get("from") or "")[1].strip().lower()


def _list_id(msg: dict) -> str:
    """The raw ``List-Id`` a listing carried, if any (normalising is the policy's
    job — the gate must not decide what counts as a usable id)."""
    return (msg.get("list_id") or "").strip()


def inbox_messages() -> list[dict]:
    """INBOX messages, newest first (or [] on failure).

    Scope is **what is in the INBOX**, not what is unread. The module docstring
    already states the rule — ``unread ≠ unhandled``, the status store is the
    single source of handled-state — but the scan itself used to contradict it
    by passing `--unseen`: a mail anyone merely *opened*, in any client or in a
    triage session that read it to classify it, dropped out of the gate's view
    permanently while still sitting in the INBOX. The mailbox is authoritative
    for what is present; the store decides what is handled. Set
    TRIAGE_SCAN_UNREAD_ONLY=1 to restore the old flag-keyed scope.

    The listing is newest-first, so a plain `--limit INBOX_SCAN_LIMIT` does not
    merely sample the mailbox — it hides its *oldest* mail, permanently and
    silently, from the moment the backlog outgrows the window. The mail hidden
    that way is exactly the mail that most needs triaging. So when the first
    pass comes back saturated, widen it once, up to INBOX_SCAN_MAX, and say so
    on stderr either way.
    """
    scope = ["--unseen"] if SCAN_UNREAD_ONLY else []
    label = "unread INBOX" if SCAN_UNREAD_ONLY else "INBOX"
    res = _email_client(
        "search", "--folder", "INBOX", *scope, "--limit", str(INBOX_SCAN_LIMIT)
    )
    if not res:
        return []
    messages = res.get("messages", [])
    if len(messages) < INBOX_SCAN_LIMIT:
        return messages

    print(
        f"[triage-gate] {label} saturated the {INBOX_SCAN_LIMIT}-message scan "
        f"window; re-scanning up to {INBOX_SCAN_MAX}",
        file=sys.stderr,
    )
    wide = _email_client(
        "search", "--folder", "INBOX", *scope, "--limit", str(INBOX_SCAN_MAX)
    )
    if not wide:
        # The widened scan failed; the narrow result is still real mail, and
        # triaging its newest INBOX_SCAN_LIMIT beats triaging nothing.
        return messages
    messages = wide.get("messages", [])
    if len(messages) >= INBOX_SCAN_MAX:
        print(
            f"[triage-gate] {label} also filled the {INBOX_SCAN_MAX}-message "
            f"ceiling — older mail is still out of view. Raise "
            f"TRIAGE_INBOX_SCAN_MAX.",
            file=sys.stderr,
        )
    return messages


def refresh_whitelist_from_sent() -> int:
    """Derive exact-address whitelist entries from the Sent folder.

    Only ever *adds* auto-derived addresses; never removes hand-added entries or
    wildcards, and never adds a domain. Returns the number of addresses now
    whitelisted (or -1 if the Sent listing was unavailable).
    """
    res = _email_client(
        "list", "--folder", SENT_FOLDER, "--limit", str(SENT_DERIVE_LIMIT)
    )
    if res is None:
        return -1
    derived = tp.recipients_from_sent(res.get("messages", []))
    pol = tp.load_email_policy()
    merged = pol.addresses | derived
    if merged != pol.addresses:
        # Save the *whole* policy: the file also holds the news senders, and
        # rendering only the whitelist would silently drop them.
        tp.save_email_policy(pol._replace(addresses=merged))
    return len(merged)


# --------------------------------------------------------------------------- #
# Sent reconciliation — settle mail the user has already answered              #
# --------------------------------------------------------------------------- #

# Reply/forward prefixes, in the locales this mailbox actually sees. Repeated
# and bracketed forms ("Re: AW: ", "Re[2]: ") are stripped as one run.
_SUBJECT_PREFIX = re.compile(
    r"^\s*(?:(?:re|aw|fw|fwd|wg|tr|antw|sv|vs)\s*(?:\[\d+\])?\s*:\s*)+", re.I
)


def _thread_key(subject: str) -> str:
    """A subject stripped of reply/forward prefixes, for pairing a mail with its
    answer. Deliberately crude, and deliberately only ever used to *nominate* a
    candidate: prefixes vary by client and locale, and a subject alone is far
    too weak to settle anything. `email_client answered` does the deciding."""
    return re.sub(r"\s+", " ", _SUBJECT_PREFIX.sub("", subject or "")).strip().lower()


def _parse_when(value: str) -> datetime | None:
    """A listing's ISO timestamp, always tz-aware (naive is read as UTC)."""
    try:
        parsed = datetime.fromisoformat((value or "").strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _confirm_answered(msg: dict) -> bool:
    """Ask the mailbox, exactly, whether this message has been replied to.

    `email_client answered` is the purpose-built check and is strictly stronger
    than anything this script can do over a listing: it runs *server-side IMAP
    SEARCH* over the Sent folder for messages citing this Message-ID in
    In-Reply-To/References, plus — for replies that do not thread at all — an
    exact `TO` search for the sender since the message's own date, under the
    same base subject. Three things follow, each of which the listing-based
    matching this replaced got wrong:

      * **Address comparison is exact.** A Python `sender in recipients`
        substring test over a listing treats `ann@example.com` as answered by a
        reply to `joann@example.com`.
      * **No window.** A server-side SEARCH sees the whole Sent folder, so a
        reply older than any listing limit still counts.
      * **Cc/Bcc are moot.** A listing summary carries `To` only, so a reply-all
        that reaches the sender via Cc is invisible to it; the header search
        does not care which field carried the address.

    Exit 0 means answered, 3 means genuinely unanswered, anything else means the
    state is unknown — and unknown must read as *not* answered, so an IMAP
    hiccup can never archive a mail nobody replied to.
    """
    mid = (msg.get("message_id") or "").strip()
    if not mid:
        return False
    rc, payload = _email_client_rc(
        "answered", "--message-id", mid, "--folder", SENT_FOLDER,
        "--in-folder", "INBOX",
    )
    if rc not in (0, 3):
        print(f"[triage-gate] sent-reconcile: answered-check inconclusive for "
              f"{mid} (rc={rc}); leaving it to triage", file=sys.stderr)
        return False
    return rc == 0 and bool((payload or {}).get("answered"))


def _settle_answered(msg: dict) -> bool:
    """Move one answered mail out of the INBOX and record it resolved."""
    uid = str(msg.get("uid") or "")
    if not (uid and ANSWERED_FOLDER):
        return False
    moved = _email_client(
        "move", "--uid", uid, "--from", "INBOX", "--to", ANSWERED_FOLDER
    )
    # Insist on the move's own receipt, not merely "the command exited 0": the
    # status record about to be written says the mail has left the INBOX, and
    # bookkeeping that outruns the mailbox is how a message becomes invisible
    # to triage while still sitting in it.
    if not (moved and str(moved.get("moved") or "") == uid):
        print(f"[triage-gate] sent-reconcile: move of uid {uid} not confirmed; "
              f"leaving it to triage", file=sys.stderr)
        return False
    path = _status_path(msg.get("message_id") or "")
    if path is None:
        return True
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    record = {
        "status": "resolved",
        "disposition": "answered",
        "channel": "email",
        "uid": uid,
        "message_id": msg.get("message_id") or "",
        "from": msg.get("from") or "",
        "subject": msg.get("subject") or "",
        "project": "unlinked",
        "note": (
            f"`email_client answered` found a reply to this message in "
            f"{SENT_FOLDER}; settled by the credit-free triage gate and moved "
            f"INBOX->{ANSWERED_FOLDER}."
        ),
        "folder": ANSWERED_FOLDER,
        "classified": stamp,
        "updated": stamp,
        "resolved_at": stamp,
    }
    try:
        TRIAGE_STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:  # noqa: BLE001 — bookkeeping must not break the tick
        print(f"[triage-gate] sent-reconcile: could not write status file: {exc}",
              file=sys.stderr)
    return True


def reconcile_answered(messages: list[dict]) -> list[dict]:
    """Archive INBOX mail the user has already answered; return what is left.

    A mail the user has already replied to is settled by definition, yet
    nothing in triage noticed, so it kept sitting in the INBOX and kept being
    re-proposed on every sweep. This is what keeps the INBOX meaning *still
    open* rather than *everything that ever arrived*, and it costs no model
    turn.

    Two steps, because the cheap check and the correct check are different
    checks:

      1. **Nominate** — one Sent listing, bounded by RECONCILE_SCAN_LIMIT.
         A candidate is a mail whose thread subject appears in that listing
         with a later timestamp. Loose on purpose: it exists only to keep step
         2 off the ~99% of the INBOX nobody has answered. If the listing fills
         the whole window, the optimisation is treated as saturated and every
         message is exact-checked instead, so the bound cannot hide older
         replies forever.
      2. **Decide** — `_confirm_answered`, one exact server-side check per
         candidate. Nothing is archived on the strength of step 1.

    Both failure directions are therefore safe. A miss in step 1 that is not a
    saturated window (for example, a subject rewritten past recognition) costs
    the mail one more appearance in the proposal. A loose match in step 1 costs
    one extra IMAP round-trip and is then rejected in step 2. And the action is
    a move to ANSWERED_FOLDER, never a delete, so even a false positive that
    got through both costs an archive rather than a message.
    """
    if not (SENT_RECONCILE and messages):
        return messages
    res = _email_client(
        "search", "--folder", SENT_FOLDER, "--limit", str(RECONCILE_SCAN_LIMIT)
    )
    if not res:
        return messages
    listed = list(res.get("messages", []))
    saturated = len(listed) >= RECONCILE_SCAN_LIMIT
    sent_index: dict[str, list[datetime]] = {}
    for sent in listed:
        key = _thread_key(sent.get("subject"))
        when = _parse_when(sent.get("date"))
        if key and when is not None:
            sent_index.setdefault(key, []).append(when)
    if saturated:
        print(f"[triage-gate] sent-reconcile: {SENT_FOLDER} filled the "
              f"{RECONCILE_SCAN_LIMIT}-message prefilter window; exact-checking "
              f"all {len(messages)} INBOX message(s)", file=sys.stderr)

    keep, settled, checked = [], 0, 0
    for msg in messages:
        key = _thread_key(msg.get("subject"))
        when = _parse_when(msg.get("date"))
        nominated = saturated or (bool(key and when is not None) and any(
            sent_at > when for sent_at in sent_index.get(key, [])
        ))
        if nominated:
            checked += 1
        if nominated and _confirm_answered(msg) and _settle_answered(msg):
            settled += 1
        else:
            keep.append(msg)
    if checked:
        print(f"[triage-gate] sent-reconcile: {checked} candidate(s) confirmed "
              f"against {SENT_FOLDER}, {settled} moved INBOX->{ANSWERED_FOLDER}",
              file=sys.stderr)
    return keep


# --------------------------------------------------------------------------- #
# News rail — file broadcast senders into the feed, credit-free                #
# --------------------------------------------------------------------------- #

_URL_IN_HEADER = re.compile(r"<\s*(https?://[^>\s]+)\s*>|(https?://\S+)")


def _status_path(message_id: str) -> Path | None:
    """The status file for a Message-ID, using triage's own naming rule.

    Filename = the Message-ID stripped of its angle brackets, with `/` replaced
    by `_` so it stays one path segment. Returns None for a message with no id.
    """
    mid = (message_id or "").strip().strip("<>").strip()
    if not mid:
        return None
    return TRIAGE_STATE_DIR / mid.replace("/", "_")


def _declared_url(detail: dict) -> str:
    """The newsletter's own declared web version of this message, or "".

    Only `Archived-At` (RFC 5064) and `List-Archive` (RFC 2369) count. Picking a
    link out of the body instead would be guesswork — the first URL in a
    newsletter is as often a tracking pixel or an unsubscribe link as the
    article — and a wrong link is worse than none, because the feed item's id is
    keyed off it.
    """
    for key in ("archived_at", "list_archive"):
        for match in _URL_IN_HEADER.finditer(detail.get(key) or ""):
            return (match.group(1) or match.group(2)).strip()
    return ""


def _excerpt(body: str, limit: int) -> str:
    """Collapse a mail body to a feed-sized excerpt (no HTML, no blank runs)."""
    lines = [ln.strip() for ln in (body or "").splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return text[:limit] + ("…" if len(text) > limit else "")


def file_news_message(msg: dict, *, consume: bool = True) -> bool:
    """File one newsletter into the feed, and — with `consume` — get it out.

    Returns True when the item reached the feed. The mailbox side is best-effort
    and reported separately: a filed item whose move failed is left non-terminal
    so the next run (or triage's Phase-1 backstop) retries the move rather than
    the filing.

    `consume=False` files the item and touches nothing else. That is the case
    for a group that is both `news` and `quieted`: triage is still owed a look at
    the mail, so marking it read, moving it or writing a terminal status record
    would take it away from the very rail the policy asked to keep it on.
    """
    uid = str(msg.get("uid") or "").strip()
    detail = _email_client("read", "--uid", uid) if uid else None
    detail = detail or {}
    subject = (detail.get("subject") or msg.get("subject") or "").strip()
    name, addr = parseaddr(detail.get("from") or msg.get("from") or "")
    addr = addr.strip().lower()
    source = name.strip() or addr or "E-Mail"
    body = _excerpt(detail.get("body") or "", NEWS_EXCERPT_CHARS)
    if not subject and not body:
        print(f"[triage-gate] news: uid {uid} unreadable; left for triage",
              file=sys.stderr)
        return False
    # Subject first so the gateway's first-line title derivation and our explicit
    # title agree, and so the id seed changes when the subject does.
    text = f"{subject}\n\n{body}".strip()
    ok = news_ingest.forward_news(
        channel="email",
        source=source,
        text=text,
        url=_declared_url(detail),
        title=subject or None,
        source_id=f"email:{addr}" if addr else None,
    )
    if not ok:
        print(f"[triage-gate] news: could not file uid {uid} ({source}); "
              "leaving it unread for the next run", file=sys.stderr)
        return False
    if not consume:
        print(f"[triage-gate] news: filed uid {uid} from {source} "
              "(left unread for triage)", file=sys.stderr)
        return True
    moved_to = ""
    if uid:
        _email_client("flag", "--uid", uid, "--read")
        if NEWS_FOLDER and _email_client(
            "move", "--uid", uid, "--from", "INBOX", "--to", NEWS_FOLDER
        ) is not None:
            moved_to = NEWS_FOLDER
    _record_news_status(msg, detail, source, moved_to)
    print(f"[triage-gate] news: filed uid {uid} from {source}"
          + (f" -> {moved_to}" if moved_to else " (still in INBOX)"),
          file=sys.stderr)
    return True


def _record_news_status(msg: dict, detail: dict, source: str, moved_to: str) -> None:
    """Write the triage status file for a mail the news rail handled.

    Triage's status store — not `\\Seen` — is what stops a message being
    re-proposed, so the rail has to write there too. Terminal only once the mail
    has actually left the INBOX: writing `resolved` while it is still there is
    exactly the drift Phase 1's third pass exists to repair.
    """
    path = _status_path(detail.get("message_id") or msg.get("message_id") or "")
    if path is None:
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    record = {
        "status": "resolved" if moved_to else "deferred",
        "disposition": "news",
        "channel": "email",
        "uid": str(msg.get("uid") or ""),
        "message_id": detail.get("message_id") or msg.get("message_id") or "",
        "from": detail.get("from") or msg.get("from") or "",
        "subject": detail.get("subject") or msg.get("subject") or "",
        "project": "unlinked",
        "note": (
            f"Declared news sender ({source}); filed to the news feed by the "
            "credit-free triage gate for the Herald to score. "
            + (f"Flagged read and moved INBOX->{moved_to}."
               if moved_to
               else "Still in the INBOX — the move failed or is disabled; "
                    "Phase 1 should move it out.")
        ),
        "classified": now,
        "updated": now,
    }
    if moved_to:
        record["folder"] = moved_to
        record["resolved_at"] = now
    try:
        TRIAGE_STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:  # noqa: BLE001 — bookkeeping must not break the tick
        print(f"[triage-gate] news: could not write status file: {exc}",
              file=sys.stderr)


def route(present: list[dict], mode: str) -> list[dict]:
    """Run both rails over the INBOX set; return what `mode` owes a model turn.

    One pass decides everything, because the two rails are not exclusive: the
    news rail is driven by the message's group, the triage rail by its sender
    plus the same group, and a mail can ride both (see the module docstring).
    `mode` is "frequent", "daily", or "news" — the last files the feed and
    returns nothing, for a run that must never spawn.

    A mail whose filing fails is handed to triage even if the policy would have
    kept it off that rail: losing it silently is the one outcome worth spending
    a model turn to avoid.
    """
    pol = tp.load_email_policy()
    news_ready = news_ingest.news_enabled()
    warned = False
    keep, filed, held = [], 0, 0
    for msg in present:
        dec = tp.email_gate_decision(_sender_address(msg), _list_id(msg), pol=pol)
        triaged = dec["daily"]
        if dec["news"]:
            if not news_ready:
                if not warned:
                    print("[triage-gate] news: NEWS_INGEST_URL unset; news groups "
                          "left to normal triage", file=sys.stderr)
                    warned = True
                triaged = True
            elif file_news_message(msg, consume=not dec["daily"]):
                filed += 1
                held += 1 if dec["daily"] else 0
            else:
                triaged = True  # filing failed — let a model turn deal with it
        if triaged and mode != "news" and (dec["triage_now"] or mode == "daily"):
            keep.append(msg)
    if filed:
        print(f"[triage-gate] news: {filed} item(s) filed to the feed"
              + (f", {held} of them also kept for triage" if held else ""),
              file=sys.stderr)
    return keep


# The statuses the triage skill names as legitimate INBOX residents — an item
# that owes something and is waiting for it. Only these can stall; every other
# status (`resolved`, and the statuses the other rails write — `status_filed`,
# `self_filed`, `abstain`, …) is settled as far as this gate is concerned. An
# allowlist of *unfinished* states rather than of terminal ones, because a rail
# added later must not have its records silently re-armed by this code.
# `omnibus_pending` is a live name the skill writes: an item classified for the
# omnibus whose digest has not gone out yet. It belongs here — it owes the user
# a digest, not a model turn — but it is the one open status this gate also
# arms on directly, once that digest is due (`omnibus_due`). Removing it from
# this set would re-spawn a session over the same bundle on every tick.
OPEN_STATUSES = frozenset(
    {"proposed", "omnibus", "omnibus_pending", "deferred", "engaged"}
)

# Every field triage may stamp a status record with. Which ones a record
# carries depends on how far it got; the newest of those present is how long
# ago anything actually happened to it.
_STATUS_TIME_FIELDS = (
    "classified",
    "proposed",
    "omnibus",
    "last_nudge",
    "updated",
    "resolved",
    "resolved_at",
)


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _last_touched(path: Path, record: dict) -> datetime:
    """When anything last happened to this item.

    The newest timestamp the record carries, or the file's mtime when it
    carries none — a record with no readable timestamp is still evidence of
    *something*, and mtime never makes an item look fresher than it is.
    """
    stamps = [
        ts for ts in (_parse_ts(record.get(f)) for f in _STATUS_TIME_FIELDS) if ts
    ]
    if stamps:
        return max(stamps)
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _read_record(path: Path) -> dict | None:
    """The status record at `path`, or None when it is unreadable or malformed.

    Every caller here fails open on None rather than trusting a record it could
    not parse — the alternative is mail no future run ever looks at again.
    """
    try:
        record = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return record if isinstance(record, dict) else None


def _stalled(path: Path) -> bool:
    """True when a recorded, non-terminal item has sat untouched past STALL_DAYS.

    An item still in the INBOX on a non-terminal status is work that was
    started and never finished. Normally the nudge cycle finishes it; when it
    does not — the proposal thread was archived or deleted, the omnibus was
    never approved — nothing else in the system ever looks at that item again,
    because the gate treats *any* record as "not new work". So the item freezes
    in the INBOX permanently. Age is the only signal available here that
    distinguishes that from an item legitimately awaiting the user, and it is
    deliberately not read off the dashboard store: an archived thread is not a
    decision (see CLAUDE.md — `muted` is the only decidable signal of that),
    so a thread that has gone quiet is exactly the case this must catch.
    """
    record = _read_record(path)
    if record is None:
        # Unreadable or malformed record: treat as stalled so a model turn can
        # repair it, rather than leaving the message invisible forever.
        return True
    status = record.get("status")
    if not isinstance(status, str) or not status.strip():
        # Parseable but malformed: no usable status at all. Same rule as an
        # unreadable record — stalled, so a model turn can repair it.
        return True
    if status.strip() not in OPEN_STATUSES:
        return False
    age = datetime.now(timezone.utc) - _last_touched(path, record)
    return age > timedelta(days=STALL_DAYS)


def _already_recorded(msg: dict) -> bool:
    """True when triage has a *live* status record for this message.

    Any record at all normally counts — `proposed`, `engaged`,
    `omnibus_pending`, `resolved`. They differ in what is still owed, but none
    of them is *new* work, and only new work justifies spending a model turn.

    The exception is a stalled one (see `_stalled`): a non-terminal record that
    nothing has touched in STALL_DAYS is not "in progress", it is abandoned,
    and re-arming it is the only way its mail ever leaves the INBOX.
    """
    path = _status_path(msg.get("message_id") or "")
    if not (path and path.exists()):
        return False
    return not _stalled(path)


def arming(messages: list[dict]) -> list[dict]:
    """The subset of `messages` that justifies a spawn: the ones triage has
    never seen, plus the ones it saw but never finished (`_stalled`).
    Everything else is settled as far as the gate is concerned and is merely
    waiting on the user or on an execution step — it must not keep re-spawning
    a session for as long as it stays in the INBOX."""
    return [m for m in messages if not _already_recorded(m)]


def _last_omnibus() -> datetime | None:
    """When the last omnibus digest went out, per the skill's marker file.

    None when the marker is missing or unparseable, which reads as "no digest
    on record" and so makes a pending bundle due. That direction is the safe
    one: its cost is one digest the user may not have been owed yet, while the
    other direction leaves mail bundled and unseen indefinitely.
    """
    try:
        raw = (TRIAGE_STATE_DIR / ".last-omnibus").read_text()
    except (OSError, UnicodeDecodeError):
        return None
    return _parse_ts(raw.strip())


def _pending_omnibus(messages: list[dict]) -> list[dict]:
    """The listed messages bundled for the omnibus but not yet sent.

    Read off the mailbox listing rather than by walking the status store:
    triage never touches mailbox flags, so an item awaiting its digest is by
    definition still unread in the INBOX and therefore already in `messages` —
    and this runs on every tick, where walking thousands of records would not
    be free.
    """
    pending = []
    for msg in messages:
        path = _status_path(msg.get("message_id") or "")
        if not (path and path.exists()):
            continue
        record = _read_record(path)
        if record is None:
            continue  # corruption is the stall path's business, not this one
        status = record.get("status")
        if isinstance(status, str) and status.strip() == "omnibus_pending":
            pending.append(msg)
    return pending


def omnibus_due(messages: list[dict]) -> list[dict]:
    """The pending-omnibus messages whose digest is now due, else [].

    The skill accrues archive/delete candidates on `omnibus_pending` and sends
    one digest per OMNIBUS_INTERVAL — that accrual is what keeps the user from
    being pinged four times a day. But this gate is the only thing that spawns
    a triage session, and an accrued item does not arm it (`_already_recorded`:
    `omnibus_pending` is an open status, so it counts as work in progress). So
    the digest has to be armed on its own terms here; otherwise it goes out
    only when unrelated new mail happens to arm the gate, or — worse — when the
    STALL_DAYS backstop eventually fires, days late.
    """
    pending = _pending_omnibus(messages)
    if not pending:
        return []
    last = _last_omnibus()
    if last is None:
        return pending
    if datetime.now(timezone.utc) - last >= timedelta(seconds=OMNIBUS_INTERVAL):
        return pending
    return []


def _merge(*groups: list[dict]) -> list[dict]:
    """The groups concatenated, deduplicated on Message-ID, order preserved.

    Messages without an id are kept as they come: there is nothing to dedupe
    them on, and dropping them would hide mail.
    """
    seen: set[str] = set()
    merged: list[dict] = []
    for group in groups:
        for msg in group:
            mid = (msg.get("message_id") or "").strip()
            if mid:
                if mid in seen:
                    continue
                seen.add(mid)
            merged.append(msg)
    return merged


def _report_arming(
    mode: str, hits: list[dict], fresh: list[dict], due: list[dict]
) -> None:
    """Log why the gate armed: never-seen mail, stalled mail, a due digest.

    A rising stalled count is the signature of the inbox-zero backstop failing,
    and it is otherwise invisible: all three kinds look identical in the spawn
    line. A run armed only by a due digest is likewise worth seeing as such.
    """
    stalled = sum(
        1
        for m in fresh
        if (p := _status_path(m.get("message_id") or "")) is not None and p.exists()
    )
    parts = []
    if fresh:
        parts.append(f"{len(fresh) - stalled} never seen")
        if stalled:
            parts.append(f"{stalled} stalled >{STALL_DAYS:g}d on a non-terminal status")
    if due:
        parts.append(f"{len(due)} bundled, omnibus digest due")
    print(
        f"[triage-gate] {mode}: arming on {len(fresh) + len(due)} "
        f"({', '.join(parts)}); {len(hits)} message(s) in scope",
        file=sys.stderr,
    )


def build_prompt(mode: str, messages: list[dict], due: int = 0) -> str:
    scope = (
        "from a whitelisted sender"
        if mode == "frequent"
        else "from any sender (daily catch-all)"
    )
    lines = [
        "The credit-free triage gate found INBOX e-mail worth handling "
        f"({scope}).",
        "",
        "Invoke the triage skill scoped to e-mail. Follow the skill exactly: "
        "reconcile against the triage status store, link each message to a "
        "project, and propose replies/actions as individual dashboard "
        "conversations with archivals/deletions bundled into the omnibus. Do not "
        "answer in chat and do not push results via Signal. A run with nothing to "
        "propose ends silently.",
    ]
    if due:
        lines += [
            "",
            f"{due} of these are already bundled on `omnibus_pending` and their "
            "digest is now due: send the omnibus this run (Phase 4b), even if "
            "nothing else here is worth proposing — that bundle is the reason "
            "this run was armed at all. What is in the bundle is what the status "
            "store says is in it: reconcile from the store, never from this "
            "listing or from a thread's prose.",
        ]
    lines += [
        "",
        "The messages the gate saw (the mailbox listing remains authoritative — "
        "reconcile, do not assume this list is complete):",
    ]
    for m in messages[:PROMPT_LIST_LIMIT]:
        frm = m.get("from") or "(unknown)"
        subj = (m.get("subject") or "").strip() or "(no subject)"
        mid = m.get("message_id") or "(no id)"
        lines.append(f"  - {frm} — {subj} [{mid}]")
    if len(messages) > PROMPT_LIST_LIMIT:
        lines.append(
            f"  … and {len(messages) - PROMPT_LIST_LIMIT} more the gate saw but did "
            "not list here. This listing is truncated, the mailbox is not: work "
            "from the mailbox."
        )
    return "\n".join(lines)


def spawn(mode: str, messages: list[dict], due: int = 0) -> int:
    print(
        f"[triage-gate] {mode}: {len(messages)} message(s) to triage"
        + (f", {due} of them a due omnibus digest" if due else "")
        + "; spawning session",
        file=sys.stderr,
    )
    cmd = ["claude", "-p", "--output-format=json",
           "--permission-mode", PERMISSION_MODE, build_prompt(mode, messages, due)]
    if CLAUDE_MODEL:
        cmd[2:2] = ["--model", CLAUDE_MODEL]
    # Refresh an access token about to expire before the session starts —
    # once, under the lock every framework spawner shares (docs/claude-auth.md).
    claude_auth.ensure_fresh_credentials(
        log=lambda msg: print(f"[triage-gate] {msg}", file=sys.stderr))
    # The allowlisted environment (scripts/session_env.py), never this
    # process's own — the triage session handles untrusted mail, so of all
    # sessions it is the one that must not inherit a secret. Also stamps the
    # model for memory entries (scripts/memory.py).
    return subprocess.run(cmd, cwd="/workspace",
                          env=session_env.build(model=CLAUDE_MODEL)).returncode


def run_frequent() -> int:
    present = reconcile_answered(inbox_messages())
    hits = route(present, "frequent")
    # A due digest is looked for across the whole INBOX listing, not just the
    # whitelisted `hits`: a bundle accrued by a daily run may well hold mail
    # from senders this pass does not whitelist, and the user is owed that
    # digest on time regardless of who sent what is in it.
    due = omnibus_due(present)
    fresh = arming(hits)
    if not fresh and not due:
        if not hits:
            print(
                f"[triage-gate] frequent: {len(present)} in INBOX, none whitelisted; "
                "nothing spawned",
                file=sys.stderr,
            )
        else:
            print(
                f"[triage-gate] frequent: {len(hits)} whitelisted, all already in the "
                "status store; nothing spawned",
                file=sys.stderr,
            )
        return 0
    _report_arming("frequent", hits, fresh, due)
    # The routed set plus whatever the digest needs: narrowing the payload to
    # what armed the run would hide the rest of the stack from reconciliation.
    return spawn("frequent", _merge(hits, due), due=len(due))


def run_daily() -> int:
    n = refresh_whitelist_from_sent()
    if n >= 0:
        print(f"[triage-gate] daily: whitelist now {n} address(es)", file=sys.stderr)
    present = reconcile_answered(inbox_messages())
    hits = route(present, "daily")
    due = omnibus_due(present)
    fresh = arming(hits)
    if not fresh and not due:
        if not hits:
            print("[triage-gate] daily: nothing in the INBOX that triage owes a "
                  "look; nothing spawned", file=sys.stderr)
        else:
            print(f"[triage-gate] daily: {len(hits)} in INBOX, all already in the "
                  "status store; nothing spawned", file=sys.stderr)
        return 0
    _report_arming("daily", hits, fresh, due)
    return spawn("daily", _merge(hits, due), due=len(due))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Credit-free e-mail triage gate")
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("frequent", help="spawn only for whitelisted senders")
    sub.add_parser("daily", help="refresh whitelist from Sent, spawn for any sender")
    sub.add_parser("derive-whitelist", help="refresh the whitelist from Sent only")
    sub.add_parser("news", help="file declared news groups to the feed only")
    args = parser.parse_args(argv)
    if args.mode == "frequent":
        return run_frequent()
    if args.mode == "daily":
        return run_daily()
    if args.mode == "news":
        route(inbox_messages(), "news")
        return 0
    if args.mode == "derive-whitelist":
        n = refresh_whitelist_from_sent()
        return 0 if n >= 0 else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
