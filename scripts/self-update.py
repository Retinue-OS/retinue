#!/usr/bin/env python3
"""Trigger a full stack rebuild/restart via the updater sidecar.

The retinue container cannot rebuild/restart itself (the moment `docker
compose up -d` recreates the `retinue` service, the process issuing the
command would be killed mid-update), so the actual
`git pull && docker compose build && docker compose up -d` recipe runs in the
separate `updater` sidecar service. This script just pokes it:

    self-update.py

`POST /update` only *dispatches* the rebuild — the sidecar answers 202 before
the recipe even starts, so that alone can't tell the caller whether the update
worked. After a successful dispatch this script polls the sidecar's own
`GET /status` (derived from `--url`/`UPDATER_URL`, not a second endpoint to
configure) until the run is no longer `running`, then prints the outcome and
exits non-zero — naming `failed_step` and `returncode` — when the update
failed. A 409 ("already in progress") and an unreachable updater are still
reported the way they always were, without polling.

What the caller can actually *observe* through that poll is asymmetric, per
the opening paragraph above: when the recipe succeeds, `docker compose up -d`
recreates the `retinue` service this script is running in, killing the poll
mid-flight — no exit code, no final message, nothing to see. When the recipe
fails (a `git pull` conflict, a broken build), it aborts *before* `up -d`, the
container survives, and the poll gets to report it. So the failure case is the
one this script can reliably surface, which is also the case #46 is about;
being killed mid-poll on success is expected, not a bug to fix. The
`self-update: update finished successfully` message only ever prints when the
recipe finished without recreating this container — a no-op rebuild (nothing
to pull), or a deployment whose `UPDATE_COMMAND` doesn't restart `retinue`.

Configuration (environment):
    UPDATER_URL           default http://updater:9000/update
    UPDATER_TOKEN          shared secret; must match the updater's UPDATER_TOKEN
    UPDATER_TIMEOUT        per-HTTP-request timeout in seconds (default 30)
    UPDATER_POLL_TIMEOUT   total time to wait for the update to finish, in
                            seconds (default 1800 -- matches the updater's own
                            UPDATE_TIMEOUT ceiling for the rebuild recipe)
    UPDATER_POLL_INTERVAL  seconds between status polls (default 5)
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

DEFAULT_URL = os.environ.get("UPDATER_URL", "http://updater:9000/update")
TOKEN = os.environ.get("UPDATER_TOKEN", "").strip()
DEFAULT_TIMEOUT = float(os.environ.get("UPDATER_TIMEOUT", "30"))
DEFAULT_POLL_TIMEOUT = float(os.environ.get("UPDATER_POLL_TIMEOUT", "1800"))
DEFAULT_POLL_INTERVAL = float(os.environ.get("UPDATER_POLL_INTERVAL", "5"))


def status_url(update_url: str) -> str:
    """`.../update` -> `.../status`, alongside it on the same host and port.

    Derived from the one URL this script already takes rather than adding a
    second variable to configure in step with it. Only the `/update` suffix is
    special-cased (the shape every real deployment uses); anything else just
    gets `/status` appended after its own path, so a deliberately different
    `--url` still resolves to something under the same endpoint.
    """
    parts = urlsplit(update_url)
    path = parts.path
    if path.endswith("/update"):
        path = path[: -len("/update")] + "/status"
    else:
        path = path.rstrip("/") + "/status"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def poll_until_done(url: str, headers: dict, request_timeout: float,
                     poll_timeout: float, poll_interval: float) -> dict | None:
    """Poll `GET url` until the updater reports the run is no longer running.

    Returns the final state dict, or None if `poll_timeout` elapsed first (the
    update may still be running -- this is a bounded wait, not a failure
    verdict). Network/HTTP errors propagate to the caller: once the update has
    been dispatched, losing the status endpoint is a real problem, not
    something to swallow silently.
    """
    deadline = time.monotonic() + poll_timeout
    while True:
        request = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(request, timeout=request_timeout) as resp:
            state = json.loads(resp.read().decode("utf-8"))
        if not state.get("running"):
            return state
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll_interval)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Fire the updater sidecar's /update endpoint.")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"updater endpoint (default {DEFAULT_URL})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="HTTP timeout in seconds")
    parser.add_argument("--poll-timeout", type=float, default=DEFAULT_POLL_TIMEOUT,
                         help="total time to wait for the update to finish, in seconds")
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL,
                         help="seconds between status polls")
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

    # Dispatched. Now find out whether the rebuild actually succeeded -- though
    # on success this process is usually the very thing `docker compose up -d`
    # recreates, so the poll below gets killed before it can print anything;
    # the "finished successfully" branch below is really only reachable when
    # the recipe finishes without recreating `retinue` (a no-op rebuild, or a
    # deployment whose UPDATE_COMMAND doesn't restart it). A failure, by
    # contrast, aborts before `up -d` runs, so this container -- and this
    # poll -- survives to report it.
    poll_url = status_url(args.url)
    try:
        final = poll_until_done(poll_url, headers, args.timeout, args.poll_timeout, args.poll_interval)
    except urllib.error.HTTPError as exc:
        print(f"self-update: {poll_url} returned {exc.code} while polling for completion", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"self-update: could not reach {poll_url} while polling for completion: {exc}", file=sys.stderr)
        return 1

    if final is None:
        print(f"self-update: timed out after {args.poll_timeout:.0f}s waiting for the update to finish "
              f"(it may still be running) -- check {poll_url}", file=sys.stderr)
        return 1

    returncode = final.get("returncode")
    failed_step = final.get("failed_step")
    if returncode == 0:
        print("self-update: update finished successfully")
        return 0
    print(f"self-update: update failed at step {failed_step!r} (returncode {returncode})", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
