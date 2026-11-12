"""The Content-Security-Policy of the dashboard's origin.

The web gateway serves the dashboard, the pages it builds itself and the JSON
API on one origin, behind one sign-in. Content from outside (a message, a
mail, a file someone sent) must never become a page, a script or a link of
another scheme there; that is what the gateway's own escaping and its media
rules see to. This policy is the second line behind them: should markup from
outside reach a page all the same, it cannot run script there, since scripts
run only from the dashboard's own files.

Every HTML response carries it (the gateway's handler adds it wherever the
answer is text/html, docs/dashboard.md, "The Content-Security-Policy"), in the
mode RETINUE_CSP names:

    report   (the default) Content-Security-Policy-Report-Only: the browser
             blocks nothing and reports what it would have blocked
    enforce  Content-Security-Policy: the browser blocks it, and reports it
    off      no policy header at all

The mode is read on every response, so a test (or a restart with a changed
environment) needs nothing else; an unknown value counts as report, the mode
that changes nothing a page does, and is named in the log once.

What the browsers report reaches POST /csp-report, and this module keeps it:
the distinct violations, by the page, the directive and what was blocked,
with how often and when each was first and last seen, in one small JSON file
(RETINUE_CSP_REPORTS_FILE). GET /csp-report shows them, so the owner can see
after a week of report mode whether the policy can be enforced.

No dependencies beyond the standard library.
"""
import json
import os
import re
import threading
import time
from pathlib import Path

MODES = ("report", "enforce", "off")
DEFAULT_MODE = "report"

# Where the browser sends what it reports: the gateway's own endpoint
# (POST /csp-report), on this origin and behind the dashboard's sign-in like
# every other path, named by `report-uri`, in enforce mode too, so what the
# policy blocks keeps being seen.
#
# Not `report-to`, though the endpoint takes its format as well: Chromium
# (and so the Android shell's WebView) ignores `report-uri` as soon as a
# policy names a `report-to` group, and leaves delivery to the Reporting API,
# which batches reports and sends them when it sees fit. Tried in headless
# Chromium, that delivered nothing within a minute, with the endpoint named
# relative or absolute, while `report-uri` alone arrives at once. A week of
# report mode that hears nothing from Chromium would read as a clean week.
REPORT_PATH = "/csp-report"

# The policy, directive by directive, each source with the page that needs it.
# What a page does not load is not allowed: a source added here is one more
# thing markup that slipped through could reach.
DIRECTIVES = (
    # Anything a directive below does not name: this origin only.
    ("default-src", ("'self'",)),
    # The point of the policy: scripts only from the dashboard's own files
    # (webapp/, served from this origin). No 'unsafe-inline': no inline
    # <script>, no handler attribute, no javascript: URL; no 'unsafe-eval':
    # no string compiled as code. /sparql's vendored YASGUI runs under it
    # too: it compiles nothing (its eval and Function fallbacks are only
    # reached where JSON or globalThis are missing).
    ("script-src", ("'self'",)),
    # Restricting styles is not the aim: the components set style attributes
    # and write their own <style> into their shadow roots, and the gateway's
    # pages carry theirs inline.
    ("style-src", ("'self'", "'unsafe-inline'")),
    # Images: this origin (icons, thread attachments, messenger media through
    # the media proxy, the pairing codes on /gateways); data: for the
    # reference documents under webapp/data/chats/, which the chat page shows
    # as data: images, and for the icons YASGUI's stylesheet carries (/sparql);
    # blob: for the chat page's previews of the images the user is about to
    # send (object URLs the page makes itself).
    ("img-src", ("'self'", "data:", "blob:")),
    # Audio and video: this origin (messenger media through the media proxy,
    # the briefing card's audio); data: for the reference documents' data:
    # audio and video on the chat page.
    ("media-src", ("'self'", "data:")),
    # No page loads a font: the dashboard uses the system's.
    ("font-src", ("'none'",)),
    # fetch() and the like go to this origin's API only; the dashboard makes
    # no third-party requests.
    ("connect-src", ("'self'",)),
    # The file viewer (viewer.html) shows a thread attachment of this origin
    # in a frame; no page frames anything else.
    ("frame-src", ("'self'",)),
    ("object-src", ("'none'",)),
    ("base-uri", ("'self'",)),
    # The approval cards' Allow and Deny post to this origin, and so does
    # every other form.
    ("form-action", ("'self'",)),
    # The service worker (sw.js).
    ("worker-src", ("'self'",)),
    ("manifest-src", ("'self'",)),
)

