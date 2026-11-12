#!/usr/bin/env python3
"""No route that serves bytes from outside answers with a script type.

The dashboard's Content-Security-Policy runs scripts from this origin only
(scripts/content_policy.py). That is a boundary only while nothing on the
origin serves bytes someone outside chose under a type a browser runs as a
script or a worker: a <script src> or a worker pointed at such an answer
would run it as the dashboard's own. Under nosniff that is the JavaScript
MIME types and nothing else, so every such route must

- never answer with a JavaScript MIME type (a type that does not read as
  exactly one media type counts as application/octet-stream, since a browser
  may read a list of types as its last one), and
- always forbid sniffing.

Runs the gateway's real handler on a loopback port, with a fake messenger
gateway (media and pairing codes) and a fake life store, over every route
that serves such bytes: thread attachments (download and ?inline=1), the
messenger media proxy, the letterbox share, /data/ (which answers JSON only,
anything else as a download), the life store's results and the pairing-code
proxy.

    python3 tests/test_web_gateway_no_script_types.py
"""
import base64
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
WEBAPP = REPO_ROOT / "webapp"

# WHATWG MIME Sniffing, "JavaScript MIME type": what a browser runs as a
# script under nosniff. Written out here rather than taken from the gateway,
# so a shortened list in the gateway fails.
SCRIPT_TYPES = [
    "application/ecmascript", "application/javascript", "application/x-ecmascript",
    "application/x-javascript", "text/ecmascript", "text/javascript",
    "text/javascript1.0", "text/javascript1.1", "text/javascript1.2", "text/javascript1.3",
    "text/javascript1.4", "text/javascript1.5", "text/jscript", "text/livescript",
    "text/x-ecmascript", "text/x-javascript",
]
# What outside parties may state: every script type, spelled as a sender
# might, and lists of types a browser may read as their last one.
STATED = (SCRIPT_TYPES
          + ["TEXT/JavaScript; charset=utf-8", "application/javascript;charset=UTF-8",
             'text/javascript; codecs="x"', "text/plain, text/javascript",
             "image/png,text/javascript"])


def is_script_type(value: str | None) -> bool:
    """Whether a browser could take this Content-Type for a script type."""
    if not value:
        return False
    # A browser extracting a MIME type from a header reads every
    # comma-separated value and uses the last valid one.
    for part in value.split(","):
        if part.split(";", 1)[0].strip().lower() in SCRIPT_TYPES:
            return True
    return False


class FakeGateway(BaseHTTPRequestHandler):
    """A messenger gateway: /health, /media/<id> and /qr, each answered with
    the type the test sets."""
    media: dict = {}
    qr_type = "image/png"

    def log_message(self, *args):
        pass

    def _answer(self, status, ctypes, body):
        self.send_response(status)
        for ctype in ctypes:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._answer(200, ["application/json"], b'{"connected": true, "mode": "inbox"}')
        elif self.path.startswith("/media/"):
            ctypes = self.media.get(self.path[len("/media/"):])
            if ctypes is None:
                self._answer(404, ["application/json"], b'{"error": "not found"}')
            else:
                self._answer(200, ctypes, b"alert(document.domain)")
        elif self.path == "/qr":
            self._answer(200, [self.qr_type], b"alert(1)")
        else:
            self._answer(404, ["application/json"], b"{}")


