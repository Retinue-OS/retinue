#!/usr/bin/env python3
"""Every HTML response of the gateway carries the dashboard's
Content-Security-Policy, in the mode RETINUE_CSP names (scripts/content_policy.py,
docs/dashboard.md "The Content-Security-Policy").

Runs the gateway's real request handler on a loopback port and asks it for
every kind of page it answers: the static pages of webapp/, the life store's
page, the pages it builds itself (pending sends, a send's status, the session
log, the messenger gateways, the Claude sign-in, their error pages) and
http.server's own error page. In each of the three modes:

- report: every page carries Content-Security-Policy-Report-Only with the
  policy, and no enforced policy of its own;
- enforce: every page carries Content-Security-Policy with the policy;
- off: no page carries either;

and in every mode the instance settings page keeps its framing policy as a
separate, enforced header, a response that is no page (JSON, a script, an
image) carries no policy, and the policy itself is what the decision says:
scripts from this origin only, no inline script, no eval, images and media
from this origin (and the chat page's own blob: previews), with data: images
on /sparql alone, which gets that policy under both its paths. A change of
mode moves the service worker's shell hash, so installed clients pick it up.

    python3 tests/test_web_gateway_csp.py
"""
import http.client
import importlib.util
import json
import os
import sys
import tempfile
import threading
import types
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
WEBAPP = REPO_ROOT / "webapp"


def _load_gateway(tmp: Path):
    os.environ["CONVERSATIONS_DIR"] = str(tmp / "convs")
    os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state.json")
    os.environ["RETINUE_INSTANCE_FILE"] = str(tmp / "instance.json")
    os.environ["WEBAPP_DIR"] = str(WEBAPP)
    os.environ["DASHBOARD_DATA_DIR"] = str(tmp / "data")
    os.environ["DOCS_DIR"] = str(REPO_ROOT / "docs")
    # Nothing answers here: the pages that read the store show their error.
    os.environ["QLEVER_LIFE_URL"] = "http://127.0.0.1:9"
    (tmp / "chambers").mkdir(parents=True, exist_ok=True)
    (tmp / "data").mkdir(parents=True, exist_ok=True)
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
                    return text

            stub = types.ModuleType("markdown_it")
            stub.MarkdownIt = _MarkdownIt
            sys.modules["markdown_it"] = stub
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "web_gateway_csp_under_test", SCRIPTS_DIR / "web-gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _request(port: int, method: str, path: str, headers: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, resp.headers, body
    finally:
        conn.close()


# Every page the handler answers, by what asks for it. Each must come back as
# text/html.
PAGES = [
    ("GET", "/", {}),
    ("GET", "/index.html", {}),
    ("GET", "/conversations.html", {}),
    ("GET", "/projects.html", {}),
    ("GET", "/project.html?id=x", {}),
    ("GET", "/news.html", {}),
    ("GET", "/chats.html", {}),
    ("GET", "/chat.html?id=x", {}),
    ("GET", "/contacts.html", {}),
    ("GET", "/settings.html", {}),
    ("GET", "/settings-instance.html", {}),
    ("GET", "/viewer.html?src=x", {}),
    ("GET", "/sync-health", {}),
    ("GET", "/sync-health.html", {}),
    ("GET", "/sparql", {"Accept": "text/html"}),
    ("GET", "/sparql.html", {}),
    ("GET", "/sends", {}),
    ("GET", "/sends/next", {}),
    ("GET", "/sends/nobody/" + "a" * 32, {}),
    ("GET", "/conversation", {}),
    ("GET", "/conversation/2026-09-02", {}),
    ("GET", "/gateways", {}),
    ("GET", "/claude-auth", {}),
    # http.server's own error page (an unsupported method).
    ("HEAD", "/nothing-here", {}),
]

# Answers that are no page: no policy.
NOT_PAGES = [
    ("GET", "/health"),
    ("GET", "/components/nav.js"),
    ("GET", "/sw-register.js"),
    ("GET", "/sw.js"),
    ("GET", "/styles.css"),
    ("GET", "/icons/icon-192.png"),
    ("GET", "/conversations"),
    ("GET", "/no-such-file"),
]


# The pages that get more than the policy every page has, by path, with the
# file of webapp/ that names their entry in content_policy.PAGE_SOURCES.
PAGE_POLICY = {"/sparql": "sparql.html", "/sparql.html": "sparql.html"}


def _directives(policy: str) -> dict:
    directives = {}
    for part in policy.split(";"):
        name, *sources = part.split()
        assert name not in directives, f"{name} twice"
        directives[name] = sources
    return directives


