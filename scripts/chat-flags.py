#!/usr/bin/env python3
"""Archive, mute or star a chat from a retinue agent.

Every chat (see the web-gateway's /chats API) carries three flags that say
where the user wants it on their screen: `archived` (out of the list until it
speaks again), `muted` (out of the list, and the next message does not bring
it back — muting archives too) and `starred` (first in its list). The user
sets them on the Chats page; an agent sets them here, with the same meaning:

    chat-flags.py --chat "email:~me@example.org:root-id@example.org" --archive

    chat-flags.py --chat "signal:+41791234567" --mute

    chat-flags.py --chat "telegram:774301992" --unarchive

Two callers have a reason to. A turn worked by a sender's standing instruction
(docs/contacts.md, "The instruction") archives the mail the instruction told
it to file — "File the document with the Archivist, book it, then archive the
mail." — and names that in its verdict (`chat-verdict.py --action "archived
the mail"`). And Ara archives or mutes a chat when the user asks her to.

For a mail thread the mailbox follows the chat, exactly as when the user
archives it themselves (docs/mail-chats.md, "The mailbox follows the chat"):
the thread's mails move out of the inbox into the archive folder. Unarchiving
moves nothing back. Archiving also settles the chat's attention item; an
archive made by an instructed turn means the message that turn works rings
nothing.

`--star` / `--unstar` exist for the same reason they do on a dashboard thread
(conversation-push.py): the star is the user's, so set or clear it only on
their word, and never archive a starred chat on your own initiative.

There is no delete. Erasing a chat is the user's own act, in the dashboard.

The chat id is ``<channel>:<chat-key>`` — the same id the chat surface shows;
it is percent-encoded into the URL here, so keys containing '/', '@' or ':'
need no quoting beyond the shell's.

This is the retinue-side client for the gateway's token-gated
``POST /internal/chats/<id>/flags`` — the sibling of chat-draft.py and
chat-verdict.py.

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
        description="Archive, mute or star a chat (there is no delete)."
    )
    parser.add_argument("--chat", required=True, metavar="ID",
                        help="the chat id, <channel>:<chat-key> "
                             "(e.g. email:~me@example.org:<message-id>)")
    archive = parser.add_mutually_exclusive_group()
    archive.add_argument("--archive", dest="archived", action="store_const", const=True,
                         help="out of the list until it speaks again; a mail "
                              "thread's mails move out of the inbox")
    archive.add_argument("--unarchive", dest="archived", action="store_const", const=False,
                         help="back into the list (the mailbox is not touched)")
    mute = parser.add_mutually_exclusive_group()
    mute.add_argument("--mute", dest="muted", action="store_const", const=True,
                      help="archive it, and let no new message bring it back")
    mute.add_argument("--unmute", dest="muted", action="store_const", const=False,
                      help="let the next message bring it back (it stays archived until then)")
    star = parser.add_mutually_exclusive_group()
    star.add_argument("--star", dest="starred", action="store_const", const=True,
                      help="first in its list — only on the user's word")
    star.add_argument("--unstar", dest="starred", action="store_const", const=False,
                      help="clear the star — only on the user's word")
    parser.add_argument("--url", default=None, help=f"endpoint base URL (default {DEFAULT_URL})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="HTTP timeout in seconds")
    args = parser.parse_args()

    payload = {name: value for name, value in (("archived", args.archived),
                                                ("muted", args.muted),
                                                ("starred", args.starred))
               if value is not None}
    if not payload:
        print("chat-flags: nothing to set (--archive, --unarchive, --mute, --unmute, "
              "--star or --unstar)", file=sys.stderr)
        return 2
    if ":" not in args.chat:
        print(f"chat-flags: not a chat id (want <channel>:<chat-key>): {args.chat}",
              file=sys.stderr)
        return 2
    if not TOKEN:
        print("chat-flags: CONVERSATION_BACKEND_TOKEN is not set", file=sys.stderr)
        return 2

    base = (args.url or DEFAULT_URL).rstrip("/")
    url = f"{base}/{urllib.parse.quote(args.chat, safe='')}/flags"
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
        if exc.code == 410:
            # A mail thread that was folded into another: the answer names
            # the chat it is part of now.
            print("chat-flags: this chat is part of another one now; use the id in "
                  f"`merged_into`: {detail}", file=sys.stderr)
        else:
            print(f"chat-flags: HTTP {exc.code}: {detail}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"chat-flags: request failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(body, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
