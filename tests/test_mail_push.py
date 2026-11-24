#!/usr/bin/env python3
"""scripts/mail-push.py — the agents' way to send a mail (docs/mail-chats.md,
"Writing"): its arguments, what it posts to the mail gateway's /send, and
what it says of the answer.

Against a stub gateway that records each request and answers as scripted,
and once against the real gateway's handler, mirror and a scripted SMTP
server, so the flags the companion's note names are known to do what it says:

  * a reply names its thread — a chat id, a bare chat key, a reply token —
    and nothing else; a new mail names recipients and a subject; mixing the
    two is refused before anything is sent;
  * the text comes from the argument, a file or standard input;
  * --attach, --user-approved, --thread (and its environment default), --url;
  * a queued send prints its id, how to take it back, and the approval URL
    made absolute; --retract takes it back;
  * a refusal is the gateway's own sentence, and a failure exit;
  * one run is one mail: every request carries a key, a run that got no
    answer asks again under it, and one that still does not know says so —
    also when the gateway took the request and was gone by the second ask;
  * a repeat is said to be one; a send that failed for certain is said to
    have sent nothing, and the same request again is a new attempt.

    python3 tests/test_mail_push.py
"""
import base64
import contextlib
import importlib.util
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

RID = "0123456789abcdef0123456789abcdef"
THREAD = "ab" * 16
_ENV = ("MAIL_GATEWAY_SEND_URL", "MAIL_GATEWAY_TOKEN", "MAIL_GATEWAY_TIMEOUT",
        "SEND_APPROVAL_BASE_URL", "CONVERSATION_BASE_URL", "RETINUE_THREAD_ID")


class Stub:
    """A gateway that records what it is sent and answers what it is told to."""

    def __init__(self):
        self.requests = []      # (path, body | None, headers)
        self.answer = (200, {"status": "sent", "message_id": "m1@example.org",
                             "chat": "a@x.org", "ts": 1.0, "attachments": []})
        self.silent = 0         # leave this many requests unanswered (the caller times out)
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length) if length else b""
                stub.requests.append((self.path, json.loads(raw) if raw else None,
                                      dict(self.headers)))
                if stub.silent:
                    stub.silent -= 1
                    time.sleep(1.5)
                    return
                status, body = stub.answer
                if self.path.endswith("/retract"):
                    status, body = 200, {"id": RID, "status": "retracted"}
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/send"

    def body(self, index=-1) -> dict:
        """What a request asked to be sent — without the key every run adds
        (see :meth:`key`)."""
        return {name: value for name, value in self.requests[index][1].items()
                if name != "request_key"}

    def key(self, index=-1) -> str:
        return self.requests[index][1]["request_key"]


def push(*argv, env=None, stdin=None):
    """Run mail-push.py's main(); ``(exit code, stdout, stderr)``."""
    saved = {name: os.environ.pop(name, None) for name in _ENV}
    os.environ.update(env or {})
    spec = importlib.util.spec_from_file_location("mail_push_under_test",
                                                  SCRIPTS_DIR / "mail-push.py")
    module = importlib.util.module_from_spec(spec)
    out, err = io.StringIO(), io.StringIO()
    real_argv, real_stdin = sys.argv, sys.stdin
    sys.argv = ["mail-push.py", *argv]
    if stdin is not None:
        sys.stdin = io.StringIO(stdin)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            spec.loader.exec_module(module)     # reads its environment as it loads
            try:
                code = module.main()
            except SystemExit as exc:           # argparse, and a missing attachment
                code = exc.code if isinstance(exc.code, int) else 1
                if not isinstance(exc.code, int) and exc.code:
                    err.write(str(exc.code))
    finally:
        sys.argv, sys.stdin = real_argv, real_stdin
        for name in _ENV:
            os.environ.pop(name, None)
            if saved[name] is not None:
                os.environ[name] = saved[name]
    return code, out.getvalue(), err.getvalue()


