#!/usr/bin/env python3
"""Read the user's calendar through the caldav-gateway.

The retinue-side client for the gateway's read endpoints — the counterpart of
caldav-push.py, and the calendar analogue of signal-contacts.py: the credentials
stay in the gateway container, this script only asks it questions. Use it before
proposing a time ("is Thursday free?"), before adding something that may already
be in the agenda, and whenever the user asks what is coming up.

Reads need no approval — nothing is written, so CALDAV_SEND_POLICY does not
apply; only the gateway token gates the request.

Examples:
    # The next 30 days, across every calendar on the account
    caldav-read.py

    # A week, human-readable
    caldav-read.py --days 7 --text

    # One explicit window (a plain --end date covers that whole day)
    caldav-read.py --start 2026-09-20 --end 2026-09-26

    # Is there anything about the dentist in the next three months?
    caldav-read.py --days 90 --query dentist

    # Which calendars exist, and where would a write land?
    caldav-read.py --calendars

    # One event by the uid caldav-push.py printed
    caldav-read.py --uid 7f3c…@example.com

Configuration (environment):
    CALDAV_GATEWAY_BASE_URL  default http://caldav-gateway:8094
    CALDAV_GATEWAY_TOKEN     optional bearer token (must match the gateway)
    CALDAV_GATEWAY_TIMEOUT   HTTP timeout in seconds (default 30)
"""
import argparse
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = os.environ.get("CALDAV_GATEWAY_BASE_URL", "http://caldav-gateway:8094").rstrip("/")
TOKEN = os.environ.get("CALDAV_GATEWAY_TOKEN", "").strip()
DEFAULT_TIMEOUT = float(os.environ.get("CALDAV_GATEWAY_TIMEOUT", "30"))


def _fetch_or_exit(base: str, path: str, params: dict, timeout: float) -> dict:
    """GET one gateway endpoint, or print a diagnostic and exit non-zero."""
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in ("", None)})
    url = f"{base}{path}" + (f"?{query}" if query else "")
    headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(raw).get("error", "")
        except ValueError:
            detail = raw.strip()[:200]
        print(f"caldav-read: gateway returned {exc.code} for {path}: {detail}", file=sys.stderr)
        raise SystemExit(1)
    except (urllib.error.URLError, OSError) as exc:
        print(f"caldav-read: could not reach gateway at {base}: {exc}", file=sys.stderr)
        raise SystemExit(1)


def _short(stamp: str) -> str:
    """An ISO stamp without its sub-second noise, for the window header.

    Only the fractional seconds go: splitting at the "." would take a trailing
    "Z" or "+02:00" with them and misstate the window's timezone.
    """
    return re.sub(r"\.\d+", "", stamp, count=1)


def _all_day_span(event: dict) -> str:
    """The days an all-day event covers, as its own dates read them.

    Its `end` is the exclusive iCalendar DTEND, so a one-day event ends the
    following day and prints as a single date, while a longer one prints through
    its last covered day — otherwise a three-day conference reads as one day.
    """
    start = event.get("start", "?")
    end = event.get("end", "")
    try:
        first = datetime.date.fromisoformat(start)
        last = datetime.date.fromisoformat(end) - datetime.timedelta(days=1)
    except ValueError:
        return start
    return start if last <= first else f"{start} – {last.isoformat()}"


def _render_events(body: dict) -> str:
    """One line per event — the compact rendering --text prints."""
    events = body.get("events", [])
    window = body.get("range")
    lines = []
    if window:
        lines.append(f"{_short(window.get('start', '?'))} → {_short(window.get('end', '?'))}: "
                     f"{body.get('total', len(events))} event(s)")
        if body.get("truncated"):
            lines[0] += f" (showing {body.get('count', len(events))})"
    for event in events:
        when = _all_day_span(event) if event.get("all_day") else \
            f"{event.get('start', '?')} – {event.get('end', '?')}"
        parts = [when, event.get("summary") or "(no title)"]
        if event.get("location"):
            parts.append(f"@ {event['location']}")
        if event.get("calendar"):
            parts.append(f"[{event['calendar']}]")
        if event.get("all_day"):
            parts.append("(all day)")
        if event.get("recurring"):
            parts.append("(recurring)")
        lines.append("  " + "  ".join(parts))
        if event.get("uid"):
            lines.append(f"      uid: {event['uid']}")
    return "\n".join(lines)


def _render_calendars(body: dict) -> str:
    write_target = body.get("write_target") or {}
    lines = [f"account: {body.get('account', '?')}"]
    if body.get("write_target_error"):
        lines.append(f"  ⚠ {body['write_target_error']}")
    for cal in body.get("calendars", []):
        marker = " ← writes land here" if cal.get("url") and cal.get("url") == write_target.get("url") else ""
        lines.append(f"  {cal.get('name') or '(unnamed)'}{marker}")
        lines.append(f"      id: {cal.get('id') or '—'}   url: {cal.get('url') or '—'}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read calendar events via the caldav-gateway."
    )
    parser.add_argument("--start", default="",
                        help="window start, ISO 8601 date or date-time "
                             "(default: now). A plain date means midnight.")
    parser.add_argument("--end", default="",
                        help="window end, ISO 8601 date or date-time. A plain "
                             "date covers that whole day. Default: --days after --start.")
    parser.add_argument("--days", default="",
                        help="window length in days when --end is not given "
                             "(gateway default: 30)")
    parser.add_argument("--calendar-id", default="",
                        help="read one calendar (id/URL/display name), or '*' for "
                             "every calendar on the account; default: the "
                             "gateway's configured calendar, or all of them")
    parser.add_argument("--query", "-q", default="",
                        help="case-insensitive substring filter on title, "
                             "description, location or uid")
    parser.add_argument("--uid", default="",
                        help="read one event by its iCalendar uid instead of a range")
    parser.add_argument("--limit", default="", help="maximum number of events to return")
    parser.add_argument("--calendars", action="store_true",
                        help="list the account's calendars instead of events")
    parser.add_argument("--text", action="store_true",
                        help="print a compact human-readable rendering instead of JSON")
    parser.add_argument("--url", default=DEFAULT_BASE,
                        help=f"gateway base URL (default {DEFAULT_BASE})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="HTTP timeout in seconds")
    args = parser.parse_args()

    base = args.url.rstrip("/")

    if args.calendars:
        body = _fetch_or_exit(base, "/calendars", {}, args.timeout)
        print(_render_calendars(body) if args.text
              else json.dumps(body, ensure_ascii=False, indent=2))
        return 0

    if args.uid:
        body = _fetch_or_exit(base, "/event",
                              {"uid": args.uid, "calendar_id": args.calendar_id},
                              args.timeout)
        if args.text:
            print(_render_events({"events": [body]}))
        else:
            print(json.dumps(body, ensure_ascii=False, indent=2))
        return 0

    body = _fetch_or_exit(base, "/events", {
        "start": args.start, "end": args.end, "days": args.days,
        "calendar_id": args.calendar_id, "query": args.query, "limit": args.limit,
    }, args.timeout)
    print(_render_events(body) if args.text
          else json.dumps(body, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
