#!/usr/bin/env python3
"""Focused checks for the dashboard's Web Push fan-out (scripts/push_notify.py).

Covers the parts that are easy to get silently wrong: VAPID key persistence
(regenerating the key would invalidate every existing subscription), the
subscription store's validation and round-trip, the real encrypted send against
a local HTTP sink, and the expiry rule — a push service answering 404/410 has
permanently dropped the subscription and it must be pruned, while any other
error must leave it in place.

Also checks that the module degrades to disabled (rather than raising) when
pywebpush is absent, since that is what keeps ad-hoc runs of web-gateway.py
working without the dependency.

    python3 tests/test_push_notify.py
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import push_notify  # noqa: E402

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _browser_subscription(endpoint: str) -> dict:
    """A subscription shaped like a real browser's PushSubscription."""
    priv = ec.generate_private_key(ec.SECP256R1())
    point = priv.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return {"endpoint": endpoint, "keys": {"p256dh": _b64(point), "auth": _b64(os.urandom(16))}}


class _Sink:
    """A stand-in push service: records one request, answers with `status`."""

    def __init__(self, status: int = 201):
        self.status = status
        self.requests: list[dict] = []
        sink = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                sink.requests.append({
                    "path": self.path,
                    "ttl": self.headers.get("TTL"),
                    "encoding": self.headers.get("Content-Encoding"),
                    "authorization": self.headers.get("Authorization") or "",
                    "body_len": len(body),
                })
                self.send_response(sink.status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):  # keep test output clean
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/push/endpoint"


def test_key_persistence(tmp: Path):
    push_notify.init(tmp / "a")
    assert push_notify.enabled(), "push should be enabled with pywebpush installed"
    key = push_notify.public_key()
    # The application server key is a raw uncompressed P-256 point: 65 bytes.
    assert len(base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))) == 65, key
    assert (tmp / "a" / "vapid_private.pem").exists()
    assert oct((tmp / "a" / "vapid_private.pem").stat().st_mode & 0o777) == "0o600"

    # Re-initialising must reuse the stored key; a fresh one would silently
    # invalidate every subscription the browsers already hold.
    push_notify.init(tmp / "a")
    assert push_notify.public_key() == key, "VAPID key must persist across restarts"

    # A different state dir is a different server, so it gets its own key.
    push_notify.init(tmp / "b")
    assert push_notify.public_key() != key
    print("ok: VAPID key generation, permissions and persistence")


def test_subscription_store(tmp: Path):
    push_notify.init(tmp / "subs")
    assert push_notify.subscription_count() == 0
    assert push_notify.subscribe({"endpoint": "https://x/1"}) is False, "keys are required"
    assert push_notify.subscribe({"endpoint": "", "keys": {"p256dh": "a", "auth": "b"}}) is False
    assert push_notify.subscribe({"keys": {"p256dh": "a", "auth": "b"}}) is False
    assert push_notify.subscription_count() == 0

    sub = _browser_subscription("https://push.example/1")
    assert push_notify.subscribe(sub) is True
    assert push_notify.subscription_count() == 1
    # Re-subscribing the same endpoint updates in place rather than duplicating.
    assert push_notify.subscribe(sub) is True
    assert push_notify.subscription_count() == 1

    assert push_notify.subscribe(_browser_subscription("https://push.example/2")) is True
    assert push_notify.subscription_count() == 2

    assert push_notify.unsubscribe("https://push.example/1") is True
    assert push_notify.subscription_count() == 1
    assert push_notify.unsubscribe("https://push.example/nope") is False
    print("ok: subscription validation, dedupe and removal")


def test_encrypted_send(tmp: Path):
    push_notify.init(tmp / "send")
    with _Sink(201) as sink:
        push_notify.subscribe(_browser_subscription(sink.url))
        sent = push_notify.notify("Party RSVP", "Confirm or decline?",
                                  url="/#conversation-abc", tag="abc")
        assert sent == 1, sent
        assert len(sink.requests) == 1
        req = sink.requests[0]
        # Web Push encryption (RFC 8291) and VAPID auth (RFC 8292).
        assert req["encoding"] == "aes128gcm", req
        assert req["authorization"].startswith("vapid t="), req
        assert req["ttl"] == str(push_notify.PUSH_TTL), req
        assert req["body_len"] > 0, "payload must be encrypted, not empty"
    print("ok: encrypted VAPID send reaches the push service")


def test_notify_async_is_best_effort(tmp: Path):
    push_notify.init(tmp / "async")
    with _Sink(201) as sink:
        push_notify.subscribe(_browser_subscription(sink.url))
        push_notify.notify_async("t", "b", url="/", tag="x")
        for _ in range(50):  # the send happens on a background thread
            if sink.requests:
                break
            time.sleep(0.1)
        assert len(sink.requests) == 1, "notify_async should deliver"
    print("ok: notify_async delivers off the request thread")


def test_expired_subscriptions_are_pruned(tmp: Path):
    for status, expected in ((404, 0), (410, 0), (500, 1), (429, 1)):
        push_notify.init(tmp / f"prune{status}")
        with _Sink(status) as sink:
            push_notify.subscribe(_browser_subscription(sink.url))
            sent = push_notify.notify("t", "b")
            assert sent == 0, f"{status} is not a delivery"
            assert push_notify.subscription_count() == expected, (
                f"HTTP {status}: expected {expected} surviving subscription(s), "
                f"got {push_notify.subscription_count()}"
            )
    print("ok: 404/410 prune the subscription, other errors keep it")


def test_disabled_without_pywebpush():
    """Without pywebpush the module must go quiet, not explode."""
    with tempfile.TemporaryDirectory() as td:
        stub = Path(td) / "stub"
        stub.mkdir()
        (stub / "pywebpush.py").write_text('raise ImportError("simulated: not installed")\n')
        env = dict(os.environ, PYTHONPATH=f"{stub}:{SCRIPTS_DIR}")
        state = Path(td) / "state"
        code = (
            "from pathlib import Path\n"
            "import push_notify as p\n"
            f"p.init(Path({str(state)!r}))\n"
            "assert p.enabled() is False\n"
            "assert p.public_key() is None\n"
            "assert p.subscribe({'endpoint': 'e', 'keys': {'p256dh': 'a', 'auth': 'b'}}) is False\n"
            "assert p.unsubscribe('e') is False\n"
            "assert p.notify('t', 'b') == 0\n"
            "p.notify_async('t', 'b')\n"
            f"assert not Path({str(state)!r}).exists(), 'no state dir when disabled'\n"
            "print('DEGRADED-OK')\n"
        )
        out = subprocess.run([sys.executable, "-c", code], env=env,
                             capture_output=True, text=True)
        assert "DEGRADED-OK" in out.stdout, out.stderr
    print("ok: degrades to disabled when pywebpush is missing")


def main():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_key_persistence(tmp)
        test_subscription_store(tmp)
        test_encrypted_send(tmp)
        test_notify_async_is_best_effort(tmp)
        test_expired_subscriptions_are_pruned(tmp)
    test_disabled_without_pywebpush()
    print("all push notification tests passed")


if __name__ == "__main__":
    main()
