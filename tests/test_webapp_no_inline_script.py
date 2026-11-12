#!/usr/bin/env python3
"""No page of the dashboard's origin carries script in its markup.

Every HTML response of the gateway is sent with a Content-Security-Policy
whose `script-src 'self'` runs scripts only from the dashboard's own files
(docs/dashboard.md, "The Content-Security-Policy"). Enforced, it blocks any
inline <script>, any handler attribute (onclick= and the like) and any
javascript: URL, so a page that still has one breaks. These checks keep it
that way, everywhere a page comes from:

- the static pages, webapp/*.html;
- what the components render: their markup is built in strings, so the
  sources are searched for handler attributes and javascript: URLs;
- every page the gateway builds itself, each builder rendered with the
  states it has (the pending sends, the approval cards, every status of a
  decided send, the messenger gateways with and without a pairing code, the
  Claude sign-in in both modes, the session log with an answer that quotes
  markup);
- the gateway's own source, for an inline <script> or a handler attribute
  in a page it writes outside those builders (an error page, say).

    python3 tests/test_webapp_no_inline_script.py
"""
import html.parser
import importlib.util
import os
import re
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
WEBAPP = REPO_ROOT / "webapp"

# Attributes whose value a browser follows as a URL.
URL_ATTRS = {"href", "src", "action", "formaction", "xlink:href", "data", "poster"}


