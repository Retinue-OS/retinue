#!/usr/bin/env python3
"""CalDAV gateway — reads and writes the user's real calendar.

Models the calendar exactly like a messenger channel (see signal-gateway.py):
one gateway instance owns one calendar account, credentials live only in this
container, and outbound writes go through the same allow/trust/verify
send-policy model with pending events approvable on the web-gateway's /sends
page (see CALDAV_SEND_POLICY below). Reads are token-gated the same way but
carry no send policy — that policy governs what an agent may *change* in the
user's calendar, and a read changes nothing.

Provider-agnostic by design (see issue #13): the backend is selected purely by
configuration (CALDAV_SERVER_URL/CALDAV_USERNAME/CALDAV_PASSWORD/
CALDAV_CALENDAR_ID) via the generic `caldav` client library (RFC 4791). There
is no provider-specific code path — Zoho, or any other CalDAV server, is just
a configured endpoint. The `caldav` import itself is guarded (see below), so
everything except the actual server write is unit-tested in
tests/test_caldav_send_policy.py without the package installed.

Endpoints:
    POST /create-event          create a calendar event (see _create_event_on_server)
    GET  /calendars             the account's calendars, plus the write target
    GET  /events                read events in a time range (see _list_events_on_server)
    GET  /event?uid=<uid>       one event by its iCalendar uid
    GET  /pending-sends         list events awaiting approval
    GET  /pending-sends/<id>    one pending event's detail
    POST /pending-sends/<id>/approve
    POST /pending-sends/<id>/reject
    GET  /health

Scope: create and read. Reading exists because an agent that can only write is
half a calendar — "am I free Thursday?", "what did we agree on?" and "is this
already in the agenda?" all need the other direction. Updating and deleting
existing events remain out of scope; so does *authoring* recurrence, though
recurring events are expanded into their instances on read.
"""
import datetime
import hmac
import json
import os
import re
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

# The caldav package (a generic RFC 4791 client) may not be installed in every
# environment this module is imported into (e.g. a test sandbox). Guard the
# import so the module always loads; only an actual write attempt requires the
# real dependency, and fails there with a clear error instead of at import time.
try:
    import caldav  # noqa: F401
except ImportError:  # pragma: no cover - exercised only when the dep is absent
    caldav = None


# ── Configuration ─────────────────────────────────────────────────────────────
# Server/account. No provider-specific defaults live here — see .env.example
# for a Zoho example, offered only as one configured endpoint among any others.
CALDAV_SERVER_URL = os.environ.get("CALDAV_SERVER_URL", "").strip()
CALDAV_USERNAME = os.environ.get("CALDAV_USERNAME", "").strip()
CALDAV_PASSWORD = os.environ.get("CALDAV_PASSWORD", "").strip()
# Which calendar on the server to write to: its CalDAV id, URL, or display
# name. Unset = the account's default/first calendar (principal.calendar()).
CALDAV_CALENDAR_ID = os.environ.get("CALDAV_CALENDAR_ID", "").strip() or None
# HTTP timeout for talking to the CalDAV server.
CALDAV_TIMEOUT = float(os.environ.get("CALDAV_TIMEOUT", "30"))

# The sending identity CALDAV_SEND_POLICY keys on — a label, not a per-request
# field, exactly like SIGNAL_ACCOUNT/WHATSAPP_ACCOUNT/TELEGRAM_ACCOUNT: one
# gateway instance = one identity. A deployment wanting a second calendar runs
# a second caldav-gateway service (its own credentials, its own CALDAV_ACCOUNT),
# the same "adding more accounts" pattern as the messenger gateways.
CALDAV_ACCOUNT = os.environ.get("CALDAV_ACCOUNT", "").strip() or "default"

HTTP_PORT = int(os.environ.get("CALDAV_GATEWAY_HTTP_PORT", "8094"))
GATEWAY_TOKEN = os.environ.get("CALDAV_GATEWAY_TOKEN", "").strip()
# An event payload is small JSON (no images/attachments) — a generous but
# bounded cap, defence-in-depth against an oversized body.
MAX_BODY_BYTES = int(os.environ.get("CALDAV_GATEWAY_MAX_BODY_BYTES", str(64 * 1024)))

# Read side (GET /events). The window a read covers when the caller names no
# end, and the hard cap on how many events one response may carry — a read of
# "the next year across five calendars" must not return an unbounded payload.
READ_DEFAULT_DAYS = float(os.environ.get("CALDAV_READ_DEFAULT_DAYS", "30"))
READ_MAX_EVENTS = int(os.environ.get("CALDAV_READ_MAX_EVENTS", "500"))

