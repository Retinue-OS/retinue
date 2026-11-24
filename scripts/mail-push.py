#!/usr/bin/env python3
"""Send an e-mail through the mail gateway.

The retinue-side client for the mail gateway's `/send` endpoint — the e-mail
sibling of sms-push.py and telegram-push.py (docs/mail-chats.md, "Writing").
The gateway holds the mailbox credentials, so this CLI carries only a
capability token. It writes either a **reply** in a thread the gateway
mirrors, or a **new mail**:

  * a reply names the thread — `--chat` (the chat id the dashboard uses, or
    the bare chat key) or `--reply-to` (a reply token) — and nothing else:
    the gateway addresses it from the mail it answers, to its sender and,
    with `--reply all` (the default), in copy to everyone else on it. You
    name no recipient and no subject;
  * a new mail names its recipients (`--to`, repeatable; `--cc`) and its
    `--subject`.

The text is the positional argument or, better, a file (`--file`): a mail is
several lines, and a text on the command line can end its own quoting.

Outbound is gated by EMAIL_SEND_POLICY, keyed by the mailbox's own address,
never the recipient: a `verify` mailbox queues the mail as a pending send that
must be approved on the web gateway's /sends page — the thread shows it
waiting meanwhile; a `trust` mailbox sends directly only with --user-approved.
On a queued send this prints the approval URL.

One run is one mail. Each run names its request to the gateway with a key of
its own, and the gateway answers a second request under the same key with
what became of the first, sending nothing. So when no answer comes back (a
timeout, a dropped connection) this asks once more, safely; and when it still
does not know — also when the gateway can no longer be reached by then — it
says so: "NOT KNOWN whether this mail was sent", with the key. Run the same
command again with `--request-key <key>` to learn the outcome. Never send the
mail anew to find out.

What the answer to such a second request tells of happened earlier — the
mail went out then, or was queued then — and this prints that it was a
repeat: the run that asked sent nothing. Only a mail that went out, waits, or
may have gone out keeps its key that way. A send that failed for certain is
over (the gateway says nothing left it, and this prints "nothing was sent"):
the same command again, with that key or without one, is a new attempt, and
sends the mail.

Examples:
    mail-push.py --chat 'email:~me@example.org:abc@x.org' --file /tmp/reply.txt
    mail-push.py --chat 'email:~me@example.org:abc@x.org' --reply sender "Thanks, got it."
    mail-push.py --to ann@x.org --cc bob@y.org --subject "Minutes" \\
        --file /tmp/minutes.txt --attach /tmp/minutes.pdf
    mail-push.py --retract 0123456789abcdef0123456789abcdef
    mail-push.py --to ann@x.org --subject "Minutes" --file /tmp/minutes.txt \\
        --request-key 5d41402abc4b2a76b9719d911017c592     # what became of that send?

Configuration (environment):
    MAIL_GATEWAY_SEND_URL   default http://mail-gateway:8096/send
    MAIL_GATEWAY_TOKEN      bearer token (must match the gateway)
"""
import argparse
import base64
import http.client
import json
import mimetypes
import os
import re
import socket
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import chat_state
import pending_retract
import send_origin

CHANNEL = "email"
DEFAULT_URL = os.environ.get("MAIL_GATEWAY_SEND_URL", "http://mail-gateway:8096/send")
TOKEN = os.environ.get("MAIL_GATEWAY_TOKEN", "").strip()
# A direct send answers once the SMTP server has taken the mail — which, with
# attachments on a slow line, takes longer than a messenger's send.
DEFAULT_TIMEOUT = float(os.environ.get("MAIL_GATEWAY_TIMEOUT", "120"))
# A request key as the gateway takes one.
_REQUEST_KEY_RE = re.compile(r"[\x21-\x7e]{1,64}")


def _encode_attachment(path: Path) -> dict:
    """One file of any type as a /send attachment."""
    if not path.is_file():
        raise SystemExit(f"mail-push: attachment not found: {path}")
    entry = {"filename": path.name,
             "data": base64.b64encode(path.read_bytes()).decode("ascii")}
    content_type = mimetypes.guess_type(path.name)[0]
    if content_type:
        entry["content_type"] = content_type
    return entry


def _thread(chat: str) -> dict:
    """`--chat` as the gateway takes it: the chat key, and — where a chat id
    named one — the mailbox the thread belongs to, which the gateway checks
    against its own (a thread of another mailbox is another gateway's:
    `--url`)."""
    ref = chat_state.split_chat_ref(chat)
    if ref is not None and ref[0] == CHANNEL:
        _channel, account, key = ref
        return {"recipient": key, **({"account": account} if account else {})}
    return {"recipient": chat}


