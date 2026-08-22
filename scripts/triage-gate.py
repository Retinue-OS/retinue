#!/usr/bin/env python3
"""Credit-free triage delivery gate (e-mail, pull side).

Runs as a scheduler `command` job, so the scheduler spends no Claude credits to
invoke it (the `agent-self-review` pattern). It checks — for free, over IMAP —
whether anything worth a model turn has arrived, and only then spawns a single
`claude -p` triage session. An empty inbox costs one IMAP round-trip and nothing
more.

Two modes:

  * ``frequent`` — spawn the model only for unread INBOX mail from a **whitelisted
    sender** (an exact address, or a hand-added ``*@domain`` / ``*@*.domain``
    wildcard). This is what runs on the fast cadence (~30 min). Mail from any
    other sender waits, untouched, for the daily run.
  * ``daily`` — first refresh the whitelist from the Sent folder (so addresses we
    have written to become trusted automatically), then spawn the model for
    unread INBOX mail from **any** sender, so nothing a narrow whitelist skipped
    is ever lost.

Both modes first divert the **news rail**: unread mail from a declared news
sender (a newsletter, a bulletin — see ``triage_policy``) is filed into the news
feed for the Herald to score, marked read, moved out of the INBOX and recorded
in the triage status store, all deterministically and with **no model turn**.
News mail therefore never triggers a spawn on its own; it is subtracted from the
unread set before either mode decides whether anything is worth one. This is the
e-mail twin of the messenger ``news`` group flag: same idea, same feed, but a
pull channel has to do its own fetching and its own inbox hygiene.

Whitelist policy lives in ``triage_policy.py`` and is persisted as N-Triples the
life store indexes. The gate reads the whitelist off disk; only the daily run
writes it. See ``docs/triage-delivery-gate.md``.

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
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import news_ingest  # noqa: E402  (local, after sys.path tweak)
import triage_policy as tp  # noqa: E402  (local, after sys.path tweak)

EMAIL_CLIENT = os.environ.get("EMAIL_CLIENT_PATH", "/workspace/scripts/email_client.py")
SENT_FOLDER = os.environ.get("SENT_FOLDER", "Sent")
SENT_DERIVE_LIMIT = int(os.environ.get("TRIAGE_SENT_DERIVE_LIMIT", "500"))
INBOX_SCAN_LIMIT = int(os.environ.get("TRIAGE_INBOX_SCAN_LIMIT", "100"))
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


def _email_client(*args: str) -> dict | None:
    """Run email_client.py and parse its JSON stdout. None on any failure.

    Under the scheduler this process holds no mailbox credentials; email_client.py
    proxies through EMAIL_BACKEND_URL, which job_env() already sets. A failure
    here (backend down, timeout) must degrade to "gate found nothing", never
    crash the scheduler tick.
    """
    cmd = ["python3", EMAIL_CLIENT, *args]
    try:
        out = subprocess.run(
            cmd, cwd="/workspace", capture_output=True, text=True, timeout=120
        )
    except Exception as e:  # noqa: BLE001 — any failure means "skip this tick"
        print(f"[triage-gate] email_client invocation failed: {e}", file=sys.stderr)
        return None
    if out.returncode != 0:
        print(
            f"[triage-gate] email_client rc={out.returncode}: {out.stderr.strip()}",
            file=sys.stderr,
        )
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError as e:
        print(f"[triage-gate] non-JSON from email_client: {e}", file=sys.stderr)
        return None


def _sender_address(msg: dict) -> str:
    return parseaddr(msg.get("from") or "")[1].strip().lower()


def unread_inbox() -> list[dict]:
    """Unread INBOX messages, newest first (or [] on failure)."""
    res = _email_client(
        "search", "--folder", "INBOX", "--unseen", "--limit", str(INBOX_SCAN_LIMIT)
    )
    return res.get("messages", []) if res else []


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


def file_news_message(msg: dict) -> bool:
    """File one newsletter into the feed, then get it out of the INBOX.

    Returns True when the item reached the feed. The mailbox side is best-effort
    and reported separately: a filed item whose move failed is left non-terminal
    so the next run (or triage's Phase-1 backstop) retries the move rather than
    the filing.
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


def divert_news(unread: list[dict]) -> list[dict]:
    """File every declared news sender's mail; return what is left to triage."""
    pol = tp.load_email_policy()
    if not pol.news and not pol.news_wildcards:
        return unread
    if not news_ingest.news_enabled():
        print("[triage-gate] news: NEWS_INGEST_URL unset; news senders left "
              "to normal triage", file=sys.stderr)
        return unread
    rest, filed = [], 0
    for msg in unread:
        if not tp.email_news_sender(_sender_address(msg), pol.news, pol.news_wildcards):
            rest.append(msg)
        elif not file_news_message(msg):
            rest.append(msg)  # filing failed — let a model turn deal with it
        else:
            filed += 1
    if filed:
        print(f"[triage-gate] news: {filed} newsletter(s) filed to the feed",
              file=sys.stderr)
    return rest


def build_prompt(mode: str, messages: list[dict]) -> str:
    scope = (
        "from a whitelisted sender"
        if mode == "frequent"
        else "from any sender (daily catch-all)"
    )
    lines = [
        "The credit-free triage gate found unread INBOX e-mail worth handling "
        f"({scope}).",
        "",
        "Invoke the triage skill scoped to e-mail. Follow the skill exactly: "
        "reconcile against the triage status store, link each message to a "
        "project, and propose replies/actions as individual dashboard "
        "conversations with archivals/deletions bundled into the omnibus. Do not "
        "answer in chat and do not push results via Signal. A run with nothing to "
        "propose ends silently.",
        "",
        "The messages the gate saw (the mailbox listing remains authoritative — "
        "reconcile, do not assume this list is complete):",
    ]
    for m in messages:
        frm = m.get("from") or "(unknown)"
        subj = (m.get("subject") or "").strip() or "(no subject)"
        mid = m.get("message_id") or "(no id)"
        lines.append(f"  - {frm} — {subj} [{mid}]")
    return "\n".join(lines)


def spawn(mode: str, messages: list[dict]) -> int:
    print(
        f"[triage-gate] {mode}: {len(messages)} message(s) to triage; spawning session",
        file=sys.stderr,
    )
    cmd = ["claude", "-p", "--output-format=json",
           "--permission-mode", PERMISSION_MODE, build_prompt(mode, messages)]
    if CLAUDE_MODEL:
        cmd[2:2] = ["--model", CLAUDE_MODEL]
    return subprocess.run(cmd, cwd="/workspace").returncode


def run_frequent() -> int:
    addresses, wildcards = tp.load_email_whitelist()
    unread = divert_news(unread_inbox())
    hits = [m for m in unread if tp.email_whitelisted(_sender_address(m), addresses, wildcards)]
    if not hits:
        print(
            f"[triage-gate] frequent: {len(unread)} unread, none whitelisted; "
            "nothing spawned",
            file=sys.stderr,
        )
        return 0
    return spawn("frequent", hits)


def run_daily() -> int:
    n = refresh_whitelist_from_sent()
    if n >= 0:
        print(f"[triage-gate] daily: whitelist now {n} address(es)", file=sys.stderr)
    unread = divert_news(unread_inbox())
    if not unread:
        print("[triage-gate] daily: inbox has no unread mail; nothing spawned",
              file=sys.stderr)
        return 0
    return spawn("daily", unread)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Credit-free e-mail triage gate")
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("frequent", help="spawn only for whitelisted senders")
    sub.add_parser("daily", help="refresh whitelist from Sent, spawn for any sender")
    sub.add_parser("derive-whitelist", help="refresh the whitelist from Sent only")
    sub.add_parser("news", help="file declared news senders to the feed only")
    args = parser.parse_args(argv)
    if args.mode == "frequent":
        return run_frequent()
    if args.mode == "daily":
        return run_daily()
    if args.mode == "news":
        divert_news(unread_inbox())
        return 0
    if args.mode == "derive-whitelist":
        n = refresh_whitelist_from_sent()
        return 0 if n >= 0 else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
