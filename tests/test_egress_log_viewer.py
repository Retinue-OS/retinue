#!/usr/bin/env python3
"""The egress log viewer's pages: escaped attributes and a policy of their own.

The deployment example (docker-compose.override.example.yml) can publish the
viewer at /egress on the dashboard's own host, which puts its pages on the
dashboard's origin. The gateway's Content-Security-Policy only reaches what
the gateway itself answers, so the viewer must keep to the same rules on its
own: what a request or the log brings into a page is escaped for an attribute
as well as for text, and every page carries a fixed, enforced policy that
runs no script, together with nosniff on every answer. What these pin down:

- a quote in the filter fields (q, host, date and the others written back
  into the form) cannot leave its value="..." attribute;
- a flow in the log whose host or path holds markup adds no element and no
  attribute to the table or to the flow's page;
- the viewer's pages use no script, and every HTML answer (the index, a
  flow, the anomalies, not found) carries the policy, enforced;
- every answer, JSON included, forbids sniffing.

Runs the viewer's real handler on a loopback port over a seeded log.

    python3 tests/test_egress_log_viewer.py
"""
import http.client
import importlib.util
import json
import os
import sys
import tempfile
import threading
import urllib.parse
from html.parser import HTMLParser
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VIEWER = REPO_ROOT / "scripts" / "egress-log-viewer.py"

POLICY = ("default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
          "base-uri 'none'; frame-ancestors 'none'")

# Each would add an attribute to the input it is written into, were a quote
# left unescaped there: a double quote for the value="..." the form writes,
# a single quote for an attribute a later edit might quote that way.
PAYLOADS = [
    '" autofocus onfocus="alert(document.domain)',
    "' autofocus onfocus='alert(document.domain)",
    '"><script>alert(1)</script>',
    '"/onmouseover="alert(1)',
]
# The fields the form writes back, and the attributes each input may have.
FIELDS = ["q", "host", "date", "status_min", "status_max"]
INPUT_ATTRS = {"type", "name", "value", "placeholder", "style"}

HOSTILE_HOST = 'x.example"><img src=x onerror=alert(1)>'
HOSTILE_PATH = "/'><svg onload=alert(1)>\"<script>alert(2)</script>"


class Page(HTMLParser):
    """Collects every start tag with its attributes."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    handle_startendtag = handle_starttag


def parse(body: str) -> list[tuple[str, dict]]:
    page = Page()
    page.feed(body)
    page.close()
    return page.tags


def _load_viewer(log_dir: Path):
    os.environ["EGRESS_AUDIT_LOG_DIR"] = str(log_dir)
    os.environ["EGRESS_AUDIT_ANOMALY_LOG"] = str(log_dir / "anomalies.ndjson")
    os.environ.pop("EGRESS_VIEWER_BASE_PATH", None)
    spec = importlib.util.spec_from_file_location("egress_log_viewer", VIEWER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed(log_dir: Path) -> dict:
    flow = {
        "ts": "2026-11-12T10:00:00Z", "method": "GET", "host": HOSTILE_HOST,
        "port": 443, "path": HOSTILE_PATH, "response_status": 200,
        "request_body": {"size": 0}, "response_body": {"size": 12},
    }
    (log_dir / "2026-11-12.ndjson").write_text(json.dumps(flow) + "\n", encoding="utf-8")
    anomaly = {"ts": "2026-11-12T10:00:01Z", "severity": "high", "type": "new-host",
               "host": HOSTILE_HOST, "path": HOSTILE_PATH, "reason": '"><b onclick="x">'}
    (log_dir / "anomalies.ndjson").write_text(json.dumps(anomaly) + "\n", encoding="utf-8")
    return flow


def _get(port: int, path: str):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.headers, resp.read().decode("utf-8", "replace")
    finally:
        conn.close()


def _no_injected_markup(tags, where: str, failures: list):
    for tag, attrs in tags:
        if tag in ("script", "img", "svg", "iframe", "object", "embed", "b"):
            failures.append(f"{where}: an injected <{tag}> became an element")
        for name in attrs:
            if name.startswith("on") or name == "autofocus":
                failures.append(f"{where}: <{tag}> gained the attribute {name}")


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp) / "egress"
        log_dir.mkdir()
        flow = _seed(log_dir)
        viewer = _load_viewer(log_dir)
        server = ThreadingHTTPServer(("127.0.0.1", 0), viewer.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        try:
            # The filter fields: each payload stays inside its value.
            for field in FIELDS:
                for payload in PAYLOADS:
                    query = urllib.parse.urlencode({field: payload})
                    status, _, body = _get(port, "/?" + query)
                    where = f"/?{field}={payload!r}"
                    if status != 200:
                        failures.append(f"{where}: answered {status}")
                        continue
                    tags = parse(body)
                    _no_injected_markup(tags, where, failures)
                    inputs = [a for t, a in tags if t == "input" and a.get("name") == field]
                    if len(inputs) != 1:
                        failures.append(f"{where}: {len(inputs)} inputs named {field}")
                        continue
                    if inputs[0].get("value") != payload:
                        failures.append(f"{where}: the value reads {inputs[0].get('value')!r}")
                    extra = set(inputs[0]) - INPUT_ATTRS
                    if extra:
                        failures.append(f"{where}: the input gained {sorted(extra)}")

            # Markup from the log: the table, the flow's page, the anomalies.
            detail = "/flow?" + urllib.parse.urlencode(
                {"ts": flow["ts"], "host": flow["host"], "path": flow["path"]})
            for path in ("/", "/?host=x.example", detail, "/anomalies"):
                status, _, body = _get(port, path)
                if status != 200:
                    failures.append(f"{path}: answered {status}")
                    continue
                _no_injected_markup(parse(body), path, failures)
            status, _, body = _get(port, detail)
            if "Flow not found" in body:
                failures.append("the seeded flow's page did not find the flow")

            # Every HTML answer carries the policy, enforced; no page uses
            # script; every answer forbids sniffing.
            pages = ["/", "/?q=x", detail, "/flow", "/anomalies", "/nowhere"]
            for path in pages:
                status, headers, body = _get(port, path)
                if not headers.get("Content-Type", "").startswith("text/html"):
                    failures.append(f"{path}: not a page ({headers.get('Content-Type')})")
                policies = headers.get_all("Content-Security-Policy") or []
                if policies != [POLICY]:
                    failures.append(f"{path} ({status}): the policy reads {policies}")
                if headers.get("Content-Security-Policy-Report-Only"):
                    failures.append(f"{path}: the policy is report-only")
                if any(tag == "script" for tag, _ in parse(body)):
                    failures.append(f"{path}: the page holds a <script>")
            for path in pages + ["/api/flows", "/api/anomalies", "/health"]:
                _, headers, _ = _get(port, path)
                if headers.get("X-Content-Type-Options") != "nosniff":
                    failures.append(f"{path}: sniffing is not forbidden")
        finally:
            server.shutdown()
            server.server_close()

    if failures:
        for failure in failures:
            print("FAIL", failure)
        return 1
    print("ok: the egress log viewer escapes attributes and carries its policy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