def test_a_reply_names_its_thread_and_nothing_else():
    stub = Stub()
    env = {"MAIL_GATEWAY_SEND_URL": stub.url, "MAIL_GATEWAY_TOKEN": "mail-secret"}
    try:
        # A chat id, as the companion's note gives it: the key, and the
        # mailbox it names, for the gateway to check against its own.
        code, out, err = push("--chat", "email:~me@example.org:root:1@x.org",
                              "Thanks, got it.", env=env)
        assert code == 0 and err == "", (code, err)
        assert stub.body() == {"message": "Thanks, got it.", "recipient": "root:1@x.org",
                               "account": "me@example.org"}
        assert stub.requests[-1][0] == "/send"
        assert stub.requests[-1][2]["Authorization"] == "Bearer mail-secret"
        assert "sent" in out and "m1@example.org" in out and "a@x.org" in out
        # An account with characters the id escapes comes back unescaped.
        push("--chat", "email:~odd%3Aname@example.org:k@x.org", "x", env=env)
        assert stub.body()["account"] == "odd:name@example.org"
        # A bare chat key — whatever it looks like — is the key.
        for key in ("root@x.org", "signal:+41790000000", "weird:~key@x.org"):
            push("--chat", key, "--reply", "sender", "x", env=env)
            assert stub.body() == {"message": "x", "recipient": key, "reply": "sender"}, key
        push("--chat", "email:plain@x.org", "--reply", "all", "x", env=env)
        assert stub.body() == {"message": "x", "recipient": "plain@x.org", "reply": "all"}
        # A reply token names the thread; a --chat beside it is not sent.
        push("--reply-to", "v1.abc.def", "--chat", "other@x.org", "x", env=env)
        assert stub.body() == {"message": "x", "reply_to": "v1.abc.def"}
        # Nothing of a new mail may ride on a reply, and the other way round.
        sent = len(stub.requests)
        for argv in (["--chat", "a@x.org", "--to", "ann@x.org", "x"],
                     ["--chat", "a@x.org", "--cc", "ann@x.org", "x"],
                     ["--reply-to", "v1.a.b", "--subject", "Other", "x"],
                     ["--to", "ann@x.org", "--reply", "all", "x"],
                     ["--reply", "sender", "x"], ["x"], ["--subject", "S", "x"],
                     ["--cc", "ann@x.org", "x"],
                     ["--chat", "a@x.org", "--reply", "everyone", "x"],
                     ["--chat", "a@x.org"], ["--chat", "a@x.org", "   "],
                     ["--to", "ann@x.org"]):
            code, _out, err = push(*argv, env=env)
            assert code == 2 and "mail-push" in err, (argv, code, err)
        assert len(stub.requests) == sent, "refused before anything was sent"
    finally:
        stub.server.shutdown()
    print("ok: a reply names its thread by chat id, key or token, and carries nothing else")


