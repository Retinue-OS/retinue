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
  sources are searched for handler attributes and javascript: URLs, line by
  line and in each string and template literal read whole, so a tag spread
  over the lines of a template is read as one;
- every page the gateway builds itself, each builder rendered with the
  states it has (the pending sends, the approval cards, every status of a
  decided send, the messenger gateways with and without a pairing code, the
  Claude sign-in in both modes, the session log with an answer that quotes
  markup);
- the gateway's own source, for an inline <script>, a handler attribute or a
  javascript: URL in a page it writes outside those builders (its error
  pages): the string literals of each statement are joined first, so a tag
  split over adjacent literals or lines is read whole.

A script a page loads must also be a file of the dashboard's own: a path on
this origin to a file in webapp/, since script-src 'self' blocks one from
elsewhere and a missing one leaves the page without it.

    python3 tests/test_webapp_no_inline_script.py
"""
import ast
import bisect
import html.parser
import importlib.util
import io
import os
import posixpath
import re
import sys
import tempfile
import tokenize
import types
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
WEBAPP = REPO_ROOT / "webapp"

# Attributes whose value a browser follows as a URL.
URL_ATTRS = {"href", "src", "action", "formaction", "xlink:href", "data", "poster"}


def src_problem(src: str, page_dir: str | None) -> str | None:
    """Why a script's URL is not a file of the dashboard's own, or None.

    page_dir is the directory of the page's own URL, which a relative URL is
    resolved against; None for a page served under paths of its own (the
    gateway's), whose scripts must be named from the root."""
    value = src.strip()
    parts = urllib.parse.urlsplit(value)
    if parts.scheme or parts.netloc:
        return f"loads {src!r} from another origin"
    path = parts.path
    if not path:
        return f"loads {src!r}, which names no file"
    if not path.startswith("/"):
        if page_dir is None:
            return f"loads {src!r} by a path relative to the page's URL"
        path = posixpath.join(page_dir, path)
    root = WEBAPP.resolve()
    target = (root / posixpath.normpath(path).lstrip("/")).resolve()
    if root not in target.parents or not target.is_file():
        return f"loads {src!r}, which is not a file in webapp/"
    return None


class _Scan(html.parser.HTMLParser):
    """Collects what a policy without 'unsafe-inline' would refuse, and the
    scripts a page loads that are not files of the dashboard's own."""

    def __init__(self, page_dir: str | None):
        super().__init__(convert_charrefs=True)
        self.page_dir = page_dir
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
        values = {name: value or "" for name, value in attrs}
        rel = values.get("rel", "").lower().split()
        if tag == "script" and "src" in names:
            self._check_src(tag, values["src"])
        if tag == "link" and "href" in names and (
                "modulepreload" in rel
                or ("preload" in rel and values.get("as", "").lower() in ("script", "worker"))):
            self._check_src(tag, values["href"])
        if tag == "script":
            if "src" not in names:
                self.problems.append("inline <script> (no src)")
            self._in_script = True
            self._script_has_src = "src" in names
            self._script_text = ""

    def _check_src(self, tag, src):
        problem = src_problem(src, self.page_dir)
        if problem:
            self.problems.append(f"<{tag}> {problem}")

    def handle_data(self, data):
        if self._in_script:
            self._script_text += data

    def handle_endtag(self, tag):
        if tag == "script":
            if self._script_has_src and self._script_text.strip():
                self.problems.append("a <script src> with a body")
            self._in_script = False


def problems_in(markup: str, page_dir: str | None = "/") -> list[str]:
    scan = _Scan(page_dir)
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
# is `on<event>=` followed by a quote or a substitution, or, unquoted, by the
# value itself (`<button onclick=go()>`). A property set in code
# (`img.onload = () => …`) is no attribute and passes: it follows a dot, not a
# blank or a quote, and an unquoted value is looked for only on a line that
# opens a tag, so a local variable (`const onKey = (e) => …`) passes as well.
# A tag a template spreads over lines has its handler on a line that opens
# none; the literals are therefore also read whole (literal_handler_lines),
# where an unquoted value counts once a tag opens earlier in the same literal.
HANDLER_IN_MARKUP = re.compile(r"""\son[a-z]{3,}\s*=\s*(?:["'`]|\\["']|\$\{)""")
HANDLER_UNQUOTED = re.compile(r"""[\s"'`]on[a-z]{3,}\s*=\s*[^\s=>]""", re.I)
HANDLER_SET_ATTRIBUTE = re.compile(r"""setAttribute\(\s*['"`]on""")
COMMENT_LINE = re.compile(r"^\s*(?://|/\*|\*)")


def handler_in_markup(line: str) -> bool:
    """Whether a line of a component's source writes a handler attribute."""
    if HANDLER_IN_MARKUP.search(line) or HANDLER_SET_ATTRIBUTE.search(line):
        return True
    return "<" in line and bool(HANDLER_UNQUOTED.search(line))


# Words after which a `/` begins a regular expression rather than divides.
_JS_OPERATOR_WORDS = {"return", "typeof", "instanceof", "in", "of", "new", "delete", "void",
                      "throw", "case", "do", "else", "yield", "await"}
_JS_WORD = re.compile(r"[A-Za-z0-9_$\u0080-\uffff]+")


def js_literals(source: str) -> list[tuple[int, str]]:
    """(line, text) for each string and template literal of a JavaScript
    source, in the order they open: text is the literal as written, its
    common escapes read, and a template's text is read whole, across its
    lines, with "${}" where a substitution stands (a template inside a
    substitution is a literal of its own). Comments and regular expressions
    are skipped, so a quote in one does not open a literal. Raises ValueError
    where a literal or comment is not closed: a source this cannot read is
    no source it has checked."""
    newlines = [i for i, c in enumerate(source) if c == "\n"]

    def line_at(pos: int) -> int:
        return bisect.bisect_right(newlines, pos - 1) + 1

    def unescape(text: str) -> str:
        return re.sub(r"\\(['\"`])", r"\1", re.sub(r"\\[ntr]", " ", text))

    out: list[tuple[int, str]] = []
    # The templates open around the code being read: where each began, its
    # text so far, and the brace depth its current substitution opened at.
    templates: list[dict] = []
    braces = 0
    regex_may_follow = True
    n = len(source)
    i = 0

    def read_template(i: int) -> tuple[int, bool]:
        """Reads template text from i; returns where code resumes and
        whether the template ended (False: a substitution opened)."""
        frame = templates[-1]
        start = i
        while i < n:
            c = source[i]
            if c == "\\":
                i += 2
            elif c == "`":
                frame["pieces"].append(source[start:i])
                out.append((line_at(frame["start"]), unescape("".join(frame["pieces"]))))
                templates.pop()
                return i + 1, True
            elif c == "$" and source.startswith("${", i):
                frame["pieces"].append(source[start:i] + "${}")
                frame["depth"] = braces
                return i + 2, False
            else:
                i += 1
        raise ValueError(f"template from line {line_at(frame['start'])} is not closed")

    while i < n:
        c = source[i]
        if c.isspace():
            i += 1
        elif source.startswith("//", i):
            end = source.find("\n", i)
            i = n if end < 0 else end
        elif source.startswith("/*", i):
            end = source.find("*/", i + 2)
            if end < 0:
                raise ValueError(f"comment from line {line_at(i)} is not closed")
            i = end + 2
        elif c == "/" and regex_may_follow:
            j, in_class = i + 1, False
            while j < n and (in_class or source[j] != "/"):
                if source[j] == "\n":
                    raise ValueError(f"regular expression on line {line_at(i)} is not closed")
                if source[j] == "\\":
                    j += 1
                elif source[j] == "[":
                    in_class = True
                elif source[j] == "]":
                    in_class = False
                j += 1
            i = j + 1
            regex_may_follow = False
        elif c in "'\"":
            j = i + 1
            while j < n and source[j] != c:
                if source[j] == "\n":
                    raise ValueError(f"string on line {line_at(i)} is not closed")
                j += 2 if source[j] == "\\" else 1
            if j >= n:
                raise ValueError(f"string on line {line_at(i)} is not closed")
            out.append((line_at(i), unescape(source[i + 1:j])))
            i = j + 1
            regex_may_follow = False
        elif c == "`":
            templates.append({"start": i, "pieces": [], "depth": None})
            i, _ = read_template(i + 1)
            regex_may_follow = False
        elif c == "}" and templates and templates[-1]["depth"] == braces:
            templates[-1]["depth"] = None
            i, _ = read_template(i + 1)
            regex_may_follow = False
        elif c in "{}":
            braces += 1 if c == "{" else -1
            i += 1
            regex_may_follow = True
        elif c in ")]":
            i += 1
            regex_may_follow = False
        else:
            m = _JS_WORD.match(source, i)
            if m:
                i = m.end()
                regex_may_follow = m.group() in _JS_OPERATOR_WORDS
            else:
                i += 1
                regex_may_follow = True
    if templates:
        raise ValueError(f"template from line {line_at(templates[-1]['start'])} is not closed")
    return out


def literal_handler_lines(source: str) -> list[int]:
    """The lines of the string and template literals of a JavaScript source
    that write a handler attribute, each literal read whole: a quoted value,
    or a substitution, after `on<event>=` anywhere in it, and an unquoted
    value where a tag opens earlier in the same literal
    (`<button\n  onclick=go()>`, a tag a line-by-line search cannot see
    whole). Code outside literals is never searched here, so a property set
    in code or a local variable named on… passes."""
    lines = []
    for line, text in js_literals(source):
        found = HANDLER_IN_MARKUP.search(text) or any(
            "<" in text[:m.start()] for m in HANDLER_UNQUOTED.finditer(text))
        if found:
            lines.append(line)
    return lines


def check_component_sources() -> list[str]:
    failures = []
    sources = (sorted(WEBAPP.glob("*.js")) + sorted((WEBAPP / "components").glob("*.js"))
               + sorted((WEBAPP / "gateway").glob("*.js")))
    for src in sources:
        rel = src.relative_to(REPO_ROOT)
        text = src.read_text(encoding="utf-8")
        for no, line in enumerate(text.splitlines(), 1):
            if COMMENT_LINE.match(line):
                continue
            if handler_in_markup(line):
                failures.append(f"{rel}:{no}: handler attribute in rendered markup")
            if "javascript:" in line.lower():
                failures.append(f"{rel}:{no}: javascript: URL")
        try:
            for no in literal_handler_lines(text):
                failures.append(f"{rel}:{no}: handler attribute in the markup of the literal "
                                f"opened here")
        except ValueError as e:
            failures.append(f"{rel}: its literals cannot be read: {e}")
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
        # The pages that need a script load it as a file of the dashboard's
        # own, named from the root: they are served under paths of their own.
        for problem in problems_in(markup, page_dir=None):
            failures.append(f"gateway page '{name}': {problem}")
    print(f"ok: rendered {len(pages)} gateway pages")
    return failures


_FSTRING_MIDDLE = getattr(tokenize, "FSTRING_MIDDLE", None)
_STRING_PREFIX = re.compile(r"""^[A-Za-z]*('{3}|"{3}|'|")(.*)\1$""", re.S)
SOURCE_SCRIPT = re.compile(r"<script\b([^>]*)>?", re.I)
SOURCE_HANDLER = re.compile(r"<[a-z][^<>]*?\son[a-z]{3,}\s*=", re.I)
# A browser drops blanks and control characters before it reads a URL's
# scheme; the search lets them stand between any two letters of it.
SOURCE_JAVASCRIPT_URL = re.compile("[\\x00-\\x20]*".join("javascript:"), re.I)


def _literal(token: tokenize.TokenInfo) -> str:
    """The text a string token stands for; for the parts of an f-string
    (and, before Python 3.12, a whole one), its source text with the common
    escapes read, which is all a search for markup needs."""
    if token.type == tokenize.STRING:
        try:
            value = ast.literal_eval(token.string)
            if isinstance(value, str):
                return value
            return value.decode("latin-1")
        except (ValueError, SyntaxError):
            m = _STRING_PREFIX.match(token.string)
            text = m.group(2) if m else token.string
    else:
        text = token.string
    return re.sub(r"\\(['\"])", r"\1", re.sub(r"\\[ntr]", " ", text))


_NOT_CODE = {tokenize.NL, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT,
             getattr(tokenize, "FSTRING_START", None), getattr(tokenize, "FSTRING_END", None)}


def statement_strings(source: str):
    """(text, line of) for each statement of a Python source that has a
    string in it beside other code: text is its strings joined as they stand,
    so a tag split over adjacent literals, or over literals and expressions
    concatenated with +, is read as one; line_of(offset) is the source line
    the text at that offset comes from. A statement that is a string alone (a
    docstring) is never sent and is left out, as comments are."""
    parts: list[tuple[int, str]] = []
    code = False
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in (tokenize.NEWLINE, tokenize.ENDMARKER):
            if parts and code:
                starts, lines, offset = [], [], 0
                for line, text in parts:
                    starts.append(offset)
                    lines.append(line)
                    offset += len(text)

                def line_of(at, starts=starts, lines=lines):
                    return lines[max(i for i, start in enumerate(starts) if start <= at)]

                yield "".join(text for _, text in parts), line_of
            parts, code = [], False
        elif token.type in (tokenize.STRING, _FSTRING_MIDDLE):
            parts.append((token.start[0], _literal(token)))
        elif token.type not in _NOT_CODE:
            code = True


def gateway_source_problems(source: str, name: str = "scripts/web-gateway.py") -> list[str]:
    """No <script> without a src, no handler attribute and no javascript: URL
    in the markup a Python source writes."""
    failures = []
    for text, line_of in statement_strings(source):
        for m in SOURCE_SCRIPT.finditer(text):
            if "src=" not in m.group(1):
                failures.append(f"{name}:{line_of(m.start())}: <script> without src")
        for m in SOURCE_HANDLER.finditer(text):
            failures.append(f"{name}:{line_of(m.start())}: handler attribute in markup")
        for m in SOURCE_JAVASCRIPT_URL.finditer(text):
            failures.append(f"{name}:{line_of(m.start())}: javascript: URL")
    return failures


def check_gateway_source() -> list[str]:
    """A page the gateway writes outside the builders above (an error page) is
    strings in its source."""
    source = (SCRIPTS_DIR / "web-gateway.py").read_text(encoding="utf-8")
    failures = gateway_source_problems(source)
    print("ok: searched the gateway's source for inline scripts, handler attributes "
          "and javascript: URLs")
    return failures


def check_the_checker() -> list[str]:
    """The scan itself finds what it is there to find."""
    failures = []
    cases = {
        "<script>alert(1)</script>": 1,
        '<script type="module">import x from "/a.js"</script>': 1,
        '<script src="/sw-register.js"></script>': 0,
        '<script type="module" src="components/nav.js"></script>': 0,
        '<link rel="modulepreload" href="/components/nav.js">': 0,
        # A script from elsewhere is blocked by script-src 'self'; a missing
        # one leaves the page without it.
        '<script src="https://elsewhere.example/x.js"></script>': 1,
        '<script src="//elsewhere.example/x.js"></script>': 1,
        '<script type="module" src="/nonexistent.js"></script>': 1,
        '<script src="/../scripts/web-gateway.py"></script>': 1,
        '<link rel="modulepreload" href="https://elsewhere.example/x.js">': 1,
        '<img src="/x.png" onerror="alert(1)">': 1,
        '<a href=" JaVa\tScRiPt:alert(1)">x</a>': 1,
        '<a href="/javascript:ok">x</a>': 0,
        '<button onclick=go()>x</button>': 1,
    }
    for markup, expected in cases.items():
        found = len(problems_in(markup))
        if found != expected:
            failures.append(f"checker: {markup!r} gave {found} problems, expected {expected}")
    for line in ('`<img src="${u}" onload="x()">`', "  `<button onclick=go()>x</button>`",
                 "  html += '<img src=x onerror=alert(1)>';", "`<div onMouseOver=${fn}>`",
                 "  `<a href=\"/x\"`, ` onfocus=\"x()\">`", "el.setAttribute('onclick', 'x()');"):
        if not handler_in_markup(line):
            failures.append(f"checker: missed a handler attribute in {line!r}")
    for line in ("    js.onload = () => resolve();", "    img.onerror = () => { this.x = 1; };",
                 "  const onKey = (e) => { if (e.key === 'Escape') close(); };",
                 "  if (a < b) img.onload = done;", "const ONTOLOGY = 'ontology';"):
        if handler_in_markup(line):
            failures.append(f"checker: took {line!r} for a handler attribute")
    # The literals of a component read whole: a tag spread over the lines of
    # a template, or a value written on the line after its name, is one tag.
    # Each source must find a handler on the line its literal opens on.
    for source, line in (
            ("el.innerHTML = `<button\n      onclick=go()\n      >x</button>`;\n", 1),
            ("x = 1;\nel.innerHTML = `<a href=\"/x\"\n  onclick=\n  \"go()\">x</a>`;\n", 2),
            # A template inside a substitution is a literal of its own.
            ("ul.innerHTML = `<ul>${items.map((i) => `<li\n  onclick=pick(${i})>${i}</li>`)"
             ".join('')}</ul>`;\n", 1),
            ("html += '<img src=x\\nonerror=alert(1)>';\n", 1),
            # A quote in a regular expression or a comment opens no literal,
            # and a division is no regular expression.
            ("const q = /'/g; // it's\nel.innerHTML = `<b\n onclick=x()>`;\n", 2),
            ("const r = total / count;\nel.innerHTML = `<b\n onclick=x()>${r}</b>`;\n", 2)):
        try:
            found = literal_handler_lines(source)
        except ValueError as e:
            found = [f"unread: {e}"]
        if found != [line]:
            failures.append(f"checker: {source!r} gave {found}, expected line {line}")
    for source in (
            "img.onload = done;\nconst onKey = (e) => { if (e.key === 'Escape') close(); };\n",
            "if (a < b) img.onload = done;\n",
            "el.innerHTML = `<button data-go>x</button>`;\nel.querySelector('button').onclick = go;\n",
            "t = `<p>${n < m ? 'once' : 'onset'}</p>`; s = 'turn onboarding off';\n",
            "// `<b\n/* `<b\n onclick=x()>` */\nconst x = 1;\n",
            "const re = /`<b onclick=x()>`/;\n"):
        try:
            found = literal_handler_lines(source)
        except ValueError as e:
            found = [f"unread: {e}"]
        if found:
            failures.append(f"checker: {source!r} gave {found}, expected none")
    for source in ("el.innerHTML = `<b>;\n", "s = '<b>;\n", "/* `<b onclick=x()>`\n"):
        try:
            literal_handler_lines(source)
            failures.append(f"checker: read {source!r}, which has a literal or comment unclosed")
        except ValueError:
            pass
    # The gateway's own pages are served under paths of their own: a script
    # there is named from the root.
    if problems_in('<script src="gateway/x.js"></script>', page_dir=None) == []:
        failures.append("checker: took a relative script URL on a gateway page")
    # The gateway's source: a page written outside its builders. Each must
    # find exactly one problem, among them a tag split over adjacent literals.
    for source in (
            'self._send_html(403, H + "<body><a href=\\"javascript:alert(1)\\">x</a>")\n',
            'self._send_html(403, H + "<body><h1\\n"\n    "onclick=\\"x()\\">x</h1>")\n',
            'self._send_html(403, H + "<body><h1"\n    + " onclick=x()>" + y)\n',
            'self._send_html(403, H + f"<body><h1>{x}</h1><script>go()</script>")\n',
            'page = ("<a href=\\" java\\tscript:go()\\">"\n        "x</a>")\n'):
        found = gateway_source_problems(source, "source")
        if len(found) != 1:
            failures.append(f"checker: {source!r} gave {found}, expected one problem")
    for source in (
            '"""A docstring may name <button onclick="x()"> and javascript: URLs."""\n',
            '# <script>x()</script> in a comment\nx = 1\n',
            'self.send_header("Content-Type", "text/javascript; charset=utf-8")\n',
            'self._send_html(200, H + \'<script src="/gateway/x.js"></script>\')\n',
            'img.onload = done; page = "<p>" + x + "</p>"\n'):
        found = gateway_source_problems(source, "source")
        if found:
            failures.append(f"checker: {source!r} gave {found}, expected none")
    print("ok: the scan finds inline scripts, handler attributes, javascript: URLs and "
          "scripts that are not the dashboard's own")
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