# Outbound send-control policy — the calendar analogue of EMAIL_SEND_POLICY /
# SIGNAL_SEND_POLICY. Keyed by the *sending* identity (CALDAV_ACCOUNT above),
# NOT any per-request field: what governs an autonomous write is which
# configured calendar it targets.
#   allow  — write directly, no confirmation.
#   trust  — write directly only when caldav-push.py passes --user-approved;
#            otherwise falls back to the verify flow.
#   verify — register the event as pending; it is written only after explicit
#            web-gateway approval at /sends (an agent can never approve its own).
# Use "*" as the account for a wildcard default. An account matching no entry
# (and no wildcard) falls back to DEFAULT_SEND_CATEGORY (verify — fail-safe),
# so an undeclared account can never write autonomously.
# Example: CALDAV_SEND_POLICY=[{"account":"default","category":"verify"},{"account":"reminders","category":"allow"}]
DEFAULT_SEND_CATEGORY = "verify"
_send_policy_raw = os.environ.get("CALDAV_SEND_POLICY", "").strip()
CALDAV_SEND_POLICY: list = []
if _send_policy_raw:
    try:
        _parsed_sp = json.loads(_send_policy_raw)
        if isinstance(_parsed_sp, list):
            CALDAV_SEND_POLICY = _parsed_sp
        else:
            print("[caldav-gateway] warning: CALDAV_SEND_POLICY must be a JSON array; using defaults", flush=True)
    except json.JSONDecodeError:
        print("[caldav-gateway] warning: invalid CALDAV_SEND_POLICY JSON; using defaults", flush=True)

# Directory for pending events awaiting web-gateway approval (mirrors
# SIGNAL_PENDING_SENDS_DIR — not on a persistent volume by default, same as
# the messenger gateways; a pending event does not survive a container
# recreation, only a plain restart).
CALDAV_PENDING_SENDS_DIR = Path(os.environ.get("CALDAV_PENDING_SENDS_DIR", "/tmp/caldav-pending-sends"))
CALDAV_PENDING_SENDS_DIR.mkdir(parents=True, exist_ok=True)

# Public base URL used to build approval links returned to the caller.
SEND_APPROVAL_BASE_URL = os.environ.get("SEND_APPROVAL_BASE_URL", "").rstrip("/")
# Optional override for the /sends/<slug>/<id> channel slug this gateway's
# pending events live under on the web-gateway. Normally UNSET: the slug is
# then derived per request from the Host header — the Docker service name the
# caller reached this gateway at — mirroring the messenger gateways exactly
# (see messenger_gateways.py), so approval links resolve with no configuration
# once this gateway is enrolled via MESSENGER_GATEWAYS (see
# docker-compose.override.example.yml — the mechanism is already
# channel-agnostic, so enrolling a calendar gateway needs no framework code
# change).
SEND_APPROVAL_SLUG = os.environ.get("SEND_APPROVAL_SLUG", "").strip("/")


def _approval_slug(host_header) -> str:
    """The /sends/<slug>/… segment for approval links this gateway emits.

    An explicit SEND_APPROVAL_SLUG wins when set; otherwise the service
    hostname from the request's Host header, falling back to "caldav" for
    callers that send none."""
    if SEND_APPROVAL_SLUG:
        return SEND_APPROVAL_SLUG
    host = (host_header or "").split(":", 1)[0].strip().strip("/")
    return host or "caldav"


def _outbound_policy_category() -> str:
    """Resolve this gateway's outbound send-control category.

    Returns 'allow', 'trust', or 'verify'. Falls back to the "*" wildcard, or —
    absent that — to DEFAULT_SEND_CATEGORY ('verify', fail-safe), so an
    undeclared account can never write autonomously.
    """
    wildcard: str | None = None
    for entry in CALDAV_SEND_POLICY:
        if not isinstance(entry, dict):
            continue
        account = str(entry.get("account", ""))
        category = str(entry.get("category", "allow"))
        if account == "*":
            wildcard = category
            continue
        if account.strip() == CALDAV_ACCOUNT:
            return category
    return wildcard if wildcard is not None else DEFAULT_SEND_CATEGORY


def _health_snapshot() -> dict:
    """No persistent link/session exists for CalDAV (unlike the phone-linked
    messenger gateways), so there is nothing to poll continuously — a write's
    own success/failure is what proves reachability, and that is exactly what
    surfaces on /sends per event. /health only reports whether this gateway is
    configured at all; a gateway that answers with configured != false counts
    as up (see gateway-monitor.py's classify_health)."""
    return {
        "status": "ok",
        "configured": bool(CALDAV_SERVER_URL and CALDAV_USERNAME and CALDAV_PASSWORD),
        "account": CALDAV_ACCOUNT,
    }


