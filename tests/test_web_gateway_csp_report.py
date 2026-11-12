#!/usr/bin/env python3
"""The Content-Security-Policy's report endpoint, POST and GET /csp-report
(scripts/content_policy.py, docs/dashboard.md "The Content-Security-Policy").

Runs the gateway's real request handler on a loopback port and pins:

- both report formats are taken: application/csp-report (report-uri) and
  application/reports+json (report-to), the latter skipping reports of other
  types in the same delivery;
- a violation is kept once, by page, directive and blocked URL, with query
  strings and fragments cut off, and counted, with when it was first and last
  seen; each new one is logged once, on one line;
- no more than the limit of distinct violations is kept, the rest counted;
- an oversized body, a body without a length, another content type and junk
  (malformed JSON, nesting past the parser's recursion limit, no report in
  it) are refused with a fixed answer and keep nothing;
- no answer reflects what was posted;
- GET /csp-report lists what was kept, with the mode and the policy;
- the policy names the endpoint (report-uri, and not report-to, which would
  make Chromium drop it), in report and in enforce mode.

    python3 tests/test_web_gateway_csp_report.py
"""
import contextlib
import http.client
import importlib.util
import io
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
    os.environ["CONVERSATIONS_DIR"] = str(tmp / "state" / "convs")
    os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state.json")
    os.environ["RETINUE_INSTANCE_FILE"] = str(tmp / "instance.json")
    os.environ["WEBAPP_DIR"] = str(WEBAPP)
    os.environ.pop("RETINUE_CSP_REPORTS_FILE", None)
    (tmp / "chambers").mkdir(parents=True, exist_ok=True)
    if "markdown_it" not in sys.modules:
        try:
            import markdown_it  # noqa: F401
        except ImportError:
            stub = types.ModuleType("markdown_it")
            stub.MarkdownIt = object
            sys.modules["markdown_it"] = stub
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "web_gateway_csp_report_under_test", SCRIPTS_DIR / "web-gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Client:
    def __init__(self, port):
        self.port = port

    def request(self, method, path, body=b"", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest(method, path)
            for name, value in (headers or {}).items():
                conn.putheader(name, value)
            conn.endheaders()
            if body:
                conn.send(body)
            resp = conn.getresponse()
            return resp.status, resp.headers, resp.read()
        finally:
            conn.close()

    def post(self, ctype, body, extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        headers = {"Content-Type": ctype, "Content-Length": str(len(body))}
        headers.update(extra or {})
        return self.request("POST", "/csp-report", body, headers)

    def kept(self):
        status, headers, body = self.request("GET", "/csp-report")
        assert status == 200, status
        assert headers.get("Content-Type", "").startswith("application/json")
        return json.loads(body)


def legacy(document, directive, blocked, **extra):
    """A report as report-uri sends it."""
    return {"csp-report": {"document-uri": document, "effective-directive": directive,
                           "violated-directive": directive, "blocked-uri": blocked,
                           "disposition": "report", "original-policy": "…", **extra}}


def reporting(document, directive, blocked, **extra):
    """One report as report-to sends it (an element of the delivery's list)."""
    return {"type": "csp-violation", "url": document, "age": 10, "user_agent": "test",
            "body": {"documentURL": document, "effectiveDirective": directive,
                     "blockedURL": blocked, "disposition": "enforce",
                     "originalPolicy": "…", "statusCode": 200, **extra}}


MARK = "REFLECTED-MARKER"


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        os.environ.pop("RETINUE_CSP", None)
        wg = _load_gateway(tmp)
        cp = wg.content_policy
        # The file lives beside the conversations, on the persistent volume.
        assert wg.CSP_REPORTS_FILE == tmp / "state" / "csp-reports.json", wg.CSP_REPORTS_FILE
        server = ThreadingHTTPServer(("127.0.0.1", 0), wg.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        client = Client(server.server_address[1])
        log = io.StringIO()
        try:
            # ── The policy names the endpoint ─────────────────────────────────
            for mode in ("report", "enforce"):
                os.environ["RETINUE_CSP"] = mode
                _, headers, _ = client.request("GET", "/settings.html")
                name = ("Content-Security-Policy-Report-Only" if mode == "report"
                        else "Content-Security-Policy")
                value = headers.get(name)
                assert value and value.endswith("; report-uri /csp-report"), value
                # report-to would make Chromium drop report-uri (content_policy.py).
                assert "report-to" not in value and headers.get("Reporting-Endpoints") is None
            os.environ.pop("RETINUE_CSP", None)
            print("ok: every page's policy names the report endpoint, in report and enforce mode")

            assert client.kept()["violations"] == []

            with contextlib.redirect_stdout(log):
                # ── report-uri's format ──────────────────────────────────────────
                status, _, body = client.post("application/csp-report", legacy(
                    f"https://dash.example/chat.html?id=secret{MARK}#frag", "script-src-elem",
                    "inline", **{"source-file": "https://dash.example/chat.html?id=1",
                                 "line-number": 12}))
                assert status == 204 and body == b"", (status, body)
                # ── report-to's format: two violations and a report of another type ──
                status, _, body = client.post("application/reports+json", [
                    reporting("https://dash.example/sparql", "script-src", "eval"),
                    {"type": "deprecation", "url": "https://dash.example/",
                     "body": {"id": "x", "message": MARK}},
                    reporting("https://dash.example/viewer.html?src=/x", "frame-src",
                              f"https://elsewhere.example/page?q={MARK}"),
                ])
                assert status == 204 and body == b"", (status, body)
                # ── The same violations again: counted, not kept twice ────────────
                client.post("application/csp-report; charset=utf-8", legacy(
                    "https://dash.example/chat.html?id=other", "script-src-elem", "inline"))
                client.post("application/reports+json", [
                    reporting("https://dash.example/sparql?doc=ontology", "script-src", "eval")])
            kept = client.kept()
            rows = {(v["document"], v["directive"], v["blocked"]): v for v in kept["violations"]}
            assert set(rows) == {("/chat.html", "script-src-elem", "inline"),
                                 ("/sparql", "script-src", "eval"),
                                 ("/viewer.html", "frame-src", "https://elsewhere.example/page")}, rows
            chat = rows[("/chat.html", "script-src-elem", "inline")]
            assert chat["count"] == 2 and chat["first_seen"] <= chat["last_seen"], chat
            assert chat["source"] == "https://dash.example/chat.html:12", chat
            assert rows[("/sparql", "script-src", "eval")]["count"] == 2
            assert rows[("/sparql", "script-src", "eval")]["disposition"] == "enforce"
            assert rows[("/viewer.html", "frame-src", "https://elsewhere.example/page")]["count"] == 1
            assert kept["mode"] == "report" and kept["policy"] == cp.policy()
            assert MARK not in json.dumps(kept), "query strings are cut off"
            print("ok: both formats are taken, kept once per page, directive and blocked URL, "
                  "and counted")
            lines = [line for line in log.getvalue().splitlines() if "CSP" in line]
            assert len(lines) == 3, lines
            assert any("script-src-elem blocked inline on /chat.html" in line for line in lines), lines
            print("ok: each new violation is logged once")

            # ── Junk, oversize, no length, another type: refused, nothing kept ──
            before = client.kept()["violations"]
            refusals = [
                (client.post("application/csp-report", b"{" + MARK.encode()), 400),
                (client.post("application/csp-report", {"not-a-report": MARK}), 400),
                (client.post("application/csp-report", legacy("", "script-src", MARK)), 400),
                (client.post("application/csp-report", legacy("https://d/x", "<script>" + MARK, "x")), 400),
                (client.post("application/reports+json", {"type": "csp-violation"}), 400),
                (client.post("application/csp-report", b"\xff\xfe" + MARK.encode()), 400),
                # Nesting past the parser's recursion limit is junk like any other.
                (client.post("application/reports+json", b"[" * 60000), 400),
                (client.post("application/csp-report",
                             b'{"csp-report": {"document-uri": ' + b"[" * 60000 + b"}}"), 400),
                (client.post("application/json", legacy("https://d/x", "script-src", MARK)), 415),
                (client.post("text/plain", MARK.encode()), 415),
                (client.post("application/csp-report",
                             b'{"csp-report": {"blocked-uri": "' + b"x" * (cp.REPORT_MAX_BYTES + 1)
                             + b'"}}'), 413),
                (client.request("POST", "/csp-report", b"",
                                {"Content-Type": "application/csp-report",
                                 "Transfer-Encoding": "chunked"}), 411),
                (client.request("POST", "/csp-report", b"",
                                {"Content-Type": "application/csp-report"}), 411),
                (client.request("POST", "/csp-report", b"",
                                {"Content-Type": "application/csp-report",
                                 "Content-Length": "12x"}), 411),
            ]
            for (status, _, body), expected in refusals:
                assert status == expected, (status, expected, body)
                assert MARK.encode() not in body, body
            # A delivery of other reports only is no error, and keeps nothing.
            status, _, _ = client.post("application/reports+json", [
                {"type": "intervention", "url": "https://dash.example/", "body": {}}])
            assert status == 204
            assert client.kept()["violations"] == before
            print("ok: junk, an oversized body, a missing length and another type are refused, "
                  "reflect nothing and keep nothing")

            # ── Fields are one short line of text ─────────────────────────────
            client.post("application/csp-report", legacy(
                "https://dash.example/news.html", "img-src",
                "https://img.example/a\r\nX-Injected: 1 " + "y" * 1000))
            row = next(v for v in client.kept()["violations"] if v["document"] == "/news.html")
            assert "\n" not in row["blocked"] and "\r" not in row["blocked"]
            assert " " not in row["blocked"] and len(row["blocked"]) <= 300, row
            print("ok: every field is kept as one short line")

            # ── The cap ──────────────────────────────────────────────────────
            cp.VIOLATIONS_LIMIT = len(client.kept()["violations"]) + 2
            for i in range(5):
                status, _, _ = client.post("application/csp-report", legacy(
                    f"https://dash.example/page-{i}.html", "img-src", "data"))
                assert status == 204
            kept = client.kept()
            assert len(kept["violations"]) == cp.VIOLATIONS_LIMIT, len(kept["violations"])
            assert kept["not_kept"] == 3 and kept["limit"] == cp.VIOLATIONS_LIMIT, kept["not_kept"]
            # A violation already kept is still counted at the limit.
            client.post("application/csp-report", legacy(
                "https://dash.example/page-0.html", "img-src", "data"))
            row = next(v for v in client.kept()["violations"] if v["document"] == "/page-0.html")
            assert row["count"] == 2
            stored = json.loads(wg.CSP_REPORTS_FILE.read_text())
            assert len(stored["violations"]) == cp.VIOLATIONS_LIMIT
            print("ok: no more than the limit of distinct violations is kept; the rest are counted")

            # ── A file that cannot be read starts afresh; the answer is the same ──
            wg.CSP_REPORTS_FILE.write_text("not json")
            status, _, _ = client.post("application/csp-report", legacy(
                "https://dash.example/", "script-src-elem", "inline"))
            assert status == 204
            assert len(client.kept()["violations"]) == 1
            print("ok: an unreadable file is replaced, not a reason to refuse")
        finally:
            server.shutdown()
            server.server_close()
            os.environ.pop("RETINUE_CSP", None)
    print("All CSP report checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
