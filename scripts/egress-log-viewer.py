#!/usr/bin/env python3
"""Web viewer for egress-audit NDJSON logs.

Serves an HTML dashboard and a JSON API over HTTP. It is read-only: it never
mutates the log volume.

Configuration (environment):
    EGRESS_AUDIT_LOG_DIR     directory containing daily *.ndjson flow logs
    EGRESS_AUDIT_ANOMALY_LOG path to the anomalies NDJSON log
    EGRESS_VIEWER_PORT       HTTP port to listen on (default 8080)
    EGRESS_VIEWER_PAGE_SIZE  rows per page (default 100)
"""
import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOG_DIR = Path(os.environ.get("EGRESS_AUDIT_LOG_DIR", "/var/log/retinue/egress"))
ANOMALY_LOG = Path(os.environ.get("EGRESS_AUDIT_ANOMALY_LOG", "/var/log/retinue/egress/anomalies.ndjson"))
PORT = int(os.environ.get("EGRESS_VIEWER_PORT", "8080"))
PAGE_SIZE = int(os.environ.get("EGRESS_VIEWER_PAGE_SIZE", "100"))
BASE_PATH = os.environ.get("EGRESS_VIEWER_BASE_PATH", "").rstrip("/")


def _list_log_files() -> list[Path]:
    if not LOG_DIR.is_dir():
        return []
    return sorted(
        (p for p in LOG_DIR.glob("*.ndjson") if p.name != "anomalies.ndjson"),
        reverse=True,
    )


def _load_flows(
    date: str | None = None,
    limit: int = PAGE_SIZE,
    offset: int = 0,
    host: str | None = None,
    method: str | None = None,
    status_min: int | None = None,
    status_max: int | None = None,
    query: str | None = None,
) -> tuple[list[dict], int]:
    paths = [LOG_DIR / f"{date}.ndjson"] if date else _list_log_files()
    all_flows: list[dict] = []
    for path in paths:
        if not path.is_file():
            continue
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    flow = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if host and host.lower() not in flow.get("host", "").lower():
                    continue
                if method and method.upper() != flow.get("method", ""):
                    continue
                if status_min is not None and flow.get("response_status", 0) < status_min:
                    continue
                if status_max is not None and flow.get("response_status", 0) > status_max:
                    continue
                if query:
                    q = query.lower()
                    if q not in json.dumps(flow, default=str).lower():
                        continue
                all_flows.append(flow)
    total = len(all_flows)
    return all_flows[offset : offset + limit], total