# ── Event creation (generic CalDAV client — no provider-specific code) ────────

def _parse_event_datetime(value: str, all_day: bool):
    """Parse an ISO 8601 date/date-time string into date (all-day) or datetime."""
    value = (value or "").strip()
    if not value:
        raise ValueError("missing date/time value")
    if all_day:
        try:
            return datetime.date.fromisoformat(value[:10])
        except ValueError as exc:
            raise ValueError(f"invalid all-day date {value!r}: {exc}") from exc
    # Accept a trailing "Z" (UTC) the way most JSON/ISO producers emit it;
    # datetime.fromisoformat only accepts "+00:00" for older Python versions.
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"invalid date-time {value!r}: {exc}") from exc


def _connect_principal():
    """Connect to the configured CalDAV server and return its principal.

    Purely generic: no provider (e.g. Zoho) is special-cased anywhere — the
    backend is entirely a function of CALDAV_SERVER_URL/USERNAME/PASSWORD.
    Shared by the write and the read path, so both fail the same way when the
    dependency or the configuration is missing.
    """
    if caldav is None:
        raise RuntimeError(
            "the 'caldav' package is not installed in this environment; "
            "cannot reach the calendar server"
        )
    if not (CALDAV_SERVER_URL and CALDAV_USERNAME and CALDAV_PASSWORD):
        raise RuntimeError(
            "CalDAV server is not configured "
            "(CALDAV_SERVER_URL/CALDAV_USERNAME/CALDAV_PASSWORD)"
        )
    client = caldav.DAVClient(
        url=CALDAV_SERVER_URL, username=CALDAV_USERNAME, password=CALDAV_PASSWORD,
        timeout=CALDAV_TIMEOUT,
    )
    return client.principal()


def _calendar_identity(cal) -> dict:
    """The id / URL / display name of one calendar, as plain strings.

    Each attribute is read defensively: in the caldav library some of these are
    properties that talk to the server, and one unreadable name must not sink a
    whole listing.
    """
    identity = {}
    for key in ("id", "url", "name"):
        try:
            value = getattr(cal, key, "")
        except Exception:  # pragma: no cover - server-dependent property
            value = ""
        identity[key] = str(value) if value else ""
    return identity


def _match_calendar(cal, target: str) -> bool:
    """Whether `target` names this calendar — by CalDAV id, URL, or display name."""
    return target in set(_calendar_identity(cal).values()) - {""}


def _resolve_calendar(calendar_id: str | None):
    """Return the caldav Calendar object to write to.

    Picks the calendar by id/URL/display name, or the account's default
    calendar when neither the request nor CALDAV_CALENDAR_ID names one.
    """
    principal = _connect_principal()
    target = calendar_id or CALDAV_CALENDAR_ID
    if not target:
        return principal.calendar()
    for cal in principal.calendars():
        if _match_calendar(cal, target):
            return cal
    raise RuntimeError(f"calendar {target!r} not found on the CalDAV server")


def _create_event_on_server(entry: dict) -> str:
    """Write one VEVENT to the configured CalDAV server. Returns the event uid.

    Uses the caldav library's own event-building support (Calendar.save_event)
    rather than hand-assembling iCalendar text, so there is nothing here that
    depends on which server ultimately receives the request.
    """
    all_day = bool(entry.get("all_day"))
    start = _parse_event_datetime(entry["start"], all_day)
    end = _parse_event_datetime(entry["end"], all_day)
    calendar = _resolve_calendar(entry.get("calendar_id"))
    kwargs = {"summary": entry.get("summary") or "", "dtstart": start, "dtend": end}
    if entry.get("description"):
        kwargs["description"] = entry["description"]
    saved = calendar.save_event(**kwargs)
    return str(getattr(saved, "id", "") or "")


# ── Calendar reading (generic CalDAV client — no provider-specific code) ──────
# The mirror image of the write path above, sharing its connection helpers.
# Reads are token-gated like every other endpoint here but deliberately NOT
# subject to CALDAV_SEND_POLICY: that policy exists so an agent cannot change
# the user's calendar unsupervised, and a read changes nothing. The credentials
# still never leave this container — a caller gets event data, never the
# account.


def _parse_window_bound(value: str, *, end_of_day: bool) -> datetime.datetime:
    """Parse one bound of a read window into a datetime.

    A bound given as a plain date (2026-09-20) means midnight for the start and
    the very last instant of that day for the end, so `start=2026-09-20&
    end=2026-09-20` reads the whole of the 20th — the obvious reading of a
    window, and unlike an iCalendar DTEND, which is exclusive.
    """
    value = (value or "").strip()
    if not value:
        raise ValueError("missing date/time value")
    # Accept a trailing "Z" (UTC) the way most ISO producers emit it.
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"invalid date-time {value!r}: {exc}") from exc
    date_only = not any(sep in value for sep in ("T", " ", ":"))
    if date_only and end_of_day:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed


