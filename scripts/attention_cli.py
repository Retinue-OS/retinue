"""The gateway call the attention CLIs share (attention-week.py,
attention-spheres.py): JSON in, JSON out, and a failure ends the script with
the gateway's own reason.

Configuration (environment): ATTENTION_URL, else the web-gateway on
localhost:WEB_GATEWAY_PORT (8080).
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("ATTENTION_URL", f"http://localhost:{os.environ.get('WEB_GATEWAY_PORT', '8080')}").rstrip("/")
TIMEOUT = 30


def call(prog: str, method: str, path: str, body: dict | None = None) -> dict:
    """``method`` ``path`` on the gateway; exits with ``prog: <reason>`` on
    an error answer or when the gateway does not answer."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            answer = json.loads(exc.read().decode("utf-8"))
        except ValueError:
            answer = None
        reason = (answer.get("error") if isinstance(answer, dict) else None) or exc.reason
        sys.exit(f"{prog}: {reason}")
    except urllib.error.URLError as exc:
        sys.exit(f"{prog}: the gateway at {BASE} did not answer ({exc.reason})")
