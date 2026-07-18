#!/usr/bin/env python3
"""
Pull daily health summaries from Garmin Connect and write them as CSV
files into observations/inbox/ for the Archivist to pick up.

Requires:
    pip install garminconnect

Auth via environment variables:
    GARMIN_EMAIL    — Garmin Connect email
    GARMIN_PASSWORD — Garmin Connect password

Usage:
    python3 scripts/sync-garmin.py            # last 7 days
    python3 scripts/sync-garmin.py --days 14  # last 14 days
"""
import argparse
import csv
import os
import sys
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(os.environ.get("CHAMBER_DIR") or str(Path(__file__).resolve().parent.parent))
INBOX     = REPO_ROOT / "observations" / "inbox"

COLUMNS = [
    "Date", "Steps", "RestingHR", "AvgHRV",
    "TotalSleepMin", "DeepSleepMin", "REMSleepMin", "LightSleepMin",
    "AvgStress", "SpO2", "BodyBattery", "SkinTemp", "Pushes",
]


def get_client(email: str, password: str):
    """Authenticate and return a Garmin Connect client.

    When the stored session has expired, Garmin demands an emailed one-time
    passcode.  We wire in the shared e-mail MFA provider so the sync can answer
    that prompt unattended (reading the code from the mailbox Notification
    folder) and self-heal instead of aborting.  Building the provider does no
    I/O, so the common case — a still-valid session, no MFA prompt — is
    unaffected.
    """
    from garminconnect import Garmin

    # login(tokenstore=...) loads an existing session when the path exists,
    # saves a new one after a fresh login, and accepts str (not Path).
    session_path = str(Path.home() / ".garmin_session")

    prompt_mfa = None
    try:
        from garmin_mfa import make_email_mfa_provider
        prompt_mfa = make_email_mfa_provider()
    except Exception:
        # No mailbox access available — fall back to a plain login attempt.
        prompt_mfa = None

    client = Garmin(email, password, prompt_mfa=prompt_mfa)
    client.login(tokenstore=session_path)
    return client


def safe_get(d: dict | None, *keys, default=""):
    """Safely traverse nested dicts, returning default on any miss."""
    val = d
    for k in keys:
        if not isinstance(val, dict):
            return default
        val = val.get(k)
        if val is None:
            return default
    return val


def fetch_day(client, day: date) -> dict:
    """Fetch a single day's summary and return a flat row dict."""
    iso = day.isoformat()

    # Daily summary (steps, stress, body battery, SpO2)
    try:
        stats = client.get_stats(iso)
    except Exception:
        stats = {}

    # Sleep
    try:
        sleep = client.get_sleep_data(iso)
        sleep_summary = safe_get(sleep, "dailySleepDTO") or {}
    except Exception:
        sleep_summary = {}

    # HRV
    try:
        hrv_data = client.get_hrv_data(iso)
        hrv_summary = safe_get(hrv_data, "hrvSummary") or {}
    except Exception:
        hrv_summary = {}

    def minutes(seconds):
        if seconds is None or seconds == "":
            return ""
        try:
            return str(round(int(seconds) / 60))
        except (ValueError, TypeError):
            return ""

    return {
        "Date":          iso,
        "Steps":         safe_get(stats, "totalSteps"),
        "RestingHR":     safe_get(stats, "restingHeartRate"),
        "AvgHRV":        safe_get(hrv_summary, "weeklyAvg",
                                  default=safe_get(hrv_summary, "lastNightAvg")),
        "TotalSleepMin": minutes(safe_get(sleep_summary, "sleepTimeInSeconds",
                                          default=safe_get(sleep_summary, "durationInSeconds"))),
        "DeepSleepMin":  minutes(safe_get(sleep_summary, "deepSleepSeconds")),
        "REMSleepMin":   minutes(safe_get(sleep_summary, "remSleepSeconds")),
        "LightSleepMin": minutes(safe_get(sleep_summary, "lightSleepSeconds")),
        "AvgStress":     safe_get(stats, "averageStressLevel"),
        "SpO2":          safe_get(stats, "averageSpo2"),
        "BodyBattery":   safe_get(stats, "bodyBatteryMostRecentValue"),
        "SkinTemp":      safe_get(sleep_summary, "averageSkinTempC"),
        "Pushes":        safe_get(stats, "totalPushes"),
    }


def main():
    parser = argparse.ArgumentParser(description="Pull Garmin daily data to inbox.")
    parser.add_argument("--days", type=int, default=7, help="Number of days to fetch (default 7)")
    args = parser.parse_args()

    email    = os.environ.get("GARMIN_EMAIL", "")
    password = os.environ.get("GARMIN_PASSWORD", "")
    if not email or not password:
        print("Error: Set GARMIN_EMAIL and GARMIN_PASSWORD environment variables.", file=sys.stderr)
        sys.exit(1)

    print("Authenticating with Garmin Connect...")
    client = get_client(email, password)

    today = date.today()
    rows  = []
    for i in range(args.days):
        day = today - timedelta(days=i)
        print(f"  Fetching {day.isoformat()} ...", end=" ", flush=True)
        row = fetch_day(client, day)
        rows.append(row)
        non_empty = sum(1 for k, v in row.items() if k != "Date" and v != "")
        print(f"{non_empty} metrics")

    # Write one combined CSV to inbox
    out_path = INBOX / f"garmin-daily-{today.isoformat()}.csv"
    INBOX.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nWrote {len(rows)} days → {out_path.relative_to(REPO_ROOT)}")
    print("Run the Archivist to move this file and ingest it into a sibling .nt file.")


if __name__ == "__main__":
    main()
