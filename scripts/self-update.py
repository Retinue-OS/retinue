#!/usr/bin/env python3
"""Trigger a full stack rebuild/restart via the updater sidecar.

The retinue container cannot rebuild/restart itself (the moment `docker
compose up -d` recreates the `retinue` service, the process issuing the
command would be killed mid-update), so the actual
`git pull && docker compose build && docker compose up -d` recipe runs in the
separate `updater` sidecar service. This script just pokes it:

    self-update.py

Configuration (environment):
    UPDATER_URL     default http://updater:9000/update
    UPDATER_TOKEN   shared secret; must match the updater's UPDATER_TOKEN
"""
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = os.environ.get("UPDATER_URL", "http://updater:9000/update")
TOKEN = os.environ.get("UPDATER_TOKEN", "").strip()
DEFAULT_TIMEOUT = float(os.environ.get("UPDATER_TIMEOUT", "30"))


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Fire the updater sidecar's /update endpoint.")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"updater endpoint (default {DEFAULT_URL})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="HTTP timeout in seconds")
    args = parser.parse_args()

    if not TOKEN:
        print("self-update: UPDATER_TOKEN is not set; refusing to send an unauthenticated request", file=sys.stderr)
        return 1

    headers = {"X-Update-Token": TOKEN}
    request = urllib.request.Request(args.url, data=b"", headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as resp:
            raw = resp.read().decode("utf-8")
        try:
            body = json.loads(raw)
            status = body.get("status", "ok")
        except ValueError:
            status = raw.strip()[:200] or "ok"
        print(f"self-update: {status}")
        return 0
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(raw).get("error", "")
        except ValueError:
            detail = raw.strip()[:200]
        if exc.code == 409:
            print("self-update: an update is already in progress", file=sys.stderr)
        else:
            print(f"self-update: updater returned {exc.code}: {detail}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"self-update: could not reach updater at {args.url}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
