#!/usr/bin/env python3
"""Declare — or revise — how much an item on the dashboard's attention list
matters, from a retinue agent.

The home screen (docs/attention-model.md) lists threads, messenger chats and
projects by importance, deadline and sphere, and the gateway decides from those
and the user's focus mode whether an item rings now, waits for the next digest,
or is merely listed. Threads declare their properties when they are opened
(conversation-push.py --importance …); this is the way to set them on anything
that already exists — canonically the triage's judgement on an inbound chat
message, which arrives on the rail before any model has read it:

    attention-set.py chat:signal:~+41790000000:+41791234567 \\
        --importance 4 --due 2026-09-04T12:00 --kind "customer request" \\
        --sphere customers --sender-sphere

    attention-set.py thread:42ecb0113a3d48ac87be514cfaf99a7c --actor Publisher
    attention-set.py urn:retinue:project:vat-q3 --importance 5 --lead 4w
    attention-set.py chat:… --done        # nothing to do here; drop it from the list

Item ids are the ones the dashboard uses: ``thread:<id>``, ``chat:<chat id>``
(the id GET /chats shows, or the rail's <channel>:~<account>:<key>), or a
project's URI. ``--sender-sphere`` also remembers the sphere for the chat's
sender, so their next message starts there. The gateway re-evaluates at once:
a raised importance can ring, and the reply says what was decided.

Configuration (environment): CONVERSATION_BACKEND_URL / _TOKEN as for
conversation-push.py — the endpoint is the same token-gated internal API.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

_PORT = os.environ.get("WEB_GATEWAY_PORT", "8080")
_BASE = os.environ.get(
    "CONVERSATION_BACKEND_URL", f"http://localhost:{_PORT}/internal/conversations"
).rstrip("/")
DEFAULT_URL = os.environ.get("ATTENTION_BACKEND_URL", _BASE.rsplit("/", 1)[0] + "/attention/set")
TOKEN = os.environ.get("CONVERSATION_BACKEND_TOKEN", "").strip()
DEFAULT_TIMEOUT = float(os.environ.get("CONVERSATION_BACKEND_TIMEOUT", "30"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Declare an attention item's properties.")
    parser.add_argument("id", help="thread:<id>, chat:<chat id>, or a project URI")
    parser.add_argument("--importance", type=float, metavar="0-5")
    parser.add_argument("--due", metavar="WHEN", help="ISO date-time, YYYY-MM-DD (17:00), or 'none'")
    parser.add_argument("--lead", metavar="SPAN", help="90m, 2h, 3d, 2w")
    parser.add_argument("--sphere", metavar="NAME")
    parser.add_argument("--tag", action="append", default=[], metavar="NAME")
    parser.add_argument("--kind", metavar="LABEL")
    parser.add_argument("--critical", dest="critical", action="store_true", default=None)
    parser.add_argument("--not-critical", dest="critical", action="store_false")
    parser.add_argument("--actor", metavar="NAME", help="who it waits on; 'you' hands it back")
    parser.add_argument("--sender-sphere", action="store_true",
                        help="remember --sphere for this chat's sender in the profile")
    parser.add_argument("--done", dest="state", action="store_const", const="done",
                        help="mark the item handled")
    parser.add_argument("--reopen", dest="state", action="store_const", const="open")
    parser.add_argument("--url", default=None, help=f"endpoint URL (default {DEFAULT_URL})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()

    if not TOKEN:
        print("attention-set: CONVERSATION_BACKEND_TOKEN is not set", file=sys.stderr)
        return 2
    payload: dict = {"id": args.id}
    if args.importance is not None:
        if not 0 <= args.importance <= 5:
            print("attention-set: --importance must be between 0 and 5", file=sys.stderr)
            return 2
        payload["importance"] = args.importance
    if args.due is not None:
        payload["due"] = None if args.due.lower() == "none" else args.due
    for key in ("lead", "sphere", "kind", "actor", "state"):
        value = getattr(args, key)
        if value:
            payload[key] = value
    if args.tag:
        payload["tags"] = args.tag
    if args.critical is not None:
        payload["critical"] = args.critical
    if args.sender_sphere:
        payload["sender_sphere"] = True
    if len(payload) == 1:
        print("attention-set: nothing to set", file=sys.stderr)
        return 2

    req = urllib.request.Request(
        args.url or DEFAULT_URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Conversation-Backend-Token": TOKEN},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"attention-set: HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}",
              file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"attention-set: request failed: {exc}", file=sys.stderr)
        return 1
    item = body.get("item") or {}
    print(json.dumps(body, ensure_ascii=False))
    print(f"attention-set: {item.get('title', args.id)} — {item.get('level', '?')}; "
          f"{item.get('delivery', '')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