def test_a_new_mail_its_text_and_its_attachments():
    stub = Stub()
    env = {"MAIL_GATEWAY_SEND_URL": stub.url}
    with tempfile.TemporaryDirectory() as tmp:
        try:
            text = "Dear Ann,\n\nthe minutes.\n$HOME `id` 'quoted' \"too\"\n"
            body = Path(tmp) / "mail.txt"
            body.write_text(text, encoding="utf-8")
            pdf = Path(tmp) / "minutes.pdf"
            pdf.write_bytes(b"%PDF-1.4")
            blob = Path(tmp) / "data.zzzz"
            blob.write_bytes(b"\x00\x01")
            code, out, err = push("--to", "ann@x.org", "--to", "Bob <bob@y.org>",
                                  "--cc", "cat@z.org", "--subject", "Minutes",
                                  "--file", str(body), "--attach", str(pdf),
                                  "--attach", str(blob), "--user-approved",
                                  "--thread", THREAD, env=env)
            assert code == 0 and err == "", err
            assert stub.body() == {
                "message": text, "to": ["ann@x.org", "Bob <bob@y.org>"], "cc": ["cat@z.org"],
                "subject": "Minutes", "user_approved": True, "thread": THREAD,
                "images": [
                    {"filename": "minutes.pdf", "content_type": "application/pdf",
                     "data": base64.b64encode(b"%PDF-1.4").decode()},
                    {"filename": "data.zzzz", "data": base64.b64encode(b"\x00\x01").decode()}]}
            assert "Authorization" not in stub.requests[-1][2], "no token configured, none sent"
            # Without the flags: no approval is claimed, no thread, no subject.
            push("--to", "ann@x.org", "Short.", env=env)
            assert stub.body() == {"message": "Short.", "to": ["ann@x.org"]}
            # The thread defaults to the turn's own; one that is no id is dropped.
            push("--to", "ann@x.org", "x", env={**env, "RETINUE_THREAD_ID": THREAD})
            assert stub.body()["thread"] == THREAD
            push("--to", "ann@x.org", "x", "--thread", "not-an-id", env=env)
            assert "thread" not in stub.body()
            # Standard input; an attachment alone; the text given twice.
            push("--to", "ann@x.org", "--file", "-", env=env, stdin="From a pipe.\n")
            assert stub.body()["message"] == "From a pipe.\n"
            push("--to", "ann@x.org", "--attach", str(pdf), env=env)
            assert stub.body()["message"] == "" and len(stub.body()["images"]) == 1
            sent = len(stub.requests)
            code, _out, err = push("--to", "ann@x.org", "--file", str(body), "Also this.", env=env)
            assert code == 2 and "not both" in err
            code, _out, err = push("--to", "ann@x.org", "--file", str(Path(tmp) / "nope"), env=env)
            assert code == 1 and "cannot read" in err
            code, _out, err = push("--to", "ann@x.org", "x", "--attach",
                                   str(Path(tmp) / "nope.pdf"), env=env)
            assert code == 1 and "attachment not found" in err
            assert len(stub.requests) == sent
            # --url is another mailbox's gateway.
            other = Stub()
            try:
                push("--to", "ann@x.org", "x", "--url", other.url, env=env)
                assert len(other.requests) == 1 and len(stub.requests) == sent
            finally:
                other.server.shutdown()
        finally:
            stub.server.shutdown()
    print("ok: a new mail carries its recipients, subject, text from a file, and attachments")