_warned: set = set()


def mode() -> str:
    """The mode in force: RETINUE_CSP, else report."""
    value = (os.environ.get("RETINUE_CSP") or "").strip().lower()
    if not value:
        return DEFAULT_MODE
    if value in MODES:
        return value
    if value not in _warned:
        _warned.add(value)
        print(f"[web-gateway] RETINUE_CSP={value!r} is none of {', '.join(MODES)}; "
              f"the policy is sent in {DEFAULT_MODE} mode", flush=True)
    return DEFAULT_MODE


def policy() -> str:
    """The policy as it is sent: the directives, then where reports go."""
    parts = [f"{name} {' '.join(sources)}" for name, sources in DIRECTIVES]
    parts.append(f"report-uri {REPORT_PATH}")
    return "; ".join(parts)


def header_name(current: str | None = None) -> str | None:
    """The response header the policy goes out in, in this mode; None when off."""
    current = current or mode()
    if current == "enforce":
        return "Content-Security-Policy"
    if current == "report":
        return "Content-Security-Policy-Report-Only"
    return None


def headers() -> list[tuple[str, str]]:
    """The headers an HTML response carries: the policy in the mode in force.
    Empty when off.

    The policy is a header of its own beside any other a page carries: the
    instance settings page's framing policy (frame-ancestors) stays a
    separate, always enforced Content-Security-Policy, whatever the mode
    here, and a browser applies both."""
    name = header_name()
    if name is None:
        return []
    return [(name, policy())]


def signature() -> str:
    """What the service worker's shell hash takes in of the policy: its mode
    and its text. The worker serves the shell pages from its cache, headers
    and all, so a page keeps the policy it was cached with until the worker
    is replaced; a changed mode or policy has to replace it, as a changed file
    does."""
    return f"{mode()}:{policy()}"


# ── The reports ───────────────────────────────────────────────────────────────
# A report is written by the browser, but about a page that may hold content
# from outside, and anyone signed in can post one: every field is text from
# outside. So a report is read for four things only, each cut to one short
# line of printable text, and nothing of it is ever sent back: the endpoint
# answers 204 or a fixed error. The query string and fragment of a URL are
# cut off (a page's query names a thread or a file; a blocked URL's may carry
# anything), which also makes the same violation on two threads one entry.

# The two formats: application/csp-report (report-uri, what the policy names)
# and application/reports+json (report-to, the Reporting API's; taken too,
# so the policy can move to it once its delivery can be relied on). Neither is a type a page of another site can post without a
# CORS preflight, which this origin never answers, so a page elsewhere
# cannot fill the file with reports through the user's sign-in.
FORMATS = ("application/csp-report", "application/reports+json")
# A report is a few hundred bytes; a Reporting API delivery batches a few.
REPORT_MAX_BYTES = 64 * 1024
# Reports read from one delivery, and distinct violations kept. A policy that
# fits the dashboard leaves a handful; past the limit only the count of what
# was not kept grows, so the file stays small whatever is posted.
REPORTS_PER_DELIVERY = 50
VIOLATIONS_LIMIT = 200
_FIELD_MAX = 300

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_DIRECTIVE_RE = re.compile(r"[a-z][a-z-]{0,39}")
_LOCK = threading.Lock()


def report_format(content_type) -> str | None:
    """Which of FORMATS a request's Content-Type names, or None."""
    essence = str(content_type or "").split(";", 1)[0].strip().lower()
    return essence if essence in FORMATS else None


def _text(value) -> str:
    if not isinstance(value, str):
        return ""
    return _CONTROL_RE.sub("", value).strip()[:_FIELD_MAX]


def _without_query(url: str) -> str:
    for mark in ("?", "#"):
        url = url.split(mark, 1)[0]
    return url


def _document(value) -> str:
    """The page a violation happened on: its path on this origin."""
    url = _without_query(_text(value))
    m = re.match(r"(?i)https?://[^/]*(/.*)?$", url)
    if m:
        return m.group(1) or "/"
    return url


