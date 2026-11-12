#!/usr/bin/env python3
"""Checks for `conversation-push.py --thread <id> --redact-message <msg id>`.

Ara redacts one of the user's own messages on their request, typically the
one in which they dictated a password she typed on their device
(shell-type.py --sensitive). The CLI posts {message_id} to the gateway's
token-gated /internal/conversations/<id>/redact; these checks cover the
argument rules and the request it makes, against a throwaway local server.

    python3 tests/test_conversation_push_redact.py
"""
import http.server
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "conversation-push.py"
THREAD = "a" * 32


def _run(args, url="http://127.0.0.1:9/internal/conversations"):
    env = {k: v for k, v in os.environ.items()
           if k not in ("RETINUE_REPLY_ATTACHMENTS_FILE",)}
    env["CONVERSATION_BACKEND_TOKEN"] = "t"
    env["CONVERSATION_BACKEND_URL"] = url
    return subprocess.run([sys.executable, str(SCRIPT), *args], env=env,
                          capture_output=True, text=True, timeout=30)


def test_argument_rules():
    for args in (["--redact-message", "3f2a9c1d0b7e"],                        # no thread
                 ["--thread", THREAD, "--redact-message", "not-an-id"],
                 ["--thread", THREAD, "--redact-message", "3f2a9c1d0b7e", "--mute"],
                 ["--thread", THREAD, "--redact-message", "3f2a9c1d0b7e", "hello"]):
        r = _run(args)
        assert r.returncode == 2, (args, r.returncode, r.stderr)
    print("ok: --redact-message needs --thread and a message id, and nothing else")


def test_posts_the_message_id():
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            seen.append((self.path, self.headers.get("X-Conversation-Backend-Token"),
                         json.loads(body)))
            out = json.dumps({"status": "redacted"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_port}/internal/conversations"
        r = _run(["--thread", THREAD, "--redact-message", "3f2a9c1d0b7e"], url)
    finally:
        srv.shutdown()
    assert r.returncode == 0, r.stderr
    assert seen == [(f"/internal/conversations/{THREAD}/redact", "t",
                     {"message_id": "3f2a9c1d0b7e"})], seen
    print("ok: it posts the message id to the thread's redact endpoint, token-gated")


def main():
    test_argument_rules()
    test_posts_the_message_id()
    print("all conversation-push --redact-message tests passed")


if __name__ == "__main__":
    main()
