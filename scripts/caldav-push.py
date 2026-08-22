#!/usr/bin/env python3
"""Create a calendar event through the caldav-gateway.

This is the retinue-side client for the gateway's `/create-event` endpoint —
the calendar analogue of signal-push.py. Agents use it to put something on the
user's real calendar ("add this to my agenda") instead of the old workarounds
(a downloaded .ics file, an external "add to calendar" link).

Examples:
    # A timed event
    caldav-push.py "Dentist" --start 2026-09-03T14:00:00 --end 2026-09-03T14:30:00

    # An all-day event with a description
    caldav-push.py "Conference" --start 2026-09-10 --end 2026-09-12 --all-day \\
        --description "Keynote at 9am, badge pickup Wednesday"

    # Target a non-default calendar
    caldav-push.py "Team lunch" --start 2026-09-05T12:00:00 --end 2026-09-05T13:00:00 \\
        --calendar-id reminders

Configuration (environment):
    CALDAV_GATEWAY_CREATE_URL  default http://caldav-gateway:8094/create-event
    CALDAV_GATEWAY_TOKEN       optional bearer token (must match the gateway)
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = os.environ.get("CALDAV_GATEWAY_CREATE_URL", "http://caldav-gateway:8094/create-event")
TOKEN = os.environ.get("CALDAV_GATEWAY_TOKEN", "").strip()
DEFAULT_TIMEOUT = float(os.environ.get("CALDAV_GATEWAY_TIMEOUT", "30"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a calendar event via the caldav-gateway.")
    parser.add_argument("summary", help="event title")
    parser.add_argument("--start", required=True,
                        help="start date/time, ISO 8601 (e.g. 2026-09-03T14:00:00, or "
                             "2026-09-03 with --all-day)")
    parser.add_argument("--end", required=True, help="end date/time, ISO 8601 (same format as --start)")
    parser.add_argument("--all-day", action="store_true",
                        help="create an all-day event (--start/--end are plain dates)")
    parser.add_argument("--description", default="", help="event description/notes")
    parser.add_argument("--calendar-id",
                        help="target calendar (id/URL/display name); defaults to the "
                             "gateway's configured calendar")
    parser.add_argument("--user-approved", action="store_true",
                        help="assert that the user has already approved this event; "
                             "bypasses the verify flow for 'trust'-category accounts")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"gateway create-event URL (default {DEFAULT_URL})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="HTTP timeout in seconds")
    args = parser.parse_args()

    payload: dict = {
        "summary": args.summary,
        "start": args.start,
        "end": args.end,
        "all_day": args.all_day,
        "description": args.description,
    }
    if args.calendar_id:
        payload["calendar_id"] = args.calendar_id
    if args.user_approved:
        payload["user_approved"] = True

    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"

    request = urllib.request.Request(
        args.url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if body.get("status") == "pending_approval":
            print(f"caldav-push: event queued for approval (id={body.get('request_id', '?')})")
            approval_url = body.get("approval_url", "")
            # The gateway returns an absolute URL only when SEND_APPROVAL_BASE_URL
            # is set on its side; otherwise it hands back a bare relative path.
            # Absolutize it here against SEND_APPROVAL_BASE_URL, falling back to
            # CONVERSATION_BASE_URL (present in this container), so the printed
            # link is always complete. Mirrors signal-push.py / email_client.
            if approval_url.startswith("/"):
                base = (os.environ.get("SEND_APPROVAL_BASE_URL")
                        or os.environ.get("CONVERSATION_BASE_URL", "")).rstrip("/")
                if base:
                    approval_url = base + approval_url
            if approval_url:
                print(f"caldav-push: approve or deny at {approval_url}")
            note = body.get("note", "")
            if note:
                print(f"caldav-push: {note}")
            return 0
        print(f"caldav-push: created (uid={body.get('event_uid', '?')})")
        return 0
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(raw).get("error", "")
        except ValueError:
            detail = raw.strip()[:200]
        print(f"caldav-push: gateway returned {exc.code}: {detail}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"caldav-push: could not reach gateway at {args.url}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