def check_policy_text(cp) -> None:
    directives = _directives(cp.policy())
    assert directives["script-src"] == ["'self'"], directives["script-src"]
    assert directives["default-src"] == ["'self'"]
    assert directives["object-src"] == ["'none'"]
    assert directives["base-uri"] == ["'self'"]
    assert directives["form-action"] == ["'self'"]
    assert directives["worker-src"] == ["'self'"]
    assert directives["manifest-src"] == ["'self'"]
    assert directives["style-src"] == ["'self'", "'unsafe-inline'"]
    for name in ("img-src", "media-src", "font-src", "connect-src", "frame-src"):
        assert name in directives, f"{name} is stated, not left to default-src"
    # Where reports go (tests/test_web_gateway_csp_report.py) is no source.
    assert directives.pop("report-uri") == ["/csp-report"]
    # Not report-to: Chromium would then ignore report-uri (content_policy.py).
    assert "report-to" not in directives
    for sources in directives.values():
        for source in sources:
            # Nothing of another origin, no scheme a script could come from.
            assert source in ("'self'", "'none'", "'unsafe-inline'", "data:", "blob:"), source
    assert "'unsafe-inline'" not in directives["script-src"]
    assert not any("eval" in s for v in directives.values() for s in v)
    assert "frame-ancestors" not in directives, "the framing policy is the embed page's own"
    print("ok: the policy runs scripts from this origin only, with no inline script and no eval")
    # Images and media: what the pages load, and no more. No data: on any
    # page but /sparql, and no remote media: the chat page links a remote
    # attachment rather than load it.
    assert directives["img-src"] == ["'self'", "blob:"], directives["img-src"]
    assert directives["media-src"] == ["'self'"], directives["media-src"]
    assert directives["font-src"] == ["'none'"]
    assert directives["connect-src"] == ["'self'"]
    assert directives["frame-src"] == ["'self'"]
    assert cp.policy("chat.html") == cp.policy() == cp.policy(None)
    # /sparql: YASGUI's stylesheet icons, data: images, for that page alone.
    sparql = _directives(cp.policy("sparql.html"))
    assert sparql.pop("img-src") == ["'self'", "blob:", "data:"], cp.policy("sparql.html")
    assert sparql.pop("report-uri") == ["/csp-report"]
    directives.pop("img-src")
    assert sparql == directives, "the /sparql policy differs from the others in img-src alone"
    print("ok: data: images on /sparql alone, data: media and remote media nowhere")


def main() -> int:
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        os.environ.pop("RETINUE_CSP", None)
        wg = _load_gateway(tmp)
        cp = wg.content_policy
        # The pending-send scan would ask IMAP and the gateways; none here.
        wg._all_pending = lambda: []
        (Path(os.environ["CONVERSATION_DIR"]) / "2026-09-02.json").write_text(json.dumps(
            [{"anchor": "a1", "timestamp": "2026-09-02T10:00:00", "question": "q",
              "answer": "a"}]))
        check_policy_text(cp)

        server = ThreadingHTTPServer(("127.0.0.1", 0), wg.Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            hashes = {}
            for mode in ("report", "enforce", "off", None, "bogus"):
                if mode is None:
                    os.environ.pop("RETINUE_CSP", None)
                else:
                    os.environ["RETINUE_CSP"] = mode
                in_force = mode if mode in ("report", "enforce", "off") else "report"
                assert cp.mode() == in_force, (mode, cp.mode())
                hashes[in_force] = wg._shell_hash()
                expect_name = {"report": "Content-Security-Policy-Report-Only",
                               "enforce": "Content-Security-Policy"}.get(in_force)
                for method, path, headers in PAGES:
                    status, resp_headers, _ = _request(port, method, path, headers)
                    ctype = resp_headers.get("Content-Type", "")
                    if not ctype.startswith("text/html"):
                        failures.append(f"{mode}: {method} {path} is not a page ({status} {ctype})")
                        continue
                    enforced = resp_headers.get_all("Content-Security-Policy") or []
                    reported = resp_headers.get_all("Content-Security-Policy-Report-Only") or []
                    framing = [v for v in enforced if v.startswith("frame-ancestors")]
                    enforced = [v for v in enforced if not v.startswith("frame-ancestors")]
                    if path == "/settings-instance.html":
                        if framing != [f"frame-ancestors 'self' {wg.APP_SETTINGS_ORIGIN}"]:
                            failures.append(f"{mode}: the embed page lost its framing policy: {framing}")
                    elif framing:
                        failures.append(f"{mode}: {path} carries a framing policy")
                    expect = cp.policy(PAGE_POLICY.get(path.split("?", 1)[0]))
                    if in_force == "report":
                        ok = reported == [expect] and not enforced
                    elif in_force == "enforce":
                        ok = enforced == [expect] and not reported
                    else:
                        ok = not enforced and not reported
                    if not ok:
                        failures.append(f"{mode}: {method} {path} ({status}) carries "
                                        f"enforced={enforced} report-only={reported}")
                for method, path in NOT_PAGES:
                    status, resp_headers, _ = _request(port, method, path)
                    if "text/html" in resp_headers.get("Content-Type", ""):
                        failures.append(f"{mode}: {path} answered as a page")
                    for name in ("Content-Security-Policy", "Content-Security-Policy-Report-Only"):
                        if resp_headers.get(name):
                            failures.append(f"{mode}: {path} ({status}) carries {name}")
                if expect_name:
                    print(f"ok: {mode or 'unset'}: every page carries {expect_name} with the policy")
                else:
                    print(f"ok: {mode}: no page carries a policy")
            if len(set(hashes.values())) != 3:
                failures.append(f"the shell hash does not follow the mode: {hashes}")
            else:
                print("ok: a change of mode moves the service worker's shell hash")
        finally:
            server.shutdown()
            server.server_close()
            os.environ.pop("RETINUE_CSP", None)
    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("All Content-Security-Policy header checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