def test_what_it_says_of_the_answer():
    stub = Stub()
    env = {"MAIL_GATEWAY_SEND_URL": stub.url, "MAIL_GATEWAY_TOKEN": "mail-secret"}
    try:
        # Queued: the id, how to take it back, and the approval link — made
        # absolute where the gateway handed back a path.
        stub.answer = (202, {"status": "pending_approval", "request_id": RID,
                             "approval_url": f"/sends/mail-gateway/{RID}",
                             "message_id": "m2@example.org", "chat": "a@x.org",
                             "note": "This e-mail requires web-gateway approval."})
        code, out, err = push("--chat", "a@x.org", "x", env={
            **env, "CONVERSATION_BASE_URL": "https://dash.example/"})
        assert code == 0 and err == ""
        assert f"queued for approval (id={RID})" in out
        assert f"mail-push.py --retract {RID}" in out
        assert f"approve or deny at https://dash.example/sends/mail-gateway/{RID}" in out
        assert "requires web-gateway approval" in out
        code, out, _err = push("--chat", "a@x.org", "x", env={
            **env, "SEND_APPROVAL_BASE_URL": "https://sends.example",
            "CONVERSATION_BASE_URL": "https://dash.example"})
        assert f"https://sends.example/sends/mail-gateway/{RID}" in out
        code, out, _err = push("--chat", "a@x.org", "x", env=env)
        assert f"approve or deny at /sends/mail-gateway/{RID}" in out
        stub.answer = (202, {"status": "pending_approval", "request_id": RID,
                             "approval_url": f"https://abs.example/sends/mail-gateway/{RID}"})
        code, out, _err = push("--chat", "a@x.org", "x", env={
            **env, "CONVERSATION_BASE_URL": "https://dash.example"})
        assert f"approve or deny at https://abs.example/sends/mail-gateway/{RID}" in out

        # Take it back: the gateway's retract endpoint, beside /send.
        code, out, err = push("--retract", RID, env=env)
        assert code == 0 and "retracted; nothing was sent" in out
        assert stub.requests[-1][0] == f"/pending-sends/{RID}/retract"
        assert stub.requests[-1][2]["Authorization"] == "Bearer mail-secret"
        code, _out, err = push("--retract", "nope", env=env)
        assert code == 2 and "not a request id" in err

        # Sent, with an address the server would not take.
        stub.answer = (200, {"status": "sent", "message_id": "m3@example.org",
                             "chat": "m3@example.org", "ts": 1.0, "attachments": [],
                             "refused": ["bob@y.org"]})
        code, out, _err = push("--to", "ann@x.org", "--to", "bob@y.org", "x", env=env)
        assert code == 0 and "refused bob@y.org" in out
        # A refusal is the gateway's sentence; so is an outcome nobody knows.
        for status, answer, word in (
                (409, {"error": "this thread has no mail left to reply to"}, "no mail left"),
                (400, {"error": "not an address: 'x'"}, "not an address"),
                (404, {"error": "not this gateway's account"}, "account"),
                (502, {"error": "the mail server did not confirm this mail",
                       "unconfirmed": True}, "did not confirm"),
                (500, b"<html>oops</html>", "oops")):
            stub.answer = (status, answer)
            code, out, err = push("--chat", "a@x.org", "x", env=env)
            assert code == 1 and out == "" and word in err and str(status) in err, (status, err)
    finally:
        stub.server.shutdown()
    code, _out, err = push("--to", "ann@x.org", "x", "--timeout", "2",
                           env={"MAIL_GATEWAY_SEND_URL": "http://127.0.0.1:9/send"})
    assert code == 1 and "could not reach gateway" in err and "NOT KNOWN" not in err
    print("ok: a queued send prints its approval link and how to retract it; a refusal fails")


