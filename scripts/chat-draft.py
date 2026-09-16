#!/usr/bin/env python3
"""Stage a messenger chat's shared draft from a retinue agent.

Every chat (see the web-gateway's /chats API) carries one shared draft — the
chat composer's text area. An agent composes *into* it rather than sending:
under the `verify` send policy the user's send button is the approval, so
staging the draft is exactly how an agent's proposed reply reaches the wire —
with the user's tap, in the user's name, editable up to the last moment.

    chat-draft.py --chat "signal:+41791234567" \
        "Samstag passt — ich bin um 18:30 da. Soll ich etwas mitbringen?"

    chat-draft.py --chat "whatsapp:4179555@s.whatsapp.net" --agent Coach \
        "Training bestätigt für Donnerstag 18:00."

    chat-draft.py --chat "telegram:774301992" --clear

The chat id is ``<channel>:<chat-key>`` — the same id the chat surface shows;
it is percent-encoded into the URL here, so keys containing '/', '@' or ':'
need no quoting beyond the shell's.

The write is refused (HTTP 409) when the draft currently holds non-empty text
the *user* typed — an agent must never clobber what the user is composing. Pass
--version <n> (the draft version the agent last read) to assert an informed
overwrite; a stale version is likewise refused with the current state in the
response body.

This is the retinue-side client for the gateway's token-gated
``POST /internal/chats/<id>/draft`` — analogous to conversation-push.py for
dashboard threads.

Configuration (environment):
    CHAT_DRAFT_BACKEND_URL      default http://localhost:${WEB_GATEWAY_PORT}/internal/chats
    CONVERSATION_BACKEND_TOKEN  shared secret gating the endpoint (set by the entrypoint)
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

_PORT = os.environ.get("WEB_GATEWAY_PORT", "8080")
DEFAULT_URL = os.environ.get(
    "CHAT_DRAFT_BACKEND_URL", f"http://localhost:{_PORT}/internal/chats"
)
TOKEN = os.environ.get("CONVERSATION_BACKEND_TOKEN", "").strip()
DEFAULT_TIMEOUT = float(os.environ.get("CONVERSATION_BACKEND_TIMEOUT", "30"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stage (or clear) a messenger chat's shared draft."
    )
    parser.add_argument("text", nargs="?", default="",
                        help="the draft text to stage")
    parser.add_argument("--chat", required=True, metavar="ID",
                        help="the chat id, <channel>:<chat-key> (e.g. signal:+41791234567)")
    parser.add_argument("--agent",
                        help="agent name shown on the staged draft (e.g. Coach)")
    parser.add_argument("--version", type=int, default=None,
                        help="draft version this edit is based on (overwrites an "
                             "existing user draft only when given and current)")
    parser.add_argument("--clear", action="store_true",
                        help="clear the draft instead of staging text")
    parser.add_argument("--url", default=None, help=f"endpoint base URL (default {DEFAULT_URL})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="HTTP timeout in seconds")
    args = parser.parse_args()

    text = args.text.strip()
    if args.clear and text:
        print("chat-draft: --clear cannot be combined with text", file=sys.stderr)
        return 2
    if not args.clear and not text:
        print("chat-draft: empty draft (use --clear to clear)", file=sys.stderr)
        return 2
    if ":" not in args.chat:
        print(f"chat-draft: not a chat id (want <channel>:<chat-key>): {args.chat}",
              file=sys.stderr)
        return 2
    if not TOKEN:
        print("chat-draft: CONVERSATION_BACKEND_TOKEN is not set", file=sys.stderr)
        return 2

    base = (args.url or DEFAULT_URL).rstrip("/")
    url = f"{base}/{urllib.parse.quote(args.chat, safe='')}/draft"
    payload: dict = {"text": "" if args.clear else text}
    if args.agent:
        payload["agent"] = args.agent
    if args.version is not None:
        payload["version"] = args.version

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
        if exc.code == 409:
            print("chat-draft: refused — the draft holds newer or user-typed text; "
                  f"read it first and pass --version: {detail}", file=sys.stderr)
        else:
            print(f"chat-draft: HTTP {exc.code}: {detail}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"chat-draft: request failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(body, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