def _align_timezones(start: datetime.datetime, end: datetime.datetime) -> tuple:
    """Make both bounds comparable when only one of them carries a timezone.

    Mixing an aware and a naive datetime raises on comparison, and a caller may
    well pass `start=2026-09-20T00:00:00Z&end=2026-09-27` (or leave one side to
    the default). The naive side is then read in the other's timezone, which is
    what the caller meant by writing them in one breath.
    """
    if (start.tzinfo is None) == (end.tzinfo is None):
        return start, end
    if start.tzinfo is None:
        return start.replace(tzinfo=end.tzinfo), end
    return start, end.replace(tzinfo=start.tzinfo)


def _parse_read_window(start: str, end: str, days: str) -> tuple:
    """Resolve (start, end) for a read from the request's parameters.

    Unset start = now; unset end = start + `days` (READ_DEFAULT_DAYS when that
    too is unset). Raises ValueError — mapped to HTTP 400 — for an unparseable
    bound or an end before the start.
    """
    window_start = _parse_window_bound(start, end_of_day=False) if start else datetime.datetime.now()
    if end:
        window_end = _parse_window_bound(end, end_of_day=True)
    else:
        if days:
            try:
                span = float(days)
            except ValueError as exc:
                raise ValueError(f"invalid 'days' value {days!r}: {exc}") from exc
            if span <= 0:
                raise ValueError("'days' must be positive")
        else:
            span = READ_DEFAULT_DAYS
        window_end = window_start + datetime.timedelta(days=span)
    window_start, window_end = _align_timezones(window_start, window_end)
    if window_end < window_start:
        raise ValueError("'end' is before 'start'")
    return window_start, window_end


def _parse_read_limit(value: str) -> int:
    """How many events one response may carry, clamped to READ_MAX_EVENTS."""
    if not value:
        return READ_MAX_EVENTS
    try:
        limit = int(value)
    except ValueError as exc:
        raise ValueError(f"invalid 'limit' value {value!r}: {exc}") from exc
    if limit <= 0:
        raise ValueError("'limit' must be positive")
    return min(limit, READ_MAX_EVENTS)


# ── iCalendar component → plain JSON ─────────────────────────────────────────
# These read an icalendar VEVENT component, which behaves as a case-insensitive
# mapping whose date/time properties wrap their value in a `.dt` attribute.
# Nothing here imports icalendar, so the whole serialization path is unit-
# testable against a plain dict of upper-case keys (see tests/test_caldav_read.py).

def _icomp_get(component, key: str):
    try:
        return component.get(key)
    except Exception:  # pragma: no cover - defensive against odd components
        return None


def _icomp_text(component, key: str) -> str:
    value = _icomp_get(component, key)
    return "" if value is None else str(value)


def _icomp_dt(component, key: str):
    """The `.dt` payload of a date/time property (date, datetime, or timedelta)."""
    return getattr(_icomp_get(component, key), "dt", None)


def _iso(value) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _serialize_event(component, calendar: dict | None = None) -> dict:
    """One VEVENT as plain JSON, in the same vocabulary /create-event accepts.

    The field names match the create payload (summary/start/end/all_day/
    description) so a read result can be handed straight back to a write. For
    an all-day event `end` is the iCalendar DTEND — exclusive, per RFC 5545 —
    exactly as the write path passes it, so an event round-trips unchanged.
    """
    start = _icomp_dt(component, "DTSTART")
    all_day = isinstance(start, datetime.date) and not isinstance(start, datetime.datetime)
    end = _icomp_dt(component, "DTEND")
    if end is None and start is not None:
        duration = _icomp_dt(component, "DURATION")
        if isinstance(duration, datetime.timedelta):
            end = start + duration
        else:
            # RFC 5545: with neither DTEND nor DURATION an all-day event covers
            # exactly one day and a timed one is instantaneous.
            end = start + datetime.timedelta(days=1) if all_day else start
    entry = {
        "uid": _icomp_text(component, "UID"),
        "summary": _icomp_text(component, "SUMMARY"),
        "start": _iso(start) if start is not None else "",
        "end": _iso(end) if end is not None else "",
        "all_day": all_day,
        "description": _icomp_text(component, "DESCRIPTION"),
        "location": _icomp_text(component, "LOCATION"),
        "status": _icomp_text(component, "STATUS"),
        # True for a recurring series and for one expanded instance of it
        # (which carries RECURRENCE-ID instead of the rule).
        "recurring": bool(_icomp_get(component, "RRULE") or _icomp_get(component, "RDATE")
                          or _icomp_get(component, "RECURRENCE-ID")),
        "calendar": (calendar or {}).get("name", ""),
        # Whatever identifies this calendar to the other endpoints, so a caller
        # can narrow a follow-up read (or a write) to where the event lives.
        "calendar_id": (calendar or {}).get("id", "") or (calendar or {}).get("url", ""),
    }
    return entry


