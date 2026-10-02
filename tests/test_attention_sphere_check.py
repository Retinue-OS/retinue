#!/usr/bin/env python3
"""Checks for the sphere check in conversation-push.py and attention-set.py.

Both CLIs refuse a --sphere or --tag that is not a word of the deployment's
vocabulary (focus.json, read through the gateway's GET /attention/profile)
before anything is sent, and print the vocabulary so the caller can correct
itself. Words are compared as the gateway stores them ("Plazi" is plazi).
When the vocabulary cannot be read, the check steps aside rather than block.

    python3 tests/test_attention_sphere_check.py
"""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PUSH = REPO_ROOT / "scripts" / "conversation-push.py"
SET = REPO_ROOT / "scripts" / "attention-set.py"
SPHERES = ["customers", "admin", "family", "unknown", "assistance"]


class _Profile(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server's name
        body = json.dumps({"profile": {}, "focus": {"spheres": SPHERES}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _run(script, args, attention_url):
    env = dict(os.environ)
    env["CONVERSATION_BACKEND_TOKEN"] = "t"
    # Unroutable, so a call that passes the check fails on the send, loudly.
    env["CONVERSATION_BACKEND_URL"] = "http://127.0.0.1:9/internal/conversations"
    env.pop("ATTENTION_BACKEND_URL", None)
    env["ATTENTION_URL"] = attention_url
    return subprocess.run([sys.executable, str(script), *args], env=env,
                          capture_output=True, text=True, timeout=60)


def test_unknown_sphere_refused(url):
    r = _run(PUSH, ["--title", "x", "--sphere", "family", "--tag", "assistenz", "hi"], url)
    assert r.returncode == 2, (r.returncode, r.stderr)
    assert "not a sphere of this deployment: assistenz" in r.stderr, r.stderr
    assert "assistance" in r.stderr, "the refusal lists the vocabulary"
    r = _run(SET, ["thread:" + "0" * 32, "--sphere", "work"], url)
    assert r.returncode == 2 and "not a sphere of this deployment: work" in r.stderr, r.stderr
    print("ok: an unknown sphere or tag is refused with the vocabulary")


def test_known_spheres_pass(url):
    r = _run(PUSH, ["--title", "x", "--sphere", "Assistance", "--tag", "admin", "hi"], url)
    assert "not a sphere" not in r.stderr, r.stderr
    r = _run(SET, ["thread:" + "0" * 32, "--sphere", "customers"], url)
    assert "not a sphere" not in r.stderr, r.stderr
    print("ok: words of the vocabulary pass, compared as the gateway stores them")


def test_unreadable_vocabulary_steps_aside():
    r = _run(PUSH, ["--title", "x", "--sphere", "anything", "hi"], "http://127.0.0.1:9")
    assert "not a sphere" not in r.stderr, r.stderr
    print("ok: without the vocabulary the check does not block")


def main():
    server = HTTPServer(("127.0.0.1", 0), _Profile)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        test_unknown_sphere_refused(url)
        test_known_spheres_pass(url)
        test_unreadable_vocabulary_steps_aside()
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