def _load_anomalies(limit: int = 100) -> list[dict]:
    if not ANOMALY_LOG.is_file():
        return []
    entries: list[dict] = []
    with open(ANOMALY_LOG, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(entries) >= limit:
                break
    return entries


def _html_escape(value) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _format_json(obj: dict) -> str:
    return _html_escape(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


_CSS = """
<style>
  body { font-family: system-ui, -apple-system, sans-serif; margin: 2rem; color: #222; }
  h1 { font-size: 1.5rem; }
  table { border-collapse: collapse; width: 100%; margin-top: 1rem; }
  th, td { border: 1px solid #ccc; padding: 0.4rem 0.6rem; text-align: left; vertical-align: top; }
  th { background: #f5f5f5; }
  tr:hover { background: #f9f9f9; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.85rem; }
  .small { font-size: 0.85rem; }
  .anomaly { background: #fff3cd; }
  .error { background: #f8d7da; }
  pre { white-space: pre-wrap; word-break: break-word; background: #f5f5f5; padding: 0.8rem; border-radius: 4px; }
  .nav { margin: 1rem 0; }
  .nav a { margin-right: 1rem; }
  form { margin: 1rem 0; padding: 0.8rem; background: #f5f5f5; border-radius: 4px; }
  input, select, button { padding: 0.3rem 0.5rem; }
</style>
"""


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{_html_escape(title)}</title>
{_CSS}
</head>
<body>
<nav class="nav">
  <a href="{BASE_PATH}/">Flows</a>
  <a href="{BASE_PATH}/anomalies">Anomalies</a>
  <a href="{BASE_PATH}/api/flows">API: flows</a>
  <a href="{BASE_PATH}/api/anomalies">API: anomalies</a>
</nav>
<h1>{_html_escape(title)}</h1>
{body}
</body>
</html>
"""


def _persist(params: dict) -> str:
    parts = []
    for key in ("host", "method", "status_min", "status_max", "q", "date"):
        val = params.get(key, [None])[0]
        if val:
            parts.append(f"&{key}={urllib.parse.quote(val)}")
    return "".join(parts)


def _index(params: dict) -> str:
    date = params.get("date", [None])[0]
    host = params.get("host", [None])[0]
    method = params.get("method", [None])[0]
    status_min = params.get("status_min", [None])[0]
    status_max = params.get("status_max", [None])[0]
    q = params.get("q", [None])[0]
    offset = int(params.get("offset", ["0"])[0] or 0)

    def _int_or_none(raw: str | None) -> int | None:
        try:
            return int(raw) if raw else None
        except ValueError:
            return None

    flows, total = _load_flows(
        date=date,
        offset=offset,
        limit=PAGE_SIZE,
        host=host,
        method=method,
        status_min=_int_or_none(status_min),
        status_max=_int_or_none(status_max),
        query=q,
    )

    form = f"""
<form method="get" action="{BASE_PATH}/">
  <label>Date <input type="date" name="date" value="{_html_escape(date or '')}"></label>
  <label>Host <input type="text" name="host" value="{_html_escape(host or '')}" placeholder="example.com"></label>
  <label>Method <select name="method"><option value="">any</option>
    <option value="GET" {"selected" if method == "GET" else ""}>GET</option>
    <option value="POST" {"selected" if method == "POST" else ""}>POST</option>
    <option value="PUT" {"selected" if method == "PUT" else ""}>PUT</option>
    <option value="DELETE" {"selected" if method == "DELETE" else ""}>DELETE</option>
  </select></label>
  <label>Status min <input type="number" name="status_min" value="{_html_escape(status_min or '')}"></label>
  <label>Status max <input type="number" name="status_max" value="{_html_escape(status_max or '')}"></label>
  <label>Query <input type="text" name="q" value="{_html_escape(q or '')}" style="width:20rem"></label>
  <button type="submit">Filter</button>
  <a href="{BASE_PATH}/">Clear</a>
</form>
<p class="small">Showing {offset + 1}-{min(offset + PAGE_SIZE, total)} of {total}</p>
"""

    rows = []
    for flow in flows:
        ts = flow.get("ts", "")
        status = flow.get("response_status", 0)
        row_class = "error" if status >= 500 else ""
        host_link = f'<a href="{BASE_PATH}/?host={urllib.parse.quote(flow.get("host", ""))}">{_html_escape(flow.get("host", ""))}</a>'
        detail_link = f'<a href="{BASE_PATH}/flow?ts={urllib.parse.quote(ts)}&host={urllib.parse.quote(flow.get("host", ""))}&path={urllib.parse.quote(flow.get("path", ""))}">view</a>'
        rows.append(f"""
<tr class="{row_class}">
  <td class="mono">{_html_escape(ts)}</td>
  <td class="mono">{_html_escape(flow.get("method", ""))}</td>
  <td>{host_link}:{flow.get("port", "")}</td>
  <td class="mono small">{_html_escape(flow.get("path", "")[:120])}</td>
  <td>{status}</td>
  <td class="small">{flow.get("request_body", {}).get("size", 0)} / {flow.get("response_body", {}).get("size", 0)}</td>
  <td>{detail_link}</td>
</tr>
""")

    table = f'<table><tr><th>Time</th><th>Method</th><th>Host</th><th>Path</th><th>Status</th><th>Req/Resp bytes</th><th></th></tr>{"".join(rows)}</table>'
    nav = ""
    if offset > 0:
        nav += f'<a href="{BASE_PATH}/?offset={max(0, offset - PAGE_SIZE)}{_persist(params)}">← Previous</a>'
    if offset + PAGE_SIZE < total:
        nav += f'<a href="{BASE_PATH}/?offset={offset + PAGE_SIZE}{_persist(params)}">Next →</a>'

    return _page("Egress audit flows", form + table + f'<p class="nav">{nav}</p>')


def _flow_detail(params: dict) -> str:
    ts = params.get("ts", [None])[0]
    host = params.get("host", [None])[0]
    path = params.get("path", [None])[0]
    if not (ts and host and path):
        return _page("Flow not found", "<p>Missing parameters.</p>")

    flow: dict | None = None
    for p in _list_log_files():
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    candidate = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if candidate.get("ts") == ts and candidate.get("host") == host and candidate.get("path") == path:
                    flow = candidate
                    break
        if flow is not None:
            break

    if flow is None:
        return _page("Flow not found", "<p>Flow not found in logs.</p>")

    body = f"""
<p><a href="{BASE_PATH}/">← Back</a></p>
<pre>{_format_json(flow)}</pre>
"""
    return _page(f"Flow {host}{path}", body)


def _anomalies() -> str:
    anomalies = _load_anomalies(limit=200)
    rows = []
    for a in anomalies:
        ts = a.get("ts", "")
        severity = a.get("severity", "info")
        row_class = "anomaly" if severity in ("warning", "high") else ""
        rows.append(f"""
<tr class="{row_class}">
  <td class="mono">{_html_escape(ts)}</td>
  <td>{_html_escape(severity)}</td>
  <td>{_html_escape(a.get("type", ""))}</td>
  <td>{_html_escape(a.get("host", ""))}</td>
  <td class="mono small">{_html_escape(str(a.get("path", ""))[:120])}</td>
  <td>{_html_escape(a.get("reason", ""))}</td>
</tr>
""")
    table = f'<table><tr><th>Time</th><th>Severity</th><th>Type</th><th>Host</th><th>Path</th><th>Reason</th></tr>{"".join(rows)}</table>'
    return _page("Egress anomalies", table)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Keep the container logs quiet; the audit log captures outbound HTTP.
        pass

    def _json(self, data: dict | list, status: int = 200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html: str, status: int = 200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path in ("/", ""):
            self._html(_index(params))
        elif parsed.path == "/flow":
            self._html(_flow_detail(params))
        elif parsed.path == "/anomalies":
            self._html(_anomalies())
        elif parsed.path == "/api/flows":
            def _int(raw: str | None, default: int) -> int:
                try:
                    return int(raw) if raw else default
                except ValueError:
                    return default

            flows, total = _load_flows(
                date=params.get("date", [None])[0],
                offset=_int(params.get("offset", ["0"])[0], 0),
                limit=_int(params.get("limit", [str(PAGE_SIZE)])[0], PAGE_SIZE),
                host=params.get("host", [None])[0],
                method=params.get("method", [None])[0],
                query=params.get("q", [None])[0],
            )
            self._json({"total": total, "flows": flows})
        elif parsed.path == "/api/anomalies":
            limit = 100
            try:
                limit = int(params.get("limit", ["100"])[0] or 100)
            except ValueError:
                pass
            self._json(_load_anomalies(limit=limit))
        elif parsed.path == "/health":
            self._json({"status": "ok"})
        else:
            self._html(_page("Not found", "<p>404</p>"), status=404)


def main() -> int:
    server = ThreadingHTTPServer(("", PORT), Handler)
    print(f"egress-log-viewer listening on :{PORT}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
