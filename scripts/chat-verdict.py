#!/usr/bin/env python3
"""Set the verdict of a turn a sender's standing instruction started.

A person in the address book can carry the user's standing instruction
(docs/contacts.md, "The instruction"). Each message of theirs is worked by one
companion turn with that instruction as its brief, and the turn ends by saying
what the message is worth — its importance on the attention model's 0–5 scale
and, where it carries one, its deadline — and what it did and flags:

    chat-verdict.py --chat "signal:+41791234567" --importance 4 \
        --due 2026-10-20T08:00 \
        --action "staged a reply confirming Monday 08:00" \
        --action "added the visit to the calendar"

    chat-verdict.py --chat "whatsapp:4179555@s.whatsapp.net" --importance 1

    chat-verdict.py --chat "telegram:774301992" --importance 3 \
        --flag "asks for the door code — kept out of the draft"

    chat-verdict.py --chat "email:~me@example.org:root-id@example.org" \
        --importance 1 --action "filed the invoice with the Archivist" \
        --action "archived the mail"

Every arrival turn — a VIP's too, whose verdict is only this — also says how
the Chats page's Triage lists the chat (docs/chat-triage.md, "Arrival
turns"), so a later press spends no second judgement on it:

    chat-verdict.py --chat "signal:+41791234567" --triage action \
        --triage-note "Mara asks whether you come on Saturday — answer her."

A mail thread is a chat like any other here (docs/mail-chats.md, "The gate"):
a mail from an instructed sender is worked by the same kind of turn, which
ends with the same verdict. What that turn archived (chat-flags.py) it names
as an action.

For a sender who is not also a VIP the verdict is what decides whether the
message rings now, waits for the next digest, or is only listed: the gateway
holds the ring until the turn ends and then puts the message through the
attention model with this importance and deadline. A turn that ends without a
verdict has the message rung as a VIP's would be. For a VIP the message rang
on arrival, and the verdict records.

The verdict is taken only while such a turn is running for the chat (HTTP 409
otherwise); a second call in the same turn replaces the first. Use the chat
id the turn's prompt names: should the mail thread have been folded into
another while the turn worked, the verdict is applied to the chat it is part
of now, and the answer's `id` names that one.

How long the ring waits for the verdict: a mail's `MAIL_INSTRUCTION_TIMEOUT`
(600 s by default), any other message's `CHAT_INSTRUCTION_TIMEOUT` (60 s).

This is the retinue-side client for the gateway's token-gated
``POST /internal/chats/<id>/verdict`` — the sibling of chat-draft.py and
chat-flags.py.

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
        description="Set the verdict of the turn a sender's standing instruction started."
    )
    parser.add_argument("--chat", required=True, metavar="ID",
                        help="the chat id, <channel>:<chat-key> (e.g. signal:+41791234567, "
                             "or a mail thread's email:~<account>:<message-id>)")
    parser.add_argument("--importance", type=float, default=None,
                        help="0–5: 0–1 trivial (listed), 2–3 ordinary (next digest), "
                             "4–5 important (rings where the Focus mode admits the sender)")
    parser.add_argument("--due", metavar="WHEN",
                        help="the deadline the message carries: an ISO date-time or YYYY-MM-DD")
    parser.add_argument("--action", action="append", default=[],
                        help="one thing the turn did (repeatable), as the user should read it "
                             "(\"staged a reply\", \"archived the mail\")")
    parser.add_argument("--flag", action="append", default=[],
                        help="one thing the user must look at (repeatable): a sensitive "
                             "request kept out of the draft, a conflict, a doubt")
    parser.add_argument("--triage", choices=("action", "archive", "delete"),
                        help="how the Chats page's Triage lists the chat (docs/chat-triage.md, "
                             "\"Arrival turns\"): action, archive or delete")
    parser.add_argument("--triage-note", default=None, metavar="TEXT",
                        help="one short sentence for the triage row: what it is and, for "
                             "action, what the user should do")
    parser.add_argument("--url", default=None, help=f"endpoint base URL (default {DEFAULT_URL})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="HTTP timeout in seconds")
    args = parser.parse_args()

    if args.importance is None and not args.triage:
        print("chat-verdict: give --importance (an instructed turn's verdict) and/or "
              "--triage (every arrival turn's)", file=sys.stderr)
        return 2
    if args.importance is not None and not 0 <= args.importance <= 5:
        print(f"chat-verdict: importance is 0–5, not {args.importance:g}", file=sys.stderr)
        return 2
    if ":" not in args.chat:
        print(f"chat-verdict: not a chat id (want <channel>:<chat-key>): {args.chat}",
              file=sys.stderr)
        return 2
    if not TOKEN:
        print("chat-verdict: CONVERSATION_BACKEND_TOKEN is not set", file=sys.stderr)
        return 2

    base = (args.url or DEFAULT_URL).rstrip("/")
    url = f"{base}/{urllib.parse.quote(args.chat, safe='')}/verdict"
    payload: dict = {"actions": args.action, "flags": args.flag}
    if args.importance is not None:
        payload["importance"] = args.importance
    if args.due:
        payload["due"] = args.due
    if args.triage:
        payload["triage"] = args.triage
        if args.triage_note:
            payload["triage_note"] = args.triage_note

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
            print("chat-verdict: refused — no arrival turn is working on this chat: "
                  f"{detail}", file=sys.stderr)
        else:
            print(f"chat-verdict: HTTP {exc.code}: {detail}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"chat-verdict: request failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(body, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