def _event_components(event) -> list:
    """The VEVENT components of one caldav event object.

    An expanded search normally yields one instance per object, but a server may
    answer with a whole calendar component (master plus overrides), so walk it
    when the library exposes the parsed instance.
    """
    instance = getattr(event, "icalendar_instance", None)
    if instance is not None:
        try:
            walked = list(instance.walk("VEVENT"))
        except Exception:  # pragma: no cover - library/version dependent
            walked = []
        if walked:
            return walked
    component = getattr(event, "icalendar_component", None)
    return [component] if component is not None else []


def _pick_read_calendars(principal, calendar_id: str | None) -> list:
    """Which calendars a read covers.

    An explicit `calendar_id` names one; the literal "*" means every calendar on
    the account even when this gateway is configured for a single one; unset
    falls back to CALDAV_CALENDAR_ID and, with that unset too, to every calendar
    — "what is on my agenda" spans the account, whereas a write needs exactly
    one target.
    """
    if calendar_id == "*":
        return list(principal.calendars())
    target = calendar_id or CALDAV_CALENDAR_ID
    if not target:
        return list(principal.calendars())
    for cal in principal.calendars():
        if _match_calendar(cal, target):
            return [cal]
    raise RuntimeError(f"calendar {target!r} not found on the CalDAV server")


def _search_calendar(cal, window_start, window_end) -> list:
    """Time-range search on one calendar, with recurring events expanded.

    `expand=True` turns a recurring series into its instances inside the window,
    which is what a reader wants ("is Thursday taken?"). Servers and caldav
    releases differ in how well they support it, so a failure retries the plain
    time-range query once rather than failing the whole read.
    """
    try:
        return list(cal.search(start=window_start, end=window_end, event=True, expand=True))
    except Exception as exc:
        print(f"[caldav-gateway] expanded search failed ({exc}); retrying unexpanded", flush=True)
        return list(cal.search(start=window_start, end=window_end, event=True))


def _list_events_on_server(window_start, window_end, calendar_id: str | None = None) -> list:
    """Read the events of one window across the calendars a read covers.

    Sorted by start, so callers get a chronological agenda across calendars
    (an all-day event sorts before the timed events of the same day, its start
    being a bare date).
    """
    principal = _connect_principal()
    entries = []
    for cal in _pick_read_calendars(principal, calendar_id):
        identity = _calendar_identity(cal)
        for event in _search_calendar(cal, window_start, window_end):
            for component in _event_components(event):
                entries.append(_serialize_event(component, identity))
    entries.sort(key=lambda entry: entry.get("start") or "")
    return entries


def _matches_query(entry: dict, needle: str) -> bool:
    """Case-insensitive substring match over an event's text fields.

    Filtered here rather than as a CalDAV text-match query: server-side text
    search is unevenly implemented, and this module has no provider-specific
    code paths.
    """
    if not needle:
        return True
    needle = needle.lower()
    return any(needle in str(entry.get(key, "")).lower()
               for key in ("summary", "description", "location", "uid", "calendar"))


def _find_event_by_uid(uid: str, calendar_id: str | None = None) -> dict | None:
    """One event by its iCalendar uid — the uid /create-event returns.

    Searches the same calendars a range read covers, and answers None when no
    calendar holds it.
    """
    principal = _connect_principal()
    for cal in _pick_read_calendars(principal, calendar_id):
        finder = getattr(cal, "event_by_uid", None) or getattr(cal, "object_by_uid", None)
        if finder is None:  # pragma: no cover - library version without uid lookup
            continue
        try:
            event = finder(uid)
        except Exception:
            # Not in this calendar (caldav raises NotFoundError) — keep looking.
            continue
        identity = _calendar_identity(cal)
        for component in _event_components(event):
            return _serialize_event(component, identity)
    return None


