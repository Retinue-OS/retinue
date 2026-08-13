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
import subprocess
import sys
from email.utils import parseaddr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import triage_policy as tp  # noqa: E402  (local, after sys.path tweak)

EMAIL_CLIENT = os.environ.get("EMAIL_CLIENT_PATH", "/workspace/scripts/email_client.py")
SENT_FOLDER = os.environ.get("SENT_FOLDER", "Sent")
SENT_DERIVE_LIMIT = int(os.environ.get("TRIAGE_SENT_DERIVE_LIMIT", "500"))
INBOX_SCAN_LIMIT = int(os.environ.get("TRIAGE_INBOX_SCAN_LIMIT", "100"))
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
    addresses, wildcards = tp.load_email_whitelist()
    merged = addresses | derived
    if merged != addresses:
        tp.write_if_changed(
            tp.render_email_whitelist(merged, wildcards), tp.email_whitelist_path()
        )
    return len(merged)


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
    unread = unread_inbox()
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
    unread = unread_inbox()
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
    args = parser.parse_args(argv)
    if args.mode == "frequent":
        return run_frequent()
    if args.mode == "daily":
        return run_daily()
    if args.mode == "derive-whitelist":
        n = refresh_whitelist_from_sent()
        return 0 if n >= 0 else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
