#!/usr/bin/env python3
"""The life store's published endpoint, /sparql (issue #261), end to end.

Runs the gateway's real request handler on a loopback port in front of a fake
store that records what reaches it, and pins what makes the endpoint safe to
open to the dashboard's users:

- only a query ever reaches the store — as a fresh form POST holding nothing
  but `query`, with the caller's Accept and never their Authorization header
  (nor the store's access token, should a caller send one);
- every way of sending a SPARQL Update is refused before the store is asked;
- the store's answer comes back as it gave it, an error included, but never as
  HTML on the dashboard's origin;
- without a query a browser gets the page and any other client a Service
  Description; a store that is down is an honest 502;
- /docs/<name>.md serves the framework docs the page renders, and nothing
  outside them.

    python3 tests/test_web_gateway_sparql.py
"""
import http.client
import importlib.util
import json
import os
import sys
import tempfile
import threading
import types
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_gateway(tmp: Path):
    """Load scripts/web-gateway.py with sandboxed state/data directories."""
    os.environ["CONVERSATIONS_DIR"] = str(tmp / "convs")
    os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state.json")
    (tmp / "chambers").mkdir(parents=True, exist_ok=True)
    if "markdown_it" not in sys.modules:
        try:
            import markdown_it  # noqa: F401
        except ImportError:
            stub = types.ModuleType("markdown_it")
            stub.MarkdownIt = object
            sys.modules["markdown_it"] = stub
    sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "web_gateway_sparql_under_test", SCRIPTS_DIR / "web-gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeStore(BaseHTTPRequestHandler):
    """Records every request; answers with whatever `reply` says."""
    calls: list = []
    reply = (200, "application/sparql-results+json", b'{"head":{"vars":[]},"results":{"bindings":[]}}')

    def log_message(self, *args):
        pass

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        FakeStore.calls.append({
            "method": self.command, "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": self.rfile.read(length).decode("utf-8") if length else "",
        })
        status, ctype, payload = FakeStore.reply
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _answer


def _serve(handler) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class Client:
    def __init__(self, port: int):
        self.port = port

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data


FORM = {"Content-Type": "application/x-www-form-urlencoded"}
QUERY = "SELECT ?class WHERE { ?s a ?class } LIMIT 3"


def test_query_reaches_the_store_alone(c: Client):
    FakeStore.calls.clear()
    FakeStore.reply = (200, "text/csv", b"class\nurn:x\n")
    status, headers, body = c.request(
        "POST", "/sparql?access-token=leaked",
        body=urllib.parse.urlencode({"query": QUERY, "default-graph-uri": "urn:g"}),
        headers={**FORM, "Accept": "text/csv", "Authorization": "Basic dXNlcjpwdw=="})
    assert status == 200, status
    assert body == b"class\nurn:x\n", body
    assert headers["content-type"] == "text/csv", headers
    assert headers["cache-control"] == "private, no-store", headers
    assert len(FakeStore.calls) == 1, FakeStore.calls
    call = FakeStore.calls[0]
    assert call["method"] == "POST" and call["path"] == "/", call
    assert urllib.parse.parse_qs(call["body"]) == {"query": [QUERY]}, call["body"]
    assert call["headers"]["accept"] == "text/csv", call["headers"]
    assert call["headers"]["content-type"] == "application/x-www-form-urlencoded"
    assert "authorization" not in call["headers"], call["headers"]
    print("ok - only the query and its Accept reach the store; credentials and tokens stay behind")


def test_every_protocol_form_of_a_query(c: Client):
    FakeStore.reply = (200, "application/sparql-results+json", b'{"boolean":true}')
    forms = [
        ("GET", "/sparql?" + urllib.parse.urlencode({"query": QUERY}), None, {}),
        ("POST", "/sparql", QUERY, {"Content-Type": "application/sparql-query; charset=utf-8"}),
        ("POST", "/sparql/", urllib.parse.urlencode({"query": QUERY}), FORM),
    ]
    for method, path, body, headers in forms:
        FakeStore.calls.clear()
        status, _h, data = c.request(method, path, body=body, headers=headers)
        assert status == 200 and data == b'{"boolean":true}', (method, path, status, data)
        assert urllib.parse.parse_qs(FakeStore.calls[0]["body"]) == {"query": [QUERY]}, (method, path)
        # No Accept from the caller: the store is asked for SPARQL JSON.
        assert FakeStore.calls[0]["headers"]["accept"] == "application/sparql-results+json"
    print("ok - URL, form and application/sparql-query bodies all carry the query")


def test_updates_are_refused_before_the_store(c: Client):
    attempts = [
        ("POST", "/sparql", urllib.parse.urlencode({"update": "DROP ALL"}), FORM),
        ("POST", "/sparql", "DROP ALL", {"Content-Type": "application/sparql-update"}),
        ("GET", "/sparql?update=DROP%20ALL", None, {}),
        ("POST", "/sparql?update=DROP%20ALL", urllib.parse.urlencode({"query": QUERY}), FORM),
    ]
    for method, path, body, headers in attempts:
        FakeStore.calls.clear()
        status, _h, data = c.request(method, path, body=body, headers=headers)
        assert status == 403, (method, path, status)
        assert "read-only" in json.loads(data)["error"]
        assert not FakeStore.calls, f"{method} {path} reached the store"
    print("ok - every form of SPARQL Update is refused and never reaches the store")


