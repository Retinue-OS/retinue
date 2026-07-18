#!/usr/bin/env python3
"""Query the signal-gateway's read API to resolve a Signal contact.

This is the retinue-side client for the gateway's token-gated read endpoints. It
exists so an agent can resolve a name (e.g. "Jane Doe") to a Signal number
before sending — the contact-lookup step the messaging-contact-lookup skill
requires. The gateway is the sole Signal contact path and works in
scheduled/headless sessions.

Lookup order mirrors the messaging-contact-lookup skill: **recent conversations
first, the full contact directory only as a fallback.** For a name query the
client hits `GET /recent-chats` first; only if nothing there matches does it fall
back to `GET /contacts`. Each returned entry carries a `source` field
("recent-chats" or "contacts") so the caller knows which layer answered. Passing
--all or --contacts skips this and dumps a whole roster; --groups lists groups.

Usage:
    # Resolve a name — recent chats first, directory as fallback (the default)
    python3 scripts/signal-contacts.py --query doe

    # Force the full contact directory (skip the recent-chats layer)
    python3 scripts/signal-contacts.py --query doe --contacts

    # Dump a whole roster
    python3 scripts/signal-contacts.py --all             # recent chats
    python3 scripts/signal-contacts.py --all --contacts  # contact directory
    python3 scripts/signal-contacts.py --groups          # groups

The base URL defaults to the system account's gateway; pass --url to target a
different account's gateway (e.g. http://signal-gateway-personal:8090 for the
user's personal Signal account).
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

# Base URL of the gateway (no trailing path). Defaults to account #1's gateway.
DEFAULT_BASE = os.environ.get(
    "SIGNAL_GATEWAY_BASE_URL", "http://signal-gateway:8090"
).rstrip("/")
TOKEN = os.environ.get("SIGNAL_GATEWAY_TOKEN", "").strip()
DEFAULT_TIMEOUT = float(os.environ.get("SIGNAL_GATEWAY_TIMEOUT", "60"))


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
    """Fetch a gateway endpoint, printing a diagnostic and exiting non-zero on error."""
    try:
        return _fetch(base, path, timeout)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(raw).get("error", "")
        except ValueError:
            detail = raw.strip()[:200]
        print(f"signal-contacts: gateway returned {exc.code} for {path}: {detail}", file=sys.stderr)
        raise SystemExit(1)
    except (urllib.error.URLError, OSError) as exc:
        print(f"signal-contacts: could not reach gateway at {base}: {exc}", file=sys.stderr)
        raise SystemExit(1)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve a Signal contact via the signal-gateway "
                    "(recent conversations first, contact directory as fallback)."
    )
    parser.add_argument("--query", "-q", default="",
                        help="case-insensitive substring filter on name/number")
    parser.add_argument("--groups", action="store_true",
                        help="list groups instead of contacts/recent chats")
    parser.add_argument("--contacts", action="store_true",
                        help="use the full contact directory, skipping the "
                             "recent-chats layer")
    parser.add_argument("--all", action="store_true",
                        help="dump the whole roster instead of resolving a name")
    parser.add_argument("--url", default=DEFAULT_BASE,
                        help=f"gateway base URL (default {DEFAULT_BASE})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="HTTP timeout in seconds")
    args = parser.parse_args()

    base = args.url.rstrip("/")

    # Groups: single endpoint, no recent-chats layer.
    if args.groups:
        body = _fetch_or_exit(base, "/groups", args.timeout)
        items = [e for e in body.get("groups", []) if _matches(e, args.query)]
        json.dump(items, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

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
    # --contacts forces the directory directly (skips the recent-chats layer).
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
