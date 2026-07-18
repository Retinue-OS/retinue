#!/usr/bin/env python3
"""Anomaly detection agent for egress-audit logs.

Tails the daily NDJSON flow log produced by the egress-audit MITM proxy,
flags suspicious patterns, and writes anomalies to a separate NDJSON file.
High-severity anomalies can be pushed as alerts via a configured command
(typically signal-push.py or conversation-push.py).

Configuration (environment):
    EGRESS_AUDIT_LOG_DIR         directory containing daily flow logs
    EGRESS_AUDIT_ANOMALY_LOG     path to write anomalies NDJSON log
    EGRESS_ANOMALY_STATE         path to persisted state (seen hosts, alerted)
    EGRESS_ANOMALY_ALERT_COMMAND command to run with the alert message as arg
    EGRESS_ANOMALY_ALERT_SEVERITY comma-separated severities that trigger alerts
    EGRESS_ANOMALY_KNOWN_HOSTS   comma-separated hosts to treat as normal
    EGRESS_ANOMALY_LARGE_BODY_BYTES threshold for "large body" anomalies
    EGRESS_ANOMALY_INTERVAL      seconds between tail checks (default 5)
"""
import ipaddress
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

LOG_DIR = Path(os.environ.get("EGRESS_AUDIT_LOG_DIR", "/var/log/retinue/egress"))
ANOMALY_LOG = Path(os.environ.get("EGRESS_AUDIT_ANOMALY_LOG", "/var/log/retinue/egress/anomalies.ndjson"))
STATE_FILE = Path(os.environ.get("EGRESS_ANOMALY_STATE", "/tmp/egress-anomaly-state.json"))
ALERT_COMMAND = os.environ.get("EGRESS_ANOMALY_ALERT_COMMAND", "")
ALERT_SEVERITY = set(os.environ.get("EGRESS_ANOMALY_ALERT_SEVERITY", "high").split(","))
KNOWN_HOSTS = set(h.strip() for h in os.environ.get("EGRESS_ANOMALY_KNOWN_HOSTS", "").split(",") if h.strip())
LARGE_BODY_BYTES = int(os.environ.get("EGRESS_ANOMALY_LARGE_BODY_BYTES", str(1024 * 1024)))
TAIL_INTERVAL = float(os.environ.get("EGRESS_ANOMALY_INTERVAL", "5"))

SUSPICIOUS_PATH_RE = re.compile(r"/(internal|admin|debug|env|config|secrets|\.env|\.git|apikeys?|tokens?)", re.I)
SUSPICIOUS_BODY_RE = re.compile(r"(password|private[_-]?key|secret|token|api[_-]?key|authorization)", re.I)


class State:
    def __init__(self, path: Path):
        self.path = path
        self.seen_hosts: set[str] = set()
        self.alerted: set[str] = set()
        self.load()

    def load(self):
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text())
                self.seen_hosts = set(data.get("seen_hosts", []))
                self.alerted = set(data.get("alerted", []))
            except Exception:
                pass

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "seen_hosts": sorted(self.seen_hosts),
            "alerted": sorted(self.alerted),
        }, indent=2))


class LogTail:
    def __init__(self, path: Path):
        self.path = path
        self.fh = None
        self.inode = None
        self.offset = 0

    def _open(self):
        if self.fh:
            self.fh.close()
        self.fh = open(self.path, "r", encoding="utf-8", errors="replace")
        self.offset = 0
        self.inode = self.path.stat().st_ino

    def lines(self):
        while True:
            if not self.path.exists():
                time.sleep(TAIL_INTERVAL)
                continue
            stat = self.path.stat()
            if self.inode != stat.st_ino or self.fh is None:
                self._open()
            if stat.st_size < self.offset:
                # file truncated or rotated
                self._open()
            self.fh.seek(self.offset)
            for line in self.fh:
                yield line
            self.offset = self.fh.tell()
            time.sleep(TAIL_INTERVAL)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _is_private(host: str) -> bool:
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_private or addr.is_loopback or addr.is_reserved
    except ValueError:
        return False


def _reason(flow: dict, severity: str, anomaly_type: str, reason: str) -> dict:
    return {
        "ts": flow.get("ts"),
        "severity": severity,
        "type": anomaly_type,
        "host": flow.get("host"),
        "port": flow.get("port"),
        "path": flow.get("path"),
        "method": flow.get("method"),
        "response_status": flow.get("response_status"),
        "reason": reason,
        "client_ip": flow.get("client_ip"),
    }


