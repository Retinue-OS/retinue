#!/usr/bin/env python3
"""Shared Garmin MFA-over-email helper.

Garmin Connect now requires a one-time passcode when it renews the OAuth
session.  The passcode is e-mailed from ``alerts@account.garmin.com`` and — on
this account — lands in the IMAP **Notification** folder, not the inbox.

``make_email_mfa_provider()`` returns a ``prompt_mfa`` callable suitable for
``garminconnect.Garmin(..., prompt_mfa=...)``.  When Garmin asks for the code,
the callback polls the mailbox via ``scripts/email_client.py`` — which routes
through the credential-isolating backend proxy, so no mailbox secrets are read
here — extracts the 6-digit code from the newest passcode mail that arrives
*after* login began, and returns it.

The provider records its creation time and only accepts mails newer than that
(minus a small clock-skew slack), so a stale passcode from an earlier login is
never reused.  Construction does no I/O, so wiring it into the normal sync path
costs nothing on the common case where the stored session is still valid and
``prompt_mfa`` is never invoked.
"""

import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
EMAIL_CLIENT = SCRIPT_DIR / "email_client.py"

# Garmin passcode mails carry the code as a standalone 6-digit number.
_CODE_RE = re.compile(r"\b(\d{6})\b")

# Defaults: Garmin sends the passcode from the account.garmin.com alerts
# address, and on this mailbox a server rule files it under "Notification".
DEFAULT_FOLDER = "Notification"
DEFAULT_SENDER = "account.garmin.com"


def _email_cmd(*args, account=None):
    """Invoke email_client.py and return its parsed JSON output."""
    cmd = [sys.executable, str(EMAIL_CLIENT)]
    if account:
        cmd += ["--account", account]
    cmd += list(args)
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        detail = res.stderr.strip() or res.stdout.strip()
        raise RuntimeError(f"email_client.py {args[0]} failed: {detail}")
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"email_client.py {args[0]} returned non-JSON: {e}")


def _search_passcodes(folder, sender, account):
    """Return candidate passcode mails (newest first) sent today."""
    since = datetime.now(timezone.utc).strftime("%d-%b-%Y")
    data = _email_cmd(
        "search",
        "--folder", folder,
        "--from", sender,
        "--since", since,
        "--limit", "20",
        account=account,
    )
    return data.get("messages", [])


def _parse_dt(value):
    """Parse an ISO-8601 or RFC-2822 date into an aware UTC datetime, or None."""
    if not value:
        return None
    for parse in (datetime.fromisoformat, parsedate_to_datetime):
        try:
            dt = parse(value)
        except (ValueError, TypeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


def _extract_code(folder, uid, account):
    """Read one message and pull the 6-digit code from its subject/body."""
    data = _email_cmd("read", "--uid", str(uid), "--folder", folder, account=account)
    for field in (data.get("subject"), data.get("body")):
        if not field:
            continue
        m = _CODE_RE.search(field)
        if m:
            return m.group(1)
    return None


def make_email_mfa_provider(
    folder=DEFAULT_FOLDER,
    sender=DEFAULT_SENDER,
    account=None,
    timeout=180,
    interval=5,
    skew_slack=120,
):
    """Build a ``prompt_mfa`` callable that reads the code from e-mail.

    Args:
        folder:     IMAP folder the passcode mail lands in.
        sender:     sender substring to match (the Garmin alerts address).
        account:    named email_client account, or None for the default.
        timeout:    seconds to wait for the passcode mail to arrive.
        interval:   seconds between mailbox polls.
        skew_slack: tolerance (seconds) subtracted from the creation time when
                    deciding whether a mail is "new", to absorb clock skew.
    """
    # Capture creation time but do NO I/O here: the common sync path never
    # invokes the callback, so it must stay free.
    not_before = datetime.now(timezone.utc) - timedelta(seconds=skew_slack)

    def prompt_mfa():
        deadline = time.monotonic() + timeout
        tried = set()
        while True:
            for msg in _search_passcodes(folder, sender, account):
                uid = msg.get("uid")
                if uid is None or uid in tried:
                    continue
                msg_dt = _parse_dt(msg.get("date"))
                if msg_dt is not None and msg_dt < not_before:
                    # An older passcode from a previous login attempt — skip it.
                    continue
                tried.add(uid)
                code = _extract_code(folder, uid, account)
                if code:
                    return code
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"No Garmin passcode e-mail (from {sender!r}) arrived in "
                    f"folder {folder!r} within {timeout}s."
                )
            time.sleep(interval)

    return prompt_mfa