def _calendars_snapshot() -> dict:
    """The account's calendars plus the one a write would land in.

    One connection answers both: which calendars exist, and which of them
    /create-event targets with no calendar_id of its own — the question an agent
    asks before offering to add something.
    """
    principal = _connect_principal()
    cals = list(principal.calendars())
    write_target = None
    try:
        if CALDAV_CALENDAR_ID:
            # Resolved against the calendars just listed rather than through
            # _resolve_calendar, which would open a second connection.
            target = next((c for c in cals if _match_calendar(c, CALDAV_CALENDAR_ID)), None)
        else:
            target = principal.calendar()
        if target is not None:
            write_target = _calendar_identity(target)
    except Exception as exc:  # pragma: no cover - server-dependent
        print(f"[caldav-gateway] could not resolve the write target: {exc}", flush=True)
    return {"calendars": [_calendar_identity(cal) for cal in cals], "write_target": write_target}


def _format_pending_body(start: str, end: str, all_day: bool, description: str) -> str:
    """Render the pending-event detail shown in the generic /sends approval
    card's <pre> block (web-gateway.py's _render_send_single_html)."""
    when = f"All day: {start}" if all_day else f"{start} → {end}"
    parts = [when]
    if description:
        parts += ["", description]
    return "\n".join(parts)


# ── Pending-send store ────────────────────────────────────────────────────────
# Mirrors signal-gateway.py's pending-send store exactly (see there for the
# rationale of each piece): events whose policy category is 'verify' (or
# 'trust' without --user-approved) are registered here and written only after
# the user approves them via the web-gateway's /sends page.

_pending_sends: dict = {}
_pending_sends_lock = threading.Lock()

# Request ids are server-generated uuid4 hex strings: 32 lowercase hex chars,
# so they can never contain a path separator or traversal sequence.
_REQUEST_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _lookup_existing_path(request_id: str) -> Path | None:
    """Find the on-disk file for a request id by scanning the pending directory.

    The path is never built from the caller-supplied id; instead the directory
    is enumerated and a file is returned only when its stem matches the id
    exactly — keeps a crafted id from escaping CALDAV_PENDING_SENDS_DIR
    (path-injection safe): only files that already exist there can be reached.
    """
    if not _REQUEST_ID_RE.match(request_id or ""):
        return None
    try:
        for path in CALDAV_PENDING_SENDS_DIR.iterdir():
            if path.is_file() and path.suffix == ".json" and path.stem == request_id:
                return path
    except OSError:
        return None
    return None


def _new_pending_send(summary: str, start: str, end: str, all_day: bool,
                      description: str, calendar_id: str | None, category: str) -> str:
    """Store a pending calendar event and return its request_id."""
    request_id = uuid.uuid4().hex
    entry = {
        "id": request_id,
        "to": CALDAV_ACCOUNT,
        "subject": summary,
        "summary": summary,
        "start": start,
        "end": end,
        "all_day": all_day,
        "description": description,
        "calendar_id": calendar_id,
        "body": _format_pending_body(start, end, all_day, description),
        "category": category,
        "created": int(time.time()),
        "status": "pending",
    }
    # request_id is a freshly generated uuid4 (trusted), so building the path
    # from it here is safe.
    path = CALDAV_PENDING_SENDS_DIR / f"{request_id}.json"
    try:
        path.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        print(f"[caldav-gateway] warning: could not persist pending event: {exc}", flush=True)
    with _pending_sends_lock:
        _pending_sends[request_id] = entry
    return request_id


def _get_pending_send_detail(request_id: str) -> dict | None:
    """Load a pending event from disk (survives service restarts)."""
    path = _lookup_existing_path(request_id)
    if path is None:
        with _pending_sends_lock:
            return dict(_pending_sends[request_id]) if request_id in _pending_sends else None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        with _pending_sends_lock:
            return dict(_pending_sends[request_id]) if request_id in _pending_sends else None


def _list_pending_sends_store() -> list:
    """List all pending events from disk."""
    items = []
    try:
        for path in sorted(CALDAV_PENDING_SENDS_DIR.glob("*.json")):
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(entry, dict):
                    continue
                if entry.get("status") == "pending":
                    items.append(entry)
            except (OSError, json.JSONDecodeError):
                continue
    except OSError:
        pass
    return items


def _execute_approved_event(path: Path, entry: dict) -> None:
    """Run an approved event write and record its terminal status (background
    thread) — mirrors signal-gateway.py's _execute_approved_send (issue #116):
    a slow write must not hold the approval response open past the
    web-gateway's proxy timeout.
    """
    request_id = entry["id"]
    try:
        uid = _create_event_on_server(entry)
        entry["status"] = "approved"
        entry["event_uid"] = uid
        entry.pop("error", None)
        print(f"[caldav-gateway] pending event {request_id} approved and created (uid={uid})", flush=True)
    except Exception as exc:
        print(f"[caldav-gateway] pending event {request_id} execution failed: {exc}", flush=True)
        entry["status"] = "error"
        entry["error"] = str(exc)
    try:
        path.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        print(f"[caldav-gateway] warning: could not update pending event: {exc}", flush=True)
    with _pending_sends_lock:
        _pending_sends.pop(request_id, None)