def _violation(document, directive, blocked, disposition, source, line) -> dict | None:
    document = _document(document)
    directive = _text(directive).split(" ", 1)[0].lower()
    if not document or not _DIRECTIVE_RE.fullmatch(directive):
        return None
    disposition = _text(disposition).lower()
    where = _without_query(_text(source))
    if where and isinstance(line, int) and not isinstance(line, bool) and line > 0:
        where = f"{where}:{line}"
    return {"document": document, "directive": directive,
            "blocked": _without_query(_text(blocked)) or "(none)",
            "disposition": disposition if disposition in ("enforce", "report") else "",
            "source": where}


def parse_reports(content_type, body: bytes) -> list[dict] | None:
    """The violations a delivery reports, or None when it is no CSP report.

    A delivery of the Reporting API may hold reports of other types (an
    intervention, a deprecation); those are skipped, and a delivery holding
    none of the CSP's is an empty list, not an error."""
    fmt = report_format(content_type)
    if fmt is None:
        return None
    try:
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    out = []
    if fmt == "application/csp-report":
        report = doc.get("csp-report") if isinstance(doc, dict) else None
        if not isinstance(report, dict):
            return None
        v = _violation(report.get("document-uri"),
                       report.get("effective-directive") or report.get("violated-directive"),
                       report.get("blocked-uri"), report.get("disposition"),
                       report.get("source-file"), report.get("line-number"))
        return [v] if v else None
    if not isinstance(doc, list):
        return None
    for report in doc[:REPORTS_PER_DELIVERY]:
        if not isinstance(report, dict) or report.get("type") != "csp-violation":
            continue
        b = report.get("body")
        if not isinstance(b, dict):
            continue
        v = _violation(b.get("documentURL") or report.get("url"), b.get("effectiveDirective"),
                       b.get("blockedURL"), b.get("disposition"), b.get("sourceFile"),
                       b.get("lineNumber"))
        if v:
            out.append(v)
    return out


def _load(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = None
    if not isinstance(doc, dict) or not isinstance(doc.get("violations"), list):
        return {"violations": [], "not_kept": 0}
    doc["violations"] = [v for v in doc["violations"] if isinstance(v, dict)]
    if not isinstance(doc.get("not_kept"), int):
        doc["not_kept"] = 0
    return doc


def record(path: Path, violations: list[dict], now: float | None = None) -> list[dict]:
    """Count ``violations`` into the file at ``path``; the ones seen for the
    first time come back, for the log. A file that cannot be written loses
    the reports, never the request: an OSError is the caller's to log."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now if now is not None else time.time()))
    new = []
    with _LOCK:
        doc = _load(path)
        index = {(v.get("document"), v.get("directive"), v.get("blocked")): v
                 for v in doc["violations"]}
        for v in violations:
            key = (v["document"], v["directive"], v["blocked"])
            entry = index.get(key)
            if entry is None:
                if len(doc["violations"]) >= VIOLATIONS_LIMIT:
                    doc["not_kept"] += 1
                    continue
                entry = {"document": v["document"], "directive": v["directive"],
                         "blocked": v["blocked"], "count": 0, "first_seen": stamp}
                doc["violations"].append(entry)
                index[key] = entry
                new.append(v)
            entry["count"] = int(entry.get("count") or 0) + 1
            entry["last_seen"] = stamp
            entry["disposition"] = v["disposition"]
            if v["source"]:
                entry["source"] = v["source"]
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    return new


def summary(path: Path) -> dict:
    """What GET /csp-report answers: the mode, the policy, and the violations
    kept, the latest first."""
    with _LOCK:
        doc = _load(path)
    violations = sorted(doc["violations"], key=lambda v: str(v.get("last_seen") or ""),
                        reverse=True)
    return {"mode": mode(), "policy": policy(), "violations": violations,
            "not_kept": doc["not_kept"], "limit": VIOLATIONS_LIMIT}


def log_line(v: dict) -> str:
    """One line for the gateway's log about a violation seen for the first
    time: every part is already one line of printable text."""
    where = f" ({v['source']})" if v.get("source") else ""
    return (f"[web-gateway] CSP {v.get('disposition') or 'report'}: {v['directive']} "
            f"blocked {v['blocked']} on {v['document']}{where}")