def test_malformed_requests(c: Client, wg):
    cases = [
        ("POST", "/sparql", None, {}, 400),                        # nothing at all
        ("POST", "/sparql", "query=", FORM, 400),                  # an empty query
        ("GET", "/sparql?query=ASK%7B%7D&query=ASK%7B%7D", None, {}, 400),  # two
    ]
    for method, path, body, headers, want in cases:
        FakeStore.calls.clear()
        status, _h, _d = c.request(method, path, body=body, headers=headers)
        assert status == want, (method, path, status)
        assert not FakeStore.calls
    limit = wg.SPARQL_MAX_BODY
    wg.SPARQL_MAX_BODY = 16
    try:
        status, _h, _d = c.request("POST", "/sparql", body="query=" + "x" * 32, headers=FORM)
        assert status == 413, status
    finally:
        wg.SPARQL_MAX_BODY = limit
    print("ok - an empty, doubled or oversized request is refused")


def test_store_answers_pass_through_but_never_as_html(c: Client):
    FakeStore.reply = (400, "application/json", b'{"exception":"Invalid SPARQL query"}')
    status, headers, body = c.request("POST", "/sparql", body="query=SELEKT", headers=FORM)
    assert status == 400 and b"Invalid SPARQL" in body, (status, body)
    assert headers["content-type"] == "application/json"
    FakeStore.reply = (502, "text/html", b"<script>alert(1)</script>")
    status, headers, body = c.request("POST", "/sparql", body="query=ASK%7B%7D", headers=FORM)
    assert status == 502, status
    assert headers["content-type"].startswith("text/plain"), headers
    assert headers["x-content-type-options"] == "nosniff"
    print("ok - the store's own errors pass through; HTML never renders on this origin")


def test_without_a_query_the_endpoint_describes_itself(c: Client):
    FakeStore.calls.clear()
    status, headers, body = c.request(
        "GET", "/sparql", headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
    assert status == 200 and b"<retinue-sparql-doc" in body, (status, body[:200])
    assert headers["content-type"].startswith("text/html")
    status, headers, body = c.request("GET", "/sparql", headers={"Accept": "text/turtle"})
    assert status == 200 and headers["content-type"].startswith("text/turtle"), headers
    text = body.decode()
    assert "a sd:Service" in text and "sd:endpoint <>" in text and "sd:SPARQL11Query" in text
    status, headers, _b = c.request("GET", "/sparql")  # no Accept at all
    assert headers["content-type"].startswith("text/turtle"), headers
    assert not FakeStore.calls, "describing the endpoint must not query the store"
    try:
        import rdflib
    except ImportError:
        print("ok - a browser gets the page, other clients a Service Description")
        return
    g = rdflib.Graph().parse(data=text, format="turtle", publicID="https://agents.example.com/sparql")
    sd = rdflib.Namespace("http://www.w3.org/ns/sparql-service-description#")
    me = rdflib.URIRef("https://agents.example.com/sparql")
    assert (me, rdflib.RDF.type, sd.Service) in g and (me, sd.endpoint, me) in g
    print("ok - a browser gets the page, other clients a Service Description (valid Turtle)")


def test_store_down_is_a_502(c: Client, wg, dead_port: int):
    live = wg.QLEVER_LIFE_URL
    wg.QLEVER_LIFE_URL = f"http://127.0.0.1:{dead_port}"
    try:
        status, _h, body = c.request("POST", "/sparql", body="query=ASK%7B%7D", headers=FORM)
    finally:
        wg.QLEVER_LIFE_URL = live
    assert status == 502 and json.loads(body)["error"] == "life store unreachable", (status, body)
    print("ok - a store that is down is an honest 502")


def test_docs_are_served_and_contained(c: Client):
    status, headers, body = c.request("GET", "/docs/ontology.md")
    assert status == 200 and body.startswith(b"# Ontology"), (status, body[:40])
    assert headers["content-type"] == "text/markdown; charset=utf-8", headers
    for path in ("/docs/../secret.md", "/docs/%2e%2e/secret.md", "/docs/sub/inner.md",
                 "/docs/missing.md", "/docs/notes.txt", "/docs/.hidden.md", "/docs/"):
        status, _h, _b = c.request("GET", path)
        assert status == 404, (path, status)
    print("ok - /docs serves the framework docs and nothing beside them")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        wg = _load_gateway(tmp)
        docs = tmp / "docs"
        (docs / "sub").mkdir(parents=True)
        (docs / "ontology.md").write_text("# Ontology defaults\n")
        (docs / "sub" / "inner.md").write_text("# inner\n")
        (docs / "notes.txt").write_text("not markdown\n")
        (docs / ".hidden.md").write_text("# hidden\n")
        (tmp / "secret.md").write_text("# secret\n")
        webapp = tmp / "webapp"
        webapp.mkdir()
        (webapp / "sparql.html").write_text("<!doctype html><retinue-sparql-doc></retinue-sparql-doc>")
        store = _serve(FakeStore)
        wg.QLEVER_LIFE_URL = f"http://127.0.0.1:{store.server_address[1]}"
        wg.DOCS_DIR = docs
        wg.WEBAPP_DIR = webapp
        gateway = _serve(wg.Handler)
        # A port nothing listens on, for the store-down case.
        probe = ThreadingHTTPServer(("127.0.0.1", 0), FakeStore)
        dead_port = probe.server_address[1]
        probe.server_close()
        c = Client(gateway.server_address[1])
        try:
            test_query_reaches_the_store_alone(c)
            test_every_protocol_form_of_a_query(c)
            test_updates_are_refused_before_the_store(c)
            test_malformed_requests(c, wg)
            test_store_answers_pass_through_but_never_as_html(c)
            test_without_a_query_the_endpoint_describes_itself(c)
            test_store_down_is_a_502(c, wg, dead_port)
            test_docs_are_served_and_contained(c)
        except AssertionError as exc:
            print(f"FAIL: {exc!r}")
            return 1
        finally:
            gateway.shutdown()
            store.shutdown()
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
