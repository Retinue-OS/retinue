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
             blocks nothing and says what it would have blocked
    enforce  Content-Security-Policy: the browser blocks it
    off      no policy header at all

The mode is read on every response, so a test (or a restart with a changed
environment) needs nothing else; an unknown value counts as report, the mode
that changes nothing a page does, and is named in the log once.

No dependencies beyond the standard library.
"""
import os

MODES = ("report", "enforce", "off")
DEFAULT_MODE = "report"

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
    """The policy as it is sent."""
    return "; ".join(f"{name} {' '.join(sources)}" for name, sources in DIRECTIVES)


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
