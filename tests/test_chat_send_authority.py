#!/usr/bin/env python3
"""Checks that no message reaches the wire without the user releasing it.

A message once went out under a `verify` policy that the user never pressed
send on. The guarantee is now structural and lives at the messenger gateways:
**no caller-supplied field skips an account's send policy.** `author == "user"`
used to be an unconditional bypass, on the reasoning that only the dashboard
sets it — but it is a JSON field any caller can write, and it describes who
composed a message rather than who authorised it. Under `verify` every send is
now queued, the dashboard's own press included; the press satisfies the policy
by releasing the queued send through the gateway's approve endpoint in the same
request, so it feels immediate without anything going around the mechanism.
That combination is covered in tests/test_signal_send_policy.py and, end to
end, in tests/test_chat_api.py.

What is covered here is the second, lesser layer: POST /chats/<id>/send and the
/sends approve action refuse a request that did not arrive through the reverse
proxy, decided on the TCP peer address and failing closed on anything it cannot
classify. This was once meant to be the guarantee and is not — the endpoint's
old justification ("it sits behind the edge auth") was simply false, since that
auth is a forward-auth Traefik consults and an in-container caller is never
asked for it. It is kept as defence in depth: it closes the obvious path and
makes an attempt visible in the log.

Neither layer is a boundary against a determined agent: the agents share a
container with the web-gateway and hold the gateways' tokens, so an agent can
make the approve call itself. That is a deliberate simulation of the button
press, not an accident, and no arrangement inside one container prevents it.
These tests pin behaviour, not a security guarantee.

    python3 tests/test_chat_send_authority.py
"""
import importlib.util
import ipaddress
import json
import os
import sys
import tempfile
import threading
import types
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

CHAT = "signal:+41794456312"
GW_SEEN: list = []