def _scan(flow: dict, state: State) -> list[dict]:
    anomalies: list[dict] = []
    host = flow.get("host", "")
    method = flow.get("method", "")
    path = flow.get("path", "")
    status = flow.get("response_status", 0)
    req_size = flow.get("request_body", {}).get("size", 0)
    resp_size = flow.get("response_body", {}).get("size", 0)

    # New host never seen before (and not in the known-hosts allowlist).
    if host and host not in state.seen_hosts and host not in KNOWN_HOSTS:
        anomalies.append(_reason(flow, "warning", "new_host", f"First outbound request to {host}"))

    # Raw IP address instead of a hostname.
    if _is_ip(host):
        anomalies.append(_reason(flow, "warning", "ip_host", f"Request to raw IP address {host}"))

    # Private/internal address being proxied (NO_PROXY misconfiguration).
    if _is_private(host):
        anomalies.append(_reason(flow, "high", "private_host", f"Proxying request to private address {host}"))

    # Non-standard port.
    if flow.get("port") not in (80, 443):
        anomalies.append(_reason(flow, "info", "nonstandard_port", f"Port {flow.get('port')}"))

    # Unusually large bodies.
    if req_size > LARGE_BODY_BYTES:
        anomalies.append(_reason(flow, "warning", "large_request", f"Request body {req_size} bytes"))
    if resp_size > LARGE_BODY_BYTES:
        anomalies.append(_reason(flow, "warning", "large_response", f"Response body {resp_size} bytes"))

    # Suspicious path patterns.
    if SUSPICIOUS_PATH_RE.search(path):
        anomalies.append(_reason(flow, "warning", "suspicious_path", f"Path matched sensitive pattern: {path}"))

    # Suspicious body text (credentials, keys, tokens).
    body_text = " ".join([
        flow.get("request_body", {}).get("text", ""),
        flow.get("response_body", {}).get("text", ""),
    ])
    matches = SUSPICIOUS_BODY_RE.findall(body_text)
    if matches:
        anomalies.append(_reason(flow, "warning", "suspicious_body", f"Body contains: {sorted(set(matches))}"))

    # HTTP errors.
    if status >= 500:
        anomalies.append(_reason(flow, "info", "server_error", f"HTTP {status}"))
    elif status >= 400:
        anomalies.append(_reason(flow, "info", "client_error", f"HTTP {status}"))

    state.seen_hosts.add(host)
    return anomalies


def _append_anomaly(anomaly: dict):
    ANOMALY_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(ANOMALY_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(anomaly, ensure_ascii=False, default=str) + "\n")


def _send_alert(state: State, anomaly: dict):
    if anomaly["severity"] not in ALERT_SEVERITY:
        return
    key = f"{anomaly['ts']}:{anomaly['host']}:{anomaly['type']}:{anomaly.get('path', '')}"
    if key in state.alerted:
        return
    if not ALERT_COMMAND:
        return
    message = (
        f"Egress anomaly: {anomaly['severity']} {anomaly['type']} "
        f"to {anomaly['host']}{anomaly.get('path', '')} — {anomaly['reason']}"
    )
    try:
        subprocess.run([*shlex.split(ALERT_COMMAND), message], check=True, timeout=30)
        state.alerted.add(key)
    except Exception as exc:
        print(f"Failed to send alert: {exc}", file=sys.stderr)


def _today_log() -> Path:
    return LOG_DIR / f"{time.strftime('%Y-%m-%d')}.ndjson"


def main() -> int:
    state = State(STATE_FILE)
    log_file = _today_log()

    print(f"egress-anomaly-agent watching {log_file}", file=sys.stderr)

    tail = LogTail(log_file)
    for line in tail.lines():
        line = line.strip()
        if not line:
            continue
        try:
            flow = json.loads(line)
        except json.JSONDecodeError:
            continue

        # Handle midnight rotation transparently.
        today = _today_log()
        if today != log_file:
            log_file = today
            tail = LogTail(log_file)

        for anomaly in _scan(flow, state):
            _append_anomaly(anomaly)
            _send_alert(state, anomaly)

        state.save()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
