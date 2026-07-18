#!/usr/bin/env python3
"""Query the telegram-gateway's read API to resolve a Telegram contact.

The Telegram sibling of signal-contacts.py — the retinue-side client for the
gateway's token-gated read endpoints. It lets an agent resolve a name to a
chat before sending — the contact-lookup step the messaging-contact-lookup skill
requires.

Because the gateway logs in as the user's own account (a Telethon user client,
not a bot), it has both the account's **recent conversations** and its real
**contact directory**. Lookup order mirrors the messaging-contact-lookup skill:
recent conversations first (`GET /recent-chats`), the contact directory only as a
fallback (`GET /contacts`). Each returned entry carries a `source` field. Passing
--all or --contacts dumps a whole roster.

Usage:
    # Resolve a name — recent chats first, directory as fallback (the default)
    python3 scripts/telegram-contacts.py --query doe

    # Force the full contact directory (skip the recent-chats layer)
    python3 scripts/telegram-contacts.py --query doe --contacts

    # Dump a whole roster
    python3 scripts/telegram-contacts.py --all             # recent chats
    python3 scripts/telegram-contacts.py --all --contacts  # contact directory

The base URL defaults to the system account's gateway; pass --url for another.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_BASE = os.environ.get(
    "TELEGRAM_GATEWAY_BASE_URL", "http://telegram-gateway:8093"
).rstrip("/")
TOKEN = os.environ.get("TELEGRAM_GATEWAY_TOKEN", "").strip()
DEFAULT_TIMEOUT = float(os.environ.get("TELEGRAM_GATEWAY_TIMEOUT", "60"))


def _fetch(base: str, path: str, timeout: float) -> dict:
    headers = {}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    request = urllib.request.Request(base + path, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _matches(entry: dict, needle: str) -> bool:
    if not needle:
        return True
    needle = needle.lower()
    for value in entry.values():
        if value and needle in str(value).lower():
            return True
    return False


def _fetch_or_exit(base: str, path: str, timeout: float) -> dict:
    try:
        return _fetch(base, path, timeout)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(raw).get("error", "")
        except ValueError:
            detail = raw.strip()[:200]
        print(f"telegram-contacts: gateway returned {exc.code} for {path}: {detail}", file=sys.stderr)
        raise SystemExit(1)
    except (urllib.error.URLError, OSError) as exc:
        print(f"telegram-contacts: could not reach gateway at {base}: {exc}", file=sys.stderr)
        raise SystemExit(1)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve a Telegram contact via the telegram-gateway "
                    "(recent conversations first, contact directory as fallback)."
    )
    parser.add_argument("--query", "-q", default="",
                        help="case-insensitive substring filter on name/username/chat_id")
    parser.add_argument("--contacts", action="store_true",
                        help="use the full contact directory, skipping the recent-chats layer")
    parser.add_argument("--all", action="store_true",
                        help="dump the whole roster instead of resolving a name")
    parser.add_argument("--url", default=DEFAULT_BASE,
                        help=f"gateway base URL (default {DEFAULT_BASE})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="HTTP timeout in seconds")
    args = parser.parse_args()

    base = args.url.rstrip("/")

    # Whole-roster dump (--all): honour --contacts to pick the layer, no fallback.
    if args.all:
        path = "/contacts" if args.contacts else "/recent-chats"
        key = "contacts" if args.contacts else "recent_chats"
        body = _fetch_or_exit(base, path, args.timeout)
        items = [e for e in body.get(key, []) if _matches(e, args.query)]
        json.dump(items, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    # Name resolution: recent conversations first, contact directory as fallback.
    results: list[dict] = []
    if not args.contacts:
        body = _fetch_or_exit(base, "/recent-chats", args.timeout)
        results = [
            {**e, "source": "recent-chats"}
            for e in body.get("recent_chats", [])
            if _matches(e, args.query)
        ]

    if not results:
        body = _fetch_or_exit(base, "/contacts", args.timeout)
        results = [
            {**e, "source": "contacts"}
            for e in body.get("contacts", [])
            if _matches(e, args.query)
        ]

    json.dump(results, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