def test_one_run_is_one_mail():
    stub = Stub()
    env = {"MAIL_GATEWAY_SEND_URL": stub.url}
    try:
        # Every request names itself with a key of the run's own.
        push("--to", "ann@x.org", "x", env=env)
        push("--to", "ann@x.org", "x", env=env)
        first, second = stub.key(-2), stub.key(-1)
        assert first != second and len(first) == 32 and first.isalnum()
        # No answer (the gateway is busy handing the mail over): asked again,
        # once, under the same key — which sends nothing a second time — and
        # the answer to that is the answer.
        sent = len(stub.requests)
        stub.silent = 1
        code, out, err = push("--to", "ann@x.org", "x", "--timeout", "0.5", env=env)
        assert code == 0 and "mail-push: sent" in out and "asking again" in err, (out, err)
        assert len(stub.requests) == sent + 2 and stub.key(-2) == stub.key(-1)
        assert stub.requests[-2][1] == stub.requests[-1][1], "the very same request"
        # Still none: not "failed" — nobody knows, and it says how to find out.
        stub.silent = 2
        code, out, err = push("--to", "ann@x.org", "x", "--timeout", "0.5", env=env)
        key = stub.key()
        assert code == 1 and out == "" and "NOT KNOWN whether this mail was sent" in err
        assert f"--request-key {key}" in err and "Do not send it again" in err
        time.sleep(1.6)
        # Asked about later, with the key that run printed.
        code, out, err = push("--to", "ann@x.org", "x", "--request-key", key, env=env)
        assert code == 0 and stub.key() == key and "mail-push: sent" in out
        code, _out, err = push("--to", "ann@x.org", "x", "--request-key", "two words", env=env)
        assert code == 2 and "--request-key" in err
        # "Not confirmed" from the gateway is the same kind of answer.
        stub.answer = (502, {"error": "the mail server has not confirmed this mail",
                             "unconfirmed": True, "message_id": "m@example.org"})
        code, out, err = push("--to", "ann@x.org", "x", env=env)
        assert code == 1 and "NOT KNOWN" in err and f"--request-key {stub.key()}" in err
        # Whatever status it comes with: something between this and the
        # gateway that answered 202 for it has not made it "sent".
        stub.answer = (202, {"error": "the mail server has not confirmed this mail",
                             "unconfirmed": True, "message_id": "m@example.org"})
        code, out, err = push("--to", "ann@x.org", "x", env=env)
        assert code == 1 and "mail-push: sent" not in out and "NOT KNOWN" in err, (out, err)
        stub.answer = (502, {"error": "send failed: 554 rejected"})
        code, out, err = push("--to", "ann@x.org", "x", env=env)
        assert code == 1 and "554" in err and "NOT KNOWN" not in err
        assert "nothing was sent" not in err, "only the gateway's own word says that"
        # A failure the gateway knows to be one: nothing left it, said so.
        stub.answer = (502, {"error": "send failed: 554 rejected", "sent": False})
        code, out, err = push("--to", "ann@x.org", "x", env=env)
        assert code == 1 and out == "" and "554" in err and "NOT KNOWN" not in err
        assert "nothing was sent" in err and "new attempt" in err
        # A repeat is said to be one: what the answer tells of happened
        # before this run — sent, queued, or not confirmed.
        sent = {"status": "sent", "message_id": "m5@example.org", "chat": "a@x.org",
                "ts": 1.0, "attachments": []}
        stub.answer = (200, sent)
        code, out, err = push("--to", "ann@x.org", "x", env=env)
        assert code == 0 and "mail-push: sent" in out and "repeat" not in out + err
        stub.answer = (200, {**sent, "repeat": True})
        code, out, err = push("--to", "ann@x.org", "x", "--request-key", key, env=env)
        assert code == 0 and "mail-push: sent" in out and "this is a repeat" in out
        assert "nothing was sent or queued a second time" in out
        stub.answer = (202, {"status": "pending_approval", "request_id": RID,
                             "approval_url": f"/sends/mail-gateway/{RID}", "repeat": True})
        code, out, err = push("--to", "ann@x.org", "x", "--request-key", key, env=env)
        assert code == 0 and "queued for approval" in out and "this is a repeat" in out
        stub.answer = (202, {"status": "sending", "request_id": RID, "repeat": True})
        code, out, err = push("--to", "ann@x.org", "x", "--request-key", key, env=env)
        assert code == 0 and "being sent" in out and "this is a repeat" in out
        stub.answer = (502, {"error": "send not confirmed", "unconfirmed": True,
                             "outcome_unknown": True, "repeat": True})
        code, out, err = push("--to", "ann@x.org", "x", "--request-key", key, env=env)
        assert code == 1 and "NOT KNOWN" in err and "this is a repeat" in err and out == ""
        # A reply that leaves an address of the answered mail out says so.
        stub.answer = (200, {"status": "sent", "message_id": "m4@example.org", "chat": "a@x.org",
                             "ts": 1.0, "attachments": [], "skipped": ["p\u00f6st@evil.org"]})
        code, out, _err = push("--chat", "a@x.org", "x", env=env)
        assert code == 0 and "not written to" in out and "p\u00f6st@evil.org" in out
    finally:
        stub.server.shutdown()
    print("ok: every run carries a request key; no answer is asked about again, never resent")


