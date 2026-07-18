#!/usr/bin/env python3
"""Re-authenticate Garmin Connect headlessly using the emailed MFA passcode.

Garmin periodically forces a full login that requires a one-time passcode
e-mailed to the account.  The non-interactive sync (`sync-garmin.py`) cannot
answer that prompt on its own the first time a session must be established, so
this command performs the login and supplies the passcode automatically via
``garmin_mfa.make_email_mfa_provider()`` (which reads the code from the mailbox
Notification folder).  On success the refreshed OAuth session is written to
``<data-dir>/.garmin_session`` and routine syncs run unattended again until
Garmin next invalidates it.

The existing session directory is moved aside before the attempt so the login
is genuinely fresh (and thus triggers the passcode), and restored if the login
fails — so a botched re-auth never leaves you worse off than before.

Auth via environment variables (same as sync-garmin.py):
    GARMIN_EMAIL    — Garmin Connect email
    GARMIN_PASSWORD — Garmin Connect password

Usage:
    python3 scripts/garmin-reauth.py
    python3 scripts/garmin-reauth.py --folder Notification --timeout 240
"""

import argparse
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from garmin_mfa import make_email_mfa_provider  # noqa: E402

REPO_ROOT = Path(os.environ.get("CHAMBER_DIR") or str(Path(__file__).resolve().parent.parent))


def main():
    parser = argparse.ArgumentParser(description="Headless Garmin MFA re-authentication.")
    parser.add_argument("--folder", default="Notification",
                        help="IMAP folder the passcode mail lands in (default: Notification)")
    parser.add_argument("--account", default=None,
                        help="named email_client account (default: the default account)")
    parser.add_argument("--timeout", type=int, default=180,
                        help="seconds to wait for the passcode mail (default: 180)")
    args = parser.parse_args()

    email = os.environ.get("GARMIN_EMAIL", "")
    password = os.environ.get("GARMIN_PASSWORD", "")
    if not email or not password:
        print("Error: set GARMIN_EMAIL and GARMIN_PASSWORD.", file=sys.stderr)
        sys.exit(1)

    from garminconnect import Garmin

    session_dir = Path.home() / ".garmin_session"
    session_path = session_dir
    backup_path = Path.home() / ".garmin_session.bak"

    # Move any existing session aside so the login is fresh and triggers MFA.
    if backup_path.exists():
        shutil.rmtree(backup_path)
    had_session = session_path.exists()
    if had_session:
        session_path.rename(backup_path)

    prompt_mfa = make_email_mfa_provider(
        folder=args.folder, account=args.account, timeout=args.timeout,
    )

    print("Authenticating with Garmin Connect (awaiting emailed passcode)...")
    try:
        client = Garmin(email, password, prompt_mfa=prompt_mfa)
        client.login(tokenstore=str(session_path))
        # Touch a lightweight endpoint to confirm the session really works.
        name = client.get_full_name()
    except Exception as e:
        # Restore the previous session so we don't leave the account worse off.
        if session_path.exists():
            shutil.rmtree(session_path)
        if had_session and backup_path.exists():
            backup_path.rename(session_path)
        print(f"Error: Garmin re-authentication failed: {e}", file=sys.stderr)
        sys.exit(1)

    # Success: drop the backup.
    if backup_path.exists():
        shutil.rmtree(backup_path)

    print(f"Re-authenticated as {name!r}; session saved to {session_path}.")
    print("Run sync-garmin.py (or wait for the refresh dispatcher) to pull data.")


if __name__ == "__main__":
    main()
