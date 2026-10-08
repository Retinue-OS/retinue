#!/usr/bin/env python3
"""Create a calendar event through the caldav-gateway.

This is the retinue-side client for the gateway's `/create-event` endpoint —
the calendar analogue of signal-push.py. Agents use it to put something on the
user's real calendar ("add this to my agenda") instead of the old workarounds
(a downloaded .ics file, an external "add to calendar" link).

A timed event must state its zone: a bare 14:00 is ambiguous, and the gateway
refuses it rather than guess. Prefer naming the zone with --tz (an IANA name
such as Europe/Zurich) — the offset for that date, summer or winter time, is
then worked out here instead of by whoever types the command. An explicit
offset (2026-09-03T14:00:00+02:00, or a trailing Z) works too.

Examples:
    # A timed event
    caldav-push.py "Dentist" --start 2026-09-03T14:00:00 --end 2026-09-03T14:30:00 \\
        --tz Europe/Zurich

    # An all-day event with a description
    caldav-push.py "Conference" --start 2026-09-10 --end 2026-09-12 --all-day \\
        --description "Keynote at 9am, badge pickup Wednesday"

    # Target a non-default calendar
    caldav-push.py "Team lunch" --start 2026-09-05T12:00:00+02:00 --end 2026-09-05T13:00:00+02:00 \\
        --calendar-id reminders

Configuration (environment):
    CALDAV_GATEWAY_CREATE_URL  default http://caldav-gateway:8094/create-event
    CALDAV_GATEWAY_TOKEN       optional bearer token (must match the gateway)
"""
import argparse
import datetime
import json
import os
import sys
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pending_retract

DEFAULT_URL = os.environ.get("CALDAV_GATEWAY_CREATE_URL", "http://caldav-gateway:8094/create-event")
TOKEN = os.environ.get("CALDAV_GATEWAY_TOKEN", "").strip()
DEFAULT_TIMEOUT = float(os.environ.get("CALDAV_GATEWAY_TIMEOUT", "30"))


def _with_offset(value: str, zone) -> str:
    """The date-time `value` as ISO 8601 with its UTC offset.

    A naive value is read as wall-clock time in `zone` (None: no zone given),
    and a value carrying its own offset must agree with `zone` at that moment.
    Raises ValueError with the message the user sees; an unparseable value is
    passed through for the gateway to reject.
    """
    value = value.strip()
    try:
        parsed = datetime.datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        if zone is None:
            raise ValueError(f"{value} names no zone; add --tz (e.g. --tz Europe/Zurich) "
                             f"or an explicit offset")
        return parsed.replace(tzinfo=zone).isoformat()
    if zone is not None and parsed.utcoffset() != parsed.astimezone(zone).utcoffset():
        raise ValueError(f"{value} contradicts --tz {zone.key}, whose offset on that "
                         f"date is {parsed.astimezone(zone).strftime('%z')}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a calendar event via the caldav-gateway.")
    parser.add_argument("summary", nargs="?", default="", help="event title")
    parser.add_argument("--start",
                        help="start date/time, ISO 8601 (e.g. 2026-09-03T14:00:00 with "
                             "--tz, 2026-09-03T14:00:00+02:00, or 2026-09-03 with --all-day)")
    parser.add_argument("--end", help="end date/time, ISO 8601 (same format as --start)")
    parser.add_argument("--tz", metavar="ZONE",
                        help="IANA zone of a timed event (e.g. Europe/Zurich); summer/winter "
                             "time is resolved for the event's date")
    parser.add_argument("--all-day", action="store_true",
                        help="create an all-day event (--start/--end are plain dates)")
    parser.add_argument("--description", default="", help="event description/notes")
    parser.add_argument("--calendar-id",
                        help="target calendar (id/URL/display name); defaults to the "
                             "gateway's configured calendar")
    parser.add_argument("--user-approved", action="store_true",
                        help="assert that the user has already approved this event; "
                             "bypasses the verify flow for 'trust'-category accounts")
    parser.add_argument("--retract", metavar="REQUEST_ID",
                        help="retract a queued event you created (the id printed when it "
                             "was queued) before the user approves it; nothing is added")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"gateway create-event URL (default {DEFAULT_URL})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="HTTP timeout in seconds")
    args = parser.parse_args()

    if args.retract:
        return pending_retract.retract("caldav-push", args.url, args.retract, TOKEN,
                                       args.timeout, noun="event")
    if not args.summary or not args.start or not args.end:
        parser.error("an event needs a summary, --start and --end")
    if not args.all_day:
        zone = None
        if args.tz:
            try:
                zone = ZoneInfo(args.tz)
            except (ZoneInfoNotFoundError, ValueError):
                parser.error(f"--tz {args.tz} is not a known IANA zone (e.g. Europe/Zurich)")
        try:
            args.start = _with_offset(args.start, zone)
            args.end = _with_offset(args.end, zone)
        except ValueError as exc:
            parser.error(str(exc))

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
            print(f"caldav-push: take it back before approval with "
                  f"caldav-push.py --retract {body.get('request_id', '?')}")
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