def test_a_gateway_that_took_the_request_and_went_away():
    """The first request is taken whole — the gateway may be sending the
    mail — and no answer comes; by the second ask the gateway is gone
    (restarted, killed). That the second could not be delivered says nothing
    of the first: not known, with the key — never "could not reach"."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    got = {}

    def serve():
        conn, _peer = listener.accept()
        conn.settimeout(5)
        data = b""
        try:
            while b"\r\n\r\n" not in data:
                data += conn.recv(65536)
            head, _sep, rest = data.partition(b"\r\n\r\n")
            length = next(int(line.split(b":")[1]) for line in head.split(b"\r\n")
                          if line.lower().startswith(b"content-length"))
            while len(rest) < length:
                rest += conn.recv(65536)
            got["body"] = json.loads(rest)
        finally:
            listener.close()    # nothing listens any more…
            conn.close()        # …and the request it took is never answered
    server = threading.Thread(target=serve, daemon=True)
    server.start()
    code, out, err = push("--to", "ann@x.org", "--subject", "Minutes", "Hello", "--timeout",
                          "5", env={"MAIL_GATEWAY_SEND_URL": f"http://127.0.0.1:{port}/send"})
    server.join(10)
    key = got["body"]["request_key"]
    assert got["body"]["to"] == ["ann@x.org"], "the gateway had the whole request"
    assert code == 1 and out == "", (code, out, err)
    assert "asking again" in err and "NOT KNOWN whether this mail was sent" in err, err
    assert f"--request-key {key}" in err and "Do not send it again" in err
    assert "could not reach gateway" not in err, err
    assert "cannot be reached any more" in err
    # A gateway that was never there is another matter: nothing was asked.
    code, _out, err = push("--to", "ann@x.org", "x", "--timeout", "2",
                           env={"MAIL_GATEWAY_SEND_URL": f"http://127.0.0.1:{port}/send"})
    assert code == 1 and "could not reach gateway" in err and "NOT KNOWN" not in err
    print("ok: a gateway that took the request and then went away leaves the send unknown")


def test_against_the_gateway_itself():
    """The flags the companion's note names, against the real /send."""
    spec = importlib.util.spec_from_file_location(
        "mail_gateway_send_helpers", REPO_ROOT / "tests" / "test_mail_gateway_send.py")
    send = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(send)
    t = send.t
    w = send.world(policy="trust")
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        box.put("INBOX", t.msg("a@x.org", cc="bob@y.org", day=1))
        mirror.poll()
        server, base = t.serve(gw)
        env = {"MAIL_GATEWAY_SEND_URL": base + "/send", "MAIL_GATEWAY_TOKEN": t.TOKEN}
        chat_id = f"email:~{send.ACCOUNT}:a@x.org"
        with tempfile.TemporaryDirectory() as tmp:
            text = Path(tmp) / "reply.txt"
            text.write_text("Count me in.\n", encoding="utf-8")
            try:
                # Not approved by the user: queued, and retracted again.
                code, out, err = push("--chat", chat_id, "--reply", "all", "--file",
                                      str(text), env=env)
                assert code == 0 and "queued for approval" in out, (out, err)
                (entry,) = gw.SENDS.waiting("a@x.org")
                assert entry["to"] == ["ann@x.org"] and entry["cc"] == ["bob@y.org"]
                assert entry["author"] == "agent" and entry["message"] == "Count me in."
                assert f"/sends/127.0.0.1/{entry['id']}" in out
                code, out, _err = push("--retract", entry["id"], env=env)
                assert code == 0 and "retracted" in out and smtp.sessions == 0
                assert gw.SENDS.waiting() == []
                # With the user's permission: sent, to the sender alone.
                code, out, err = push("--chat", chat_id, "--reply", "sender", "--file",
                                      str(text), "--user-approved", env=env)
                assert code == 0 and "mail-push: sent" in out, (out, err)
                assert "this is a repeat" not in out, "the run that sent it"
                assert smtp.mails[-1]["rcpt"] == ["ann@x.org"]
                assert smtp.parsed().get_content().strip() == "Count me in."
                # Too late to take back; and a thread of another mailbox is
                # not this gateway's.
                code, _out, err = push("--retract", "0" * 32, env=env)
                assert code == 1 and "no pending send" in err
                code, _out, err = push("--chat", "email:~other@example.org:a@x.org", "x", env=env)
                assert code == 1 and "404" in err and "account" in err
                # A new mail, with a file.
                pdf = Path(tmp) / "plan.pdf"
                pdf.write_bytes(b"%PDF-1.4")
                code, out, err = push("--to", "dora@w.org", "--subject", "Plan", "--attach",
                                      str(pdf), "--user-approved", "Attached.", env=env)
                assert code == 0, err
                assert smtp.mails[-1]["rcpt"] == ["dora@w.org"]
                assert [p.get_filename() for p in smtp.parsed().iter_attachments()] == ["plan.pdf"]
                assert len(smtp.mails) == 2
                # The same send asked about again: its outcome, and no mail.
                key = next(e["request_key"] for e in gw.SENDS.entries()
                           if e["to"] == ["dora@w.org"])
                code, out, err = push("--to", "dora@w.org", "--subject", "Plan", "--attach",
                                      str(pdf), "--user-approved", "Attached.",
                                      "--request-key", key, env=env)
                assert code == 0 and "mail-push: sent" in out and len(smtp.mails) == 2, err
                assert "this is a repeat" in out, out
                code, _out, err = push("--to", "dora@w.org", "--subject", "Another",
                                       "--user-approved", "Else.", "--request-key", key, env=env)
                assert code == 1 and "409" in err and len(smtp.mails) == 2
                # A send that failed for certain: said so — and the same
                # request again is a new attempt, which sends the mail.
                smtp.reject_data = "554 5.7.1 content rejected"
                again = ("--to", "eve@v.org", "--subject", "Try", "--user-approved",
                         "Once more.", "--request-key", "retry-1")
                code, out, err = push(*again, env=env)
                # (The gateway runs in this process: its log is in ``out``.)
                assert code == 1 and "mail-push" not in out, (out, err)
                assert "send failed" in err and "554" in err
                assert "nothing was sent" in err and "NOT KNOWN" not in err
                assert len(smtp.mails) == 2 and gw.SENDS.listed() == []
                smtp.reject_data = None
                code, out, err = push(*again, env=env)
                assert code == 0 and "mail-push: sent" in out, err
                assert "this is a repeat" not in out, "a new attempt, and it sent the mail"
                assert len(smtp.mails) == 3 and smtp.mails[-1]["rcpt"] == ["eve@v.org"]
                code, out, err = push(*again, env=env)
                assert code == 0 and "this is a repeat" in out and len(smtp.mails) == 3
                # One of unknown outcome is never such a one: asked about
                # again, it is still not known, and nothing is sent.
                smtp.drop = "dot"
                maybe = ("--to", "fay@u.org", "--subject", "Maybe", "--user-approved",
                         "Perhaps.", "--request-key", "maybe-1")
                code, out, err = push(*maybe, env=env)
                smtp.drop = None
                assert code == 1 and "NOT KNOWN" in err and "nothing was sent" not in err
                sessions = smtp.sessions
                code, out, err = push(*maybe, env=env)
                assert code == 1 and "NOT KNOWN" in err and "this is a repeat" in err
                assert smtp.sessions == sessions and len(smtp.mails) == 4
                # Taking it back is too late — and is not told "failed".
                (unknown,) = gw.SENDS.listed()
                code, out, err = push("--retract", unknown["id"], env=env)
                assert code == 1 and "NOT KNOWN whether it went out" in err, (out, err)
                assert "failed" not in err and "Do not send it again" in err
            finally:
                server.shutdown()
    finally:
        send.done(w)
    print("ok: mail-push.py replies, queues, retracts and writes new mail through the gateway")


def main():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failures = 0
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            import traceback
            print(f"FAIL {test.__name__}: {exc}")
            traceback.print_exc()
    if failures:
        print(f"\n{failures} mail-push check(s) failed.")
        return 1
    print("\nAll mail-push checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