class _Scan(html.parser.HTMLParser):
    """Collects what a policy without 'unsafe-inline' would refuse."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.problems: list[str] = []
        self._in_script = False
        self._script_has_src = False
        self._script_text = ""

    def handle_starttag(self, tag, attrs):
        names = {name for name, _ in attrs}
        for name, value in attrs:
            if name.startswith("on"):
                self.problems.append(f"handler attribute <{tag} {name}=…>")
            if name in URL_ATTRS and value is not None:
                # A browser strips leading blanks and control characters, and
                # tabs and newlines anywhere, before it reads the scheme.
                plain = re.sub(r"[\x00-\x20]", "", value).lower()
                if plain.startswith("javascript:"):
                    self.problems.append(f"javascript: URL in <{tag} {name}>")
        if tag == "script":
            if "src" not in names:
                self.problems.append("inline <script> (no src)")
            self._in_script = True
            self._script_has_src = "src" in names
            self._script_text = ""

    def handle_data(self, data):
        if self._in_script:
            self._script_text += data

    def handle_endtag(self, tag):
        if tag == "script":
            if self._script_has_src and self._script_text.strip():
                self.problems.append("a <script src> with a body")
            self._in_script = False


def problems_in(markup: str) -> list[str]:
    scan = _Scan()
    scan.feed(markup)
    scan.close()
    return scan.problems


def _load_gateway(tmp: Path):
    os.environ["CONVERSATIONS_DIR"] = str(tmp / "convs")
    os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state.json")
    os.environ["RETINUE_INSTANCE_FILE"] = str(tmp / "instance.json")
    (tmp / "chambers").mkdir(parents=True, exist_ok=True)
    if "markdown_it" not in sys.modules:
        try:
            import markdown_it  # noqa: F401
        except ImportError:
            class _MarkdownIt:
                def __init__(self, *args, **kwargs):
                    pass

                def enable(self, *args, **kwargs):
                    return self

                def render(self, text):
                    import html as _html
                    return "<p>" + _html.escape(text) + "</p>"

            stub = types.ModuleType("markdown_it")
            stub.MarkdownIt = _MarkdownIt
            sys.modules["markdown_it"] = stub
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "web_gateway_no_inline_script_under_test", SCRIPTS_DIR / "web-gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check_static_pages() -> list[str]:
    failures = []
    pages = sorted(WEBAPP.glob("*.html"))
    assert pages, "no pages found in webapp/"
    for page in pages:
        for problem in problems_in(page.read_text(encoding="utf-8")):
            failures.append(f"webapp/{page.name}: {problem}")
    print(f"ok: checked {len(pages)} static pages")
    return failures


# Markup in a component is a string or a template: a handler attribute there
# is `on<event>=` followed by a quote or a substitution. A property set in
# code (`img.onload = () => …`) is no attribute and passes.
HANDLER_IN_MARKUP = re.compile(r"""\son[a-z]{3,}\s*=\s*(?:["'`]|\\["']|\$\{)""")
HANDLER_SET_ATTRIBUTE = re.compile(r"""setAttribute\(\s*['"`]on""")
COMMENT_LINE = re.compile(r"^\s*(?://|/\*|\*)")


def check_component_sources() -> list[str]:
    failures = []
    sources = (sorted(WEBAPP.glob("*.js")) + sorted((WEBAPP / "components").glob("*.js"))
               + sorted((WEBAPP / "gateway").glob("*.js")))
    for src in sources:
        rel = src.relative_to(REPO_ROOT)
        for no, line in enumerate(src.read_text(encoding="utf-8").splitlines(), 1):
            if COMMENT_LINE.match(line):
                continue
            if HANDLER_IN_MARKUP.search(line) or HANDLER_SET_ATTRIBUTE.search(line):
                failures.append(f"{rel}:{no}: handler attribute in rendered markup")
            if "javascript:" in line.lower():
                failures.append(f"{rel}:{no}: javascript: URL")
    print(f"ok: checked {len(sources)} scripts for handler attributes and javascript: URLs")
    return failures


def rendered_gateway_pages(wg) -> dict[str, str]:
    """Every page builder of the gateway, rendered in each of its states."""
    pages: dict[str, str] = {}
    pages["session log index"] = wg._render_index_html(["2026-09-01", "2026-09-02"])
    pages["session log index, empty"] = wg._render_index_html([])
    pages["session log day"] = wg._render_day_html(
        [{"anchor": "a1", "timestamp": "2026-09-02T10:00:00", "question": "<b>hi</b>",
          "answer": "An answer quoting <script>alert(1)</script> and "
                    "<img src=x onerror=alert(1)> and [a link](javascript:alert(1)) "
                    "and https://example.org/page"}],
        "2026-09-02", ["2026-09-01", "2026-09-02"])
    pending = [{"account": "ari", "request_id": "42", "subject": "Lunch", "to": "a@example.com",
                "category": "reply"},
               {"account": "caldav-gateway", "request_id": "b" * 32, "kind": "event",
                "start": "2026-09-03T14:00:00", "end": "2026-09-03T14:30:00",
                "subject": "Dentist", "category": "verify"}]
    pages["pending sends"] = wg._render_sends_index_html(pending)
    pages["pending sends, empty"] = wg._render_sends_index_html([])
    pages["pending sends, leaving"] = wg._render_sends_index_html([], leave=True)
    pages["e-mail approval"] = wg._render_send_single_html(
        {"request_id": "42", "subject": "Lunch", "from": "Agent <agent@example.org>",
         "to": "a@example.com", "cc": "c@example.com", "bcc": "d@example.com",
         "category": "reply", "body": "See you <script>x</script>", "attachments": ["menu.pdf"]},
        "ari", "/sends/signal-gateway/bbb")
    pages["e-mail outcome"] = wg._render_email_send_outcome_html(
        {"approved": "42", "sent": True, "from": "Agent <agent@example.org>",
         "to": ["a@example.com"], "subject": "Lunch"}, "ari", "42", None)
    message = {"recipient": "+15551112222", "category": "verify", "message": "hello"}
    event = {"kind": "event", "to": "default", "category": "verify", "subject": "Dentist",
             "summary": "Dentist", "start": "2026-09-03T14:00:00", "end": "2026-09-03T14:30:00",
             "all_day": False, "description": "Bring the card", "calendar_id": None}
    agenda = {"events": [{"start": "2026-09-03T14:15:00", "end": "2026-09-03T15:00:00",
                          "summary": "Call", "all_day": False, "uid": "u1",
                          "calendar": "Personal", "overlaps": True}],
              "error": None, "truncated": False}
    for status in ("pending", "sending", "approved", "rejected", "retracted", "error"):
        for next_url in (None, "/sends/signal-gateway/bbb"):
            suffix = f"{status}{', with a next request' if next_url else ''}"
            pages[f"messenger send, {suffix}"] = wg._render_channel_send_html(
                dict(message, status=status, error="timed out"), "whatsapp-gateway",
                "a" * 32, next_url)
            pages[f"calendar event, {suffix}"] = wg._render_channel_send_html(
                dict(event, status=status, error="refused"), "caldav-gateway", "a" * 32,
                next_url, agenda if status == "pending" else None)
    pages["messenger gateways"] = wg._render_gateways_html([
        {"slug": "signal-gateway", "label": "Signal",
         "health": {"connected": False, "needs_repair": True, "error": "unlinked"}},
        {"slug": "whatsapp-gateway", "label": "WhatsApp",
         "health": {"connected": False, "needs_repair": False, "error": "wedged"}},
        {"slug": "telegram-gateway", "label": "Telegram",
         "health": {"connected": True, "recipient_lookup_ok": False, "last_ok_age": 12,
                    "build": {"framework": "other"}}},
        {"slug": "sms-gateway", "label": "SMS", "health": {"configured": False}},
    ])
    pages["messenger gateways, none to pair"] = wg._render_gateways_html([
        {"slug": "signal-gateway", "label": "Signal", "health": {"connected": True}}])
    pages["messenger gateways, none configured"] = wg._render_gateways_html([])
    for state in ("ok", "expiring", "stale", "needs_login"):
        pages[f"Claude sign-in, {state}"] = wg._render_claude_auth_html(
            {"state": state, "mode": "oauth", "reason": "Signed in.", "subscription": "max",
             "refresh_expires_at": 1_800_000_000_000, "access_expires_at": 1_800_000_000_000,
             "remote_control_running": state == "ok", "remote_control_available": True,
             "backup_present": True, "backup_rejected": state == "stale"})
    pages["Claude sign-in, behind a gateway"] = wg._render_claude_auth_html(
        {"state": "ok", "mode": "gateway"})
    return pages


def check_gateway_pages(wg) -> list[str]:
    failures = []
    pages = rendered_gateway_pages(wg)
    for name, markup in pages.items():
        assert markup.lstrip().startswith("<!DOCTYPE html>"), name
        for problem in problems_in(markup):
            failures.append(f"gateway page '{name}': {problem}")
    # The pages that need a script load it as a file of the dashboard's own,
    # and the file exists.
    for name, markup in pages.items():
        for src in re.findall(r'<script src="([^"]+)"', markup):
            if not (WEBAPP / src.lstrip("/")).is_file():
                failures.append(f"gateway page '{name}' loads {src}, which is not in webapp/")
    print(f"ok: rendered {len(pages)} gateway pages")
    return failures


def check_gateway_source() -> list[str]:
    """A page the gateway writes outside the builders above is a string in its
    source: no <script> there without a src, and no handler attribute."""
    failures = []
    source = (SCRIPTS_DIR / "web-gateway.py").read_text(encoding="utf-8")
    for no, line in enumerate(source.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        for m in re.finditer(r"<script\b([^>]*)>?", line, re.I):
            if "src=" not in m.group(1):
                failures.append(f"scripts/web-gateway.py:{no}: <script> without src")
        if re.search(r"""<[a-z][^<>]*\son[a-z]{3,}\s*=""", line, re.I):
            failures.append(f"scripts/web-gateway.py:{no}: handler attribute in markup")
    print("ok: searched the gateway's source for inline scripts and handler attributes")
    return failures


def check_the_checker() -> list[str]:
    """The scan itself finds what it is there to find."""
    failures = []
    cases = {
        "<script>alert(1)</script>": 1,
        '<script type="module">import x from "/a.js"</script>': 1,
        '<script src="/a.js"></script>': 0,
        '<img src="/x.png" onerror="alert(1)">': 1,
        '<a href=" JaVa\tScRiPt:alert(1)">x</a>': 1,
        '<a href="/javascript:ok">x</a>': 0,
        '<button onclick=go()>x</button>': 1,
    }
    for markup, expected in cases.items():
        found = len(problems_in(markup))
        if found != expected:
            failures.append(f"checker: {markup!r} gave {found} problems, expected {expected}")
    if not HANDLER_IN_MARKUP.search('`<img src="${u}" onload="x()">`'):
        failures.append("checker: missed a handler attribute in a template")
    if HANDLER_IN_MARKUP.search("    js.onload = () => resolve();"):
        failures.append("checker: took a property assignment for an attribute")
    print("ok: the scan finds inline scripts, handler attributes and javascript: URLs")
    return failures


def main() -> int:
    failures = check_the_checker()
    failures += check_static_pages()
    failures += check_component_sources()
    failures += check_gateway_source()
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        failures += check_gateway_pages(wg)
    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("All no-inline-script checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