def _complete_pending_send(request_id: str, approved: bool) -> dict | None:
    """Approve or reject a pending event (see signal-gateway.py for the same
    asynchronous-approval rationale, issue #116)."""
    path = _lookup_existing_path(request_id)
    if path is None:
        return None
    with _pending_sends_lock:
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if entry.get("status") != "pending":
            return entry
        entry["status"] = "sending" if approved else "rejected"
        try:
            path.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            print(f"[caldav-gateway] warning: could not update pending event: {exc}", flush=True)
        _pending_sends.pop(request_id, None)
        snapshot = dict(entry)
    if approved:
        threading.Thread(target=_execute_approved_event, args=(path, dict(entry)),
                         name=f"event-{request_id[:8]}", daemon=True).start()
    else:
        print(f"[caldav-gateway] pending event {request_id} rejected", flush=True)
    return snapshot


# ── HTTP API ─────────────────────────────────────────────────────────────────

_PENDING_SEND_RE = re.compile(r"^/pending-sends/([0-9a-f]{32})(?:/(approve|reject))?/?$")


def _route_and_params(path: str) -> tuple:
    """Split a request target into its route and its query parameters.

    The read endpoints take their arguments in the query string, so routes are
    matched against the path alone — /health?x=1 is still /health.
    """
    parsed = urlsplit(path or "")
    return parsed.path.rstrip("/"), parse_qs(parsed.query, keep_blank_values=True)


def _param(params: dict, name: str) -> str:
    """The first value of one query parameter, stripped; "" when absent."""
    values = params.get(name) or [""]
    return (values[0] or "").strip()


