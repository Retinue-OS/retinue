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

Examples:
    mail-push.py --chat 'email:~me@example.org:abc@x.org' --file /tmp/reply.txt
    mail-push.py --chat 'email:~me@example.org:abc@x.org' --reply sender "Thanks, got it."
    mail-push.py --to ann@x.org --cc bob@y.org --subject "Minutes" \\
        --file /tmp/minutes.txt --attach /tmp/minutes.pdf
    mail-push.py --retract 0123456789abcdef0123456789abcdef

Configuration (environment):
    MAIL_GATEWAY_SEND_URL   default http://mail-gateway:8096/send
    MAIL_GATEWAY_TOKEN      bearer token (must match the gateway)
"""
import argparse
import base64
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import chat_state
import pending_retract
import send_origin

CHANNEL = "email"
DEFAULT_URL = os.environ.get("MAIL_GATEWAY_SEND_URL", "http://mail-gateway:8096/send")
TOKEN = os.environ.get("MAIL_GATEWAY_TOKEN", "").strip()
# A direct send answers once the SMTP server took the mail and the Sent copy
# is filed: longer than a messenger's.
DEFAULT_TIMEOUT = float(os.environ.get("MAIL_GATEWAY_TIMEOUT", "120"))


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

    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    request = urllib.request.Request(
        args.url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if body.get("status") == "pending_approval":
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
            return 0
        print(f"mail-push: sent (message id {body.get('message_id', '?')}, "
              f"thread {body.get('chat', '?')})")
        refused = body.get("refused") or []
        if refused:
            print(f"mail-push: the mail server refused {', '.join(map(str, refused))}; "
                  f"the mail went to the others")
        return 0
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(raw).get("error", "")
        except (ValueError, AttributeError):
            detail = raw.strip()[:200]
        print(f"mail-push: gateway returned {exc.code}: {detail}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"mail-push: could not reach gateway at {args.url}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