def main() -> int:
    parser = argparse.ArgumentParser(description="Send an e-mail via the mail gateway.")
    parser.add_argument("message", nargs="?", default="", help="the mail's text")
    parser.add_argument("--file", metavar="PATH",
                        help="read the text from this file (UTF-8) instead of the "
                             "command line; '-' reads standard input")
    parser.add_argument("--chat", metavar="CHAT",
                        help="reply in this thread: its chat id "
                             "(email:~<mailbox>:<key>) or its bare chat key")
    parser.add_argument("--reply-to", metavar="TOKEN",
                        help="reply in the thread a reply token names. Overrides --chat.")
    parser.add_argument("--reply", choices=("all", "sender"),
                        help="whom a reply goes to: 'all' (the default) — the sender of "
                             "the mail it answers, in copy to everyone else on it — or "
                             "'sender' alone")
    parser.add_argument("--to", action="append", default=[], metavar="ADDRESS",
                        help="a new mail's recipient; repeat for several")
    parser.add_argument("--cc", action="append", default=[], metavar="ADDRESS",
                        help="a new mail's copy recipient; repeat for several")
    parser.add_argument("--subject", help="a new mail's subject")
    parser.add_argument("--attach", action="append", default=[], metavar="PATH",
                        help="attach a file of any type (repeatable)")
    parser.add_argument("--user-approved", action="store_true",
                        help="assert that the user has already approved this very send; "
                             "bypasses the verify flow for 'trust'-category mailboxes")
    send_origin.add_argument(parser)
    parser.add_argument("--request-key", metavar="KEY",
                        help="ask about a send an earlier run made and got no answer "
                             "for (the key that run printed), with the same arguments: "
                             "the gateway answers what became of it and sends nothing "
                             "a second time")
    parser.add_argument("--retract", metavar="REQUEST_ID",
                        help="retract a queued send you created (the id printed when it "
                             "was queued) before the user approves it; nothing is sent")
    parser.add_argument("--url", default=DEFAULT_URL,
                        help=f"gateway send URL — another mailbox's gateway has its own "
                             f"(default {DEFAULT_URL})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="HTTP timeout in seconds")
    args = parser.parse_args()

    if args.retract:
        return pending_retract.retract("mail-push", args.url, args.retract, TOKEN,
                                       args.timeout)

    if args.file is not None and args.message:
        parser.error("give the text as an argument or with --file, not both")
    if args.file is not None:
        try:
            text = (sys.stdin.read() if args.file == "-"
                    else Path(args.file).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            print(f"mail-push: cannot read {args.file}: {exc}", file=sys.stderr)
            return 1
    else:
        text = args.message
    if not text.strip() and not args.attach:
        parser.error("provide a text (an argument or --file) and/or at least one --attach")

    reply = bool(args.reply_to or args.chat)
    if reply and (args.to or args.cc or args.subject):
        parser.error("a reply is addressed by the gateway, from the mail it answers: "
                     "--to, --cc and --subject go with a new mail, not with "
                     "--chat / --reply-to")
    if not reply and args.reply:
        parser.error("--reply goes with --chat or --reply-to")
    if not reply and not args.to:
        parser.error("name the thread to reply in (--chat or --reply-to) or a new "
                     "mail's recipients (--to)")

    payload: dict = {"message": text}
    if args.reply_to:
        # The token names the thread; do not also send a (possibly other) --chat.
        payload["reply_to"] = args.reply_to
    elif args.chat:
        payload.update(_thread(args.chat))
    else:
        payload["to"] = args.to
        if args.cc:
            payload["cc"] = args.cc
        if args.subject:
            payload["subject"] = args.subject
    if args.reply:
        payload["reply"] = args.reply
    if args.user_approved:
        payload["user_approved"] = True
    send_origin.stamp(payload, args.thread)
    if args.attach:
        # "images" is the wire key's historical name; it carries any file.
        payload["images"] = [_encode_attachment(Path(p)) for p in args.attach]
    # One run is one mail: the key lets the gateway tell a second request
    # for this very mail from a new one, and answer it without sending.
    key = args.request_key or uuid.uuid4().hex
    if not _REQUEST_KEY_RE.fullmatch(key):
        parser.error("--request-key is the key an earlier run printed: up to 64 "
                     "printable ASCII characters, without spaces")
    payload["request_key"] = key

    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    data = json.dumps(payload).encode("utf-8")

    def ask():
        """One request: ``(status, body)``, or None when no answer came back
        although the gateway may have the request (a timeout, a connection
        that broke) — raises for a gateway that was never reached."""
        request = urllib.request.Request(args.url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=args.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return resp.status, body if isinstance(body, dict) else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                body = json.loads(raw)
            except ValueError:
                body = None
            return exc.code, body if isinstance(body, dict) else {"error": raw.strip()[:200]}
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (ConnectionRefusedError, socket.gaierror)):
                raise       # never reached: nothing was asked, nothing sent
            return None
        except (OSError, http.client.HTTPException, ValueError):
            return None

    try:
        answer = ask()
    except urllib.error.URLError as exc:
        # The first request, and it never reached the gateway: nothing was
        # asked, nothing sent.
        print(f"mail-push: could not reach gateway at {args.url}: {exc}", file=sys.stderr)
        return 1
    gone = None
    if answer is None:
        # The gateway may be sending the mail right now. Asking again
        # under the same key sends nothing a second time: it is told
        # what became of the first request, or waits for it.
        print("mail-push: no answer from the gateway yet; asking again about this "
              "same send (it is not sent twice)", file=sys.stderr)
        try:
            answer = ask()
        except urllib.error.URLError as exc:
            # Not reachable NOW. That says nothing of the first request,
            # which the gateway may have taken — and sent — before it went
            # away: this is "not known", never "could not reach".
            gone = exc
    unknown = (f"mail-push: it is NOT KNOWN whether this mail was sent. Do not send it "
               f"again as a new mail. Ask about this same send with the same arguments "
               f"plus --request-key {key} (that never sends a second time), or look in "
               f"the thread.")
    if answer is None:
        print(f"mail-push: the gateway at {args.url} did not answer"
              + (f", and cannot be reached any more ({gone})." if gone is not None else "."),
              file=sys.stderr)
        print(unknown, file=sys.stderr)
        return 1
    status, body = answer
    # The gateway had this request before (the key it carries) and answers
    # with what became of it then: nothing was sent, or queued, a second time.
    repeat = ("mail-push: this is a repeat: the gateway had this request before, under "
              "the same key, and answers with what became of it then — nothing was sent "
              "or queued a second time" if body.get("repeat") is True else "")
    if 200 <= status < 300 and body.get("status") == "sending":
        # (Asked about again after the user allowed it: on its way.)
        print(f"mail-push: send {body.get('request_id', '?')} was approved and is "
              f"being sent")
        if repeat:
            print(repeat)
        return 0
    if 200 <= status < 300 and body.get("status") == "pending_approval":
        print(f"mail-push: send queued for approval (id={body.get('request_id', '?')})")
        print(f"mail-push: take it back before approval with "
              f"mail-push.py --retract {body.get('request_id', '?')}")
        approval_url = body.get("approval_url", "")
        # Absolutize a relative approval path, as telegram-push.py does.
        if approval_url.startswith("/"):
            base = (os.environ.get("SEND_APPROVAL_BASE_URL")
                    or os.environ.get("CONVERSATION_BASE_URL", "")).rstrip("/")
            if base:
                approval_url = base + approval_url
        if approval_url:
            print(f"mail-push: approve or deny at {approval_url}")
        note = body.get("note", "")
        if note:
            print(f"mail-push: {note}")
        _say_skipped(body)
        if repeat:
            print(repeat)
        return 0
    if 200 <= status < 300:
        print(f"mail-push: sent (message id {body.get('message_id', '?')}, "
              f"thread {body.get('chat', '?')})")
        refused = body.get("refused") or []
        if refused:
            print(f"mail-push: the mail server refused {', '.join(map(str, refused))}; "
                  f"the mail went to the others")
        _say_skipped(body)
        if repeat:
            print(repeat)
        return 0
    print(f"mail-push: gateway returned {status}: {body.get('error', '')}", file=sys.stderr)
    if body.get("unconfirmed"):
        print(unknown, file=sys.stderr)
    elif body.get("sent") is False:
        # The gateway's word that nothing left it: this send is over, and
        # the command run again — with this key or a new one — is a new
        # attempt, not a second mail.
        print("mail-push: nothing was sent. Running the command again is a new attempt.",
              file=sys.stderr)
    if repeat:
        print(repeat, file=sys.stderr)
    return 1


def _say_skipped(body: dict) -> None:
    """Whom a reply was not addressed to, although the mail it answers names
    them: the gateway found no address there that a mail can be sent to."""
    skipped = body.get("skipped") or []
    if skipped:
        print(f"mail-push: not written to (no address a mail can be sent to): "
              f"{', '.join(map(str, skipped))}")


if __name__ == "__main__":
    raise SystemExit(main())