class FakeStore(BaseHTTPRequestHandler):
    ctype = "text/javascript"

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = b"alert(1)"
        self.send_response(200)
        self.send_header("Content-Type", self.ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _load_gateway(tmp: Path, gw_port: int, store_port: int):
    os.environ["CONVERSATIONS_DIR"] = str(tmp / "convs")
    os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state.json")
    os.environ["RETINUE_INSTANCE_FILE"] = str(tmp / "instance.json")
    os.environ["WEBAPP_DIR"] = str(WEBAPP)
    os.environ["DASHBOARD_DATA_DIR"] = str(tmp / "data")
    os.environ["LETTERBOX_DIR"] = str(tmp / "letterbox")
    os.environ["QLEVER_LIFE_URL"] = f"http://127.0.0.1:{store_port}"
    os.environ["SIGNAL_GATEWAY_BASE_URL"] = f"http://127.0.0.1:{gw_port}"
    os.environ["SIGNAL_GATEWAY_TOKEN"] = "gw-secret"
    for name in ("WHATSAPP_GATEWAY_BASE_URL", "TELEGRAM_GATEWAY_BASE_URL", "MESSENGER_GATEWAYS"):
        os.environ.pop(name, None)
    for d in ("chambers", "data", "letterbox"):
        (tmp / d).mkdir(parents=True, exist_ok=True)
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
        "web_gateway_no_script_types_under_test", SCRIPTS_DIR / "web-gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _request(port, method, path, body=b"", headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, body=body or None, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.headers, resp.read()
    finally:
        conn.close()


def main() -> int:
    failures = []

    def check(route, status, headers, expect_status=200):
        ctypes = headers.get_all("Content-Type") or []
        if status != expect_status:
            failures.append(f"{route}: status {status}, expected {expect_status}")
        if len(ctypes) != 1:
            failures.append(f"{route}: {len(ctypes)} Content-Type headers")
        if any(is_script_type(c) for c in ctypes):
            failures.append(f"{route}: answered as a script ({ctypes})")
        if (headers.get("X-Content-Type-Options") or "").lower() != "nosniff":
            failures.append(f"{route}: sniffing is not forbidden")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        gw_server, store_server = _serve(FakeGateway), _serve(FakeStore)
        wg = _load_gateway(tmp, gw_server.server_address[1], store_server.server_address[1])
        server = _serve(wg.Handler)
        port = server.server_address[1]
        try:
            # ── The gateway's own reading of a stated type ─────────────────
            for stated in STATED:
                served = wg._served_media_type(stated)
                if is_script_type(served):
                    failures.append(f"_served_media_type({stated!r}) = {served!r}")
                headers = wg._chat_media_headers([stated])
                if is_script_type(headers["Content-Type"]):
                    failures.append(f"_chat_media_headers({stated!r}) = {headers}")
            assert wg._served_media_type("image/png") == "image/png"
            assert wg._served_media_type("text/plain; charset=utf-8") == "text/plain; charset=utf-8"
            print(f"ok: no stated type of {len(STATED)} becomes a script type")

            # ── Thread attachments, stored as stated and as metadata of old ──
            conv = wg._new_conv("agent", "Web", "files", "agent", "files", first_attachments=[
                {"filename": f"f{i}.js", "content_type": stated,
                 "data": base64.b64encode(b"alert(document.domain)").decode()}
                for i, stated in enumerate(STATED)])
            atts = conv["messages"][0]["attachments"]
            assert len(atts) == len(STATED), len(atts)
            # Metadata stored before types were read strictly holds the type
            # as it came; the route reads it again.
            for att, stated in zip(atts, STATED):
                att["content_type"] = stated
            wg._save_conv(conv)
            for att in atts:
                for query in ("", "?inline=1"):
                    route = f"/conversations/{conv['id']}/attachments/{att['id']}/f.js{query}"
                    status, headers, _ = _request(port, "GET", route)
                    check(route, status, headers)
            print("ok: thread attachments are never served as a script, inline or not")

            # ── Messenger media through the proxy ────────────────────────────
            slug = next(iter(wg._CHANNEL_GATEWAYS))
            for i, stated in enumerate(STATED):
                FakeGateway.media[f"{i:032x}"] = [stated]
            FakeGateway.media["f" * 32] = ["image/png", "text/javascript"]
            for media_id in FakeGateway.media:
                route = f"/chats/media/{slug}/{media_id}"
                status, headers, _ = _request(port, "GET", route)
                check(route, status, headers)
            print("ok: messenger media are never served as a script")

            # ── The letterbox share ──────────────────────────────────────────
            for name in ("a.js", "b.mjs", "c.html", "d.svg"):
                status, _, _ = _request(port, "PUT", f"/inbox/{name}", b"alert(1)",
                                        {"Content-Type": "text/javascript"})
                assert status in (201, 204), (name, status)
                status, headers, _ = _request(port, "GET", f"/inbox/{name}")
                check(f"/inbox/{name}", status, headers)
            print("ok: the letterbox share serves no script")

            # ── /data/: JSON only, anything else a download ──────────────────
            data_dir = Path(os.environ["DASHBOARD_DATA_DIR"])
            (data_dir / "cards.json").write_text('{"ok": true}')
            for name in ("x.js", "x.mjs", "x.html", "x.svg", "x.css", "x.webmanifest", "x.md"):
                (data_dir / name).write_text("alert(1)")
                status, headers, _ = _request(port, "GET", f"/data/{name}")
                check(f"/data/{name}", status, headers)
                if headers.get("Content-Type") != "application/octet-stream" or \
                        headers.get("Content-Disposition") != "attachment":
                    failures.append(f"/data/{name}: not a download ({headers.get('Content-Type')})")
            status, headers, body = _request(port, "GET", "/data/cards.json")
            check("/data/cards.json", status, headers)
            if not headers.get("Content-Type", "").startswith("application/json") or \
                    headers.get("Content-Disposition") or json.loads(body) != {"ok": True}:
                failures.append("/data/cards.json is not served as the JSON the cards read")
            print("ok: /data/ answers JSON, and anything else as a download")

            # ── The life store's results ─────────────────────────────────────
            form = urllib.parse.urlencode({"query": "SELECT * WHERE { ?s ?p ?o }"}).encode()
            for stated in STATED:
                FakeStore.ctype = stated
                status, headers, _ = _request(
                    port, "POST", "/sparql", form,
                    {"Content-Type": "application/x-www-form-urlencoded"})
                check(f"/sparql answered by the store as {stated}", status, headers)
            print("ok: the life store's results are never served as a script")

            # ── The pairing-code proxy ───────────────────────────────────────
            for stated in STATED + ["text/html"]:
                FakeGateway.qr_type = stated
                route = f"/gateways/{slug}/qr"
                status, headers, _ = _request(port, "GET", route)
                check(f"{route} as {stated}", status, headers)
                if headers.get("Content-Type") not in ("application/octet-stream",):
                    failures.append(f"{route} as {stated}: {headers.get('Content-Type')}")
            FakeGateway.qr_type = "image/png"
            status, headers, _ = _request(port, "GET", f"/gateways/{slug}/qr")
            check("pairing code as PNG", status, headers)
            assert headers.get("Content-Type") == "image/png", headers.get("Content-Type")
            print("ok: the pairing-code proxy passes on a PNG or JSON and nothing else")

            # ── The API's own answers forbid sniffing too ────────────────────
            status, headers, _ = _request(port, "GET", f"/conversations/{conv['id']}")
            check(f"/conversations/{conv['id']}", status, headers)
            print("ok: the API's JSON forbids sniffing")

            # ── The dashboard's own scripts forbid sniffing as well ──────────
            # They are scripts and say so, which is what script-src 'self' is
            # for; the header is there so that no answer of this origin is
            # left to a browser's guess. The service worker has a responder
            # of its own (the cache name is stamped into it), so it is named
            # here beside two files the static responder serves.
            for route in ("/sw.js", "/sw-register.js", "/components/shell.js"):
                status, headers, _ = _request(port, "GET", route)
                if status != 200:
                    failures.append(f"{route}: status {status}, expected 200")
                if (headers.get("X-Content-Type-Options") or "").lower() != "nosniff":
                    failures.append(f"{route}: sniffing is not forbidden")
            print("ok: the dashboard's own scripts forbid sniffing, the service worker included")
        finally:
            for s in (server, gw_server, store_server):
                s.shutdown()
                s.server_close()
    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("All no-script-type checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