class _PushHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # suppress default access log noise
        return

    def _reply(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authorized(self) -> bool:
        if not GATEWAY_TOKEN:
            return True
        auth = self.headers.get("Authorization", "")
        token = auth[len("Bearer "):].strip() if auth.startswith("Bearer ") else ""
        return bool(token) and hmac.compare_digest(token, GATEWAY_TOKEN)

    def _read_failed(self, what: str, exc: Exception) -> None:
        """Answer a failed read: 400 for a bad request, 502 for the server."""
        if isinstance(exc, ValueError):
            self._reply(400, {"error": str(exc)})
            return
        print(f"[caldav-gateway] {what} failed: {exc}\n{traceback.format_exc()}", flush=True)
        self._reply(502, {"error": f"{what} failed: {exc}"})

    def do_GET(self):
        route, params = _route_and_params(self.path)
        if route in ("", "/health"):
            self._reply(200, _health_snapshot())
            return
        if route == "/pending-sends":
            if not self._authorized():
                self._reply(401, {"error": "unauthorized"})
                return
            self._reply(200, {"pending": _list_pending_sends_store()})
            return
        m = _PENDING_SEND_RE.match(route)
        if m and not m.group(2):
            if not self._authorized():
                self._reply(401, {"error": "unauthorized"})
                return
            detail = _get_pending_send_detail(m.group(1))
            if detail is None:
                self._reply(404, {"error": "not found"})
                return
            self._reply(200, detail)
            return

        # ── Read endpoints (no send policy applies — see the read section) ────
        if route == "/calendars":
            if not self._authorized():
                self._reply(401, {"error": "unauthorized"})
                return
            try:
                snapshot = _calendars_snapshot()
            except Exception as exc:
                self._read_failed("list-calendars", exc)
                return
            snapshot["account"] = CALDAV_ACCOUNT
            self._reply(200, snapshot)
            return

        if route == "/events":
            if not self._authorized():
                self._reply(401, {"error": "unauthorized"})
                return
            try:
                window_start, window_end = _parse_read_window(
                    _param(params, "start"), _param(params, "end"), _param(params, "days"))
                limit = _parse_read_limit(_param(params, "limit"))
            except ValueError as exc:
                self._reply(400, {"error": str(exc)})
                return
            calendar_id = _param(params, "calendar_id") or None
            try:
                events = _list_events_on_server(window_start, window_end, calendar_id)
            except Exception as exc:
                self._read_failed("read-events", exc)
                return
            matched = [e for e in events if _matches_query(e, _param(params, "query"))]
            page = matched[:limit]
            self._reply(200, {
                "events": page,
                "count": len(page),
                "total": len(matched),
                "truncated": len(matched) > len(page),
                "range": {"start": window_start.isoformat(), "end": window_end.isoformat()},
                "account": CALDAV_ACCOUNT,
            })
            return

        if route == "/event":
            if not self._authorized():
                self._reply(401, {"error": "unauthorized"})
                return
            uid = _param(params, "uid")
            if not uid:
                self._reply(400, {"error": "'uid' is required"})
                return
            try:
                entry = _find_event_by_uid(uid, _param(params, "calendar_id") or None)
            except Exception as exc:
                self._read_failed("read-event", exc)
                return
            if entry is None:
                self._reply(404, {"error": f"no event with uid {uid!r}"})
                return
            self._reply(200, entry)
            return

        self._reply(404, {"error": "not found"})

    def do_POST(self):
        route, _params = _route_and_params(self.path)
        # Pending-event approval/rejection.
        m = _PENDING_SEND_RE.match(route)
        if m and m.group(2):
            if not self._authorized():
                self._reply(401, {"error": "unauthorized"})
                return
            request_id = m.group(1)
            verb = m.group(2)
            entry = _complete_pending_send(request_id, approved=(verb == "approve"))
            if entry is None:
                self._reply(404, {"error": "pending event not found"})
                return
            self._reply(200, entry)
            return

        if route != "/create-event":
            self._reply(404, {"error": "not found"})
            return
        if not self._authorized():
            self._reply(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            self._reply(400, {"error": "empty body"})
            return
        if length > MAX_BODY_BYTES:
            self._reply(413, {"error": "payload too large"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            self._reply(400, {"error": f"invalid JSON: {exc}"})
            return
        if not isinstance(payload, dict):
            self._reply(400, {"error": "body must be a JSON object"})
            return

        summary = (payload.get("summary") or "").strip()
        start = (payload.get("start") or "").strip()
        end = (payload.get("end") or "").strip()
        all_day = bool(payload.get("all_day", False))
        description = payload.get("description") or ""
        calendar_id = (payload.get("calendar_id") or "").strip() or None
        user_approved = bool(payload.get("user_approved", False))

        if not summary:
            self._reply(400, {"error": "'summary' is required"})
            return
        if not start or not end:
            self._reply(400, {"error": "'start' and 'end' are required"})
            return
        try:
            _parse_event_datetime(start, all_day)
            _parse_event_datetime(end, all_day)
        except ValueError as exc:
            self._reply(400, {"error": str(exc)})
            return

        category = _outbound_policy_category()
        if category == "verify" or (category == "trust" and not user_approved):
            request_id = _new_pending_send(summary, start, end, all_day, description, calendar_id, category)
            approval_path = f"/sends/{_approval_slug(self.headers.get('Host'))}/{request_id}"
            approval_url = (SEND_APPROVAL_BASE_URL + approval_path) if SEND_APPROVAL_BASE_URL else approval_path
            print(f"[caldav-gateway] pending event registered for {CALDAV_ACCOUNT} "
                  f"(category={category}, id={request_id})", flush=True)
            self._reply(202, {
                "status": "pending_approval",
                "request_id": request_id,
                "approval_url": approval_url,
                "note": (
                    "This calendar event requires web-gateway approval. "
                    "Visit the approval URL to allow or deny."
                ),
            })
            return

        entry = {
            "summary": summary, "start": start, "end": end, "all_day": all_day,
            "description": description, "calendar_id": calendar_id,
        }
        try:
            uid = _create_event_on_server(entry)
        except ValueError as exc:
            self._reply(400, {"error": str(exc)})
            return
        except Exception as exc:
            print(f"[caldav-gateway] create-event failed: {exc}\n{traceback.format_exc()}", flush=True)
            self._reply(502, {"error": f"create-event failed: {exc}"})
            return
        print(f"[caldav-gateway] event created for {CALDAV_ACCOUNT} (uid={uid})", flush=True)
        self._reply(200, {"status": "created", "event_uid": uid, "account": CALDAV_ACCOUNT})


def _serve_http() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), _PushHandler)
    print(f"[caldav-gateway] listening on port {HTTP_PORT}"
          + (" (token required)" if GATEWAY_TOKEN else ""), flush=True)
    server.serve_forever()


def main() -> None:
    if not (CALDAV_SERVER_URL and CALDAV_USERNAME and CALDAV_PASSWORD):
        # Stay up (with /health reporting configured: false) instead of crash-
        # looping: an unconfigured calendar is a deliberate deployment choice,
        # not a fault — mirrors every other gateway's idle-when-unconfigured
        # behaviour.
        print("[caldav-gateway] CalDAV server is not configured — idling "
              "(health reports unconfigured)", flush=True)
    else:
        print(f"[caldav-gateway] started (account={CALDAV_ACCOUNT}, server={CALDAV_SERVER_URL})", flush=True)
    _serve_http()


if __name__ == "__main__":
    main()