class _MockGateway(BaseHTTPRequestHandler):
    """Just enough channel gateway: an inbox-mode identity and a /send that
    records what the web-gateway asked it to do."""

    def log_message(self, fmt, *args):
        pass

    def _json(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/health"):
            self._json(200, {"status": "ok", "mode": "inbox",
                             "account": "+41791112233", "configured": True})
            return
        self._json(404, {"error": "nope"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        GW_SEEN.append(payload)
        self._json(200, {"status": "sent", "message_id": "1", "ts": 1756000000.0})


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _load_gateway(tmp: Path, gw_port: int):
    os.environ["SIGNAL_GATEWAY_BASE_URL"] = f"http://127.0.0.1:{gw_port}"
    os.environ["SIGNAL_GATEWAY_TOKEN"] = "gw-secret"
    for var in ("WHATSAPP_GATEWAY_BASE_URL", "TELEGRAM_GATEWAY_BASE_URL",
                "MESSENGER_GATEWAYS", "RETINUE_CONVERSATION_MODELS",
                "RETINUE_LITELLM_URL", "ANTHROPIC_BASE_URL",
                "ANTHROPIC_CUSTOM_HEADERS", "EDGE_PROXY_PEERS"):
        os.environ.pop(var, None)
    os.environ["QLEVER_LIFE_URL"] = "http://127.0.0.1:1"  # unused by these paths
    os.environ["CHAT_STATE_DIR"] = str(tmp / "chat-state")
    os.environ["CHAT_LIST_CACHE_SECONDS"] = "0"
    os.environ["CONVERSATIONS_DIR"] = str(tmp / "convs")
    os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state.json")
    os.environ["PUSH_DIR"] = str(tmp / "push")
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
        "web_gateway_authority_under_test", SCRIPTS_DIR / "web-gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _http(base, method, path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def _quote(chat_id):
    import urllib.parse
    return urllib.parse.quote(chat_id, safe="")


def test_peer_classification(wg):
    """The discriminator itself, over every shape a peer address takes."""
    assert wg._EDGE_PEER_NETS == [], "harness expects the default rule"
    edge = wg._classify_request_origin("172.19.0.4", "172.19.0.9")
    assert edge == (True, ""), edge

    # An in-container caller reaches us on loopback…
    ok, why = wg._classify_request_origin("127.0.0.1", "127.0.0.1")
    assert ok is False and "loopback" in why, why
    ok, why = wg._classify_request_origin("::1", "::1")
    assert ok is False and "loopback" in why, why
    # …including through a dual-stack listener, where a v4 client shows up
    # v6-mapped and reports itself as neither loopback nor private unmapped.
    ok, why = wg._classify_request_origin("::ffff:127.0.0.1", "::ffff:127.0.0.1")
    assert ok is False and "loopback" in why, why

    # …or by dialling this container's own address, which is not loopback but
    # is the very address this socket is bound to.
    ok, why = wg._classify_request_origin("172.19.0.9", "172.19.0.9")
    assert ok is False and "own address" in why, why

    # Fail closed on anything unclassifiable — "cannot tell" is not "allowed".
    for bad in (None, "", "not-an-address", "localhost"):
        ok, why = wg._classify_request_origin(bad, "172.19.0.9")
        assert ok is False and "unclassifiable" in why, (bad, why)
    # A missing local address must not turn a real peer into a refusal.
    assert wg._classify_request_origin("172.19.0.4", None) == (True, "")
    print("PASS test_peer_classification")


def test_configured_peers_are_the_whole_rule(wg):
    """EDGE_PROXY_PEERS pins the proxy explicitly; nothing else is accepted."""
    nets = wg._parse_edge_peers("10.0.0.0/8, 172.19.0.4 , , bogus")
    assert nets == [ipaddress.ip_network("10.0.0.0/8"),
                    ipaddress.ip_network("172.19.0.4/32")], nets
    saved = wg._EDGE_PEER_NETS
    try:
        wg._EDGE_PEER_NETS = nets
        assert wg._classify_request_origin("10.1.2.3", "172.19.0.9")[0] is True
        ok, why = wg._classify_request_origin("172.19.0.5", "172.19.0.9")
        assert ok is False and "EDGE_PROXY_PEERS" in why, why
        # An allowlisted loopback is a deployment's explicit choice (host
        # networking), so it is honoured — and only then.
        wg._EDGE_PEER_NETS = wg._parse_edge_peers("127.0.0.1")
        assert wg._classify_request_origin("127.0.0.1", "127.0.0.1")[0] is True
    finally:
        wg._EDGE_PEER_NETS = saved
    print("PASS test_configured_peers_are_the_whole_rule")


def test_send_from_inside_the_container_is_refused(base, wg):
    """The incident, end to end: an in-container caller asking for a send."""
    GW_SEEN.clear()
    status, body = _http(base, "POST", f"/chats/{_quote(CHAT)}/send",
                         {"text": "geht raus"})
    assert status == 403, (status, body)
    answer = json.loads(body)
    assert "reverse proxy" in answer["error"]
    assert "loopback" in answer["detail"], answer
    # Naming the legitimate route matters: this is read by an agent.
    assert "chat-draft.py" in answer["remedy"]
    assert GW_SEEN == [], "the gateway was asked to send anyway"
    print("PASS test_send_from_inside_the_container_is_refused")


def test_approving_a_pending_send_is_refused(base, wg):
    """Otherwise an agent queues its own send and approves it."""
    status, body = _http(base, "POST", "/sends/signal-gateway/deadbeef/approve")
    assert status == 403, (status, body)
    assert "user&#x27;s own decision" in body or "user's own decision" in body
    status, body = _http(base, "POST", "/sends/signal-gateway/deadbeef/reject")
    assert status == 403, (status, body)
    print("PASS test_approving_a_pending_send_is_refused")


def test_dashboard_send_still_works(base, wg):
    """The press that arrives through the proxy sends.

    Driven by allowlisting this harness's own peer, which is the same code
    path a deployment uses to pin its proxy. The mock account is `allow`, so
    the send completes on the first hop; the queue-and-approve path a stricter
    account takes is covered in tests/test_chat_api.py."""
    GW_SEEN.clear()
    saved = wg._EDGE_PEER_NETS
    try:
        wg._EDGE_PEER_NETS = wg._parse_edge_peers("127.0.0.1")
        status, body = _http(base, "POST", f"/chats/{_quote(CHAT)}/send",
                             {"text": "geht raus"})
        assert status == 200, (status, body)
        assert json.loads(body)["text"] == "geht raus"
    finally:
        wg._EDGE_PEER_NETS = saved
    assert len(GW_SEEN) == 1, GW_SEEN
    sent = GW_SEEN[0]
    # `author` is what it always meant — who composed the words — and the body
    # carries nothing that asks the gateway to skip its policy.
    assert sent["author"] == "user", sent
    assert "edge_verified" not in sent and "user_approved" not in sent, sent
    print("PASS test_dashboard_send_still_works")


def main():
    gw = _serve(_MockGateway)
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp), gw.server_address[1])
        wg.push_notify.enabled = lambda: False
        test_peer_classification(wg)
        test_configured_peers_are_the_whole_rule(wg)

        server = ThreadingHTTPServer(("127.0.0.1", 0), wg.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            test_send_from_inside_the_container_is_refused(base, wg)
            test_approving_a_pending_send_is_refused(base, wg)
            test_dashboard_send_still_works(base, wg)
        finally:
            server.shutdown()
    gw.shutdown()
    print("all chat-send-authority tests passed")


if __name__ == "__main__":
    main()
