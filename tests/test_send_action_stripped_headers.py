#!/usr/bin/env python3
"""Checks that approving a pending send reports which provider headers were
stripped (#67 item 1).

approve_pending_send()'s return value used to be discarded at its only call
site, so the one place that could tell an operator the Zoho/Exchange
round-trip workaround (#60) actually fired never did -- a message that arrived
fine looked identical, from the log, to one where stripping silently did
nothing.

    python3 tests/test_send_action_stripped_headers.py
"""
import importlib.util
import io
import os
import sys
import tempfile
import types
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_gateway(tmp: Path):
    """Load scripts/web-gateway.py with sandboxed state, as the other
    web-gateway tests do. Also seeds the default e-mail account's required
    Config() fields -- dummy values, since approve/delete are mocked below and
    no real IMAP/SMTP connection is ever attempted."""
    os.environ["CONVERSATIONS_DIR"] = str(tmp / "convs")
    os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state.json")
    os.environ["EMAIL_USER"] = "you@example.com"
    os.environ["EMAIL_PASS"] = "x"
    os.environ["IMAP_HOST"] = "imap.example.com"
    os.environ["SMTP_HOST"] = "smtp.example.com"
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
        "web_gateway_send_action_under_test", SCRIPTS_DIR / "web-gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeConnection:
    def __init__(self, local):
        self._local = local

    def getsockname(self):
        return (self._local, 8080)


class _FakeSendHandler:
    """Just enough of the request handler for _handle_send_action: it only
    ever calls self._send_html() (on error) or self._redirect() (on
    success), so those are stand-ins rather than the real socket-backed ones.

    The addresses are real, though: approving a pending send is the user's own
    decision, so the handler runs the origin check first, and these tests drive
    the genuine one rather than stubbing it out. Default is a proxy-shaped
    peer; pass loopback for the in-container caller."""

    def __init__(self, peer="172.19.0.4", local="172.19.0.9"):
        self.html = None
        self.redirected_to = None
        self.client_address = (peer, 51234)
        self.connection = _FakeConnection(local)

    def _send_html(self, status, body):
        self.html = (status, body)

    def _redirect(self, location):
        self.redirected_to = location


def _bind_real_gate(wg):
    """Run the handler's own origin check against the fake, unbound — the
    point is to exercise the gate, not to stub it away."""
    _FakeSendHandler._request_from_edge = wg.Handler._request_from_edge


def test_in_container_caller_cannot_approve(wg):
    """An agent that could approve its own pending send would defeat `verify`
    entirely: queue, then approve. The origin check covers this handler too."""
    fake = _FakeSendHandler(peer="127.0.0.1", local="127.0.0.1")
    buf = io.StringIO()
    with patch.object(wg.ec, "approve_pending_send") as approve:
        with redirect_stdout(buf):
            wg.Handler._handle_send_action(fake, "default", "42", "approve")
    approve.assert_not_called()
    assert fake.redirected_to is None
    assert fake.html and fake.html[0] == 403, fake.html
    assert "REFUSED" in buf.getvalue(), buf.getvalue()


def test_approve_logs_stripped_headers(wg):
    fake = _FakeSendHandler()
    result = {"approved": "42", "sent": True, "to": ["a@example.com"],
              "subject": "hi", "saved_to_sent": True,
              "stripped_headers": ["X-ZohoMail-Sender"]}
    buf = io.StringIO()
    with patch.object(wg.ec, "approve_pending_send", return_value=result):
        with redirect_stdout(buf):
            wg.Handler._handle_send_action(fake, "default", "42", "approve")
    assert fake.redirected_to == "/sends/next", fake.redirected_to
    logged = buf.getvalue()
    assert "42" in logged and "X-ZohoMail-Sender" in logged, logged


def test_approve_silent_when_nothing_stripped(wg):
    fake = _FakeSendHandler()
    result = {"approved": "43", "sent": True, "to": ["a@example.com"],
              "subject": "hi", "saved_to_sent": True, "stripped_headers": []}
    buf = io.StringIO()
    with patch.object(wg.ec, "approve_pending_send", return_value=result):
        with redirect_stdout(buf):
            wg.Handler._handle_send_action(fake, "default", "43", "approve")
    assert fake.redirected_to == "/sends/next", fake.redirected_to
    assert buf.getvalue() == "", buf.getvalue()


def test_reject_path_unaffected(wg):
    """delete_pending_draft's path carries no stripped_headers report at all --
    this fix only touches the "approve" branch."""
    fake = _FakeSendHandler()
    with patch.object(wg.ec, "delete_pending_draft", return_value=None) as mock_delete:
        wg.Handler._handle_send_action(fake, "default", "44", "reject")
    mock_delete.assert_called_once()
    assert fake.redirected_to == "/sends/next", fake.redirected_to


def main():
    with tempfile.TemporaryDirectory() as td:
        wg = _load_gateway(Path(td))
        _bind_real_gate(wg)
        test_in_container_caller_cannot_approve(wg)
        test_approve_logs_stripped_headers(wg)
        test_approve_silent_when_nothing_stripped(wg)
        test_reject_path_unaffected(wg)
    print("all send-action stripped-header tests passed")


if __name__ == "__main__":
    main()
