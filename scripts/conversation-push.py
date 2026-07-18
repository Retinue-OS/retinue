#!/usr/bin/env python3
"""Open a conversation tab in the Retinue dashboard from a retinue agent.

A "conversation tab" is a chat thread with Ara shown on the dashboard. Besides
threads the user starts, an agent can *initiate* one when it hits a decision it
should not make alone — e.g. an RSVP, an ambiguous e-mail, a calendar clash:

    conversation-push.py --title "Party RSVP" "You've got an invitation to Mara's birthday party on Saturday. Shall I confirm you'll attend and add it to your agenda, or politely decline?"

Use --attach to hand the user a file to download from the thread (e.g. an
e-mail attachment forwarded into the dashboard):

    conversation-push.py --title "Invoice" --attach /tmp/BEL14603717.PDF \
        "Here's the Eier Meier invoice — CHF 57.00. PDF attached to download."

Use --thread to append to a thread that already exists, instead of opening a new
one — so a file lands in the conversation the user is already reading:

    conversation-push.py --thread 42ecb0113a3d48ac87be514cfaf99a7c \
        --attach /tmp/termine.ics "Here are the appointments as an .ics file."

The thread appears on the dashboard with an unread badge; when the user replies,
Ara picks up the thread (with full context) and carries out what they approve.

This is the retinue-side client for the gateway's token-gated
`/internal/conversations` endpoint — analogous to signal-push.py for Signal.
The token keeps the endpoint reachable only from in-container agents, not from
the authenticated-but-public dashboard.

Configuration (environment):
    CONVERSATION_BACKEND_URL    default http://localhost:${WEB_GATEWAY_PORT}/internal/conversations
    CONVERSATION_BACKEND_TOKEN  shared secret gating the endpoint (set by the entrypoint)
"""
import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

_PORT = os.environ.get("WEB_GATEWAY_PORT", "8080")
DEFAULT_URL = os.environ.get(
    "CONVERSATION_BACKEND_URL", f"http://localhost:{_PORT}/internal/conversations"
)
TOKEN = os.environ.get("CONVERSATION_BACKEND_TOKEN", "").strip()
DEFAULT_TIMEOUT = float(os.environ.get("CONVERSATION_BACKEND_TIMEOUT", "30"))
_THREAD_ID_RE = re.compile(r"[0-9a-f]{32}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Open a dashboard conversation tab with the user."
    )
    parser.add_argument("message", help="the message/question to show the user")
    parser.add_argument("--title", help="short tab title (derived from the message if omitted)")
    parser.add_argument("--thread", metavar="ID",
                        help="append to this existing thread instead of opening a new one")
    parser.add_argument("--on-behalf-of", dest="on_behalf_of",
                        help="requester identity that owns the thread (defaults to the dashboard user)")
    parser.add_argument("--attach", action="append", default=[], metavar="PATH",
                        help="attach a file the user can download from the thread (repeatable)")
    parser.add_argument("--url", default=None, help=f"endpoint URL (default {DEFAULT_URL})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="HTTP timeout in seconds")
    args = parser.parse_args()

    message = args.message.strip()
    if not message and not args.attach:
        print("conversation-push: empty message", file=sys.stderr)
        return 2
    if not TOKEN:
        print("conversation-push: CONVERSATION_BACKEND_TOKEN is not set", file=sys.stderr)
        return 2

    if args.thread and not _THREAD_ID_RE.fullmatch(args.thread):
        print(f"conversation-push: not a thread id: {args.thread}", file=sys.stderr)
        return 2
    if args.thread and args.title:
        print("conversation-push: --title applies only to a new thread", file=sys.stderr)
        return 2

    url = args.url or DEFAULT_URL
    if args.thread and not args.url:
        url = f"{DEFAULT_URL.rstrip('/')}/{args.thread}/messages"

    payload: dict = {"message": message}
    if args.title:
        payload["title"] = args.title
    if args.on_behalf_of:
        payload["on-behalf-of"] = args.on_behalf_of
    if args.attach:
        attachments = []
        for spec in args.attach:
            path = Path(spec)
            if not path.is_file():
                print(f"conversation-push: attachment not found: {spec}", file=sys.stderr)
                return 2
            attachments.append({
                "filename": path.name,
                "content_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                "data": base64.b64encode(path.read_bytes()).decode("ascii"),
            })
        payload["attachments"] = attachments

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Conversation-Backend-Token": TOKEN,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"conversation-push: HTTP {exc.code}: {detail}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"conversation-push: request failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(body, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
